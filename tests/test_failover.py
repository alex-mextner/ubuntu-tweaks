"""Reconcile and failover: classification, retries, dependency blocking, fallbacks, root deferral, the journal."""

import tempfile
import unittest
from pathlib import Path

from ubuntu_tweaks import journal
from ubuntu_tweaks.actions import (
    Action,
    ManualStepRequired,
    Phase,
    Plan,
    RetryPolicy,
    Transient,
    is_transient,
)
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import parse_manifest
from ubuntu_tweaks.process import CommandError


class Step(Action):
    """Becomes satisfied when ``run`` finishes; ``script`` is a list of exceptions to raise on successive runs."""

    def __init__(
        self,
        name: str,
        phase: Phase = Phase.LINK,
        *,
        scope: str | None = None,
        needs_root: bool = False,
        script: list[BaseException | None] | None = None,
        alternatives: list[Action] | None = None,
        kind: str | None = None,
    ) -> None:
        self.name = name
        self.phase = phase
        self.kind = kind or name
        self.scope = scope
        self.needs_root = needs_root
        self.script = list(script or [])
        self.alts = alternatives or []
        self.done = False
        self.runs = 0

    def describe(self) -> str:
        return f"do {self.name}"

    def check(self) -> bool:
        return self.done

    def run(self) -> None:
        self.runs += 1
        if self.script:
            outcome = self.script.pop(0)
            if outcome is not None:
                raise outcome
        self.done = True

    def alternatives(self) -> list[Action]:
        return self.alts


class Alternative(Action):
    phase = Phase.LINK
    kind = "alt"

    def __init__(self, primary: Step, *, needs_root: bool = False, works: bool = True) -> None:
        self.primary = primary
        self.needs_root = needs_root
        self.works = works
        self.runs = 0

    def describe(self) -> str:
        return "alternative way"

    def check(self) -> bool:
        return self.primary.done

    def run(self) -> None:
        self.runs += 1
        if not self.works:
            raise RuntimeError("alternative broke")
        self.primary.done = True


def quick() -> tuple[RetryPolicy, list[float]]:
    slept: list[float] = []
    return RetryPolicy(attempts=3, base=2.0, cap=30.0, sleep=slept.append, jitter=lambda low, high: high), slept


class ClassificationTests(unittest.TestCase):
    def test_transient_by_type_text_and_exit_75(self) -> None:
        self.assertTrue(is_transient(Transient("busy")))
        self.assertTrue(is_transient(CommandError("apt: Could not get lock /var/lib/dpkg/lock-frontend", 100)))
        self.assertTrue(is_transient(CommandError("curl: Could not resolve host: github.com", 6)))
        self.assertTrue(is_transient(CommandError("installer said wait", 75)))
        self.assertFalse(is_transient(CommandError("exited with status 2", 2)))
        self.assertFalse(is_transient(ValueError("bad value")))

    def test_backoff_is_exponential_capped_and_jittered(self) -> None:
        policy = RetryPolicy(base=2.0, cap=10.0, jitter=lambda low, high: high)
        self.assertEqual([policy.delay(n) for n in range(4)], [2.0, 4.0, 8.0, 10.0])
        halves = RetryPolicy(base=2.0, cap=10.0, jitter=lambda low, high: high / 2)
        self.assertEqual(halves.delay(1), 2.0)


class RetryTests(unittest.TestCase):
    def test_a_transient_failure_is_retried_with_backoff_then_succeeds(self) -> None:
        policy, slept = quick()
        step = Step("net", script=[Transient("network down"), Transient("still down"), None])
        retries: list[tuple[int, float]] = []
        result = Plan((step,)).execute(policy=policy, on_retry=lambda a, n, d, m: retries.append((n, d)))
        self.assertEqual((result.changed, result.failed), (1, ()))
        self.assertEqual(step.runs, 3)
        self.assertEqual(slept, [2.0, 4.0])
        self.assertEqual(retries, [(1, 2.0), (2, 4.0)])

    def test_transient_failure_that_never_ends_becomes_a_failure_that_says_so(self) -> None:
        policy, _ = quick()
        step = Step("net", script=[Transient("down")] * 5)
        result = Plan((step,)).execute(policy=policy)
        (failure,) = result.failed
        self.assertEqual((step.runs, failure.attempts, failure.transient), (3, 3, True))
        self.assertIn("transient", result.render_failed())

    def test_a_permanent_failure_is_not_retried_and_the_run_goes_on(self) -> None:
        policy, slept = quick()
        bad = Step("bad", script=[CommandError("exited with status 2", 2)])
        good = Step("good")
        result = Plan((bad, good)).execute(policy=policy)
        self.assertEqual((bad.runs, slept), (1, []))
        self.assertEqual((result.changed, len(result.failed)), (1, 1))
        self.assertTrue(good.done)
        self.assertIn("ubuntu-tweak apply --commit", result.failed[0].next_command())

    def test_an_unexpected_exception_is_a_failure_not_a_crash(self) -> None:
        result = Plan((Step("odd", script=[RuntimeError("bug")]),)).execute(policy=quick()[0])
        self.assertIn("bug", result.failed[0].message)

    def test_a_manual_step_is_neither_retried_nor_failed(self) -> None:
        result = Plan((Step("login", script=[ManualStepRequired("log in first")]),)).execute(policy=quick()[0])
        self.assertEqual((len(result.manual), result.failed), (1, ()))

    def test_a_check_that_raises_is_a_failure(self) -> None:
        class Broken(Step):
            def check(self) -> bool:
                raise OSError("disk gone")

        result = Plan((Broken("x"), Step("y"))).execute(policy=quick()[0])
        self.assertEqual((len(result.failed), result.changed), (1, 1))


class DependencyTests(unittest.TestCase):
    def test_a_module_that_requires_a_failed_module_is_blocked_everything_else_runs(self) -> None:
        base = Step("base", scope="base", script=[RuntimeError("nope")])
        dependent = Step("dep", scope="app")
        other = Step("other", scope="unrelated")
        result = Plan((base, dependent, other)).execute(policy=quick()[0], requires={"app": ("base",)})
        self.assertEqual([item.action for item in result.blocked], [dependent])
        self.assertEqual(result.blocked[0].by, base)
        self.assertFalse(dependent.done)
        self.assertTrue(other.done)
        self.assertIn("blocked by [base] do base", result.render_blocked())

    def test_blocking_is_transitive_through_blocked_modules(self) -> None:
        a = Step("a", scope="a", script=[RuntimeError("x")])
        b = Step("b", scope="b")
        c = Step("c", scope="c")
        result = Plan((a, b, c)).execute(policy=quick()[0], requires={"b": ("a",), "c": ("b",)})
        self.assertEqual({item.action.name for item in result.blocked}, {"b", "c"})

    def test_a_failed_link_blocks_its_modules_enable_and_settings_but_not_others(self) -> None:
        link = Step("link", Phase.LINK, scope="ext", kind="link", script=[OSError("no space")])
        enable = Step("enable", Phase.TOGGLE, scope="ext")
        settings = Step("settings", Phase.SETTINGS_MODULE, scope="ext")
        unrelated = Step("unrelated", Phase.SETTINGS_MODULE, scope="other")
        result = Plan((link, enable, settings, unrelated)).execute(policy=quick()[0])
        self.assertEqual({item.action.name for item in result.blocked}, {"enable", "settings"})
        self.assertTrue(unrelated.done)

    def test_a_satisfied_action_of_a_failed_module_is_not_blocked(self) -> None:
        link = Step("link", scope="ext", kind="link", script=[OSError("x")])
        enable = Step("enable", Phase.TOGGLE, scope="ext")
        enable.done = True
        result = Plan((link, enable)).execute(policy=quick()[0])
        self.assertEqual(result.blocked, ())


class FallbackTests(unittest.TestCase):
    def test_a_fallback_runs_when_the_primary_fails_for_good(self) -> None:
        primary = Step("install", script=[RuntimeError("apt broke")])
        alternative = Alternative(primary)
        primary.alts = [alternative]
        result = Plan((primary,)).execute(policy=quick()[0])
        self.assertEqual((result.changed, result.failed), (1, ()))
        self.assertEqual(result.fallbacks, ((primary, "alternative way"),))
        self.assertIn("via fallback alternative way", result.render_fallbacks())

    def test_fallbacks_are_tried_in_order_and_all_failing_is_one_failure_listing_them(self) -> None:
        primary = Step("install", script=[RuntimeError("apt broke")])
        first = Alternative(primary, works=False)
        second = Alternative(primary, works=False)
        primary.alts = [first, second]
        result = Plan((primary,)).execute(policy=quick()[0])
        (failure,) = result.failed
        self.assertEqual((first.runs, second.runs), (1, 1))
        self.assertEqual(len(failure.tried), 3)

    def test_sudo_unavailable_uses_a_user_level_alternative_instead_of_deferring(self) -> None:
        primary = Step("tool", needs_root=True)
        alternative = Alternative(primary, needs_root=False)
        primary.alts = [alternative]
        result = Plan((primary,)).execute(no_sudo=True, policy=quick()[0])
        self.assertEqual((primary.runs, alternative.runs, result.skipped, result.changed), (0, 1, (), 1))

    def test_sudo_unavailable_without_a_user_level_alternative_defers(self) -> None:
        primary = Step("tool", needs_root=True)
        primary.alts = [Alternative(primary, needs_root=True)]
        result = Plan((primary,)).execute(no_sudo=True, policy=quick()[0])
        self.assertEqual((result.skipped, result.changed), ((primary,), 0))

    def test_a_transient_fallback_is_retried_too(self) -> None:
        primary = Step("install", script=[RuntimeError("apt broke")])

        class Flaky(Alternative):
            def run(self) -> None:
                self.runs += 1
                if self.runs < 2:
                    raise Transient("mirror down")
                self.primary.done = True

        flaky = Flaky(primary)
        primary.alts = [flaky]
        result = Plan((primary,)).execute(policy=quick()[0])
        self.assertEqual((flaky.runs, result.changed), (2, 1))


class ObserverTests(unittest.TestCase):
    def test_the_observer_sees_every_outcome(self) -> None:
        events: list[tuple[str, str]] = []
        done = Step("done")
        failed = Step("failed", scope="s", script=[RuntimeError("x")], kind="link")
        blocked = Step("blocked", Phase.TOGGLE, scope="s")
        deferred = Step("deferred", needs_root=True)
        manual = Step("manual", script=[ManualStepRequired("you")])
        Plan((done, failed, blocked, deferred, manual)).execute(
            no_sudo=True, policy=quick()[0], observer=lambda a, s, d: events.append((a.name, s))
        )
        self.assertEqual(
            events,
            [
                ("done", "done"),
                ("failed", "failed"),
                ("blocked", "blocked"),
                ("deferred", "deferred"),
                ("manual", "manual"),
            ],
        )


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state/run-journal.json"

    def test_entries_are_written_before_the_run_and_updated_after_each_action(self) -> None:
        one, two = Step("one"), Step("two")
        run_journal = journal.RunJournal(self.path, "apply --commit")
        run_journal.begin([one, two])
        before = journal.previous(self.path)
        assert before is not None
        self.assertEqual((before.finished, before.counts()), (False, {"pending": 2}))
        run_journal.record(one, "done")
        run_journal.record(two, "failed", "boom")
        run_journal.finish(7)
        after = journal.previous(self.path)
        assert after is not None
        self.assertEqual((after.finished, after.exit_code, after.counts()), (True, 7, {"done": 1, "failed": 1}))
        self.assertEqual([entry.detail for entry in after.problems()], ["boom"])

    def test_a_crash_leaves_an_unfinished_journal_the_next_run_can_see(self) -> None:
        run_journal = journal.RunJournal(self.path, "apply --commit")
        run_journal.begin([Step("one")])
        earlier = journal.previous(self.path)
        assert earlier is not None
        self.assertFalse(earlier.finished)

    def test_resume_is_just_a_rerun_because_every_item_is_observed_again(self) -> None:
        steps = [Step("a"), Step("b"), Step("c")]
        steps[1].script = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            Plan(tuple(steps)).execute(policy=quick()[0])
        self.assertEqual([step.done for step in steps], [True, False, False])
        result = Plan(tuple(steps)).execute(policy=quick()[0])
        self.assertEqual((result.changed, [step.done for step in steps]), (2, [True, True, True]))

    def test_an_unreadable_journal_is_none_and_a_write_failure_never_stops_a_run(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        self.assertIsNone(journal.previous(self.path))
        blocked_dir = Path(self.dir.name) / "file"
        blocked_dir.write_text("x", encoding="utf-8")
        journal.RunJournal(blocked_dir / "sub/journal.json", "c").begin([Step("x")])


class ManifestKeysTests(unittest.TestCase):
    def parse(self, text: str):  # type: ignore[no-untyped-def]
        return parse_manifest("demo", text, Path("/x/modules/demo"))

    def test_requires_fallback_and_coverage_are_parsed(self) -> None:
        manifest = self.parse(
            'requires = ["base-tools"]\n'
            '[source]\nkind = "system"\ninstall = "install.sh"\ncheck = "install.sh --check"\n'
            '[[fallback]]\ninstall = "install-user.sh"\nroot = false\ndescription = "user-level tarball"\n'
            '[[coverage]]\nmatch = ["file:~/.config/demo*"]\nclass = "excluded"\nreason = "cache"\n'
        )
        self.assertEqual(manifest.requires, ("base-tools",))
        (fallback,) = manifest.fallbacks
        self.assertEqual(
            (fallback.install, fallback.root, fallback.description), (("install-user.sh",), False, "user-level tarball")
        )
        self.assertEqual(manifest.coverage[0].reason, "cache")
        self.assertEqual(manifest.coverage[0].owner, "demo")

    def test_a_fallback_defaults_to_the_primarys_root_flag(self) -> None:
        manifest = self.parse(
            '[source]\nkind = "system"\ninstall = "i.sh"\ncheck = "c.sh"\nroot = false\n[[fallback]]\ninstall = "j.sh"\n'
        )
        self.assertFalse(manifest.fallbacks[0].root)

    def test_a_fallback_on_a_non_system_module_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "only for system modules"):
            self.parse('[source]\nkind = "builtin"\n[[fallback]]\ninstall = "j.sh"\n')

    def test_an_excluded_rule_needs_a_reason(self) -> None:
        with self.assertRaisesRegex(ConfigError, "needs a 'reason'"):
            self.parse('[source]\nkind = "builtin"\n[[coverage]]\nmatch = ["file:x"]\nclass = "excluded"\n')


if __name__ == "__main__":
    unittest.main()


class StatusLastRunTests(unittest.TestCase):
    def test_status_lists_what_did_not_converge_in_the_last_run(self) -> None:
        from unittest import mock

        from ubuntu_tweaks.commands import status

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-journal.json"
            run_journal = journal.RunJournal(path, "apply --commit")
            ok, bad, waiting = Step("ok"), Step("bad"), Step("waiting", needs_root=True)
            run_journal.begin([ok, bad, waiting])
            run_journal.record(ok, "done")
            run_journal.record(bad, "failed", "apt broke")
            run_journal.record(waiting, "deferred", "needs root")
            with mock.patch.object(journal, "journal_path", return_value=path):
                self.assertEqual(
                    status.last_run_lines(),
                    ["Last run: interrupted before it finished; `ubuntu-tweak apply --commit` continues it"],
                )
                run_journal.finish(7)
                lines = status.last_run_lines()
        self.assertIn("2 item(s) did not converge", lines[0])
        self.assertIn("  failed: [bad] do bad: apt broke", lines)
        self.assertIn("  deferred: [waiting] do waiting: needs root", lines)

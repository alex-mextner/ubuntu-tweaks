"""``ubuntu_tweaks.finish``: step states, the walk (machine part, human part, check again) and the built-in apply step."""

import io
import signal
import sys
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from dataclasses import replace
from unittest import mock

from tests.util import TempDirCase
from ubuntu_tweaks import finish, ui
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.finish import ExitReport, Output, Probe, State, Step, SubprocessShell, Text, evaluate
from ubuntu_tweaks.manifest import FinishStep
from ubuntu_tweaks.profile import Config


def step(
    step_id: str,
    *,
    done: Callable[[], bool] | bool = False,
    gate: Callable[[], str] | None = None,
    prepare: Callable[[], Probe] | None = None,
    run: Callable[[], int] | None = None,
    requires: tuple[str, ...] = (),
    optional: bool = False,
    settled: bool = False,
    explain_exit: Callable[[int], ExitReport | None] | None = None,
) -> Step:
    check = done if callable(done) else (lambda: done)

    def probe() -> Probe:
        ok = check()
        return Probe(ok, "" if ok else f"{step_id} not done", settled)

    return Step(
        id=step_id,
        title=Text(f"Title {step_id}", f"Заголовок {step_id}"),
        why=Text(f"Why {step_id}", f"Зачем {step_id}"),
        action=Text(f"Do {step_id}", f"Сделай {step_id}"),
        check=probe,
        gate=gate,
        prepare=prepare,
        run=run,
        requires=requires,
        optional=optional,
        explain_exit=explain_exit,
    )


class EvaluateTests(unittest.TestCase):
    def test_a_passing_check_is_done_and_a_failing_one_pending_with_its_detail(self) -> None:
        got = evaluate([step("a", done=True), step("b")])
        self.assertEqual((got["a"].state, got["b"].state), (State.DONE, State.PENDING))
        self.assertEqual(got["b"].detail, "b not done")

    def test_a_gate_that_prints_something_makes_a_pending_step_wait_with_that_reason(self) -> None:
        got = evaluate([step("a", gate=lambda: "#29 open")])
        self.assertEqual(got["a"].state, State.WAIT)
        assert got["a"].gate is not None
        self.assertEqual((got["a"].gate.go, got["a"].gate.reason), (False, "#29 open"))

    def test_an_empty_gate_is_go_and_the_step_stays_pending(self) -> None:
        got = evaluate([step("a", gate=lambda: "")])
        self.assertEqual(got["a"].state, State.PENDING)
        assert got["a"].gate is not None
        self.assertTrue(got["a"].gate.go)

    def test_a_done_step_never_runs_its_gate(self) -> None:
        def gate() -> str:
            raise AssertionError("the gate of a finished step is not consulted")

        self.assertEqual(evaluate([step("a", done=True, gate=gate)])["a"].state, State.DONE)

    def test_a_step_is_blocked_until_every_step_it_requires_is_done(self) -> None:
        got = evaluate([step("apply"), step("key", requires=("apply",))])
        self.assertEqual((got["apply"].state, got["key"].state), (State.PENDING, State.BLOCKED))
        self.assertIn("Title apply", got["key"].detail)
        ready = evaluate([step("apply", done=True), step("key", requires=("apply",))])
        self.assertEqual(ready["key"].state, State.PENDING)

    def test_a_step_requiring_a_settled_but_not_done_step_is_pending_not_blocked(self) -> None:
        # apply ran to its end and some module failed: that failure does not stop independent steps from starting
        got = evaluate([step("apply", settled=True), step("key", requires=("apply",))])
        self.assertEqual((got["apply"].state, got["key"].state), (State.PENDING, State.PENDING))

    def test_a_step_requiring_an_unsettled_step_stays_blocked(self) -> None:
        got = evaluate([step("apply", settled=False), step("key", requires=("apply",))])
        self.assertEqual(got["key"].state, State.BLOCKED)

    def test_a_settled_apply_that_waits_on_its_gate_keeps_the_steps_behind_it_blocked(self) -> None:
        calls: list[str] = []

        def gate() -> str:
            calls.append("gate")
            return "dock code not released"

        got = evaluate(
            [step("apply", settled=True, gate=gate), step("a", requires=("apply",)), step("b", requires=("apply",))]
        )
        self.assertEqual(
            (got["apply"].state, got["a"].state, got["b"].state), (State.WAIT, State.BLOCKED, State.BLOCKED)
        )
        self.assertEqual(calls, ["gate"])  # one run of the gate per evaluation

    def test_a_settled_apply_whose_gate_says_go_lets_the_steps_behind_it_run(self) -> None:
        got = evaluate([step("apply", settled=True, gate=lambda: ""), step("a", requires=("apply",))])
        self.assertEqual(got["a"].state, State.PENDING)

    def test_a_reopened_step_is_unsettled_even_when_its_probe_was_settled(self) -> None:
        got = evaluate([step("apply", settled=True), step("key", requires=("apply",))], reopened={"apply": "owed"})
        self.assertEqual(got["key"].state, State.BLOCKED)

    def test_a_requirement_that_no_step_declares_is_ignored(self) -> None:
        # the module that owned it is disabled: nothing could ever satisfy it, so it must not block forever
        self.assertEqual(evaluate([step("key", requires=("gone",))])["key"].state, State.PENDING)

    def test_a_failed_step_stays_failed_until_its_check_passes(self) -> None:
        got = evaluate([step("a")], failed={"a": "exit 1"})
        self.assertEqual((got["a"].state, got["a"].detail), (State.FAILED, "exit 1"))
        self.assertEqual(evaluate([step("a", done=True)], failed={"a": "exit 1"})["a"].state, State.DONE)

    def test_a_step_that_requires_itself_or_a_cycle_is_refused_up_front(self) -> None:
        for steps in (
            [step("a", requires=("a",))],
            [step("a", requires=("b",)), step("b", requires=("a",))],
            [step("a", requires=("b",)), step("b", requires=("c",)), step("c", requires=("a",))],
        ):
            with self.subTest(ids=[s.id for s in steps]), self.assertRaisesRegex(ConfigError, "requires itself"):
                finish.validate(steps)
        finish.validate([step("a"), step("b", requires=("a", "gone"))])  # a missing id is ignored, not an error

    def test_exit_code_is_3_while_a_required_step_is_not_done(self) -> None:
        steps = [step("a", done=True), step("b", optional=True)]
        self.assertEqual(finish.exit_code(steps, evaluate(steps)), 0)
        steps = [step("a", done=True), step("b")]
        self.assertEqual(finish.exit_code(steps, evaluate(steps)), 3)


class RequirementGraphTests(unittest.TestCase):
    def test_requirements_are_transitive_and_ignore_unknown_ids(self) -> None:
        steps = [step("a"), step("b", requires=("a",)), step("c", requires=("b", "gone"))]
        self.assertEqual(finish.requirements(steps, "c"), {"a", "b"})
        self.assertEqual(finish.requirements(steps, "a"), set())
        self.assertEqual(finish.requirements(steps, "nope"), set())

    def test_closure_is_the_step_and_what_it_needs_in_the_original_order(self) -> None:
        steps = [step("a"), step("x"), step("b", requires=("a",))]
        self.assertEqual([s.id for s in finish.closure(steps, "b")], ["a", "b"])

    def test_an_unknown_step_names_the_known_ones(self) -> None:
        with self.assertRaisesRegex(ConfigError, "nope.*a, b"):
            finish.closure([step("a"), step("b")], "nope")


class Console:
    """What a person at the terminal does: the answers ``walk`` asks for, in order."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"asked {prompt!r} with no answer left")
        return self.answers.pop(0)


class WalkTests(unittest.TestCase):
    def setUp(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)
        self.out = io.StringIO()
        self.events: list[str] = []

    def walk(self, steps: Sequence[Step], *answers: str, yes: bool = False, interactive: bool = True):
        console = Console(*answers)
        walked = finish.walk(steps, out=self.out, ask=console, interactive=interactive, assume_yes=yes)
        self.console = console
        self.walked = walked
        return walked.statuses

    def doing(self, name: str, state: dict[str, bool], code: int = 0) -> Callable[[], int]:
        def run() -> int:
            self.events.append(f"run:{name}")
            state[name] = code == 0
            return code

        return run

    def test_it_runs_the_prepare_then_the_human_part_then_checks_again(self) -> None:
        state = {"a": False}

        def prepare() -> Probe:
            self.events.append("prepare:a")
            return Probe(True)

        steps = [step("a", done=lambda: state["a"], prepare=prepare, run=self.doing("a", state))]
        result = self.walk(steps, "")
        self.assertEqual(self.events, ["prepare:a", "run:a"])
        self.assertEqual(result["a"].state, State.DONE)
        self.assertIn("Title a", self.out.getvalue())
        self.assertIn("Why a", self.out.getvalue())
        self.assertIn("Do a", self.out.getvalue())

    def test_done_steps_are_only_listed_never_run(self) -> None:
        steps = [step("a", done=True, run=lambda: self.events.append("run:a") or 0)]
        self.walk(steps)
        self.assertEqual(self.events, [])
        self.assertEqual(self.console.asked, [])

    def test_a_failing_prepare_stops_that_step_and_the_run_is_not_attempted(self) -> None:
        steps = [
            step(
                "a",
                prepare=lambda: Probe(False, "venv still answers -80045"),
                run=lambda: self.events.append("run:a") or 0,
            ),
            step("b", done=False, run=lambda: self.events.append("run:b") or 0),
        ]
        result = self.walk(steps, "", "", "", "")
        self.assertNotIn("run:a", self.events)
        self.assertEqual(result["a"].state, State.FAILED)
        self.assertIn("venv still answers -80045", result["a"].detail)
        self.assertIn("run:b", self.events)  # an independent step still gets its turn

    def test_a_step_is_tried_at_most_twice_per_run(self) -> None:
        steps = [step("a", run=lambda: self.events.append("run:a") or 0)]
        result = self.walk(steps, "", "", "", "")
        self.assertEqual(self.events.count("run:a"), 2)
        self.assertEqual(result["a"].state, State.FAILED)

    def test_only_enter_starts_a_step_anything_else_is_asked_again(self) -> None:
        state = {"a": False}
        steps = [step("a", done=lambda: state["a"], run=self.doing("a", state))]
        self.walk(steps, "n", "no", "skip", "")
        self.assertEqual(len(self.console.asked), 4)
        self.assertEqual(self.events, ["run:a"])  # started once, by the Enter

    def test_a_decline_never_starts_the_step(self) -> None:
        steps = [step("a", run=lambda: self.events.append("run:a") or 0)]
        self.walk(steps, "n", "q")
        self.assertEqual(self.events, [])

    def test_ctrl_c_inside_a_step_ends_the_attempt_not_the_walk(self) -> None:
        def interrupted() -> int:
            raise KeyboardInterrupt

        steps = [step("a", run=interrupted), step("b", run=lambda: self.events.append("run:b") or 0)]
        result = self.walk(steps, "", "", "", "")
        self.assertEqual(result["a"].state, State.FAILED)
        self.assertIn("run:b", self.events)  # the walk went on
        self.assertIn("Interrupted", self.out.getvalue())

    def test_s_skips_a_step_and_q_stops_the_walk(self) -> None:
        steps = [
            step("a", run=lambda: self.events.append("run:a") or 0),
            step("b", run=lambda: self.events.append("run:b") or 0),
        ]
        result = self.walk(steps, "s", "q")
        self.assertEqual(self.events, [])
        self.assertEqual((result["a"].state, result["b"].state), (State.PENDING, State.PENDING))

    def test_a_step_with_nothing_to_run_is_only_listed_never_offered(self) -> None:
        steps = [
            Step(
                "relogin",
                Text("Log out"),
                Text("Why"),
                Text("Log out and in"),
                check=lambda: Probe(False, "not loaded"),
            )
        ]
        result = self.walk(steps)
        self.assertEqual(self.console.asked, [])
        self.assertEqual(result["relogin"].state, State.PENDING)
        self.assertIn("Log out and in", self.out.getvalue())

    def test_end_of_input_counts_as_quit(self) -> None:
        def eof(prompt: str) -> str:
            raise EOFError

        steps = [step("a", run=lambda: self.events.append("run:a") or 0)]
        finish.walk(steps, out=self.out, ask=eof, interactive=True, assume_yes=False)
        self.assertEqual(self.events, [])

    def test_a_waiting_step_is_listed_with_wait_and_never_run(self) -> None:
        steps = [step("a", gate=lambda: "#29 feat(settings)", run=lambda: self.events.append("run:a") or 0)]
        self.walk(steps)
        text = self.out.getvalue()
        self.assertIn("WAIT", text)
        self.assertIn("#29 feat(settings)", text)
        self.assertIn("Why a", text)  # a step that cannot run now still says what it will need
        self.assertEqual(self.events, [])

    def test_a_go_gate_is_announced_as_go_before_the_step_runs(self) -> None:
        state = {"a": False}
        steps = [step("a", done=lambda: state["a"], gate=lambda: "", run=self.doing("a", state))]
        self.walk(steps, "")
        self.assertIn("GO", self.out.getvalue())

    def test_a_step_that_became_possible_runs_in_the_same_walk(self) -> None:
        state = {"apply": False, "key": False}
        steps = [
            step("apply", done=lambda: state["apply"], run=self.doing("apply", state)),
            step("key", done=lambda: state["key"], requires=("apply",), run=self.doing("key", state)),
        ]
        self.walk(steps, "", "")
        self.assertEqual(self.events, ["run:apply", "run:key"])

    def test_a_step_that_makes_an_earlier_one_pending_again_sends_the_walk_back(self) -> None:
        state = {"apply": 0, "key": False}

        def apply_done() -> bool:
            return state["apply"] >= (2 if state["key"] else 1)

        def apply_run() -> int:
            state["apply"] += 1
            self.events.append("run:apply")
            return 0

        steps = [
            step("apply", done=apply_done, run=apply_run),
            step("key", done=lambda: state["key"], requires=("apply",), run=self.doing("key", state)),
        ]
        self.walk(steps, "", "", "")
        self.assertEqual(self.events, ["run:apply", "run:key", "run:apply"])

    def test_yes_does_not_pause_and_leaves_optional_steps_alone(self) -> None:
        steps = [
            step("a", run=lambda: self.events.append("run:a") or 0),
            step("o", optional=True, run=lambda: self.events.append("run:o") or 0),
        ]
        self.walk(steps, yes=True)
        self.assertEqual(self.console.asked, [])
        self.assertIn("run:a", self.events)
        self.assertNotIn("run:o", self.events)
        self.assertIn("Do o", self.out.getvalue())  # not offered, so the overview must say what it is

    def test_without_a_terminal_nothing_is_run_and_the_checklist_says_so(self) -> None:
        steps = [
            step(
                "a",
                prepare=lambda: self.events.append("prepare:a") or Probe(True),
                run=lambda: self.events.append("run:a") or 0,
            )
        ]
        result = self.walk(steps, interactive=False)
        self.assertEqual(self.events, [])
        self.assertEqual(result["a"].state, State.PENDING)
        self.assertIn("terminal", self.out.getvalue())

    def test_a_step_that_reapplies_sends_apply_back_even_though_its_check_still_passes(self) -> None:
        # the plan cannot know the key unlocked root work (the journal still says "manual"): the step says so
        state = {"key": False}
        key = step("key", done=lambda: state["key"], run=self.doing("key", state))
        steps = [
            step("apply", done=True, run=lambda: self.events.append("run:apply") or 0),
            replace(key, reapply=True),
        ]
        result = self.walk(steps, "", "")
        self.assertEqual(self.events, ["run:key", "run:apply"])
        self.assertEqual(result["apply"].state, State.DONE)  # and it is not offered a third time
        self.assertIn("runs again", self.out.getvalue())

    def test_steps_given_as_a_function_are_collected_again_after_every_attempt(self) -> None:
        state = {"a": False, "b": False}
        calls: list[int] = []

        def source() -> list[Step]:
            calls.append(1)
            found = [step("a", done=lambda: state["a"], run=self.doing("a", state))]
            if state["a"]:  # running `a` made a new step appear, like apply cloning the profile
                found.append(step("b", done=lambda: state["b"], run=self.doing("b", state)))
            return found

        result = self.walk(source, "", "")
        self.assertEqual(self.events, ["run:a", "run:b"])
        self.assertEqual({k: v.state for k, v in result.items()}, {"a": State.DONE, "b": State.DONE})

    def test_the_owed_reapply_survives_a_quit_and_is_paid_by_the_next_run(self) -> None:
        # the key is stored, then the person quits before the second apply: the next run must still run it
        ledger = finish.MemoryObligations()
        state = {"key": False}
        key = step("key", done=lambda: state["key"], run=self.doing("key", state))
        steps = [step("apply", done=True, run=lambda: self.events.append("run:apply") or 0), replace(key, reapply=True)]
        finish.walk(steps, out=self.out, ask=Console("", "q"), interactive=True, assume_yes=False, obligations=ledger)
        self.assertEqual(self.events, ["run:key"])
        self.assertEqual(list(ledger.load()), ["apply"])
        finish.walk(steps, out=self.out, ask=Console(""), interactive=True, assume_yes=False, obligations=ledger)
        self.assertEqual(self.events, ["run:key", "run:apply"])
        self.assertEqual(ledger.load(), {})

    def test_an_owed_apply_that_fails_stays_owed(self) -> None:
        ledger = finish.MemoryObligations()
        ledger.save({"apply": "owed"})
        steps = [step("apply", done=True, run=lambda: self.events.append("run:apply") or 1)]
        walked = finish.walk(
            steps, out=self.out, ask=Console("", ""), interactive=True, assume_yes=False, obligations=ledger
        )
        self.assertEqual(list(ledger.load()), ["apply"])
        self.assertEqual(walked.statuses["apply"].state, State.FAILED)

    def test_the_debt_is_written_before_the_slow_check_so_an_interrupt_there_cannot_lose_it(self) -> None:
        ledger = finish.MemoryObligations()
        state = {"key": False}
        calls = {"n": 0}

        def key_done() -> bool:
            calls["n"] += 1
            if state["key"] and calls["n"] > 1:  # the re-check after the attempt
                raise KeyboardInterrupt
            return state["key"]

        key = replace(step("key", done=key_done, run=self.doing("key", state)), reapply=True)
        steps = [step("apply", done=True, run=lambda: 0), key]
        with self.assertRaises(KeyboardInterrupt):
            finish.walk(steps, out=self.out, ask=Console(""), interactive=True, assume_yes=False, obligations=ledger)
        self.assertEqual(list(ledger.load()), ["apply"])

    def test_a_debt_that_was_already_owed_is_not_taken_back_by_a_step_that_did_not_take(self) -> None:
        ledger = finish.MemoryObligations()
        ledger.save({"apply": "owed from an earlier run"})
        key = replace(step("key", run=lambda: 0), reapply=True)  # runs fine, its check keeps failing
        finish.walk([key], out=self.out, ask=Console("", ""), interactive=True, assume_yes=False, obligations=ledger)
        self.assertEqual(list(ledger.load()), ["apply"])

    def test_a_reapply_step_records_the_debt_even_when_apply_is_not_in_the_list(self) -> None:
        ledger = finish.MemoryObligations()
        state = {"key": False}
        key = replace(step("key", done=lambda: state["key"], run=self.doing("key", state)), reapply=True)
        finish.walk([key], out=self.out, ask=Console(""), interactive=True, assume_yes=False, obligations=ledger)
        self.assertEqual(list(ledger.load()), ["apply"])

    def test_a_step_that_vanishes_after_its_attempt_does_not_crash_the_walk(self) -> None:
        state = {"a": False}
        order = {"gone": False}

        def source() -> list[Step]:
            return [] if order["gone"] else [step("a", done=lambda: state["a"], run=gone)]

        def gone() -> int:
            order["gone"] = True
            return 0

        walked = self.walk(source, "")
        self.assertEqual(walked, {})

    def test_an_invalid_profile_after_an_attempt_ends_in_a_summary_not_a_traceback(self) -> None:
        calls = {"n": 0}

        def source() -> list[Step]:
            calls["n"] += 1
            if calls["n"] > 1:
                raise ConfigError("profile.toml: broken")
            return [step("a", run=lambda: 0)]

        self.walk(source, "", "")
        self.assertIn("profile.toml: broken", self.out.getvalue())
        self.assertIn("Summary", self.out.getvalue())

    def test_the_walk_returns_the_steps_it_last_looked_at(self) -> None:
        def source() -> list[Step]:
            return [step("a", done=True)]

        self.walk(source)
        self.assertEqual([s.id for s in self.walked.steps], ["a"])

    def test_a_command_that_ended_on_ctrl_c_is_an_interruption_not_a_failure_code(self) -> None:
        steps = [step("a", run=lambda: 130)]
        self.walk(steps, "", "")
        self.assertIn("Interrupted", self.out.getvalue())
        self.assertNotIn("exited with 130", self.out.getvalue())

    def test_a_reported_exit_prints_its_text_and_not_the_bare_exit_number(self) -> None:
        report = ExitReport("PLAIN SUMMARY OF 7", "the command reported problems, listed above")
        steps = [step("a", run=lambda: 7, explain_exit=lambda code: report if code == 7 else None)]
        self.walk(steps, "", "s")
        text = self.out.getvalue()
        self.assertIn("PLAIN SUMMARY OF 7", text)
        self.assertIn("Not done yet: Title a. the command reported problems, listed above", text)
        self.assertNotIn("exited with 7", text)

    def test_an_exit_the_step_does_not_explain_keeps_the_generic_line(self) -> None:
        steps = [step("a", run=lambda: 9, explain_exit=lambda code: None)]
        self.walk(steps, "", "s")
        self.assertIn("The step's command exited with 9.", self.out.getvalue())

    def test_an_explainer_is_not_asked_about_ctrl_c_or_success(self) -> None:
        asked: list[int] = []

        def explain(code: int) -> None:
            asked.append(code)

        state = {"b": False}
        self.walk([step("a", run=lambda: 130, explain_exit=explain)], "", "")
        self.walk([step("b", done=lambda: state["b"], run=self.doing("b", state), explain_exit=explain)], "")
        self.assertEqual(asked, [])
        self.assertIn("Interrupted", self.out.getvalue())

    def test_a_failed_apply_that_is_settled_lets_the_steps_behind_it_run(self) -> None:
        state = {"key": False}
        report = ExitReport("APPLY SUMMARY", "reported above")
        apply = step(
            "apply", settled=True, run=lambda: self.events.append("run:apply") or 7, explain_exit=lambda code: report
        )
        key = step("key", done=lambda: state["key"], run=self.doing("key", state), requires=("apply",))
        statuses = self.walk([apply, key], "", "s", "")  # apply, apply again (skipped), then key
        self.assertEqual(self.events, ["run:apply", "run:key"])
        self.assertEqual((statuses["apply"].state, statuses["key"].state), (State.FAILED, State.DONE))
        self.assertEqual(finish.exit_code([apply, key], statuses), 3)  # apply is still not done

    def test_without_a_terminal_a_step_with_nothing_to_run_does_not_ask_for_one(self) -> None:
        steps = [Step("relogin", Text("Log out"), Text("Why"), Text("Log out and in"), check=lambda: Probe(False))]
        self.walk(steps, interactive=False)
        self.assertNotIn("needs a terminal", self.out.getvalue())

    def test_the_pause_prompt_has_a_word_the_session_log_masks_in_both_languages(self) -> None:
        from ubuntu_tweaks import sessionlog

        for lang in ("en", "ru"):
            ui.configure(color="never", plain=True, lang=lang)
            self.assertTrue(sessionlog._SECRET_WORD.search(ui.t("finish.pause")), lang)

    def test_the_summary_says_everything_is_done_or_what_is_left(self) -> None:
        self.walk([step("a", done=True)])
        self.assertIn("All done", self.out.getvalue())
        self.out.truncate(0)
        self.out.seek(0)
        self.walk([step("a", done=True), step("b", gate=lambda: "later")])
        self.assertIn("Left: 1", self.out.getvalue())

    def test_the_text_follows_the_language_and_falls_back_to_english(self) -> None:
        ui.configure(color="never", plain=True, lang="ru")
        self.walk([step("a", done=False, gate=lambda: "позже")])
        text = self.out.getvalue()
        self.assertIn("Заголовок a", text)
        self.assertIn("Зачем a", text)
        self.assertEqual(Text("only english").get("ru"), "only english")

    def test_dry_run_mode_lists_and_never_runs(self) -> None:
        steps = [
            step(
                "a",
                prepare=lambda: self.events.append("prepare:a") or Probe(True),
                run=lambda: self.events.append("run:a") or 0,
            )
        ]
        walked = finish.walk(steps, out=self.out, ask=Console(), interactive=False, assume_yes=False, dry_run=True)
        self.assertEqual(self.events, [])
        self.assertEqual(walked.statuses["a"].state, State.PENDING)
        self.assertNotIn("needs a terminal", self.out.getvalue())


class FakeShell:
    def __init__(self, outputs: Mapping[str, Output] | None = None) -> None:
        self.outputs = dict(outputs or {})
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.interactive_code = 0

    def capture(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> Output:
        self.calls.append((list(argv), cwd, dict(env)))
        return self.outputs.get(Path(argv[0]).name, Output(0))

    def stream(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> Output:
        self.calls.append((list(argv), cwd, dict(env)))
        return self.outputs.get(Path(argv[0]).name, Output(0))

    def interactive(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        self.calls.append((list(argv), cwd, dict(env)))
        return self.interactive_code


SPEC = FinishStep(
    id="icloud-signin",
    title="Sign in",
    why="Why",
    action="Do",
    check=("./finish", "icloud-signin", "check"),
    title_ru="Войти",
    prepare=("./finish", "icloud-signin", "prepare"),
    run=("passwords-app", "signin"),
    gate=("./finish", "icloud-signin", "gate"),
)


class CommandStepTests(TempDirCase):
    def build(self, shell: FakeShell) -> Step:
        return finish.command_step(SPEC, directory=self.tmp, env={"K": "v"}, shell=shell)

    def test_dot_slash_commands_run_from_the_module_directory_and_others_from_path(self) -> None:
        shell = FakeShell()
        built = self.build(shell)
        built.check()
        assert built.run is not None
        built.run()
        self.assertEqual(shell.calls[0][0], [str(self.tmp / "finish"), "icloud-signin", "check"])
        self.assertEqual(shell.calls[0][1], self.tmp)
        self.assertEqual(shell.calls[1][0], ["passwords-app", "signin"])
        self.assertEqual(shell.calls[0][2], {"K": "v"})

    def test_check_exit_zero_is_done_and_the_detail_is_the_first_output_line(self) -> None:
        shell = FakeShell({"finish": Output(1, out="\n  signed out\nsecond line\n")})
        probe = self.build(shell).check()
        self.assertEqual((probe.ok, probe.detail), (False, "signed out"))
        self.assertTrue(self.build(FakeShell()).check().ok)

    def test_a_missing_command_is_a_pending_check_not_a_crash(self) -> None:
        shell = FakeShell({"finish": Output(127, err="finish: not found")})
        probe = self.build(shell).check()
        self.assertEqual((probe.ok, probe.detail), (False, "finish: not found"))

    def test_gate_output_is_the_wait_reason_and_silence_is_go(self) -> None:
        built = self.build(FakeShell({"finish": Output(0, out="#29 open\n#30 open\n")}))
        assert built.gate is not None
        self.assertEqual(built.gate(), "#29 open\n#30 open")
        quiet = self.build(FakeShell())
        assert quiet.gate is not None
        self.assertEqual(quiet.gate(), "")

    def test_a_gate_that_fails_to_run_waits_it_never_says_go(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)
        built = self.build(FakeShell({"finish": Output(1, err="gh: not logged in")}))
        assert built.gate is not None
        reason = built.gate()
        self.assertIn("gh: not logged in", reason)
        self.assertNotEqual(reason, "")

    def test_a_failed_prepare_shows_the_last_line_the_machine_part_printed(self) -> None:
        shell = FakeShell({"finish": Output(3, out="the probe still says -80045")})
        built = self.build(shell)
        assert built.prepare is not None
        probe = built.prepare()
        self.assertEqual((probe.ok, probe.detail), (False, "the probe still says -80045"))
        self.assertEqual(shell.calls[-1][0], [str(self.tmp / "finish"), "icloud-signin", "prepare"])

    def test_a_silent_failed_prepare_falls_back_to_the_exit_code(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)
        built = self.build(FakeShell({"finish": Output(3)}))
        assert built.prepare is not None
        probe = built.prepare()
        self.assertEqual((probe.ok, probe.detail), (False, "exit code 3"))

    def test_a_prepare_that_succeeds_is_ok_and_the_run_keeps_the_terminal(self) -> None:
        shell = FakeShell({"finish": Output(0, out="ready\n")})
        built = self.build(shell)
        assert built.prepare is not None and built.run is not None
        self.assertTrue(built.prepare().ok)
        shell.interactive_code = 4
        self.assertEqual(built.run(), 4)

    def test_texts_and_requirements_come_from_the_manifest(self) -> None:
        built = self.build(FakeShell())
        self.assertEqual((built.title.get("ru"), built.title.get("en")), ("Войти", "Sign in"))
        self.assertEqual(built.why.get("ru"), "Why")


class SubprocessShellTests(TempDirCase):
    """A command that cannot start is an exit code and a line, never an exception that stops the checklist."""

    def script(self, name: str, text: str, mode: int = 0o755) -> Path:
        path = self.tmp / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_a_script_without_a_shebang_is_not_executable_not_a_crash(self) -> None:
        path = self.script("noshebang", "echo hi\n")
        output = SubprocessShell().capture([str(path)], cwd=self.tmp, env={})
        self.assertIn(output.code, (0, 126))  # some systems fall back to /bin/sh: either way no exception

    def test_a_missing_program_is_127_and_a_file_where_a_directory_should_be_is_126(self) -> None:
        shell = SubprocessShell()
        self.assertEqual(shell.capture(["/nonexistent/prog"], cwd=self.tmp, env={}).code, 127)
        self.script("plain", "x")
        self.assertEqual(shell.capture([str(self.tmp / "plain" / "x")], cwd=self.tmp, env={}).code, 126)

    def test_a_missing_working_directory_is_reported_by_both_runners(self) -> None:
        shell = SubprocessShell()
        gone = self.tmp / "gone"
        self.assertNotEqual(shell.capture(["true"], cwd=gone, env={}).code, 0)
        self.assertNotEqual(shell.interactive(["true"], cwd=gone, env={}), 0)

    def test_a_streamed_command_reaches_our_stream_and_returns_its_last_line(self) -> None:
        path = self.script("noisy", "#!/bin/sh\necho first\necho 'the reason' >&2\nexit 1\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            output = SubprocessShell().stream([str(path)], cwd=self.tmp, env={})
        self.assertEqual((output.code, output.out), (1, "the reason"))
        self.assertEqual(out.getvalue(), "first\nthe reason\n")  # stderr is merged: one pipe, the child's own order

    def test_the_last_line_is_the_childs_last_in_either_stream_order_every_time(self) -> None:
        for body in ("echo working >&2; echo fatal", "echo working; echo fatal >&2"):
            path = self.script("order", f"#!/bin/sh\n{body}\nexit 7\n")
            for _ in range(40):
                with mock.patch.object(sys, "stdout", io.StringIO()):
                    self.assertEqual(SubprocessShell().stream([str(path)], cwd=self.tmp, env={}).out, "fatal")

    def test_a_diagnostic_followed_by_a_flood_of_blank_text_is_still_the_reason(self) -> None:
        path = self.script("blank", "#!/bin/sh\necho fatal\nprintf '%3000s\\n\\n   ' ''\nexit 7\n")
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(SubprocessShell().stream([str(path)], cwd=self.tmp, env={}).out, "fatal")

    def test_a_key_fragment_in_the_last_line_is_masked_by_the_block_it_belongs_to(self) -> None:
        body = "echo '-----BEGIN " + "PRIVATE KEY-----'; echo SYNTHETIC-KEY-FRAGMENT; exit 3"
        path = self.script("key", f"#!/bin/sh\n{body}\n")
        with mock.patch.object(sys, "stdout", io.StringIO()):
            output = SubprocessShell().stream([str(path)], cwd=self.tmp, env={})
        self.assertNotIn("SYNTHETIC-KEY-FRAGMENT", output.out)

    def test_escape_sequences_and_redraws_are_not_part_of_the_reason(self) -> None:
        path = self.script("tty", "#!/bin/sh\nprintf '10%%\\r\\033[31mfailed to build\\033[0m\\n'\nexit 1\n")
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(SubprocessShell().stream([str(path)], cwd=self.tmp, env={}).out, "failed to build")

    def test_a_last_line_without_a_newline_counts(self) -> None:
        path = self.script("bare", "#!/bin/sh\nprintf 'done\\nstopped at step 3'\nexit 1\n")
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(SubprocessShell().stream([str(path)], cwd=self.tmp, env={}).out, "stopped at step 3")

    def test_a_streamed_command_that_cannot_start_is_an_exit_code_and_a_line(self) -> None:
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            output = SubprocessShell().stream(["/nonexistent/prog"], cwd=self.tmp, env={})
        self.assertEqual(output.code, 127)
        self.assertIn("cannot run", err.getvalue())
        self.assertIn("cannot run", output.out)

    def test_a_streamed_command_gets_exactly_the_environment_given(self) -> None:
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            output = SubprocessShell().stream(["/usr/bin/env"], cwd=self.tmp, env={"ONLY": "this"})
        self.assertEqual(output.code, 0)
        self.assertIn("ONLY=this", out.getvalue())
        self.assertNotIn("HOME=", out.getvalue())

    def test_a_failed_prepare_through_the_real_shell_logs_its_line_and_shows_it_as_the_reason(self) -> None:
        self.script("finish", "#!/bin/sh\necho 'Apple refuses the anisette data'\nexit 1\n")
        out = io.StringIO()
        built = finish.command_step(SPEC, directory=self.tmp, env={}, shell=SubprocessShell())
        assert built.prepare is not None
        with mock.patch.object(sys, "stdout", out):
            probe = built.prepare()
        self.assertEqual((probe.ok, probe.detail), (False, "Apple refuses the anisette data"))
        self.assertEqual(out.getvalue(), "Apple refuses the anisette data\n")

    def test_a_timeout_is_124(self) -> None:
        with mock.patch.object(finish, "CHECK_TIMEOUT_SECONDS", 0.2):
            output = SubprocessShell().capture(["sleep", "5"], cwd=self.tmp, env={})
        self.assertEqual(output.code, 124)

    def test_ctrl_c_handling_is_restored_and_the_command_does_not_inherit_an_ignored_signal(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        code = SubprocessShell().interactive(
            [
                sys.executable,
                "-c",
                "import signal, sys; sys.exit(0 if signal.getsignal(signal.SIGINT) == signal.default_int_handler else 3)",
            ],
            cwd=self.tmp,
            env={},
        )
        self.assertEqual(code, 0)
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_the_environment_is_exactly_the_one_given(self) -> None:
        output = SubprocessShell().capture(["/usr/bin/env"], cwd=self.tmp, env={"ONLY": "this"})
        self.assertEqual(output.out.strip(), "ONLY=this")


class FileObligationsTests(TempDirCase):
    def test_what_is_owed_round_trips_through_the_file_per_scope(self) -> None:
        path = self.tmp / "state" / "finish-state.json"
        first, second = finish.FileObligations(path, "A|box"), finish.FileObligations(path, "B|box")
        self.assertEqual(first.load(), {})
        first.save({"apply": "key done: ранее"})
        self.assertEqual(first.load(), {"apply": "key done: ранее"})
        self.assertEqual(second.load(), {})  # what A owes is not B's debt
        second.save({"apply": "b"})
        second.save({})  # B paid; A still owes
        self.assertEqual(first.load(), {"apply": "key done: ранее"})
        self.assertEqual(second.load(), {})

    def test_a_damaged_file_owes_nothing_instead_of_stopping_the_walk(self) -> None:
        path = self.tmp / "finish-state.json"
        for damaged in ("not json", "[]", '{"scopes": 3}', '{"scopes": {"A": 3}}', '{"other": {}}'):
            path.write_text(damaged)
            self.assertEqual(finish.FileObligations(path, "A").load(), {}, damaged)


class ApplySettingsTests(unittest.TestCase):
    def config(self, **apply: object) -> Config:
        return Config({"finish": {"apply": dict(apply)}} if apply else {})

    def test_no_table_means_no_gate(self) -> None:
        settings = finish.apply_settings(self.config())
        self.assertEqual((settings.gate, settings.gate_why), ((), Text("")))

    def test_a_russian_only_reason_is_kept(self) -> None:
        settings = finish.apply_settings(self.config(gate=["true"], gate_why_ru="только по-русски"))
        self.assertEqual(settings.gate_why.get("ru"), "только по-русски")

    def test_gate_and_its_reason_are_read_with_the_russian_text(self) -> None:
        settings = finish.apply_settings(
            self.config(gate=["gh", "pr", "list"], gate_why="released first", gate_why_ru="сначала релиз")
        )
        self.assertEqual(settings.gate, ("gh", "pr", "list"))
        self.assertEqual(settings.gate_why.get("ru"), "сначала релиз")

    def test_unknown_keys_and_wrong_types_are_a_config_error(self) -> None:
        for bad in ({"gate": "gh pr list"}, {"gate": []}, {"colour": "red"}, {"gate_why": 3}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                finish.apply_settings(self.config(**bad))


class ApplyStepTests(TempDirCase):
    def test_it_is_done_when_the_probe_says_so_and_runs_the_given_command(self) -> None:
        ran: list[str] = []
        built = finish.apply_step(
            finish.ApplySettings(),
            directory=self.tmp,
            shell=FakeShell(),
            env={},
            probe=lambda: Probe(False, "3 root actions"),
            run=lambda: ran.append("apply") or 0,
        )
        self.assertEqual(built.id, "apply")
        self.assertEqual(built.check().detail, "3 root actions")
        assert built.run is not None
        built.run()
        self.assertEqual(ran, ["apply"])
        self.assertIsNone(built.gate)

    def test_the_exit_explainer_becomes_the_steps_hook(self) -> None:
        def explain(code: int) -> ExitReport | None:
            return ExitReport("", f"code {code}")

        built = finish.apply_step(
            finish.ApplySettings(),
            directory=self.tmp,
            shell=FakeShell(),
            env={},
            probe=lambda: Probe(False),
            run=lambda: 0,
            explain=explain,
        )
        self.assertIs(built.explain_exit, explain)

    def test_the_gate_runs_in_the_given_directory(self) -> None:
        shell = FakeShell({"gh": Output(0, out="#29 open")})
        built = finish.apply_step(
            finish.ApplySettings(gate=("gh", "pr", "list"), gate_why=Text("dock first")),
            directory=self.tmp,
            shell=shell,
            env={},
            probe=lambda: Probe(False),
            run=lambda: 0,
        )
        assert built.gate is not None
        self.assertEqual(built.gate(), "#29 open")
        self.assertEqual(shell.calls[-1][1], self.tmp)
        self.assertEqual(built.gate_why, Text("dock first"))

    def test_a_gate_that_cannot_answer_is_a_wait_never_a_go(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)
        built = finish.apply_step(
            finish.ApplySettings(gate=("gh",)),
            directory=self.tmp,
            shell=FakeShell({"gh": Output(4, err="gh: not logged in")}),
            env={},
            probe=lambda: Probe(False),
            run=lambda: 0,
        )
        assert built.gate is not None
        self.assertIn("gh: not logged in", built.gate())


if __name__ == "__main__":
    unittest.main()

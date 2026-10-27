"""``ubuntu-tweak finish``: steps collected from modules and the profile, run end to end with real shell scripts."""

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_cli import run_main
from tests.util import TempDirCase, write
from ubuntu_tweaks import finish, ui
from ubuntu_tweaks.actions import Action
from ubuntu_tweaks.commands import finish as finish_command
from ubuntu_tweaks.commands._support import has_gi
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.finish import Probe, State, SubprocessShell
from ubuntu_tweaks.journal import Entry, Run, action_key
from ubuntu_tweaks.profile import Profile

MODULE = """
description = "demo"
order = {order}

[source]
kind = "system"
root = false
install = "demo"
check = "demo"

[[finish]]
id = "{step}"
title = "Title {step}"
why = "Why {step}"
action = "Do {step}"
check = ["./probe", "{step}"]
run = ["./probe", "{step}", "run"]
{extra}
"""

PROBE = """#!/bin/sh
# exit 0 once the marker file of the step exists; `run` creates it
marker="$(dirname "$0")/$1.done"
if [ "${2:-}" = "run" ]; then echo "$1 run options=$UBUNTU_TWEAK_MODULE_CONFIG lang=$UBUNTU_TWEAK_LANG dry=${UBUNTU_TWEAK_FINISH_DRY_RUN:-no}" >> "$(dirname "$0")/log"; touch "$marker"; exit 0; fi
[ -e "$marker" ]
"""


class Plans:
    """The apply step's verdict, scripted."""

    def __init__(self, *verdicts: bool, settled: bool = False) -> None:
        self.verdicts = list(verdicts)
        self.settled = settled

    def __call__(self, ctx: Context) -> Probe:
        done = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        return Probe(done, "" if done else "2 root actions", self.settled and not done)


class FinishCommandCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)
        self.repo = self.tmp / "repo"
        self.profile = Profile(self.tmp / "profile", "box")
        self.profile.root.mkdir()

    def module(self, name: str, step: str, *, order: int = 500, extra: str = "", profile: bool = False) -> Path:
        directory = (self.profile.modules_dir if profile else self.repo / "modules") / name
        write(directory / "module.toml", MODULE.format(order=order, step=step, extra=extra))
        script = write(directory / "probe", PROBE)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return directory

    def context(self, only: frozenset[str] = frozenset()) -> Context:
        return Context(self.repo, self.profile, only=only)

    def steps(self, *, dry_run: bool = False, plans: Plans | None = None):
        return finish_command.collect_steps(
            self.context(),
            shell=SubprocessShell(),
            environ={"PATH": os.environ["PATH"]},
            dry_run=dry_run,
            probe=plans or Plans(True),
            apply_run=lambda: 0,
        )


class CollectStepsTests(FinishCommandCase):
    def test_apply_comes_first_then_modules_by_order_then_name(self) -> None:
        self.module("zeta", "z-step", order=10)
        self.module("alpha", "a-step", order=900)
        self.module("beta", "b-step", order=900)
        self.module("mine", "p-step", order=20, profile=True)
        self.assertEqual([s.id for s in self.steps()], ["apply", "z-step", "p-step", "a-step", "b-step"])

    def test_a_disabled_module_adds_no_step(self) -> None:
        self.module("off", "off-step")
        write(self.profile.profile_toml, "[modules.off]\nenabled = false\n")
        self.assertEqual([s.id for s in self.steps()], ["apply"])

    def test_a_missing_profile_directory_does_not_stop_the_apply_step_from_being_built(self) -> None:
        # a machine whose dotfiles profile is not cloned yet: `apply` clones it, so its command must still run
        self.profile = Profile(self.tmp / "not-cloned", "box")
        self.assertEqual([s.id for s in self.steps()], ["apply"])

    def test_a_step_that_requires_itself_is_a_config_error(self) -> None:
        self.module("a", "a-step", extra='requires = ["a-step"]')
        with self.assertRaisesRegex(ConfigError, "requires itself"):
            self.steps()

    def test_the_same_step_id_in_two_modules_is_a_config_error(self) -> None:
        self.module("a", "same")
        self.module("b", "same")
        with self.assertRaisesRegex(ConfigError, "same.*module a.*module b"):
            self.steps()

    def test_the_module_options_language_and_dry_run_reach_the_commands_as_environment(self) -> None:
        directory = self.module("a", "a-step")
        write(self.profile.profile_toml, '[modules.a]\napple_id = "me@example.org"\n')
        for dry in (False, True):
            (directory / "log").unlink(missing_ok=True)
            steps = self.steps(dry_run=dry)
            run = steps[1].run
            assert run is not None
            self.assertEqual(run(), 0)
            logged = (directory / "log").read_text()
            self.assertIn('options={"apple_id":"me@example.org"}', logged)
            self.assertIn("lang=en", logged)
            self.assertIn("dry=1" if dry else "dry=no", logged)
            (directory / "a-step.done").unlink()

    def test_what_the_caller_inherited_about_modules_and_dry_runs_does_not_leak_into_a_command(self) -> None:
        directory = self.module("a", "a-step")
        steps = finish_command.collect_steps(
            self.context(),
            shell=SubprocessShell(),
            environ={
                "PATH": os.environ["PATH"],
                "UBUNTU_TWEAK_MODULE_CONFIG": '{"stale":true}',
                "UBUNTU_TWEAK_FINISH_DRY_RUN": "1",
            },
            dry_run=False,
            probe=Plans(True),
            apply_run=lambda: 0,
        )
        run = steps[1].run
        assert run is not None
        run()
        logged = (directory / "log").read_text()
        self.assertIn("options= ", logged)
        self.assertIn("dry=no", logged)

    def test_apply_gets_its_gate_from_the_profile_and_runs_in_the_profile_directory(self) -> None:
        write(
            self.profile.profile_toml,
            '[finish.apply]\ngate = ["sh", "-c", "echo \\"#29 open\\""]\ngate_why = "dock first"\n',
        )
        (apply_step, *_) = self.steps(plans=Plans(False))
        assert apply_step.gate is not None
        self.assertEqual(apply_step.gate(), "#29 open")
        self.assertEqual(apply_step.gate_why, finish.Text("dock first"))

    def test_the_machine_overlay_can_set_the_gate_too(self) -> None:
        write(self.profile.machine_toml, '[finish.apply]\ngate = ["true"]\n')
        (apply_step, *_) = self.steps()
        self.assertIsNotNone(apply_step.gate)


class ProfileStepsTests(FinishCommandCase):
    STEP = """
[[finish.steps]]
id = "{name}"
title = "Title {name}"
why = "Why"
action = "Do"
check = ["./probe", "{name}"]
"""

    def test_steps_of_profile_toml_then_machine_toml_come_after_the_modules(self) -> None:
        self.module("a", "a-step")
        write(self.profile.profile_toml, self.STEP.format(name="shared-step"))
        write(self.profile.machine_toml, self.STEP.format(name="machine-step"))
        write(self.profile.root / "probe", PROBE)
        self.assertEqual([s.id for s in self.steps()], ["apply", "a-step", "shared-step", "machine-step"])

    def test_a_profile_command_runs_from_the_profile_directory(self) -> None:
        script = write(self.profile.root / "probe", PROBE)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        write(self.profile.machine_toml, self.STEP.format(name="chore"))
        chore = self.steps()[1]
        self.assertFalse(chore.check().ok)
        (self.profile.root / "chore.done").touch()
        self.assertTrue(chore.check().ok)

    def test_a_profile_step_may_not_reuse_a_module_step_id(self) -> None:
        self.module("a", "dup")
        write(self.profile.machine_toml, self.STEP.format(name="dup"))
        with self.assertRaisesRegex(ConfigError, "dup"):
            self.steps()

    def test_a_malformed_finish_table_is_a_config_error_naming_the_file(self) -> None:
        write(self.profile.machine_toml, 'finish = "x"\n')
        with self.assertRaisesRegex(ConfigError, "machine.toml"):
            self.steps()
        write(self.profile.machine_toml, '[[finish.steps]]\nid = "x"\n')
        with self.assertRaisesRegex(ConfigError, "missing required key"):
            self.steps()

    def test_the_apply_table_beside_the_steps_is_not_mistaken_for_a_step(self) -> None:
        write(self.profile.machine_toml, '[finish.apply]\ngate = ["true"]\n' + self.STEP.format(name="chore"))
        self.assertEqual([s.id for s in self.steps()], ["apply", "chore"])


class WalkEndToEndTests(FinishCommandCase):
    def walk(self, steps, *answers: str, **kwargs):
        queue = list(answers)
        out = io.StringIO()
        walked = finish.walk(
            steps,
            out=out,
            ask=lambda prompt: queue.pop(0),
            interactive=kwargs.pop("interactive", True),
            assume_yes=kwargs.pop("yes", False),
            dry_run=kwargs.pop("dry_run", False),
        )
        return walked.statuses, out.getvalue()

    def test_the_real_scripts_are_checked_run_and_checked_again(self) -> None:
        directory = self.module("a", "a-step")
        result, text = self.walk(self.steps(), "")
        self.assertEqual(result["a-step"].state, State.DONE)
        self.assertTrue((directory / "a-step.done").exists())
        self.assertIn("All done", text)

    def test_a_step_that_requires_apply_waits_for_it_and_runs_once_apply_is_done(self) -> None:
        self.module("a", "a-step", extra='requires = ["apply"]')
        steps = self.steps(plans=Plans(False))
        result, _ = self.walk(steps, "q")  # apply is offered first; the person quits before it runs
        self.assertEqual(result["a-step"].state, State.BLOCKED)

    def test_dry_run_runs_no_run_command_and_creates_nothing(self) -> None:
        directory = self.module("a", "a-step")
        result, text = self.walk(self.steps(dry_run=True), dry_run=True, interactive=False)
        self.assertEqual(result["a-step"].state, State.PENDING)
        self.assertFalse((directory / "a-step.done").exists())
        self.assertIn("Do a-step", text)


class ApplyProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)

    class Root(Action):
        kind = "system"
        needs_root = True

        def describe(self) -> str:
            return "root thing"

        def check(self) -> bool:
            return False

        def run(self) -> None: ...

    class User(Root):
        needs_root = False

    def run_of(self, *statuses: str, finished: bool = True) -> Run:
        entries = [Entry(f"k{n}", "system", "x", status, "") for n, status in enumerate(statuses)]
        return Run("1", "apply --commit", 0.0, finished, 0, entries)

    def test_no_root_action_and_a_clean_last_run_is_done(self) -> None:
        self.assertTrue(finish.apply_probe([self.User()], self.run_of("done", "manual")).ok)
        self.assertTrue(finish.apply_probe([], None).ok)

    def test_a_pending_root_action_is_not_done(self) -> None:
        probe = finish.apply_probe([self.Root(), self.User()], None)
        self.assertFalse(probe.ok)
        self.assertEqual(probe.detail, ui.t("finish.apply.left", root=1, problems=0))

    def test_a_failed_or_deferred_item_of_the_last_run_is_not_done(self) -> None:
        for status in ("failed", "deferred"):
            with self.subTest(status=status):
                self.assertFalse(finish.apply_probe([], self.run_of("done", status)).ok)

    def test_a_root_action_that_waits_for_a_person_does_not_keep_apply_pending(self) -> None:
        # firefox-policy waits for the AMO key: that is another finish step's business, and that step needs apply first
        root = self.Root()
        waiting = Entry(action_key(root), "system", "x", "manual", "")
        last = Run("1", "apply --commit", 0.0, True, 0, [waiting])
        self.assertTrue(finish.apply_probe([root], last).ok)

    def test_a_blocked_root_action_is_not_taken_for_waiting(self) -> None:
        # `blocked` means a prerequisite failed (which counts), not that a person can unblock it
        root = self.Root()
        last = Run("1", "apply --commit", 0.0, True, 0, [Entry(action_key(root), "system", "x", "blocked", "")])
        self.assertFalse(finish.apply_probe([root], last).ok)

    def test_a_root_action_the_last_run_did_not_report_still_needs_apply(self) -> None:
        other = Entry("system|x|other", "system", "x", "manual", "")
        last = Run("1", "apply --commit", 0.0, True, 0, [other])
        self.assertFalse(finish.apply_probe([self.Root()], last).ok)

    def test_an_interrupted_last_run_is_not_done(self) -> None:
        self.assertFalse(finish.apply_probe([], self.run_of("done", finished=False)).ok)

    def test_a_finished_run_that_reported_every_remaining_root_action_is_settled(self) -> None:
        failing, waiting = self.Root(), self.Root()
        failing.key, waiting.key = "f", "w"
        last = Run(
            "1",
            "apply --commit",
            0.0,
            True,
            7,
            [
                Entry(action_key(failing), "system", "x", "failed", "boom"),
                Entry(action_key(waiting), "system", "x", "manual", "key"),
            ],
        )
        probe = finish.apply_probe([failing, waiting], last)
        self.assertFalse(probe.ok)
        self.assertTrue(probe.settled)
        self.assertEqual(probe.detail, ui.t("finish.apply.reported", n=1))

    def test_a_failed_item_that_needs_no_root_settles_a_finished_run(self) -> None:
        probe = finish.apply_probe([], self.run_of("done", "failed"))
        self.assertFalse(probe.ok)
        self.assertTrue(probe.settled)

    def test_a_blocked_root_action_the_run_reported_is_settled(self) -> None:
        root = self.Root()
        last = Run("1", "apply --commit", 0.0, True, 7, [Entry(action_key(root), "system", "x", "blocked", "")])
        probe = finish.apply_probe([root], last)
        self.assertFalse(probe.ok)
        self.assertTrue(probe.settled)

    def test_deferred_root_work_never_settles_apply(self) -> None:
        # exit 6: root was not obtained, so a run with root still has all of it to do; nothing behind apply may start
        root = self.Root()
        last = Run(
            "1", "apply --commit", 0.0, True, 6, [Entry(action_key(root), "system", "x", "deferred", "needs root")]
        )
        probe = finish.apply_probe([root], last)
        self.assertFalse(probe.ok)
        self.assertFalse(probe.settled)

    def test_deferred_entries_keep_apply_unsettled_even_when_the_plan_lists_no_root_action(self) -> None:
        last = Run("1", "apply --commit", 0.0, True, 6, [Entry("system|x|d", "system", "x", "deferred", "needs root")])
        probe = finish.apply_probe([], last)
        self.assertFalse(probe.ok)
        self.assertFalse(probe.settled)

    def test_a_settled_run_counts_failed_and_blocked_items_only(self) -> None:
        root = self.Root()
        last = Run(
            "1",
            "apply --commit",
            0.0,
            True,
            7,
            [
                Entry(action_key(root), "system", "x", "blocked", ""),
                Entry("system|x|f", "system", "x", "failed", "boom"),
                Entry("system|x|w", "system", "x", "manual", ""),
            ],
        )
        probe = finish.apply_probe([root], last)
        self.assertTrue(probe.settled)
        self.assertEqual(probe.detail, ui.t("finish.apply.reported", n=2))

    def test_apply_that_never_ran_with_root_work_left_is_not_settled(self) -> None:
        probe = finish.apply_probe([self.Root()], None)
        self.assertFalse(probe.ok)
        self.assertFalse(probe.settled)

    def test_an_interrupted_run_is_not_settled(self) -> None:
        probe = finish.apply_probe([], self.run_of("done", "failed", finished=False))
        self.assertFalse(probe.ok)
        self.assertFalse(probe.settled)

    def test_a_root_action_no_run_reported_keeps_the_step_unsettled_even_with_a_failure(self) -> None:
        failed = Entry("system|x|other", "system", "x", "failed", "boom")
        probe = finish.apply_probe([self.Root()], Run("1", "apply --commit", 0.0, True, 7, [failed]))
        self.assertFalse(probe.ok)
        self.assertFalse(probe.settled)
        self.assertEqual(probe.detail, ui.t("finish.apply.left", root=1, problems=1))

    @unittest.skipUnless(has_gi(), "needs PyGObject")
    def test_a_plan_that_cannot_be_computed_keeps_the_step_pending_with_the_reason(self) -> None:
        from ubuntu_tweaks import reconcile
        from ubuntu_tweaks.actions import ActionFailed

        with (
            mock.patch.object(reconcile, "open_backend", return_value=None),
            mock.patch.object(
                reconcile, "build_plan", side_effect=ActionFailed("mod: cannot run check x\n  not found")
            ),
        ):
            probe = finish.plan_probe(mock.sentinel.ctx)
        self.assertFalse(probe.ok)
        self.assertIn("mod: cannot run check x", probe.detail)


class CommandLineTests(FinishCommandCase):
    def run_finish(self, *, dry_run: bool = True, step: str | None = None, yes: bool = False, lang: str = "en"):
        ui.configure(color="never", plain=True, lang=lang)
        args = argparse.Namespace(dry_run=dry_run, step=step, yes=yes)
        out = io.StringIO()
        with mock.patch.object(finish_command, "_apply_probe", Plans(True)), contextlib.redirect_stdout(out):
            code = finish_command.run(self.context(), args)
        return code, out.getvalue()

    def test_dry_run_lists_every_step_and_exits_3_while_one_is_pending(self) -> None:
        self.module("a", "a-step")
        code, out = self.run_finish()
        self.assertEqual(code, 3)
        self.assertIn("Title a-step", out)

    def test_step_limits_the_list_to_one_step_and_what_it_requires(self) -> None:
        self.module("a", "a-step")
        self.module("b", "b-step", extra='requires = ["a-step"]')
        self.module("c", "c-step")
        code, out = self.run_finish(step="b-step")
        self.assertEqual(code, 3)
        self.assertIn("Title a-step", out)
        self.assertIn("Title b-step", out)
        self.assertNotIn("Title c-step", out)
        self.assertNotIn("Apply the setup", out)

    def test_an_unknown_step_is_a_config_error_naming_the_known_ones(self) -> None:
        self.module("a", "a-step")
        with self.assertRaisesRegex(ConfigError, "nope.*apply, a-step"):
            self.run_finish(step="nope")

    def test_exit_0_when_everything_is_done(self) -> None:
        directory = self.module("a", "a-step")
        (directory / "a-step.done").touch()
        code, out = self.run_finish()
        self.assertEqual(code, 0)
        self.assertIn("All done", out)

    def test_russian_output_with_lang_ru(self) -> None:
        self.module("a", "a-step")
        _, out = self.run_finish(lang="ru")
        self.assertIn("Завершение настройки", out)

    def test_without_a_terminal_a_real_run_lists_and_runs_nothing(self) -> None:
        directory = self.module("a", "a-step")
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, out = self.run_finish(dry_run=False)
        self.assertEqual(code, 3)
        self.assertIn("needs a terminal", out)
        self.assertFalse((directory / "a-step.done").exists())

    def test_only_is_refused_with_a_pointer_to_step(self) -> None:
        code, _, err = run_main("finish", "--only", "passwords-app")
        self.assertEqual(code, 2)
        self.assertIn("--step", err)

    def test_the_apply_step_runs_apply_commit_through_the_streamed_runner_after_enter(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        out = io.StringIO()
        with (
            mock.patch.object(finish_command, "_apply_probe", Plans(False, True)),
            mock.patch.object(finish_command, "run_streamed", return_value=0) as streamed,
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(out),
        ):
            code = finish_command.run(self.context(), argparse.Namespace(dry_run=False, step=None, yes=False))
        self.assertEqual(code, 0)
        (argv,), _ = streamed.call_args
        self.assertEqual(argv[1:4], [str(self.repo / "bin" / "ubuntu-tweak"), "apply", "--commit"])
        self.assertIn("apply", argv)
        self.assertIn("--commit", argv)
        self.assertIn("All done", out.getvalue())

    def test_a_partial_apply_is_explained_and_the_steps_behind_it_still_run(self) -> None:
        directory = self.module("a", "a-step", extra='requires = ["apply"]')
        document = {
            "id": "1",
            "command": "apply --commit",
            "started": time.time() + 60,  # after the apply this test starts
            "finished": True,
            "exit_code": 7,
            "entries": [
                {"key": "link||l", "kind": "link", "text": "link x", "status": "done", "detail": ""},
                {
                    "key": "system-module|passwords-app-polkit|system-module:passwords-app-polkit",
                    "kind": "system-module",
                    "text": "run passwords-app-polkit",
                    "status": "failed",
                    "detail": "sudo -- x install: exited with status 1: refusing the policy: a DOCTYPE could hide actions",
                },
            ],
        }
        write(self.tmp / "state" / "ubuntu-tweaks" / "run-journal.json", json.dumps(document))
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "state")}),
            mock.patch.object(finish_command, "_apply_probe", Plans(False, settled=True)),
            mock.patch.object(finish_command, "run_streamed", return_value=7),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(out),
        ):
            code = finish_command.run(self.context(), argparse.Namespace(dry_run=False, step=None, yes=False))
        text = out.getvalue()
        self.assertEqual(code, 3)  # apply is still not done
        self.assertIn("1 changed, 1 failed, 0 blocked", text)
        self.assertIn("refusing the policy: a DOCTYPE could hide actions", text)
        self.assertIn("ubuntu-tweak apply --commit --only passwords-app-polkit", text)
        self.assertNotIn("exited with 7", text)
        self.assertTrue((directory / "a-step.done").exists())  # the step behind apply was not held back by it

    def test_a_terminal_on_stdin_alone_is_enough_to_walk(self) -> None:
        # `finish-setup | tee log` still has a terminal to answer on
        self.module("a", "a-step")
        ui.configure(color="never", plain=True, lang="en")
        out = io.StringIO()
        with (
            mock.patch.object(finish_command, "_apply_probe", Plans(True)),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(out),
        ):
            code = finish_command.run(self.context(), argparse.Namespace(dry_run=False, step=None, yes=False))
        self.assertEqual(code, 0)
        self.assertNotIn("needs a terminal", out.getvalue())

    def test_dry_run_and_yes_are_a_usage_error_through_the_real_entry_point(self) -> None:
        code, _, err = run_main("finish", "--dry-run", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("exclude each other", err)

    def test_the_command_is_discovered_and_takes_the_global_flags(self) -> None:
        from ubuntu_tweaks.cli import build_parser

        args = build_parser().parse_args(["finish", "--step", "x", "--lang", "ru", "--dry-run"])
        self.assertEqual((args.step, args.lang, args.dry_run), ("x", "ru", True))


if __name__ == "__main__":
    unittest.main()

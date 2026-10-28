"""The ``system`` module planner: one root action per enabled module, decided by its checker."""

import stat
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks import process
from ubuntu_tweaks.actions import ActionFailed, Phase, Plan, RetryPolicy
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.planners import system_modules
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend

MANIFEST = """
description = "root thing"
enabled = {enabled}

[source]
kind = "system"
install = "install.sh --now"
check = "install.sh --check"
"""

LOCAL = """
[source]
kind = "local"
path = "gnome-extensions/x"
"""


class Recorder(process.Process):
    """A process double: ``query`` is the checker, ``execute`` the installer; both are recorded."""

    def __init__(self, check_exit: int = 0, install_exit: int = 0, stderr: str = "") -> None:
        self.check_exit = check_exit
        self.install_exit = install_exit
        self.stderr = stderr
        self.calls: list[tuple[list[str], Path, float | None, bool]] = []

    def query(self, argv: Sequence[str], *, timeout: float = 60.0, cwd: Path | None = None) -> process.CommandResult:
        self.calls.append((list(argv), cwd, timeout, True))
        return process.CommandResult(self.check_exit, "", self.stderr)

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        self.calls.append((list(argv), cwd, None, False))
        if self.install_exit:
            raise process.CommandError(f"{' '.join(argv)}: exited with status {self.install_exit}", self.install_exit)

    def elevate(self, argv: Sequence[str]) -> list[str]:
        return ["sudo", "--", *argv]


class SystemModulePlannerTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.profile = Profile(self.tmp / "profile", "home")
        self.profile.root.mkdir()

    def module(self, name: str, *, enabled: bool = True) -> None:
        write(self.repo / "modules" / name / "module.toml", MANIFEST.format(enabled=str(enabled).lower()))

    def candidates(self, runner: Recorder, only: frozenset[str] = frozenset()) -> list:
        ctx = Context(self.repo, self.profile, only)
        return system_modules.build(ctx, FakeBackend(), process=runner)

    def test_unsatisfied_module_is_one_root_system_action_with_the_exact_command(self) -> None:
        self.module("memguard")
        runner = Recorder(check_exit=3)

        plan = Plan.build(self.candidates(runner))

        (action,) = plan.actions
        self.assertEqual(
            (action.kind, action.phase, action.needs_root, action.scope),
            ("system-module", Phase.SYSTEM, True, "memguard"),
        )
        self.assertEqual(action.describe(), "run memguard: sudo -- modules/memguard/install.sh --now")
        self.assertIn("[sudo] [system-module] run memguard: sudo -- modules/memguard/install.sh --now", plan.render())

    def test_the_action_is_named_by_its_module_and_carries_the_manifests_stall_time(self) -> None:
        write(
            self.repo / "modules/earlyoom/module.toml",
            "stall_seconds = 900\n" + MANIFEST.format(enabled="true"),
        )
        self.module("plain")

        actions = {action.scope: action for action in self.candidates(Recorder(check_exit=3))}

        earlyoom, plain = actions["earlyoom"], actions["plain"]
        self.assertEqual((earlyoom.stall_seconds, earlyoom.step_name()), (900, "run earlyoom"))
        self.assertIsNone(plain.stall_seconds)

    def test_checker_runs_inside_the_module_directory_without_sudo_and_with_a_timeout(self) -> None:
        self.module("memguard")
        runner = Recorder(check_exit=0)

        plan = Plan.build(self.candidates(runner))

        directory = self.repo / "modules/memguard"
        self.assertEqual(len(plan), 0)
        self.assertEqual(plan.satisfied, 1)
        self.assertEqual(runner.calls, [([str(directory / "install.sh"), "--check"], directory, 60, True)])

    def test_a_crashing_checker_fails_with_the_module_name_and_its_output(self) -> None:
        self.module("memguard")
        runner = Recorder(check_exit=1, stderr="line one\nmemguard.toml: user 'x' does not exist\n")

        with self.assertRaisesRegex(
            ActionFailed, r"(?s)memguard: check .*install\.sh --check crashed \(exit 1.*\n    memguard.toml: user 'x'"
        ):
            Plan.build(self.candidates(runner))

    def test_a_checker_timeout_and_a_missing_checker_are_clear_failures(self) -> None:
        self.module("memguard")
        for code, stderr, pattern in (
            (124, "x: timed out after 60 s", r"memguard: check .* did not finish within 60s"),
            (127, "x: command not found", r"(?s)memguard: cannot run check .*command not found"),
            (126, "x: cannot run: Permission denied", r"(?s)memguard: cannot run check .*Permission denied"),
        ):
            with self.subTest(code=code):
                runner = Recorder(check_exit=code, stderr=stderr)
                ctx = Context(self.repo, self.profile)
                actions = system_modules.build(ctx, FakeBackend(), process=runner)
                with self.assertRaisesRegex(ActionFailed, pattern):
                    Plan.build(actions)

    def test_modules_off_by_default_or_by_overlay_are_not_planned_and_overlay_can_enable(self) -> None:
        self.module("apple-hid-battery", enabled=False)
        self.module("memguard")
        write(self.profile.machine_toml, "[modules.memguard]\nenabled = false\n")
        self.assertEqual(self.candidates(Recorder(check_exit=3)), [])

        write(self.profile.machine_toml, "[modules.apple-hid-battery]\nenabled = true\n")
        names = [action.scope for action in self.candidates(Recorder(check_exit=3))]
        self.assertEqual(names, ["apple-hid-battery", "memguard"])

    def test_other_source_kinds_are_not_system_modules(self) -> None:
        write(self.repo / "modules/ext/module.toml", LOCAL)
        self.assertEqual(self.candidates(Recorder()), [])

    def test_only_selects_modules_by_name(self) -> None:
        self.module("memguard")
        self.module("apple-hid-battery")
        write(self.profile.machine_toml, "[modules.apple-hid-battery]\nenabled = true\n")
        plan = Plan.build(
            self.candidates(Recorder(check_exit=3), frozenset({"memguard"})), only=frozenset({"memguard"})
        )
        self.assertEqual([action.scope for action in plan.actions], ["memguard"])

    def test_no_sudo_skips_the_installer_and_says_so(self) -> None:
        self.module("memguard")
        runner = Recorder(check_exit=3)
        plan = Plan.build(self.candidates(runner))

        result = plan.execute(no_sudo=True)

        self.assertEqual((result.changed, len(result.skipped)), (0, 1))
        self.assertIn("[sudo, skipped: --no-sudo]", plan.render(no_sudo=True))
        self.assertEqual({capture for *_, capture in runner.calls}, {True})

    def test_commit_runs_the_installer_through_sudo_attached_and_then_rechecks(self) -> None:
        self.module("memguard")

        class Converging(Recorder):
            def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
                self.check_exit = 0
                super().execute(argv, input_text=input_text, cwd=cwd)

        runner = Converging(check_exit=3)
        plan = Plan.build(self.candidates(runner))

        result = plan.execute()

        directory = self.repo / "modules/memguard"
        self.assertEqual(result.changed, 1)
        self.assertIn((["sudo", "--", str(directory / "install.sh"), "--now"], directory, None, False), runner.calls)

    def test_a_failing_installer_and_one_that_does_not_converge_both_fail(self) -> None:
        self.module("memguard")
        failed = Plan.build(self.candidates(Recorder(check_exit=3, install_exit=2))).execute().failed
        self.assertRegex(failed[0].message, r"memguard: sudo -- .*install\.sh --now: exited with status 2")
        failed = Plan.build(self.candidates(Recorder(check_exit=3, install_exit=0))).execute().failed
        self.assertRegex(failed[0].message, r"still not satisfied")

    FALLBACK = (
        MANIFEST.format(enabled="true")
        + '\n[[fallback]]\ninstall = "install-user.sh"\nroot = false\ndescription = "user-level tarball"\n'
    )

    class Flipping(Recorder):
        """The checker turns satisfied once the user-level installer ran."""

        def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
            if any("install-user.sh" in part for part in argv):
                self.check_exit = 0
                self.install_exit = 0
            super().execute(argv, input_text=input_text, cwd=cwd)

    def test_a_failing_primary_installer_falls_back_to_the_declared_alternative(self) -> None:
        write(self.repo / "modules/memguard/module.toml", self.FALLBACK)
        runner = self.Flipping(check_exit=3, install_exit=2)

        result = Plan.build(self.candidates(runner)).execute()

        directory = self.repo / "modules/memguard"
        self.assertEqual((result.changed, result.failed), (1, ()))
        self.assertEqual([item[1] for item in result.fallbacks], ["user-level tarball"])
        self.assertIn(([str(directory / "install-user.sh")], directory, None, False), runner.calls)

    def test_without_root_the_user_level_fallback_runs_and_the_root_primary_never_does(self) -> None:
        write(self.repo / "modules/memguard/module.toml", self.FALLBACK)
        runner = self.Flipping(check_exit=3)

        result = Plan.build(self.candidates(runner)).execute(no_sudo=True)

        self.assertEqual((result.changed, result.skipped), (1, ()))
        self.assertFalse(any(call[0][:2] == ["sudo", "--"] for call in runner.calls))

    def test_dry_run_lists_the_declared_fallbacks_under_the_action(self) -> None:
        write(self.repo / "modules/memguard/module.toml", self.FALLBACK)
        (action,) = Plan.build(self.candidates(Recorder(check_exit=3))).actions
        self.assertIn("fallback (no root): install-user.sh", action.describe())

    def test_root_does_not_prefix_sudo(self) -> None:
        self.module("memguard")
        ctx = Context(self.repo, self.profile)
        with mock.patch.object(process.os, "geteuid", return_value=0):
            (action,) = system_modules.build(ctx, FakeBackend(), process=process.Process())
            self.assertEqual(action.describe(), "run memguard: modules/memguard/install.sh --now")

    def test_root_false_runs_the_installer_directly_without_sudo_marker(self) -> None:
        write(
            self.repo / "modules/user-tool/module.toml",
            MANIFEST.format(enabled="true").replace('kind = "system"', 'kind = "system"\nroot = false'),
        )

        class Converging(Recorder):
            def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
                self.check_exit = 0
                super().execute(argv, input_text=input_text, cwd=cwd)

        runner = Converging(check_exit=3)
        plan = Plan.build(self.candidates(runner))

        (action,) = plan.actions
        self.assertFalse(action.needs_root)
        self.assertEqual(action.describe(), "run user-tool: modules/user-tool/install.sh --now")
        self.assertNotIn("[sudo]", plan.render())
        result = plan.execute(no_sudo=True)
        self.assertEqual(len(result.skipped), 0)
        directory = self.repo / "modules/user-tool"
        self.assertIn(([str(directory / "install.sh"), "--now"], directory, None, False), runner.calls)

    def test_module_options_reach_the_checker_and_the_installer_through_env_even_under_sudo(self) -> None:
        self.module("ssh-server")
        write(
            self.profile.machine_toml,
            "[modules.ssh-server]\nenabled = true\npassword_auth = false\nports = [22, 2222]\n",
        )
        runner = Recorder(check_exit=3)
        plan = Plan.build(self.candidates(runner))
        directory = self.repo / "modules/ssh-server"
        wanted = 'UBUNTU_TWEAK_MODULE_CONFIG={"password_auth":false,"ports":[22,2222]}'

        self.assertEqual(runner.calls[0][0], ["env", wanted, str(directory / "install.sh"), "--check"])
        plan.actions[0].run()
        self.assertEqual(runner.calls[-1][0], ["sudo", "--", "env", wanted, str(directory / "install.sh"), "--now"])

    def test_no_options_means_no_env_prefix(self) -> None:
        self.module("memguard")
        write(self.profile.machine_toml, "[modules.memguard]\nenabled = true\n")
        runner = Recorder(check_exit=0)
        Plan.build(self.candidates(runner))
        self.assertEqual(runner.calls[0][0][0], str(self.repo / "modules/memguard/install.sh"))

    def test_installer_exit_10_is_a_manual_step_not_a_failure(self) -> None:
        self.module("browser-tool")
        runner = Recorder(check_exit=3, install_exit=10)
        plan = Plan.build(self.candidates(runner))

        result = plan.execute()

        self.assertEqual(result.changed, 0)
        ((action, message),) = result.manual
        self.assertEqual(action.scope, "browser-tool")
        self.assertIn("browser-tool: the installer needs a manual step (exit 10", message)
        self.assertFalse(action.check())

    def test_installer_exit_75_is_transient_and_retried(self) -> None:
        self.module("browser-tool")
        runner = Recorder(check_exit=3, install_exit=75)
        plan = Plan.build(self.candidates(runner))
        slept: list[float] = []

        result = plan.execute(policy=RetryPolicy(attempts=3, sleep=slept.append, jitter=lambda low, high: high))

        (failure,) = result.failed
        self.assertEqual((failure.attempts, failure.transient), (3, True))
        self.assertEqual(len(slept), 2)
        self.assertEqual(sum(1 for call in runner.calls if not call[3]), 3)

    def test_preview_command_output_is_shown_under_the_action_and_never_fails_the_plan(self) -> None:
        write(
            self.repo / "modules/passkey/module.toml",
            MANIFEST.format(enabled="true").replace(
                'check = "install.sh --check"', 'check = "install.sh --check"\npreview = "install.sh plan"'
            ),
        )

        class Previewing(Recorder):
            plan_exit = 0

            def query(self, argv, *, timeout=60.0, cwd=None):  # type: ignore[no-untyped-def]
                if argv[-1] == "plan":
                    self.calls.append((list(argv), cwd, timeout, True))
                    if self.plan_exit:
                        return process.CommandResult(self.plan_exit, "", "\nno network\nmore\n")
                    return process.CommandResult(0, "step one\nstep two\n", "")
                return super().query(argv, timeout=timeout, cwd=cwd)

        runner = Previewing(check_exit=3)
        plan = Plan.build(self.candidates(runner))
        self.assertEqual(
            plan.render().splitlines()[1:],
            [
                "  1. [sudo] [system-module] run passkey: sudo -- modules/passkey/install.sh --now",
                "        step one",
                "        step two",
            ],
        )
        calls = len(runner.calls)
        plan.actions[0].describe()
        self.assertEqual(len(runner.calls), calls, "the preview command runs once per action")

        runner.plan_exit = 2
        (action,) = Plan.build(self.candidates(runner)).actions
        self.assertEqual(action.describe().splitlines()[1:], ["        (preview unavailable: exit 2: no network)"])


class DefaultRunnerTests(TempDirCase):
    def test_a_real_checker_script_is_run_relative_to_its_module(self) -> None:
        repo = self.tmp / "repo"
        write(
            repo / "modules/demo/module.toml", MANIFEST.format(enabled="true").replace("install.sh --now", "install.sh")
        )
        script = write(
            repo / "modules/demo/install.sh",
            '#!/bin/sh\n[ "$1" = --check ] && [ "$(basename "$PWD")" = demo ] && exit 3\nexit 9\n',
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        profile = Profile(self.tmp / "profile", "home")
        profile.root.mkdir()

        plan = Plan.build(system_modules.build(Context(repo, profile), FakeBackend()))

        self.assertEqual([action.scope for action in plan.actions], ["demo"])

    def test_a_checker_without_the_exec_bit_is_a_clear_failure_not_a_traceback(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "modules/demo/module.toml", MANIFEST.format(enabled="true"))
        write(repo / "modules/demo/install.sh", "#!/bin/sh\nexit 3\n")  # no chmod +x
        profile = Profile(self.tmp / "profile", "home")
        profile.root.mkdir()

        with self.assertRaisesRegex(ActionFailed, r"demo: cannot run check .*install\.sh --check"):
            Plan.build(system_modules.build(Context(repo, profile), FakeBackend()))

    def test_preview_renders_without_sudo_or_pkexec_on_the_machine(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "modules/demo/module.toml", MANIFEST.format(enabled="true"))
        (action,) = system_modules.build(Context(repo, Profile(self.tmp / "profile", "home")), FakeBackend())

        with (
            mock.patch.object(process.os, "geteuid", return_value=1000),
            mock.patch.object(process.shutil, "which", return_value=None),
        ):
            self.assertIn("sudo -- modules/demo/install.sh --now", action.describe())
            with self.assertRaisesRegex(ActionFailed, "neither sudo nor pkexec"):
                action.run()


if __name__ == "__main__":
    unittest.main()

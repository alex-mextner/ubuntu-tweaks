"""The ``system`` module planner: one root action per enabled module, decided by its checker."""

import stat
import subprocess
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import ActionFailed, Phase, Plan
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


class Recorder:
    """A runner that answers from ``exit_codes`` (by flag) and remembers every call."""

    def __init__(self, check_exit: int = 0, install_exit: int = 0, stderr: str = "") -> None:
        self.check_exit = check_exit
        self.install_exit = install_exit
        self.stderr = stderr
        self.calls: list[tuple[list[str], Path, float | None, bool]] = []

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float | None, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd, timeout, capture))
        code = self.check_exit if capture else self.install_exit
        return subprocess.CompletedProcess(list(argv), code, stdout="", stderr=self.stderr if capture else "")


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
        with mock.patch.object(system_modules.os, "geteuid", return_value=1000):
            return system_modules.build(ctx, FakeBackend(), runner=runner)

    def test_unsatisfied_module_is_one_root_system_action_with_the_exact_command(self) -> None:
        self.module("memguard")
        runner = Recorder(check_exit=3)

        plan = Plan.build(self.candidates(runner))

        (action,) = plan.actions
        self.assertEqual(
            (action.kind, action.phase, action.needs_root, action.scope),
            ("system-module", Phase.SYSTEM, True, "memguard"),
        )
        self.assertEqual(action.describe(), "run memguard: sudo modules/memguard/install.sh --now")
        self.assertIn("[sudo] [system-module] run memguard: sudo modules/memguard/install.sh --now", plan.render())

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
        for error, pattern in (
            (subprocess.TimeoutExpired("x", 60), r"memguard: check .* did not finish within 60s"),
            (FileNotFoundError(2, "No such file or directory"), r"memguard: cannot run check .*No such file"),
        ):
            with self.subTest(error=type(error).__name__):

                def runner(
                    *_args: object, _error: Exception = error, **_kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    raise _error

                ctx = Context(self.repo, self.profile)
                actions = system_modules.build(ctx, FakeBackend(), runner=runner)
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
            def __call__(self, argv, *, cwd, timeout, capture):  # type: ignore[no-untyped-def]
                if not capture:
                    self.check_exit = 0
                return super().__call__(argv, cwd=cwd, timeout=timeout, capture=capture)

        runner = Converging(check_exit=3)
        plan = Plan.build(self.candidates(runner))

        result = plan.execute()

        directory = self.repo / "modules/memguard"
        self.assertEqual(result.changed, 1)
        self.assertIn((["sudo", str(directory / "install.sh"), "--now"], directory, None, False), runner.calls)

    def test_a_failing_installer_and_one_that_does_not_converge_both_fail(self) -> None:
        self.module("memguard")
        with self.assertRaisesRegex(ActionFailed, r"memguard: sudo .*install\.sh --now exited 2"):
            Plan.build(self.candidates(Recorder(check_exit=3, install_exit=2))).execute()
        with self.assertRaisesRegex(ActionFailed, r"still not satisfied"):
            Plan.build(self.candidates(Recorder(check_exit=3, install_exit=0))).execute()

    def test_root_does_not_prefix_sudo(self) -> None:
        self.module("memguard")
        ctx = Context(self.repo, self.profile)
        with mock.patch.object(system_modules.os, "geteuid", return_value=0):
            (action,) = system_modules.build(ctx, FakeBackend(), runner=Recorder(check_exit=3))
        self.assertEqual(action.describe(), "run memguard: modules/memguard/install.sh --now")


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


if __name__ == "__main__":
    unittest.main()

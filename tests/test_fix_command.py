"""``ubuntu-tweak fix``: doctor, the whole plan, status; one sudo prompt; exit 0 only when clean."""

import argparse
import contextlib
import io
import shlex
import types
import unittest
from unittest import mock

from tests.util import RootStep, host

try:
    from tests.test_apply_commands import CommandScenario, MY_PLANNERS
    from ubuntu_tweaks import planners, reconcile
    from ubuntu_tweaks.actions import Action, ManualStepRequired, Phase
    from ubuntu_tweaks.cli import build_parser
    from ubuntu_tweaks.commands import _support, doctor, fix, status
    from ubuntu_tweaks.commands._support import rerun_command
    from ubuntu_tweaks.elevation import SUDO_ASK, SUDO_CHECK, Cause, RootSession, RootUnavailable
    from ubuntu_tweaks.errors import ExitCode
    from ubuntu_tweaks.process import CommandResult, FakeProcess
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

PASSWORD_NEEDED = CommandResult(1, "", "sudo: a password is required")


class NeedsYou(Action):
    """A step only the user can finish: it stays Missing."""

    kind = "firefox-extension"
    phase = Phase.SYSTEM

    def describe(self) -> str:
        return "sign in"

    def check(self) -> bool:
        return False

    def run(self) -> None:
        raise ManualStepRequired("sign in to iCloud with `passwords-app signin`")


class FixScenario(CommandScenario):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[str] = []
        self.wanted: list[Action] = []
        self.needed: list[doctor.Requirement] = []
        self.fake_process = FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}, effects=self.on_execute)
        self.the_host = host(terminal=True)
        self.confirm = mock.Mock(return_value=True)
        self.global_flags: dict[str, str] = {}
        real_apply = reconcile.apply_detailed
        planner = types.SimpleNamespace(build=lambda ctx, backend: self.wanted)
        for patcher in (
            mock.patch.object(planners, "discover_planners", return_value=[*MY_PLANNERS, planner]),
            mock.patch.object(doctor, "requirements", side_effect=lambda ctx: self.needed),
            mock.patch.object(doctor, "install", side_effect=lambda absent: self.on_install(absent)),
            mock.patch.object(fix, "root_session", side_effect=self.session),
            mock.patch.object(fix, "confirm", self.confirm),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
            mock.patch.object(
                reconcile,
                "apply_detailed",
                side_effect=lambda ctx, **kwargs: real_apply(ctx, **{**kwargs, "backend_factory": self.factory}),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run_apply(commit=True, no_sudo=True)  # the scenario's own extension module: converge it first

    def on_execute(self, argv: tuple[str, ...], input_text: str | None) -> None:
        if argv == SUDO_ASK:
            self.events.append("sudo")

    def session(self, args: argparse.Namespace, subcommand: str, *, skip: str | None = None) -> RootSession:
        return RootSession(
            command=rerun_command(args, subcommand),
            skip=rerun_command(args, skip) if skip else None,
            process=self.fake_process,
            host=self.the_host,
            say=lambda text: None,
        )

    def on_install(self, absent: list[doctor.Requirement]) -> None:
        self.events.append("install")

    def run_fix(self, *, yes: bool = True) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fix.run(self.context(), argparse.Namespace(yes=yes, **self.global_flags))
        return code, out.getvalue()


class FixFlowTests(FixScenario):
    def test_root_is_asked_once_before_the_first_root_action_then_status_runs(self) -> None:
        self.wanted = [RootStep(self.events, "timezone"), RootStep(self.events, "locale")]
        real_status = status.run
        with mock.patch.object(
            status, "run", side_effect=lambda *a: (self.events.append("status"), real_status(*a))[1]
        ):
            code, out = self.run_fix()

        self.assertEqual(self.events, ["sudo", "run:timezone", "run:locale", "status"])
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("2 changed", out)
        self.assertIn("Missing (0):", out)
        self.assertEqual(self.fake_process.executed, [(SUDO_ASK, None)])

    def test_missing_packages_are_installed_after_one_prompt_before_the_plan(self) -> None:
        present = {"tmux": False}
        self.needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: present["tmux"])]
        self.on_install = lambda absent: (self.events.append("install"), present.update(tmux=True))  # noqa: E731
        self.wanted = [RootStep(self.events, "timezone")]

        code, _ = self.run_fix()

        self.assertEqual(self.events, ["sudo", "install", "run:timezone"])
        self.assertEqual(code, ExitCode.OK)
        self.assertEqual(self.fake_process.executed, [(SUDO_ASK, None)])

    def test_a_second_run_changes_nothing_asks_nothing_and_exits_0(self) -> None:
        self.wanted = [RootStep(self.events, "timezone")]
        self.run_fix()
        executed = list(self.fake_process.executed)

        code, out = self.run_fix()

        self.assertEqual(code, ExitCode.OK)
        self.assertIn("0 changed", out)
        self.assertEqual(self.fake_process.executed, executed)
        self.assertEqual(self.events.count("run:timezone"), 1)

    def test_what_only_the_user_can_do_is_listed_with_its_reason_and_exits_3(self) -> None:
        self.wanted = [RootStep(self.events, "timezone"), NeedsYou()]

        code, out = self.run_fix()

        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("1 changed", out)
        tail = out[out.index("still needs you") :]
        self.assertIn("[firefox-extension] sign in to iCloud with `passwords-app signin`", tail)


class FixConsentTests(FixScenario):
    def test_the_plan_is_applied_only_after_the_question_is_answered_yes(self) -> None:
        self.wanted = [RootStep(self.events, "timezone"), RootStep(self.events, "locale")]

        code, _ = self.run_fix(yes=False)

        question = self.confirm.call_args.args[0]
        self.assertIn("2 action(s)", question)
        self.assertIn("2 need root", question)
        self.assertEqual((code, self.events), (ExitCode.OK, ["sudo", "run:timezone", "run:locale"]))

    def test_without_a_terminal_and_without_yes_nothing_is_changed(self) -> None:
        self.confirm.return_value = False
        self.wanted = [RootStep(self.events, "timezone")]
        with mock.patch.object(fix.sys.stdin, "isatty", return_value=False):
            code, out = self.run_fix(yes=False)

        self.assertEqual((code, self.events), (ExitCode.DRIFT, []))
        self.assertIn("Nothing was changed", out)
        self.assertIn("--yes", out)
        self.assertEqual((self.fake_process.queries, self.fake_process.executed), ([], []))

    def test_answering_no_on_a_terminal_changes_nothing(self) -> None:
        self.confirm.return_value = False
        self.wanted = [RootStep(self.events, "timezone")]
        with mock.patch.object(fix.sys.stdin, "isatty", return_value=True):
            code, out = self.run_fix(yes=False)

        self.assertEqual((code, self.events), (ExitCode.DRIFT, []))
        self.assertIn("Nothing was changed", out)

    def test_missing_packages_are_not_installed_without_consent(self) -> None:
        self.confirm.return_value = False
        self.needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: False)]
        with mock.patch.object(fix.sys.stdin, "isatty", return_value=False):
            code, out = self.run_fix(yes=False)

        self.assertEqual((code, self.events), (ExitCode.DRIFT, []))
        self.assertIn("tmux", out)
        self.assertIn("Nothing was changed", out)

    def test_installed_packages_are_admitted_when_the_plan_is_then_declined(self) -> None:
        present = {"tmux": False}
        self.needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: present["tmux"])]
        self.on_install = lambda absent: (self.events.append("install"), present.update(tmux=True))  # noqa: E731
        self.confirm.side_effect = [True, False]  # install: yes, apply the plan: no
        self.wanted = [RootStep(self.events, "timezone")]
        with mock.patch.object(fix.sys.stdin, "isatty", return_value=True):
            code, out = self.run_fix(yes=False)

        self.assertEqual((code, self.events), (ExitCode.DRIFT, ["sudo", "install"]))
        self.assertIn("The missing packages were installed, but nothing else was changed", out)
        self.assertNotIn("Nothing was changed", out)

    def test_ctrl_d_at_a_question_is_a_no(self) -> None:
        with (
            mock.patch.object(_support.sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", side_effect=EOFError),
        ):
            self.assertFalse(_support.confirm("Apply?"))


class FixRootTests(FixScenario):
    def test_no_new_privs_stops_before_anything_changes_with_the_command_to_run(self) -> None:
        self.the_host = host(terminal=True, no_new_privs=True)
        self.wanted = [RootStep(self.events, "timezone")]

        with self.assertRaises(RootUnavailable) as caught:
            self.run_fix()

        error = caught.exception
        self.assertEqual((error.cause, int(error.exit_code), error.fix), (Cause.NO_NEW_PRIVS, 6, "ubuntu-tweak fix"))
        self.assertIn("Or skip the root actions (they are listed): ubuntu-tweak apply --commit --no-sudo", str(error))
        self.assertEqual(self.events, [])

    def test_the_command_to_run_keeps_the_flags_the_user_gave(self) -> None:
        self.the_host = host(no_new_privs=True)
        self.global_flags = {"only": "eng", "profile": str(self.profile_root)}
        self.wanted = [RootStep(self.events, "timezone", scope="eng")]

        with self.assertRaises(RootUnavailable) as caught:
            self.run_fix()

        self.assertEqual(caught.exception.fix, f"ubuntu-tweak fix --profile {self.profile_root} --only eng")

    def test_nothing_to_repair_needs_no_root_even_where_root_is_impossible(self) -> None:
        self.the_host = host(no_new_privs=True)

        code, out = self.run_fix()

        self.assertEqual(code, ExitCode.OK)
        self.assertIn("0 changed", out)
        self.assertEqual((self.fake_process.queries, self.fake_process.executed), ([], []))


class RerunCommandTests(unittest.TestCase):
    def test_the_command_is_the_subcommand_plus_the_global_flags_given(self) -> None:
        self.assertEqual(rerun_command(argparse.Namespace(), "fix"), "ubuntu-tweak fix")
        args = argparse.Namespace(profile="/my profile", machine=None, only="b,a", dotfiles_repo="me/.files")

        command = rerun_command(args, "apply --commit")

        self.assertEqual(
            command, "ubuntu-tweak apply --commit --profile '/my profile' --only b,a --dotfiles-repo me/.files"
        )
        parsed = build_parser().parse_args(shlex.split(command)[1:])  # what we print must be accepted
        self.assertEqual((parsed.profile, parsed.only, parsed.dotfiles_repo), ("/my profile", "b,a", "me/.files"))
        self.assertTrue(parsed.commit)


class RegistrationTests(unittest.TestCase):
    def test_fix_is_a_command_with_yes(self) -> None:
        parsed = build_parser().parse_args(["fix", "--yes"])

        self.assertTrue(parsed.yes)
        self.assertIs(parsed.handler, fix.run)
        self.assertFalse(build_parser().parse_args(["fix"]).yes)


if __name__ == "__main__":
    unittest.main()

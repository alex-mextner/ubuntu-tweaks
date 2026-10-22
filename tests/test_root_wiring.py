"""apply / init / doctor / update ask for root once, up front, and only when they are going to use it."""

import argparse
import contextlib
import io
from collections.abc import Callable
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.util import RootStep, Said, host

try:
    from tests.test_apply_commands import MY_PLANNERS, CloneProfile, CommandScenario
    from ubuntu_tweaks import planners, reconcile
    from ubuntu_tweaks.actions import Action
    from ubuntu_tweaks.commands import apply, doctor, init, update
    from ubuntu_tweaks.context import Context
    from ubuntu_tweaks.elevation import (
        EARLIER_STEPS_RAN,
        SUDO_ASK,
        SUDO_CHECK,
        Cause,
        Host,
        RootSession,
        RootUnavailable,
    )
    from ubuntu_tweaks.identity import Location
    from ubuntu_tweaks.process import CommandResult, FakeProcess
    from ubuntu_tweaks.profile import Profile
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

PASSWORD_NEEDED = CommandResult(1, "", "sudo: a password is required")


class RootUntilCloned(RootStep):
    """A root action the not-yet-cloned profile still plans and the cloned one switches off."""

    def __init__(self, events: list[str], profile: Path) -> None:
        super().__init__(events)
        self._profile = profile

    def check(self) -> bool:
        return self._profile.exists()


class CloneAndNote(CloneProfile):
    """What the dotfiles module does, noting in ``events`` when it ran."""

    def __init__(self, root: Path, events: list[str]) -> None:
        super().__init__(root)
        self._events = events

    def run(self) -> None:
        self._events.append("clone")
        super().run()


class WiringScenario(CommandScenario):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[str] = []
        self.fake_process = FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}, effects=self.on_execute)
        self.the_host = host(terminal=True)
        self.said = Said()
        self.wanted: list[Action] = []
        planner = types.SimpleNamespace(build=lambda ctx, backend: self.wanted)
        patcher = mock.patch.object(planners, "discover_planners", return_value=[*MY_PLANNERS, planner])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_apply(commit=True, no_sudo=True)  # converge the scenario's own extension module first

    def on_execute(self, argv: tuple[str, ...], input_text: str | None) -> None:
        if argv == SUDO_ASK:
            self.events.append("sudo")

    def root(self) -> RootSession:
        session = RootSession(
            command="ubuntu-tweak apply --commit",
            skip="ubuntu-tweak apply --commit --no-sudo",
            process=self.fake_process,
            host=self.the_host,
            say=self.said,
        )
        self.addCleanup(session.close)  # a kept-alive ticket runs a thread
        return session

    def commit(self, *, no_sudo: bool = False) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            reconcile.apply(
                self.context(), commit=True, no_sudo=no_sudo, backend_factory=self.factory, root=self.root()
            )
        return out.getvalue()


class ApplyRootTests(WiringScenario):
    def test_the_password_is_asked_before_the_first_root_action_and_once(self) -> None:
        self.wanted = [RootStep(self.events, "a"), RootStep(self.events, "b")]

        out = self.commit()

        self.assertEqual(self.events, ["sudo", "run:a", "run:b"])
        self.assertIn("2 changed", out)
        self.assertEqual(len(self.said.lines), 1)
        self.assertIn("2 root action(s)", self.said.lines[0])
        self.assertIn("system", self.said.lines[0])

    def test_a_preview_never_asks(self) -> None:
        self.wanted = [RootStep(self.events)]

        code, out = self.run_apply(commit=False)

        self.assertEqual(code, 0)
        self.assertIn("[sudo]", out)
        self.assertEqual((self.fake_process.queries, self.fake_process.executed, self.events), ([], [], []))

    def test_no_sudo_never_asks_and_never_runs_root_actions(self) -> None:
        self.wanted = [RootStep(self.events)]

        out = self.commit(no_sudo=True)

        self.assertEqual((self.fake_process.queries, self.fake_process.executed, self.events), ([], [], []))
        self.assertIn("Skipped 1 root action(s) because of --no-sudo", out)

    def test_a_plan_without_root_actions_never_asks_even_where_root_is_impossible(self) -> None:
        self.the_host = host(no_new_privs=True)

        out = self.commit()

        self.assertIn("0 changed", out)
        self.assertEqual((self.fake_process.queries, self.fake_process.executed), ([], []))

    def test_passwordless_sudo_runs_without_a_prompt(self) -> None:
        self.fake_process = FakeProcess({SUDO_CHECK: CommandResult(0)}, effects=self.on_execute)
        self.wanted = [RootStep(self.events)]

        self.commit()

        self.assertEqual(self.events, ["run:timezone"])
        self.assertEqual((self.fake_process.executed, self.said.lines), ([], []))

    def test_root_that_cannot_be_had_stops_before_any_action_with_a_typed_error(self) -> None:
        self.the_host = host(no_new_privs=True)
        self.wanted = [RootStep(self.events)]

        with self.assertRaises(RootUnavailable) as caught:
            self.commit()

        error = caught.exception
        self.assertEqual((error.cause, error.fix), (Cause.NO_NEW_PRIVS, "ubuntu-tweak apply --commit"))
        self.assertIn("Nothing was changed.", str(error))
        self.assertEqual(self.events, [])

    def commit_cloning_a_profile(self, late: Callable[[Path], Action] | None = None) -> None:
        profile = self.tmp / "later-profile"
        # by default a step that is only planned once the profile exists
        step = late(profile) if late else RootStep(self.events, ready=profile)
        with contextlib.redirect_stdout(io.StringIO()):
            reconcile.apply(
                Context(self.repo, Profile(profile, "home")),
                commit=True,
                no_sudo=False,
                backend_factory=self.factory,
                extra_actions=(CloneAndNote(profile, self.events), step),
                relocate=lambda: Location(profile),
                root=self.root(),
            )

    def test_a_rebuilt_plan_that_gains_root_actions_asks_then_not_at_the_start(self) -> None:
        self.commit_cloning_a_profile()

        self.assertEqual(self.events, ["clone", "sudo", "run:timezone"])

    def test_root_missing_after_earlier_steps_ran_does_not_claim_nothing_changed(self) -> None:
        self.the_host = host(no_new_privs=True)

        with self.assertRaises(RootUnavailable) as caught:
            self.commit_cloning_a_profile()

        self.assertEqual(self.events, ["clone"])
        self.assertIn(EARLIER_STEPS_RAN, str(caught.exception))
        self.assertNotIn("Nothing was changed", str(caught.exception))

    def test_a_root_action_the_cloned_profile_switches_off_never_asks(self) -> None:
        self.the_host = host(no_new_privs=True)  # asking would fail: it must not happen

        self.commit_cloning_a_profile(late=lambda profile: RootUntilCloned(self.events, profile))

        self.assertEqual((self.events, self.fake_process.queries, self.fake_process.executed), (["clone"], [], []))


class CommandRootTests(WiringScenario):
    """The commands build their own session: the command lines it names are the ones the user ran."""

    init_needed: list[doctor.Requirement]

    def setUp(self) -> None:
        super().setUp()
        self.init_needed = []

    @staticmethod
    def no_new_privs() -> contextlib.AbstractContextManager[object]:
        return mock.patch.object(Host, "detect", return_value=host(no_new_privs=True))

    def test_apply_names_apply_commit_keeps_the_flags_and_offers_no_sudo(self) -> None:
        self.wanted = [RootStep(self.events, scope="eng")]
        args = argparse.Namespace(commit=True, no_sudo=False, only="eng")
        with (
            self.no_new_privs(),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(RootUnavailable) as caught,
        ):
            apply.run(self.context(frozenset({"eng"})), args)

        self.assertEqual(caught.exception.fix, "ubuntu-tweak apply --commit --only eng")
        self.assertIn(
            "Or skip the root actions (they are listed): ubuntu-tweak apply --commit --no-sudo --only eng",
            str(caught.exception),
        )
        self.assertEqual(self.events, [])

    def test_apply_preview_does_not_look_at_sudo(self) -> None:
        self.wanted = [RootStep(self.events)]
        with (
            mock.patch.object(Host, "detect", side_effect=AssertionError("a preview must not look at sudo")),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = apply.run(self.context(), argparse.Namespace(commit=False, no_sudo=False))

        self.assertEqual(code, 0)

    def test_doctor_yes_asks_for_root_before_the_install(self) -> None:
        needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: "install" in self.events)]
        with (
            mock.patch.object(doctor, "requirements", return_value=needed),
            mock.patch.object(doctor, "install", side_effect=lambda absent: self.events.append("install")),
            mock.patch.object(doctor, "root_session", return_value=self.root()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = doctor.run(self.context(), argparse.Namespace(yes=True))

        self.assertEqual((code, self.events), (0, ["sudo", "install"]))
        self.assertIn("apt-get install tmux", self.said.lines[0])

    def test_doctor_without_yes_never_asks(self) -> None:
        needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: False)]
        with (
            mock.patch.object(doctor, "requirements", return_value=needed),
            mock.patch.object(doctor, "root_session", side_effect=AssertionError("no session without --yes")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = doctor.run(self.context(), argparse.Namespace(yes=False))

        self.assertEqual(code, 3)

    def test_doctor_yes_where_root_is_impossible_names_the_command_and_has_no_skip(self) -> None:
        needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: False)]
        with (
            self.no_new_privs(),
            mock.patch.object(doctor, "requirements", return_value=needed),
            mock.patch.object(doctor, "install", side_effect=AssertionError("must not install")),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(RootUnavailable) as caught,
        ):
            doctor.run(self.context(), argparse.Namespace(yes=True))

        self.assertEqual(caught.exception.fix, "ubuntu-tweak doctor --yes")
        self.assertNotIn("skip", str(caught.exception))  # skipping the root step of doctor means no install

    def run_init(self, **flags: bool) -> str:
        real_apply = reconcile.apply
        args = argparse.Namespace(**{"commit": True, "yes": True, "no_sudo": False, **flags})
        out = io.StringIO()
        with (
            mock.patch.object(doctor, "requirements", return_value=self.init_needed),
            mock.patch.object(doctor, "install", side_effect=lambda absent: self.events.append("install")),
            mock.patch.object(
                reconcile,
                "apply",
                side_effect=lambda ctx, **kw: real_apply(ctx, **{**kw, "backend_factory": self.factory}),
            ),
            contextlib.redirect_stdout(out),
        ):
            init.run(self.context(), args)
        return out.getvalue()

    def test_init_uses_one_session_for_the_apt_install_and_the_plan(self) -> None:
        self.init_needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: "install" in self.events)]
        self.wanted = [RootStep(self.events)]
        with mock.patch.object(init, "root_session", return_value=self.root()):
            self.run_init()

        self.assertEqual(self.events, ["sudo", "install", "run:timezone"])  # one prompt for both root steps
        self.assertEqual(self.fake_process.executed, [(SUDO_ASK, None)])

    def test_init_where_root_is_impossible_names_init_commit(self) -> None:
        self.wanted = [RootStep(self.events)]
        with self.no_new_privs(), self.assertRaises(RootUnavailable) as caught:
            self.run_init()

        self.assertEqual(caught.exception.fix, "ubuntu-tweak init --commit")
        self.assertIn("ubuntu-tweak init --commit --no-sudo", str(caught.exception))
        self.assertEqual(self.events, [])

    def test_init_no_sudo_and_preview_never_ask(self) -> None:
        self.wanted = [RootStep(self.events)]
        with mock.patch.object(Host, "detect", side_effect=AssertionError("must not look at sudo")):
            self.run_init(no_sudo=True)
            self.run_init(commit=False)

        self.assertEqual(self.events, [])


class UpdateRootTests(WiringScenario):
    def test_update_hands_the_commit_to_an_apply_child_which_asks_for_itself(self) -> None:
        (self.repo / ".git").mkdir(exist_ok=True)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(Host, "detect", side_effect=AssertionError("the parent must not look at sudo")),
            mock.patch.object(update, "run_command", return_value=""),
            mock.patch.object(update, "_code_state", return_value="same"),
            mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=6)) as spawn,
        ):
            code = update.run(self.context(), argparse.Namespace(commit=True, no_sudo=False))

        (command,) = spawn.call_args.args
        self.assertEqual((command[2:4], "--no-sudo" in command), (["apply", "--commit"], False))
        self.assertEqual(code, 6)  # the child's exit 6 (root not obtainable) is update's own


if __name__ == "__main__":
    unittest.main()

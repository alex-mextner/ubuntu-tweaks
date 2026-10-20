"""apply / status / init / doctor / update on top of a fake backend and a throwaway HOME."""

import argparse
import contextlib
import io
import types
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from tests.util import write

try:
    from tests.actions_scenario import Scenario
    from ubuntu_tweaks import planners, reconcile
    from ubuntu_tweaks.actions import Action, ActionFailed, Phase
    from ubuntu_tweaks.commands import _support, apply, doctor, init, status, update
    from ubuntu_tweaks import process
    from ubuntu_tweaks.process import CommandError
    from ubuntu_tweaks.context import Context
    from ubuntu_tweaks.errors import ExitCode, UnknownModuleError
    from ubuntu_tweaks.extension import RELOGIN_HINT
    from ubuntu_tweaks.planners import extension_links, schemas, submodule, toggle
    from ubuntu_tweaks.profile import Profile
    from ubuntu_tweaks.settings import FakeBackend
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

EXTENSION_MODULE = '[extension]\nconflicts = ["dock@x"]\n'
MY_PLANNERS = [submodule, schemas, extension_links, toggle]


class RecordingAction(Action):
    """A settings-phase action that notes what the world looked like when it ran."""

    kind = "set-key"
    phase = Phase.SETTINGS_MODULE

    def __init__(self, observe: Callable[[], object], log: list[object]) -> None:
        self._observe = observe
        self._log = log
        self._done = False

    def describe(self) -> str:
        return "record"

    def check(self) -> bool:
        return self._done

    def run(self) -> None:
        self._log.append(self._observe())
        self._done = True


class RootAction(Action):
    kind = "system"
    phase = Phase.SYSTEM
    needs_root = True

    def describe(self) -> str:
        return "set timezone"

    def check(self) -> bool:
        return False

    def run(self) -> None:
        raise AssertionError("must not run without sudo")


class CommandScenario(Scenario):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(planners, "discover_planners", return_value=MY_PLANNERS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.add_extension("gnome-extensions/eng@ultra", "eng@ultra")
        self.add_module("eng", path="gnome-extensions/eng@ultra", extension=EXTENSION_MODULE)
        self.link = self.extensions_dir / "eng@ultra"
        self.fake: FakeBackend = self.backend()
        self.factory_calls = 0

    def factory(self, ctx: Context) -> FakeBackend:
        self.factory_calls += 1
        return self.fake

    def run_apply(self, *, commit: bool, no_sudo: bool = False, extra: tuple[Action, ...] = ()) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reconcile.apply(
                self.context(), commit=commit, no_sudo=no_sudo, extra_actions=extra, backend_factory=self.factory
            )
        return code, out.getvalue()

    def run_status(self, ctx: Context | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
        ):
            code = status.run(ctx or self.context(), argparse.Namespace())
        return code, out.getvalue()


class ApplyTests(CommandScenario):
    def test_preview_prints_the_plan_and_changes_nothing(self) -> None:
        code, out = self.run_apply(commit=False)
        self.assertEqual(code, 0)
        self.assertIn("Plan: 3 action(s) to run", out)
        self.assertIn("[link]", out)
        self.assertIn("[enable] enable eng@ultra", out)
        self.assertIn("Preview only, nothing was changed", out)
        self.assertFalse(self.link.exists())
        self.assertEqual(self.fake.writes, [])

    def test_preview_header_names_machine_and_missing_profile(self) -> None:
        _, out = self.run_apply(commit=False)
        self.assertIn("machine: home (no overlay: shared profile only)", out)
        write(self.profile_root / "machines/home/machine.toml", "")
        _, out = self.run_apply(commit=False)
        self.assertIn("machine: home (overlay ", out)

    def test_commit_reports_changed_and_the_relogin_hint_and_converges(self) -> None:
        code, out = self.run_apply(commit=True)
        self.assertEqual(code, 0)
        self.assertIn("3 changed", out)
        self.assertIn(RELOGIN_HINT, out)
        self.assertTrue(self.link.is_symlink())
        code, out = self.run_apply(commit=True)
        self.assertIn("0 changed", out)
        self.assertNotIn(RELOGIN_HINT, out)
        self.assertIn("Nothing to do", out)

    def test_commit_without_code_changes_prints_no_relogin_hint(self) -> None:
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.source)
        _, out = self.run_apply(commit=True)
        self.assertIn("2 changed", out)
        self.assertNotIn(RELOGIN_HINT, out)

    def test_settings_phase_runs_after_a_fresh_backend_when_code_was_installed(self) -> None:
        log: list[object] = []
        recorder = RecordingAction(lambda: (self.link.is_symlink(), self.factory_calls), log)
        self.run_apply(commit=True, extra=(recorder,))
        self.assertEqual(log, [(True, 2)])  # the link existed and the backend had been re-opened

    def test_preview_notes_staged_settings(self) -> None:
        _, out = self.run_apply(commit=False)
        self.assertIn("planned after those actions ran", out)

    def test_no_sudo_skips_root_actions_and_says_so(self) -> None:
        code, out = self.run_apply(commit=True, no_sudo=True, extra=(RootAction(),))
        self.assertEqual(code, 0)
        self.assertIn("[sudo, skipped: --no-sudo]", out)
        self.assertIn("Skipped 1 root action(s) because of --no-sudo", out)

    def test_commands_that_need_pygobject_say_so_instead_of_a_traceback(self) -> None:
        with mock.patch.object(_support, "has_gi", return_value=False):
            for command in (apply, status, update):
                with self.subTest(command=command.__name__), self.assertRaisesRegex(CommandError, "doctor --yes"):
                    command.run(self.context(), argparse.Namespace(commit=False, no_sudo=False))

    def test_unknown_only_name_is_exit_4(self) -> None:
        ctx = self.context(frozenset({"nope"}))
        with self.assertRaises(UnknownModuleError):
            reconcile.build_plan(ctx, self.fake)

    def test_only_limits_the_plan_to_the_module(self) -> None:
        self.add_extension("gnome-extensions/other", "other@x")
        self.add_module("other", path="gnome-extensions/other")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            reconcile.apply(
                self.context(frozenset({"other"})), commit=False, no_sudo=False, backend_factory=self.factory
            )
        self.assertIn("other@x", out.getvalue())
        self.assertNotIn("eng@ultra", out.getvalue())

    def test_a_plan_that_does_not_converge_fails(self) -> None:
        class Stubborn(RecordingAction):
            def check(self) -> bool:
                return False

        with self.assertRaises(ActionFailed):
            self.run_apply(commit=True, extra=(Stubborn(lambda: None, []),))

    def test_planner_warnings_are_printed_under_the_plan(self) -> None:
        warn = types.SimpleNamespace(
            build=lambda ctx, backend: [], warnings=lambda ctx, backend: ["skipped x: no schema"]
        )
        with mock.patch.object(planners, "discover_planners", return_value=[*MY_PLANNERS, warn]):
            _, out = self.run_apply(commit=False)
        self.assertIn("Warnings:\n  skipped x: no schema", out)


class StatusTests(CommandScenario):
    def test_missing_items_exit_3(self) -> None:
        code, out = self.run_status()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("Missing (3):", out)
        self.assertIn("Modified (0):", out)

    def test_clean_system_exits_0_and_extras_alone_do_not_fail(self) -> None:
        self.run_apply(commit=True)
        self.fake.set_user("org.gnome.shell", None, "enabled-extensions", "['eng@ultra', 'gsconnect@x']")
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("Missing (0):", out)
        self.assertIn("Extra (1):\n  extension gsconnect@x is enabled but no module declares it", out)

    def test_replaced_link_is_modified_not_missing(self) -> None:
        self.run_apply(commit=True)
        self.link.unlink()
        write(self.link / "metadata.json", "{}")
        code, out = self.run_status()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("Modified (1):\n  [link-replace]", out)
        self.assertIn("Missing (0):", out)

    def test_status_ignores_a_disabled_local_module_with_missing_metadata(self) -> None:
        self.add_module("gone", path="gnome-extensions/gone", enabled=False)
        self.run_apply(commit=True)
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("Missing (0):", out)

    def test_only_hides_extras(self) -> None:
        self.run_apply(commit=True)
        self.fake.set_user("org.gnome.shell", None, "enabled-extensions", "['eng@ultra', 'gsconnect@x']")
        _, out = self.run_status(self.context(frozenset({"eng"})))
        self.assertIn("Extra (0):", out)

    def test_classify_maps_set_key_and_link_replace_to_modified(self) -> None:
        plan = reconcile.build_plan(self.context(), self.fake, [RecordingAction(lambda: None, [])])
        missing, modified = status.classify(plan)
        self.assertEqual([a.kind for a in modified], ["set-key"])
        self.assertEqual(sorted(a.kind for a in missing), ["disable", "enable", "link"])


class InitCommandTests(CommandScenario):
    def setUp(self) -> None:
        super().setUp()
        self.real_apply = reconcile.apply
        for patcher in (
            mock.patch.object(doctor, "requirements", return_value=[]),
            mock.patch.object(
                reconcile,
                "apply",
                side_effect=lambda ctx, **kwargs: self.real_apply(ctx, backend_factory=self.factory, **kwargs),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_init(self, ctx: Context | None = None, **flags: bool) -> tuple[int, str]:
        args = argparse.Namespace(**{"commit": False, "yes": False, "no_sudo": False, **flags})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = init.run(ctx or self.context(), args)
        return code, out.getvalue()

    def test_preview_plans_the_cli_link_and_changes_nothing(self) -> None:
        code, out = self.run_init()
        self.assertEqual(code, 0)
        self.assertIn("[cli-link] link ~/.local/bin/ubuntu-tweak -> ", out)
        self.assertFalse((self.home / ".local/bin/ubuntu-tweak").exists())

    def test_commit_links_the_cli_and_is_rerunnable(self) -> None:
        code, out = self.run_init(commit=True)
        self.assertEqual(code, 0)
        self.assertEqual((self.home / ".local/bin/ubuntu-tweak").readlink(), self.repo / "bin/ubuntu-tweak")
        self.assertIn("4 changed", out)
        _, out = self.run_init(commit=True)
        self.assertIn("0 changed", out)

    def test_missing_default_profile_prints_the_exact_clone_command_and_continues(self) -> None:
        ctx = Context(self.repo, Profile(self.home / ".files/ubuntu", "home"))
        code, out = self.run_init(ctx)
        self.assertEqual(code, 0)
        self.assertIn("gh repo clone alex-mextner/.files ~/.files", out)
        self.assertIn("Plan: ", out)

    def test_commit_with_missing_packages_stops_without_yes(self) -> None:
        absent = doctor.Requirement("tmux", "tmux", "module x", lambda: False)
        with (
            mock.patch.object(doctor, "requirements", return_value=[absent]),
            mock.patch.object(doctor, "install") as install,
            mock.patch.object(init.sys.stdin, "isatty", return_value=False),
        ):
            code, out = self.run_init(commit=True)
        self.assertEqual(code, ExitCode.DRIFT)
        install.assert_not_called()
        self.assertIn("ubuntu-tweak doctor --yes", out)

    def test_cli_link_alone_is_not_extension_code(self) -> None:
        self.run_init(commit=True)
        (self.home / ".local/bin/ubuntu-tweak").unlink()
        before = self.factory_calls
        code, out = self.run_init(commit=True)
        self.assertEqual(code, 0)
        self.assertIn("1 changed", out)
        self.assertNotIn(RELOGIN_HINT, out)
        self.assertNotIn("planned after those actions ran", out)
        self.assertEqual(self.factory_calls - before, 1)  # no staged second pass

    def test_commit_no_sudo_does_not_install_and_goes_on(self) -> None:
        absent = doctor.Requirement("tmux", "tmux", "module x", lambda: False)
        with (
            mock.patch.object(doctor, "requirements", return_value=[absent]),
            mock.patch.object(doctor, "install") as install,
        ):
            code, out = self.run_init(commit=True, no_sudo=True, yes=True)
        self.assertEqual(code, 0)
        install.assert_not_called()
        self.assertIn("Not installing tmux (--no-sudo)", out)
        self.assertTrue((self.home / ".local/bin/ubuntu-tweak").is_symlink())

    def test_commit_yes_installs_then_continues(self) -> None:
        state = {"present": False}
        needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: state["present"])]

        def fake_install(absent: list[object]) -> None:
            state["present"] = True

        with (
            mock.patch.object(doctor, "requirements", return_value=needed),
            mock.patch.object(doctor, "install", side_effect=fake_install) as install,
        ):
            code, out = self.run_init(commit=True, yes=True)
        self.assertEqual(code, 0)
        install.assert_called_once()
        self.assertIn("changed", out)

    def test_preview_with_missing_packages_does_not_install(self) -> None:
        absent = doctor.Requirement("tmux", "tmux", "module x", lambda: False)
        with (
            mock.patch.object(doctor, "requirements", return_value=[absent]),
            mock.patch.object(doctor, "install") as install,
        ):
            code, out = self.run_init()
        self.assertEqual(code, 0)
        install.assert_not_called()
        self.assertIn("init --commit would install: sudo apt-get install -y tmux", out)

    def test_missing_gi_skips_the_apply_preview(self) -> None:
        gi = doctor.Requirement("python3-gi (PyGObject)", "python3-gi", "the CLI", lambda: False)
        with mock.patch.object(doctor, "requirements", return_value=[gi]):
            code, out = self.run_init()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("apply preview skipped", out)


class DoctorTests(CommandScenario):
    def run_doctor(self, needed: list[object], *, yes: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(doctor, "requirements", return_value=needed),
        ):
            code = doctor.run(self.context(), argparse.Namespace(yes=yes))
        return code, out.getvalue()

    def test_everything_present_exits_0(self) -> None:
        code, out = self.run_doctor([doctor.Requirement("git", "git", "x", lambda: True)])
        self.assertEqual(code, 0)
        self.assertIn("ok      git", out)

    def test_missing_without_yes_exits_3_and_names_the_install_command(self) -> None:
        with mock.patch.object(doctor, "install") as install:
            code, out = self.run_doctor([doctor.Requirement("tmux", "tmux", "module x", lambda: False)])
        self.assertEqual(code, ExitCode.DRIFT)
        install.assert_not_called()
        self.assertIn("MISSING tmux", out)
        self.assertIn("sudo apt-get install -y tmux", out)

    def test_yes_installs_and_rechecks(self) -> None:
        state = {"present": False}
        needed = [doctor.Requirement("tmux", "tmux", "module x", lambda: state["present"])]
        with mock.patch.object(doctor, "install", side_effect=lambda absent: state.update(present=True)) as install:
            code, _ = self.run_doctor(needed, yes=True)
        self.assertEqual(code, 0)
        install.assert_called_once()

    def test_requirements_cover_the_base_tools_and_enabled_module_deps(self) -> None:
        write(self.repo / "modules/eng/module.toml", self.module_with_deps(enabled=True))
        self.add_extension("gnome-extensions/off", "off@x")
        self.add_module("off", path="gnome-extensions/off", enabled=False)
        write(self.repo / "modules/off/module.toml", self.module_with_deps(enabled=False, apt="never-needed"))
        labels = {item.label for item in doctor.requirements(self.context())}
        self.assertTrue({"python3-gi (PyGObject)", "dconf", "git", "glib-compile-schemas", "tmux"} <= labels)
        self.assertNotIn("never-needed", labels)

    @staticmethod
    def module_with_deps(*, enabled: bool, apt: str = "tmux") -> str:
        return (
            f"enabled = {str(enabled).lower()}\n"
            '[source]\nkind = "local"\npath = "gnome-extensions/eng@ultra"\n'
            f'[deps]\napt = ["{apt}"]\n'
        )

    def test_install_uses_sudo_unless_root(self) -> None:
        absent = [
            doctor.Requirement("tmux", "tmux", "x", lambda: False),
            doctor.Requirement("git", "git", "x", lambda: False),
        ]
        with (
            mock.patch.object(doctor, "run_command") as run_command,
            mock.patch.object(process.os, "geteuid", return_value=1000),
            mock.patch.object(process.shutil, "which", return_value="/usr/bin/sudo"),
        ):
            doctor.install(absent)
        run_command.assert_called_once_with(["sudo", "--", "apt-get", "install", "-y", "git", "tmux"])
        with (
            mock.patch.object(doctor, "run_command") as run_command,
            mock.patch.object(process.os, "geteuid", return_value=0),
        ):
            doctor.install(absent)
        run_command.assert_called_once_with(["apt-get", "install", "-y", "git", "tmux"])


class UpdateTests(CommandScenario):
    def run_update(self, **flags: bool) -> tuple[int, str]:
        args = argparse.Namespace(**{"commit": False, "no_sudo": False, **flags})
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(
                reconcile,
                "apply",
                side_effect=lambda ctx, **kwargs: self.real_apply(ctx, backend_factory=self.factory, **kwargs),
            ),
        ):
            code = update.run(self.context(), args)
        return code, out.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self.real_apply = reconcile.apply

    def test_preview_prints_the_git_commands_and_the_current_plan(self) -> None:
        with mock.patch.object(update, "run_command") as run_command:
            code, out = self.run_update()
        self.assertEqual(code, 0)
        run_command.assert_not_called()
        self.assertIn("git pull --ff-only", out)
        self.assertIn("git submodule update --init --recursive", out)
        self.assertIn("Plan: ", out)

    def test_commit_pulls_updates_submodules_then_applies_in_a_fresh_interpreter(self) -> None:
        states = iter(["old", "new"])
        calls: list[object] = []

        def fake_run(argv: tuple[str, ...] | list[str], **kwargs: object) -> str:
            calls.append((list(argv), kwargs.get("cwd")))
            return ""

        with (
            mock.patch.object(update, "run_command", side_effect=fake_run),
            mock.patch.object(update, "_code_state", side_effect=lambda ctx: next(states)),
            mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=0)) as spawn,
        ):
            code, out = self.run_update(commit=True, no_sudo=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [(["git", "pull", "--ff-only"], self.repo), (list(update.SUBMODULES), self.repo)],
        )
        (command,) = spawn.call_args.args
        self.assertEqual(command[1:3], [str(Path(self.repo) / "bin" / "ubuntu-tweak"), "apply"])
        self.assertIn("--commit", command)
        self.assertIn("--no-sudo", command)
        self.assertEqual(command[command.index("--machine") + 1], "home")
        self.assertEqual(command[command.index("--profile") + 1], str(self.profile_root))
        self.assertIn(RELOGIN_HINT, out)

    def test_commit_runs_git_even_when_this_interpreter_lacks_pygobject(self) -> None:
        # The child `apply` guards itself; the parent must not refuse (or pull) based on its own gi.
        with (
            mock.patch.object(_support, "has_gi", return_value=False),
            mock.patch.object(update, "run_command", return_value="") as run_command,
            mock.patch.object(update, "_code_state", return_value="same"),
            mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=0)) as spawn,
        ):
            code, _ = self.run_update(commit=True)
        self.assertEqual(code, 0)
        self.assertEqual(run_command.call_count, 2)
        spawn.assert_called_once()

    def test_commit_propagates_apply_failure_and_skips_hint_when_nothing_changed(self) -> None:
        with (
            mock.patch.object(update, "run_command", return_value=""),
            mock.patch.object(update, "_code_state", return_value="same"),
            mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=3)),
        ):
            code, out = self.run_update(commit=True)
        self.assertEqual(code, 3)
        self.assertNotIn(RELOGIN_HINT, out)


if __name__ == "__main__":
    unittest.main()

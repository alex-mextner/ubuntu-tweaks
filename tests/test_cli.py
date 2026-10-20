"""cli dispatch (self-registration, global flags, exit codes) and the list / machine commands."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks import commands
from ubuntu_tweaks.cli import build_parser, discover_commands, main
from ubuntu_tweaks.commands import list as list_command
from ubuntu_tweaks.commands import machine as machine_command
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.errors import ConfigError, ExitCode, RefusedError, UnknownModuleError
from ubuntu_tweaks.profile import Profile

LOCAL_MODULE = '[source]\nkind = "local"\npath = "gnome-extensions/x"\n'
SYSTEM_MODULE = 'enabled = false\n[source]\nkind = "system"\ninstall = "install.sh"\ncheck = "install.sh --check"\n'

DROPPED_COMMAND = """
def register(subparsers):
    parser = subparsers.add_parser("dropped", help="dropped in by a test")
    parser.set_defaults(handler=run)


def run(ctx, args):
    return 7
"""

ALIASED_COMMAND = """
def register(subparsers):
    parser = subparsers.add_parser("aliased", aliases=["aka"])
    parser.set_defaults(handler=lambda ctx, args: 7)
"""

FAILING_COMMANDS = """
from ubuntu_tweaks.errors import RefusedError


def register(subparsers):
    subparsers.add_parser("refuse").set_defaults(handler=refuse)
    subparsers.add_parser("crash").set_defaults(handler=crash)


def refuse(ctx, args):
    raise RefusedError("secret finding")


def crash(ctx, args):
    raise RuntimeError("bug")
"""


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ExitCodeTests(unittest.TestCase):
    def test_exit_code_values_are_stable(self) -> None:
        self.assertEqual(
            [(code.name, int(code)) for code in ExitCode],
            [("OK", 0), ("INTERNAL", 1), ("INVALID_CONFIG", 2), ("DRIFT", 3), ("UNKNOWN_MODULE", 4), ("REFUSED", 5)],
        )
        self.assertEqual(ConfigError.exit_code, ExitCode.INVALID_CONFIG)
        self.assertEqual(UnknownModuleError.exit_code, ExitCode.UNKNOWN_MODULE)
        self.assertEqual(RefusedError.exit_code, ExitCode.REFUSED)


class DispatchTests(TempDirCase):
    def drop_commands(self, **files: str) -> None:
        directory = self.tmp / "commands_drop"
        for name, source in files.items():
            write(directory / f"{name}.py", source)
        commands.__path__.append(str(directory))
        self.addCleanup(commands.__path__.remove, str(directory))
        for name in files:
            self.addCleanup(sys.modules.pop, f"ubuntu_tweaks.commands.{name}", None)

    def test_a_file_dropped_into_commands_registers_without_editing_cli(self) -> None:
        self.drop_commands(dropped=DROPPED_COMMAND)
        self.assertEqual(run_main("dropped")[0], 7)

    def test_global_flags_work_before_and_after_the_command(self) -> None:
        profile = self.tmp / "p"
        profile.mkdir()
        before = run_main("--profile", str(profile), "--machine", "m1", "machine")
        after = run_main("machine", "--profile", str(profile), "--machine", "m1")
        self.assertEqual(before[0], 0)
        self.assertEqual(before, after)
        self.assertIn("machine:  m1 (from --machine)", before[1])

    def test_only_is_a_global_flag_of_every_command(self) -> None:
        names = [module.__name__.rsplit(".", 1)[1] for module in discover_commands()]
        self.assertGreaterEqual(len(names), 2)
        for name in names:
            with self.subTest(command=name):
                self.assertEqual(build_parser().parse_args([name, "--only", "a,b"]).only, "a,b")

    def test_expected_errors_map_to_their_exit_codes(self) -> None:
        self.drop_commands(failing=FAILING_COMMANDS)
        code, _, err = run_main("refuse")
        self.assertEqual((code, err), (5, "ubuntu-tweak: secret finding\n"))

    def test_unexpected_errors_are_internal_and_show_a_traceback(self) -> None:
        self.drop_commands(failing=FAILING_COMMANDS)
        code, _, err = run_main("crash")
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: bug", err)

    def test_a_command_module_without_register_is_invalid_config(self) -> None:
        self.drop_commands(broken="x = 1\n")
        code, _, err = run_main("list")
        self.assertEqual(code, 2)
        self.assertIn("has no register(subparsers)", err)

    def test_a_command_with_aliases_still_gets_the_global_flags(self) -> None:
        self.drop_commands(aliased=ALIASED_COMMAND)
        self.assertEqual(run_main("aka", "--machine", "m1")[0], 7)

    def test_explicit_missing_profile_is_invalid_config(self) -> None:
        code, _, err = run_main("--profile", str(self.tmp / "absent"), "list")
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)


class ListCommandTests(TempDirCase):
    def context(self, only: frozenset[str] = frozenset()) -> Context:
        repo = self.tmp / "repo"
        write(repo / "modules/english-in-overview/module.toml", LOCAL_MODULE)
        write(repo / "modules/memguard/module.toml", SYSTEM_MODULE)
        write(repo / "modules/legacy/readme.txt")
        profile = Profile(self.tmp / "profile", "home")
        profile.root.mkdir(exist_ok=True)
        return Context(repo, profile, only)

    def test_table_has_kind_enabled_and_source(self) -> None:
        self.assertEqual(
            list_command.render(self.context()),
            "\n".join(
                [
                    "MODULE               KIND    ENABLED  SOURCE",
                    "english-in-overview  local   yes      gnome-extensions/x",
                    "memguard             system  no       modules/memguard/install.sh (root)",
                ]
            ),
        )

    def test_enabled_reflects_the_machine_overlay(self) -> None:
        ctx = self.context()
        write(ctx.profile.machine_toml, "[modules.memguard]\nenabled = true\n")
        self.assertIn("memguard             system  yes", list_command.render(ctx))

    def test_only_limits_rows_and_rejects_unknown_names(self) -> None:
        self.assertNotIn("memguard", list_command.render(self.context(frozenset({"english-in-overview"}))))
        with self.assertRaisesRegex(UnknownModuleError, "unknown module.*nope"):
            list_command.render(self.context(frozenset({"nope"})))

    def test_no_modules(self) -> None:
        ctx = Context(self.tmp, Profile(self.tmp / "p", "home"))
        self.assertEqual(list_command.render(ctx), "No modules.")


class MachineCommandTests(TempDirCase):
    def test_report_shows_origin_of_modules_panels_and_files(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "modules/memguard/module.toml", SYSTEM_MODULE)
        profile = Profile(self.tmp / "profile", "home", "hostname")
        write(profile.profile_toml, '[modules.hyperdock.settings]\nicon-size = "0.2"\n')
        write(profile.machine_toml, "[modules.memguard]\nenabled = true\n")
        write(profile.settings_dir / "appearance.ini")
        write(profile.settings_dir / "power.ini")
        write(profile.overlay_settings_dir / "power.ini")
        write(profile.files_dir / ".config/a.conf")
        write(profile.files_dir / ".config/b.conf")
        write(profile.overlay_files_dir / ".config/b.conf")
        write(profile.packages_toml)
        write(profile.overlay_packages_toml)

        report = machine_command.render(Context(repo, profile))

        for line in (
            "machine:  home (from /etc/hostname)",
            f"overlay:  {profile.overlay_dir} (present)",
            "  memguard: enabled (overlay)",
            "  hyperdock: enabled (module default) (no such module)",
            '    settings.icon-size = "0.2" (shared)',
            "  appearance: shared",
            "  power: overlay over shared (key-level)",
            "  ~/.config/a.conf: shared",
            "  ~/.config/b.conf: overlay (shadows shared)",
            "packages.toml: shared + overlay (lists unioned)",
            "system.toml:   none",
        ):
            self.assertIn(line, report)

    def test_only_rejects_unknown_modules_like_list_does(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "modules/memguard/module.toml", SYSTEM_MODULE)
        ctx = Context(repo, Profile(self.tmp / "profile", "home"), frozenset({"memgaurd"}))
        with self.assertRaisesRegex(UnknownModuleError, "unknown module.*memgaurd"):
            machine_command.render(ctx)
        known = Context(repo, Profile(self.tmp / "profile", "home"), frozenset({"memguard"}))
        self.assertIn("  memguard: disabled (module default)", machine_command.render(known))

    def test_only_does_not_accept_a_module_that_only_the_profile_configures(self) -> None:
        repo = self.tmp / "repo"
        write(repo / "modules/memguard/module.toml", SYSTEM_MODULE)
        profile = Profile(self.tmp / "profile", "home")
        write(profile.profile_toml, "[modules.hyperdock]\nenabled = false\n")
        self.assertIn("  hyperdock: disabled (shared) (no such module)", machine_command.render(Context(repo, profile)))
        with self.assertRaisesRegex(UnknownModuleError, "unknown module.*hyperdock"):
            machine_command.render(Context(repo, profile, frozenset({"hyperdock"})))

    def test_non_ascii_settings_are_printed_readably(self) -> None:
        profile = Profile(self.tmp / "profile", "home")
        write(profile.profile_toml, '[modules.hyperdock.settings]\nlabel = "Обзор"\n')
        report = machine_command.render(Context(self.tmp / "repo", profile))
        self.assertIn('settings.label = "Обзор" (shared)', report)

    def test_absent_overlay_and_profile_are_said_plainly(self) -> None:
        profile = Profile(self.tmp / "missing", "home")
        report = machine_command.render(Context(self.tmp / "repo", profile))
        self.assertIn("profile:  " + str(profile.root) + " (missing: module defaults only)", report)
        self.assertIn("(absent: shared profile only)", report)
        self.assertIn("settings panels:\n  (none)", report)


class RepoSmokeTests(unittest.TestCase):
    def test_list_and_machine_run_against_this_repo_without_a_profile(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            profile = Profile.resolve(None, "smoke", env={}, home=Path(home))
            ctx = Context(REPO_ROOT, profile)
            self.assertIn("MODULE", list_command.render(ctx))
            self.assertIn("machine:  smoke", machine_command.render(ctx))

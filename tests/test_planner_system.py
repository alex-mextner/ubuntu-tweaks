"""The system.toml (root) and packages.toml planners, with an injected process and keyboard file."""

import json
import unittest
from collections.abc import Sequence
from pathlib import Path

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError, UbuntuTweakError
from ubuntu_tweaks.planners import packages as packages_planner
from ubuntu_tweaks.planners import system_root as system_planner
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend
from tests.util import TempDirCase, write

LOCALECTL = """   System Locale: LANG=en_GB.UTF-8
                  LC_TIME=de_DE.UTF-8
       VC Keymap: us
      X11 Layout: us
"""


class Machine(FakeProcess):
    """A tiny fake machine whose state the install commands change."""

    def __init__(self) -> None:
        super().__init__()
        self.timezone = "UTC"
        self.locale_status = LOCALECTL
        self.apt: set[str] = set()
        self.snaps: set[str] = set()
        self.flatpaks: set[str] = set()

    def query(self, argv: Sequence[str], *, timeout: float = 60.0) -> CommandResult:
        super().query(argv, timeout=timeout)
        match argv[0]:
            case "timedatectl":
                return CommandResult(0, self.timezone + "\n")
            case "localectl":
                return CommandResult(0, self.locale_status)
            case "dpkg-query":
                lines = "".join(f"{name}\tii \n" for name in argv[3:] if name in self.apt)
                return CommandResult(0 if lines else 1, lines)
            case "snap":
                return CommandResult(0, "Name Version Rev\n" + "".join(f"{n} 1 1\n" for n in sorted(self.snaps)))
            case "flatpak":
                return CommandResult(0, "".join(f"{n}\n" for n in sorted(self.flatpaks)))
        return CommandResult(127)

    def execute(self, argv: Sequence[str], *, input_text: str | None = None) -> None:
        super().execute(argv, input_text=input_text)
        words = list(argv)
        if words[:2] == ["sudo", "--"]:
            words = words[2:]
        match words[:2]:
            case ["apt-get", "install"]:
                self.apt |= {w for w in words[2:] if not w.startswith("-")}
            case ["snap", "install"]:
                self.snaps |= set(words[2:])
            case ["flatpak", "install"]:
                self.flatpaks |= set(words[words.index("flathub") + 1 :])
            case ["timedatectl", "set-timezone"]:
                self.timezone = words[2]
            case ["localectl", "set-locale"]:
                lines = [f"   System Locale: {words[2]}"] + [f"                  {word}" for word in words[3:]]
                self.locale_status = "\n".join([*lines, "       VC Keymap: us", ""])
            case ["install", "-m"]:
                Path(words[-1]).write_text(Path(words[-2]).read_text(encoding="utf-8"), encoding="utf-8")


class ProfileCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "profile"
        self.root.mkdir()
        (self.tmp / "repo").mkdir()
        self.ctx = Context(self.tmp / "repo", Profile(self.root, "home"))
        self.machine = Machine()


class SystemRootTests(ProfileCase):
    def build(self, keyboard: Path | None = None) -> list:
        return list(
            system_planner.build(
                self.ctx, FakeBackend(), process=self.machine, keyboard_file=keyboard or self.tmp / "keyboard"
            )
        )

    def test_without_system_toml_nothing_is_planned(self) -> None:
        self.assertEqual(self.build(), [])

    def test_timezone_and_locale_are_root_actions_that_converge(self) -> None:
        write(self.root / "system.toml", 'timezone = "Europe/Belgrade"\nlocale = "en_US.UTF-8"\n')
        plan = Plan.build(self.build())

        self.assertEqual([a.kind for a in plan.actions], ["system", "system"])
        self.assertTrue(all(a.needs_root for a in plan.actions))
        self.assertEqual(plan.actions[0].describe(), "timezone UTC -> Europe/Belgrade (timedatectl set-timezone)")
        self.assertEqual(
            plan.actions[1].describe(),
            "locale LANG=en_GB.UTF-8 -> LANG=en_US.UTF-8 (localectl set-locale), keeping LC_TIME=de_DE.UTF-8",
        )
        self.assertIn("[sudo]", plan.render())
        self.assertEqual(self.machine.executed, [])  # preview ran nothing

        plan.execute()
        self.assertEqual(
            [argv for argv, _ in self.machine.executed],
            [
                ("sudo", "--", "timedatectl", "set-timezone", "Europe/Belgrade"),
                ("sudo", "--", "localectl", "set-locale", "LANG=en_US.UTF-8", "LC_TIME=de_DE.UTF-8"),
            ],
        )
        self.assertEqual(len(Plan.build(self.build())), 0)
        self.assertIn("LC_TIME=de_DE.UTF-8", self.machine.locale_status)  # the other locale settings survive

    def test_locale_is_not_changed_when_the_current_one_cannot_be_read(self) -> None:
        write(self.root / "system.toml", 'locale = "en_US.UTF-8"\n')
        (action,) = self.build()
        self.machine.query = lambda argv, *, timeout=60.0: CommandResult(1, "", "dbus down")  # type: ignore[method-assign]

        with self.assertRaisesRegex(UbuntuTweakError, "not changing it"):
            action.run()
        self.assertEqual(self.machine.executed, [])

    def test_no_sudo_skips_root_actions_and_says_so(self) -> None:
        write(self.root / "system.toml", 'timezone = "Europe/Belgrade"\n')
        plan = Plan.build(self.build())

        self.assertIn("skipped: --no-sudo", plan.render(no_sudo=True))
        result = plan.execute(no_sudo=True)
        self.assertEqual(result.changed, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(self.machine.executed, [])

    def test_a_keyboard_value_that_is_unsafe_in_a_shell_file_is_invalid_config(self) -> None:
        for bad in ('us"; rm -rf /; "', "a$(b)", "a`b`", "a\\b", "a\nb"):
            with self.subTest(bad=bad):
                write(self.root / "system.toml", f"[keyboard]\nlayout = {json.dumps(bad)}\n")
                with self.assertRaisesRegex(ConfigError, "unsafe in a shell file"):
                    self.build()

    def test_an_unreadable_keyboard_file_is_an_error_not_an_empty_file(self) -> None:
        write(self.root / "system.toml", '[keyboard]\nlayout = "us"\n')
        directory = self.tmp / "kbd-is-a-directory"
        directory.mkdir()

        with self.assertRaisesRegex(UbuntuTweakError, "cannot read"):
            Plan.build(self.build(directory))

    def test_overlay_scalars_replace_shared(self) -> None:
        write(self.root / "system.toml", 'timezone = "Europe/Belgrade"\n')
        write(self.root / "machines/home/system.toml", 'timezone = "UTC"\n')

        plan = Plan.build(self.build())

        self.assertEqual((len(plan), plan.satisfied), (0, 1))  # the machine already is on the overlay's UTC

    def test_keyboard_is_compared_per_listed_field_and_other_lines_are_kept(self) -> None:
        keyboard = write(
            self.tmp / "keyboard",
            '# KEYBOARD CONFIGURATION FILE\nXKBMODEL="pc105"\nXKBLAYOUT="us"\nXKBOPTIONS=""\nBACKSPACE="guess"\n',
        )
        write(self.root / "system.toml", '[keyboard]\nlayout = "us,ru"\noptions = "grp_led:scroll"\n')
        plan = Plan.build(self.build(keyboard))

        self.assertEqual(len(plan), 1)
        self.assertIn('XKBLAYOUT "us" -> "us,ru"; XKBOPTIONS "" -> "grp_led:scroll"', plan.actions[0].describe())
        plan.execute()

        self.assertEqual(
            keyboard.read_text(encoding="utf-8"),
            '# KEYBOARD CONFIGURATION FILE\nXKBMODEL="pc105"\nXKBLAYOUT="us,ru"\nXKBOPTIONS="grp_led:scroll"\n'
            'BACKSPACE="guess"\n',
        )
        install = self.machine.executed[0][0]
        self.assertEqual(install[:7], ("sudo", "--", "install", "-m", "644", "-o", "root"))
        self.assertEqual(len(Plan.build(self.build(keyboard))), 0)

    def test_missing_keyboard_file_gets_the_listed_fields_only(self) -> None:
        write(self.root / "system.toml", '[keyboard]\nlayout = "us"\n')
        keyboard = self.tmp / "keyboard"
        Plan.build(self.build(keyboard)).execute()

        self.assertEqual(keyboard.read_text(encoding="utf-8"), 'XKBLAYOUT="us"\n')

    def test_parse_keyboard_handles_quotes_comments_and_bare_values(self) -> None:
        self.assertEqual(
            system_planner.parse_keyboard("# c\nXKBMODEL='pc105'\nXKBLAYOUT=us\nXKBVARIANT=\"\"\n"),
            {"XKBMODEL": "pc105", "XKBLAYOUT": "us", "XKBVARIANT": ""},
        )


class PackagesTests(ProfileCase):
    def build(self) -> list:
        return list(packages_planner.build(self.ctx, FakeBackend(), process=self.machine))

    def test_missing_names_are_installed_in_one_batch_per_manager(self) -> None:
        write(
            self.root / "packages.toml",
            'apt = ["tmux", "ripgrep", "curl"]\nsnap = ["firefox"]\nflatpak = ["org.gnome.Extensions"]\n',
        )
        self.machine.apt = {"curl"}
        plan = Plan.build(self.build())

        self.assertEqual([a.kind for a in plan.actions], ["package"] * 3)
        self.assertEqual([a.needs_root for a in plan.actions], [True, True, False])
        self.assertEqual(plan.actions[0].describe(), "apt: install tmux, ripgrep")
        self.assertEqual(self.machine.executed, [])

        plan.execute()
        self.assertEqual(
            [argv for argv, _ in self.machine.executed],
            [
                ("sudo", "--", "apt-get", "install", "-y", "tmux", "ripgrep"),
                ("sudo", "--", "snap", "install", "firefox"),
                ("flatpak", "install", "-y", "--noninteractive", "flathub", "org.gnome.Extensions"),
            ],
        )
        self.assertEqual(len(Plan.build(self.build())), 0)

    def test_overlay_names_are_unioned_and_nothing_is_ever_removed(self) -> None:
        write(self.root / "packages.toml", 'apt = ["tmux"]\n')
        write(self.root / "machines/home/packages.toml", 'apt = ["htop", "tmux"]\n')
        self.machine.apt = {"vim"}  # installed but not listed: left alone
        Plan.build(self.build()).execute()

        self.assertEqual(self.machine.apt, {"vim", "tmux", "htop"})
        self.assertTrue(all("remove" not in " ".join(argv) for argv, _ in self.machine.executed))

    def test_everything_installed_means_an_empty_plan(self) -> None:
        write(self.root / "packages.toml", 'apt = ["tmux"]\nsnap = ["firefox"]\n')
        self.machine.apt, self.machine.snaps = {"tmux"}, {"firefox"}
        plan = Plan.build(self.build())

        self.assertEqual(len(plan), 0)
        self.assertEqual(plan.satisfied, 2)

    def test_no_packages_toml_plans_nothing(self) -> None:
        self.assertEqual(self.build(), [])

    def test_a_name_that_looks_like_an_option_is_invalid_config(self) -> None:
        write(self.root / "packages.toml", 'apt = ["--allow-downgrades"]\n')
        with self.assertRaisesRegex(ConfigError, "not a package name"):
            self.build()

    def test_no_sudo_leaves_root_installs_undone_but_still_runs_flatpak(self) -> None:
        write(self.root / "packages.toml", 'apt = ["tmux"]\nflatpak = ["org.x.App"]\n')
        result = Plan.build(self.build()).execute(no_sudo=True)

        self.assertEqual(result.changed, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(self.machine.apt, set())


if __name__ == "__main__":
    unittest.main()

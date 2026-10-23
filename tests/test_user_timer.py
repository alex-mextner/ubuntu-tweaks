"""The ``user-timer`` step behind the opt-in ``autoheal`` module: user units, never root."""

import unittest
from collections.abc import Sequence
from pathlib import Path

from tests.util import TempDirCase
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import load_manifest
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Config
from ubuntu_tweaks.steps import StepEnv, UserTimerAction, get_step

REPO = Path(__file__).resolve().parent.parent


class Systemd(FakeProcess):
    """``systemctl --user`` double: ``enable`` makes ``is-enabled`` answer ``enabled``."""

    def __init__(self) -> None:
        super().__init__()
        self.enabled: set[str] = set()

    def query(self, argv: Sequence[str], **kwargs: object) -> CommandResult:  # type: ignore[override]
        self.queries.append(tuple(argv))
        if tuple(argv[:3]) == ("systemctl", "--user", "is-enabled"):
            return CommandResult(0, "enabled\n", "") if argv[3] in self.enabled else CommandResult(1, "disabled\n", "")
        return CommandResult(127, "", "")

    def execute(self, argv: Sequence[str], **kwargs: object) -> None:  # type: ignore[override]
        self.executed.append((tuple(argv), None))
        if tuple(argv[:3]) == ("systemctl", "--user", "enable"):
            self.enabled.update(argv[3:])


class UserTimerTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.process = Systemd()
        self.env = StepEnv(Config(), self.process, root=self.tmp, home=self.tmp / "home")

    def action(self) -> UserTimerAction:
        (action,) = get_step("user-timer")(
            self.env, "autoheal", {"command": "%h/.local/bin/ubuntu-tweak apply --commit --no-sudo"}
        )
        assert isinstance(action, UserTimerAction)
        return action

    def test_run_writes_both_units_enables_them_and_converges(self) -> None:
        action = self.action()
        self.assertFalse(action.check())
        action.run()
        self.assertTrue(action.check())
        units = self.tmp / "home/.config/systemd/user"
        self.assertIn(
            "ExecStart=%h/.local/bin/ubuntu-tweak apply --commit --no-sudo", (units / "autoheal.service").read_text()
        )
        self.assertIn("OnCalendar=daily", (units / "autoheal.timer").read_text())
        self.assertIn(
            (("systemctl", "--user", "enable", "autoheal.service", "autoheal.timer"), None), self.process.executed
        )

    def test_a_changed_unit_file_is_drift(self) -> None:
        action = self.action()
        action.run()
        (self.tmp / "home/.config/systemd/user/autoheal.timer").write_text("[Timer]\nOnCalendar=weekly\n")
        self.assertFalse(action.check())

    def test_it_never_asks_for_root(self) -> None:
        action = self.action()
        action.run()
        self.assertFalse(action.needs_root)
        self.assertFalse(any(call[0][0] == "sudo" for call in self.process.executed))

    def test_a_bad_unit_name_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "name"):
            get_step("user-timer")(self.env, "autoheal", {"name": "../x", "command": "true"})


class AutohealModuleTests(unittest.TestCase):
    def test_the_module_is_off_by_default_and_user_level(self) -> None:
        manifest = load_manifest(REPO / "modules/autoheal")
        self.assertFalse(manifest.enabled)
        self.assertEqual([step.name for step in manifest.steps], ["user-timer"])
        self.assertIn("--no-sudo", str(manifest.steps[0].args["command"]))


if __name__ == "__main__":
    unittest.main()


class UserTimerHardeningTests(TempDirCase):
    def test_a_newline_in_the_command_cannot_inject_unit_directives(self) -> None:
        env = StepEnv(Config(), Systemd(), root=self.tmp, home=self.tmp / "home")
        with self.assertRaisesRegex(ConfigError, "control characters"):
            get_step("user-timer")(env, "autoheal", {"command": "true\nExecStart=/bin/evil"})
        with self.assertRaisesRegex(ConfigError, "control characters"):
            get_step("user-timer")(env, "autoheal", {"command": "true", "calendar": "daily\n[Service]"})

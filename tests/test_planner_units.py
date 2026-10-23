"""The units planner: units.toml becomes daemon-reload, enable / start / disable and linger actions, run after the
files planner linked the unit files, against a fake systemd user manager (no real systemd is touched)."""

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    import unittest

    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import Action, ExecutionResult, Phase, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.planners import files as files_planner
from ubuntu_tweaks.planners import units as planner
from ubuntu_tweaks.process import CommandError, CommandResult, Process
from ubuntu_tweaks.profile import Profile, unit_template
from ubuntu_tweaks.settings import FakeBackend

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
USER_DIR = ".config/systemd/user"


class FakeSystemd(Process):
    """A user manager and a login manager in memory. ``units`` maps a loaded unit to its properties; a unit file
    in ``home/.config/systemd/user`` is loaded by ``daemon-reload`` only, like the real manager."""

    def __init__(self, home: Path, *, reachable: bool = True, linger: bool = False) -> None:
        self.home = home
        self.reachable = reachable
        self.linger = linger
        self.units: dict[str, dict[str, str]] = {}
        self.stale: set[str] = set()  # loaded units whose file or drop-in changed on disk
        self.fail: set[str] = set()  # units whose enable fails
        self.executed: list[tuple[str, ...]] = []
        self.aliases: dict[str, str] = {}  # alias -> canonical name, which is what ``Id`` reports
        self.system_enabled: set[str] = set()

    def load(
        self,
        name: str,
        *,
        kind: str = "simple",
        state: str = "disabled",
        active: str = "inactive",
        system: bool = False,
    ) -> None:
        """``system``: the enablement is a package's link outside ``~/.config`` (``disable`` cannot remove it)."""
        self.units[name] = {"UnitFileState": state, "ActiveState": active, "Type": kind}
        if system:
            self.system_enabled.add(name)
        elif state == "enabled":
            wants = self.home / USER_DIR / "default.target.wants"
            wants.mkdir(parents=True, exist_ok=True)
            (wants / name).unlink(missing_ok=True)
            (wants / name).symlink_to(self.home / USER_DIR / name)

    def _lookup(self, name: str, *, create: bool = False) -> dict[str, str] | None:
        """The unit's properties; an instance of a loaded template is only remembered when ``create``: asking is
        read-only."""
        name = self.aliases.get(name, name)
        if name in self.units:
            return self.units[name]
        template = unit_template(name)
        if template != name and template in self.units:
            instance = dict(self.units[template])
            if create:
                self.units[name] = instance
            return instance
        return None

    def _show(self, names: Sequence[str]) -> str:
        blocks = []
        for name in names:
            canonical = self.aliases.get(name, name)
            unit = self._lookup(canonical)
            if unit is None:
                props = {
                    "Id": canonical,
                    "LoadState": "not-found",
                    "UnitFileState": "",
                    "ActiveState": "inactive",
                    "Type": "simple",
                    "NeedDaemonReload": "no",
                }
            else:
                props = {
                    "Id": canonical,
                    "LoadState": "loaded",
                    **unit,
                    "NeedDaemonReload": "yes" if name in self.stale else "no",
                }
            blocks.append("\n".join(f"{key}={value}" for key, value in props.items()))
        return "\n\n".join(blocks) + "\n"

    def query(self, argv: Sequence[str], *, timeout: float = 60.0, cwd: Path | None = None) -> CommandResult:
        words = tuple(argv)
        if words[:2] == ("systemctl", "--user"):
            if not self.reachable:
                return CommandResult(1, "", "Failed to connect to user scope bus via local transport")
            if words[2:] == ("show-environment",):
                return CommandResult(0, "HOME=/x\n", "")
            if words[2] == "show" and words[3] == "-p":
                return CommandResult(0, self._show(words[5:]), "")
        if words[:2] == ("loginctl", "show-user"):
            return CommandResult(0, f"Linger={'yes' if self.linger else 'no'}\n", "")
        return CommandResult(127, "", f"{argv[0]}: command not found")

    def _load_files(self) -> None:
        directory = self.home / USER_DIR
        for entry in sorted(directory.iterdir()) if directory.is_dir() else ():
            if entry.is_dir() or entry.name in self.units:
                continue
            kind = "oneshot" if "Type=oneshot" in entry.read_text(encoding="utf-8") else "simple"
            self.load(entry.name, kind=kind)
        self.stale.clear()

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        words = tuple(argv)
        self.executed.append(words)
        if words[0] == "loginctl":
            self.linger = words[1] == "enable-linger"
            return
        if words[0] == "rm":
            for path in words[3:]:
                Path(path).unlink()
                unit = self._lookup(Path(path).name)
                if unit is not None:
                    unit["UnitFileState"] = "linked"
            return
        if not self.reachable:
            raise CommandError(f"{' '.join(words)}: exited with status 1", 1)
        verb, rest = words[2], words[3:]
        if verb in ("enable", "disable", "stop", "mask") and rest[-1] in self.aliases:
            raise CommandError(f"{' '.join(words)}: Refusing to operate on alias name or linked unit file", 1)
        if verb == "daemon-reload":
            self._load_files()
        elif verb == "enable":
            name = rest[-1]
            unit = self._lookup(name, create=True)
            if unit is None or name in self.fail:
                raise CommandError(f"{' '.join(words)}: exited with status 1", 1)
            unit["UnitFileState"] = "enabled"
            if "--now" in rest and unit["Type"] != "oneshot":
                unit["ActiveState"] = "active"
            file = self.home / USER_DIR / name
            if os.path.lexists(file):
                wants = self.home / USER_DIR / "default.target.wants"
                wants.mkdir(parents=True, exist_ok=True)
                (wants / name).unlink(missing_ok=True)
                (wants / name).symlink_to(file)
        elif verb == "stop":
            unit = self._lookup(rest[-1])
            if unit is not None:
                unit["ActiveState"] = "inactive"
        elif verb == "disable":
            name = rest[-1]
            link = self.home / USER_DIR / name
            if link.is_symlink():  # the real `systemctl disable` removes a unit file that is a link, too
                link.unlink()
            for wants in (self.home / USER_DIR).glob("*.wants"):
                (wants / name).unlink(missing_ok=True)
            unit = self._lookup(name, create=True)
            if unit is not None:
                unit["UnitFileState"] = "enabled" if name in self.system_enabled else "disabled"
                unit["ActiveState"] = "inactive"
        elif verb == "mask":
            unit = self._lookup(rest[-1], create=True)
            if unit is not None:
                unit["UnitFileState"] = "masked"
                unit["ActiveState"] = "inactive"
        else:
            raise AssertionError(f"unexpected {words}")


class PlannerCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "rootfs"
        self.profile_root = self.tmp / "profile"
        self.profile_root.mkdir()
        (self.tmp / "repo").mkdir()
        self.ctx = Context(self.tmp / "repo", Profile(self.profile_root, "home"))
        self.systemd = FakeSystemd(self.home)

    def declare(self, text: str, *, overlay: bool = False) -> None:
        profile = self.ctx.profile
        write(profile.overlay_units_toml if overlay else profile.units_toml, text)

    def unit_file(self, name: str, text: str = "[Service]\nExecStart=/bin/true\n") -> Path:
        return write(self.profile_root / "files" / USER_DIR / name, text)

    def actions(self, *, with_files: bool = False) -> list[Action]:
        found: list[Action] = []
        if with_files:
            found += files_planner.build(
                self.ctx, FakeBackend(), home=self.home, clock=lambda: NOW, state_home=self.tmp / "state"
            )
        found += planner.build(
            self.ctx, FakeBackend(), process=self.systemd, home=self.home, root=self.root, user="alice"
        )
        return found

    def plan(self, *, with_files: bool = False) -> Plan:
        return Plan.build(self.actions(with_files=with_files))

    def kinds(self, plan: Plan) -> list[str]:
        return [action.kind for action in plan.actions]

    def commands(self, plan: Plan) -> list[str]:
        return [action.describe() for action in plan.actions]


class NothingDeclaredTests(PlannerCase):
    def test_no_units_toml_and_no_unit_files_plan_nothing_and_ask_nothing(self) -> None:
        self.assertEqual(self.actions(), [])

    def test_the_planner_is_discovered(self) -> None:
        from ubuntu_tweaks import planners

        self.assertIn("units", [module.__name__.rsplit(".", 1)[-1] for module in planners.discover_planners()])

    def test_an_invalid_units_toml_is_a_config_error(self) -> None:
        self.declare('enable = ["glances"]\n')
        with self.assertRaisesRegex(ConfigError, "not a unit name"):
            self.actions()


class EnableTests(PlannerCase):
    def test_enable_and_start_is_one_command_and_converges(self) -> None:
        self.systemd.load("glances.service")
        self.declare('enable = ["glances.service"]\n')
        plan = self.plan()

        self.assertEqual(self.kinds(plan), ["user-unit"])
        self.assertEqual(self.commands(plan), ["systemctl --user enable --now glances.service"])
        self.assertEqual(self.systemd.executed, [])  # a preview changes nothing
        result = plan.execute()
        self.assertEqual((result.changed, result.failed, result.blocked), (1, (), ()))
        self.assertEqual(self.systemd.executed, [("systemctl", "--user", "enable", "--now", "glances.service")])
        self.assertEqual(len(self.plan()), 0)  # the second run has nothing to do

    def test_start_false_enables_without_starting_and_an_inactive_unit_is_converged(self) -> None:
        self.systemd.load("glances.service")
        self.declare('enable = ["glances.service"]\nstart = false\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user enable glances.service"])
        plan.execute()
        self.assertEqual(self.systemd.units["glances.service"]["ActiveState"], "inactive")
        self.assertEqual(len(self.plan()), 0)

    def test_an_enabled_but_stopped_service_is_started(self) -> None:
        self.systemd.load("glances.service", state="enabled")
        self.declare('enable = ["glances.service"]\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user enable --now glances.service"])
        plan.execute()
        self.assertEqual(self.systemd.units["glances.service"]["ActiveState"], "active")

    def test_a_oneshot_service_is_converged_when_enabled_and_is_not_run_by_enabling(self) -> None:
        self.systemd.load("backup.service", kind="oneshot")
        self.declare('enable = ["backup.service"]\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user enable backup.service"])
        plan.execute()
        self.assertEqual(
            self.systemd.units["backup.service"],
            {"UnitFileState": "enabled", "ActiveState": "inactive", "Type": "oneshot"},
        )
        self.assertEqual(len(self.plan()), 0)

    def test_an_enabled_active_timer_is_satisfied(self) -> None:
        self.systemd.load("backup.timer", state="enabled", active="active")
        self.declare('enable = ["backup.timer"]\n')
        plan = self.plan()
        self.assertEqual(len(plan), 0)
        self.assertEqual(plan.satisfied, 2)  # the unit and the (not needed) reload

    def test_static_and_indirect_units_count_as_enabled(self) -> None:
        self.systemd.load("a.service", state="static", active="active")
        self.systemd.load("b.service", state="indirect", active="active")
        self.declare('enable = ["a.service", "b.service"]\n')
        self.assertEqual(len(self.plan()), 0)

    def test_a_failed_unit_is_tried_again(self) -> None:
        self.systemd.load("a.service", state="enabled", active="failed")
        self.declare('enable = ["a.service"]\n')
        self.assertEqual(len(self.plan()), 1)

    def test_the_reason_comment_ends_the_preview_line(self) -> None:
        self.systemd.load("glances.service")
        self.declare('enable = [\n  "glances.service",  # system monitor web UI\n]\n')
        self.assertEqual(
            self.commands(self.plan()), ["systemctl --user enable --now glances.service  # system monitor web UI"]
        )

    def test_the_machine_overlay_enables_more_and_disables_a_shared_name(self) -> None:
        for name in ("b.service", "c.service"):
            self.systemd.load(name, state="disabled")
        self.systemd.load("a.service", state="enabled", active="active")
        self.declare('enable = ["a.service", "b.service"]\n')
        self.declare('enable = ["c.service"]\ndisable = ["a.service"]\n', overlay=True)
        plan = self.plan()
        self.assertEqual(
            self.commands(plan),
            [
                "systemctl --user enable --now b.service",
                "systemctl --user enable --now c.service",
                "systemctl --user disable --now a.service",
            ],
        )

    def test_a_unit_installed_by_the_system_needs_no_profile_file(self) -> None:
        self.systemd.load("gnome-thing.service")
        self.declare('enable = ["gnome-thing.service"]\n')
        result = self.plan().execute()
        self.assertEqual((result.changed, result.blocked), (1, ()))


class DisableTests(PlannerCase):
    def test_disable_stops_and_disables_an_enabled_unit(self) -> None:
        self.systemd.load("old.service", state="enabled", active="active")
        self.declare('disable = ["old.service"]\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user disable --now old.service"])
        plan.execute()
        self.assertEqual(len(self.plan()), 0)

    def test_disable_stops_a_running_unit_that_is_not_enabled(self) -> None:
        self.systemd.load("old.service", state="static", active="active")
        self.declare('disable = ["old.service"]\n')
        self.assertEqual(len(self.plan()), 1)

    def test_a_unit_the_machine_does_not_have_is_already_disabled(self) -> None:
        self.declare('disable = ["ghost.service"]\n')
        self.assertEqual(len(self.plan()), 0)

    def test_a_disabled_stopped_masked_unit_is_converged(self) -> None:
        self.systemd.load("old.service", state="masked")
        self.declare('disable = ["old.service"]\n')
        self.assertEqual(len(self.plan()), 0)


class LingerTests(PlannerCase):
    def test_linger_true_runs_loginctl_for_the_user_and_converges(self) -> None:
        self.declare("linger = true\n")
        plan = self.plan()
        self.assertEqual(self.kinds(plan), ["linger"])
        self.assertEqual(self.commands(plan), ["loginctl enable-linger alice"])
        self.assertFalse(plan.actions[0].needs_root)
        plan.execute()
        self.assertEqual(self.systemd.executed, [("loginctl", "enable-linger", "alice")])
        self.assertEqual(len(self.plan()), 0)

    def test_linger_already_on_is_satisfied(self) -> None:
        self.systemd.linger = True
        self.declare("linger = true\n")
        self.assertEqual(len(self.plan()), 0)

    def test_linger_false_switches_it_off(self) -> None:
        self.systemd.linger = True
        self.declare("linger = false\n")
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["loginctl disable-linger alice"])
        plan.execute()
        self.assertFalse(self.systemd.linger)

    def test_linger_unset_leaves_it_alone(self) -> None:
        self.systemd.linger = True
        self.declare('enable = ["a.service"]\n')
        self.systemd.load("a.service", state="enabled", active="active")
        self.assertEqual([a.kind for a in self.actions()], ["daemon-reload", "user-unit"])
        self.assertEqual(len(self.plan()), 0)


class DaemonReloadTests(PlannerCase):
    def test_a_unit_file_the_files_planner_has_not_linked_yet_is_followed_by_one_reload(self) -> None:
        self.unit_file("backup.service", "[Service]\nType=oneshot\n")
        self.unit_file("backup.timer", "[Timer]\nOnCalendar=daily\n")
        self.declare('enable = ["backup.timer"]\n')
        plan = self.plan(with_files=True)

        self.assertEqual(self.kinds(plan), ["link", "link", "daemon-reload", "user-unit"])
        self.assertEqual(
            self.commands(plan)[2:],
            ["systemctl --user daemon-reload", "systemctl --user enable --now backup.timer"],
        )
        result = plan.execute()
        self.assertEqual((result.changed, result.failed, result.blocked, result.manual), (4, (), (), ()))
        self.assertEqual(
            self.systemd.executed,
            [("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "enable", "--now", "backup.timer")],
        )
        self.assertTrue((self.home / USER_DIR / "backup.timer").is_symlink())
        self.assertEqual(len(self.plan(with_files=True)), 0)  # idempotent

    def test_a_loaded_unit_whose_drop_in_changed_is_reloaded(self) -> None:
        self.systemd.load("glances.service", state="enabled", active="active")
        self.systemd.stale.add("glances.service")
        self.declare('enable = ["glances.service"]\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user daemon-reload"])
        plan.execute()
        self.assertEqual(len(self.plan()), 0)

    def test_nothing_pending_means_no_reload(self) -> None:
        self.unit_file("glances.service")
        self.declare('enable = ["glances.service"]\n')
        self.plan(with_files=True).execute()
        self.assertEqual(self.systemd.executed[0], ("systemctl", "--user", "daemon-reload"))
        self.assertEqual(self.kinds(self.plan(with_files=True)), [])

    def test_unit_files_without_any_declaration_are_still_reloaded(self) -> None:
        self.unit_file("helper.service")
        plan = self.plan(with_files=True)
        self.assertEqual(self.kinds(plan), ["link", "daemon-reload"])

    def test_the_reload_runs_even_when_the_link_was_made_by_an_earlier_run_of_the_same_plan(self) -> None:
        """The plan is built before the links exist and its checks run again right before each action: the
        reload must not turn itself off once the link it waits for is there."""
        self.unit_file("a.service")
        self.declare('enable = ["a.service"]\n')
        plan = self.plan(with_files=True)
        plan.execute()
        self.assertIn(("systemctl", "--user", "daemon-reload"), self.systemd.executed)


class TemplateTests(PlannerCase):
    def test_an_instance_is_provided_by_its_template_file(self) -> None:
        self.unit_file("3d-printer-watch@.service", "[Service]\nExecStart=/bin/true %i\n")
        self.declare('enable = ["3d-printer-watch@u1.service"]\n')
        plan = self.plan(with_files=True)
        self.assertEqual(
            self.commands(plan)[1:],
            ["systemctl --user daemon-reload", "systemctl --user enable --now 3d-printer-watch@u1.service"],
        )
        result = plan.execute()
        self.assertEqual((result.failed, result.blocked), ((), ()))
        self.assertEqual(self.systemd.units["3d-printer-watch@u1.service"]["UnitFileState"], "enabled")
        self.assertEqual(len(self.plan(with_files=True)), 0)

    def test_a_oneshot_template_is_enabled_without_starting_in_the_preview_too(self) -> None:
        self.unit_file("job@.service", "[Service]\nType=oneshot\n")
        self.declare('enable = ["job@x.service"]\n')
        self.assertEqual(self.commands(self.plan(with_files=True))[-1], "systemctl --user enable job@x.service")


class FailoverTests(PlannerCase):
    def test_no_user_manager_is_one_manual_step_with_the_exact_commands_and_nothing_is_run(self) -> None:
        self.systemd.reachable = False
        self.declare('enable = ["a.service", "b.timer"]\ndisable = ["c.service"]\n')
        plan = self.plan()

        self.assertEqual(self.kinds(plan), ["user-units"])
        result = plan.execute()
        self.assertEqual((result.changed, result.failed, result.blocked), (0, (), ()))
        self.assertEqual(len(result.manual), 1)
        message = result.manual[0][1]
        self.assertIn("systemctl --user enable --now a.service b.timer", message)
        self.assertIn("systemctl --user disable --now c.service", message)
        self.assertIn("no systemd user manager", message)
        self.assertEqual(self.systemd.executed, [])
        self.assertEqual(self.kinds(self.plan()), ["user-units"])  # still Missing until someone does it

    def test_the_deferred_commands_include_the_reload_and_honor_start_false(self) -> None:
        self.systemd.reachable = False
        self.declare('enable = ["a.service"]\nstart = false\n')
        self.assertEqual(
            self.commands(self.plan()),
            ["systemctl --user daemon-reload; systemctl --user enable a.service"],
        )

    def test_linger_still_runs_when_the_user_manager_is_unreachable(self) -> None:
        self.systemd.reachable = False
        self.declare('enable = ["a.service"]\nlinger = true\n')
        plan = self.plan()
        self.assertEqual(self.kinds(plan), ["linger", "user-units"])
        result = plan.execute()
        self.assertEqual((result.changed, len(result.manual)), (1, 1))
        self.assertTrue(self.systemd.linger)

    def test_an_unreachable_manager_with_only_unit_files_plans_nothing(self) -> None:
        self.systemd.reachable = False
        self.unit_file("helper.service")
        self.assertEqual(self.kinds(self.plan(with_files=True)), ["link"])

    def test_a_unit_nobody_provides_is_blocked_by_its_missing_file_and_the_others_still_run(self) -> None:
        self.systemd.load("fine.service")
        self.declare('enable = ["ghost.service", "fine.service"]\n')
        result = self.plan().execute()

        self.assertEqual(result.changed, 1)
        self.assertEqual(result.failed, ())
        self.assertEqual(
            [item.action.describe().split("  [")[0] for item in result.blocked],
            ["systemctl --user enable --now ghost.service"],
        )
        self.assertIn("files/.config/systemd/user/ghost.service", result.blocked[0].by.describe())
        self.assertIn("blocked by [unit-file]", result.render_blocked())
        self.assertNotIn(("systemctl", "--user", "enable", "--now", "ghost.service"), self.systemd.executed)
        self.assertEqual(self.systemd.units["fine.service"]["UnitFileState"], "enabled")

    def test_a_unit_file_in_a_system_directory_is_not_missing(self) -> None:
        for directory in ("usr/lib/systemd/user", "usr/share/systemd/user", "run/systemd/user"):
            write(self.root / directory / "sys.service", "x")
            self.systemd.load("sys.service")
            self.declare('enable = ["sys.service"]\n')
            result = self.plan().execute()
            self.assertEqual((result.changed, result.blocked), (1, ()), directory)
            (self.root / directory / "sys.service").unlink()
            self.systemd.units.clear()
            self.systemd.executed.clear()

    def test_the_preview_says_where_an_unprovided_unit_would_have_to_come_from(self) -> None:
        self.declare('enable = ["ghost.service"]\n')
        line = self.commands(self.plan())[0]
        self.assertIn("systemctl --user enable --now ghost.service", line)
        self.assertIn("no unit file yet", line)

    def test_one_unit_that_fails_does_not_hide_the_others(self) -> None:
        self.systemd.load("bad.service")
        self.systemd.load("good.service")
        self.systemd.fail.add("bad.service")
        self.declare('enable = ["bad.service", "good.service"]\n')
        result: ExecutionResult = self.plan().execute()

        self.assertEqual(result.changed, 1)
        self.assertEqual(
            [failure.action.describe() for failure in result.failed], ["systemctl --user enable --now bad.service"]
        )
        self.assertEqual(self.systemd.units["good.service"]["UnitFileState"], "enabled")


class OrderingTests(PlannerCase):
    def test_the_units_phase_is_after_the_files_and_before_the_toggle(self) -> None:
        self.assertLess(Phase.FILES, Phase.UNITS)
        self.assertLess(Phase.UNITS, Phase.TOGGLE)
        self.systemd.load("a.service")
        self.declare('enable = ["a.service"]\nlinger = true\n')
        phases = {action.phase for action in self.actions()}
        self.assertEqual(phases, {Phase.UNITS})

    def test_nothing_is_written_outside_the_profile_by_building_the_plan(self) -> None:
        self.unit_file("a.service")
        self.declare('enable = ["a.service"]\n')
        self.plan(with_files=True)
        self.assertFalse(os.path.lexists(self.home / USER_DIR / "a.service"))
        self.assertEqual(self.systemd.executed, [])


class AliasTests(PlannerCase):
    def test_an_alias_is_enabled_by_its_canonical_name_because_systemctl_refuses_the_alias(self) -> None:
        self.systemd.load("dbus-broker.service")
        self.systemd.aliases["dbus.service"] = "dbus-broker.service"
        self.declare('enable = ["dbus.service"]\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user enable --now dbus-broker.service"])
        result = plan.execute()
        self.assertEqual((result.changed, result.failed), (1, ()))
        self.assertEqual(len(self.plan()), 0)

    def test_an_alias_is_disabled_by_its_canonical_name_too(self) -> None:
        self.systemd.load("dbus-broker.service", state="enabled", active="active")
        self.systemd.aliases["dbus.service"] = "dbus-broker.service"
        self.declare('disable = ["dbus.service"]\n')
        self.assertEqual(self.commands(self.plan()), ["systemctl --user disable --now dbus-broker.service"])


class QuotingTests(PlannerCase):
    def test_a_name_with_an_escape_is_quoted_so_a_pasted_command_names_the_same_unit(self) -> None:
        name = "app@my\\x2dinst.service"
        self.systemd.load(name)
        self.declare('enable = ["app@my\\\\x2dinst.service"]\n')
        self.assertEqual(self.commands(self.plan()), ["systemctl --user enable --now 'app@my\\x2dinst.service'"])

    def test_the_manual_step_is_quoted_too(self) -> None:
        self.systemd.reachable = False
        self.declare('enable = ["app@my\\\\x2dinst.service"]\n')
        message = self.plan().execute().manual[0][1]
        self.assertIn("enable --now 'app@my\\x2dinst.service'", message)


class DottedInstanceTests(PlannerCase):
    def test_an_instance_with_dots_finds_its_template_file(self) -> None:
        self.unit_file("vpn@.service", "[Service]\nType=oneshot\n")
        self.declare('enable = ["vpn@client.conf.service"]\n')
        plan = self.plan(with_files=True)
        self.assertEqual(self.commands(plan)[-1], "systemctl --user enable vpn@client.conf.service")
        result = plan.execute()
        self.assertEqual((result.failed, result.blocked), ((), ()))


class ManualOneshotTests(PlannerCase):
    def test_the_manual_commands_keep_a_oneshot_out_of_enable_now(self) -> None:
        self.systemd.reachable = False
        self.unit_file("backup.service", "[Service]\nType=oneshot\n")
        self.declare('enable = ["backup.service", "run.service"]\n')
        self.assertEqual(
            self.commands(self.plan(with_files=True))[-1],
            "systemctl --user daemon-reload; systemctl --user enable --now run.service; "
            "systemctl --user enable backup.service",
        )


class ProfileLinkedDisableTests(PlannerCase):
    def link_unit(self, name: str) -> Path:
        source = self.unit_file(name)
        link = self.home / USER_DIR / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)
        wants = self.home / USER_DIR / "default.target.wants"
        wants.mkdir()
        (wants / name).symlink_to(link)
        return link

    def test_disabling_a_unit_the_profile_links_stops_it_and_keeps_the_link(self) -> None:
        link = self.link_unit("foo.service")
        self.systemd.load("foo.service", state="enabled", active="active")
        self.declare('enable = ["foo.service"]\n')
        self.declare('disable = ["foo.service"]\n', overlay=True)
        plan = self.plan(with_files=True)

        wants = self.home / USER_DIR / "default.target.wants/foo.service"
        self.assertEqual(
            self.commands(plan),
            [f"systemctl --user stop foo.service; rm -f -- {wants}; systemctl --user daemon-reload"],
        )
        result = plan.execute()
        self.assertEqual((result.changed, result.failed), (1, ()))
        self.assertTrue(link.is_symlink())  # `systemctl disable` would have deleted it
        self.assertFalse(os.path.lexists(wants))
        self.assertNotIn("disable", [words[2] for words in self.systemd.executed if words[0] == "systemctl"])
        self.assertEqual(len(self.plan(with_files=True)), 0)  # nothing relinks it, nothing is left to do

    def test_a_unit_the_profile_does_not_ship_is_disabled_the_usual_way(self) -> None:
        self.systemd.load("other.service", state="enabled", active="active")
        self.declare('disable = ["other.service"]\n')
        self.assertEqual(self.commands(self.plan()), ["systemctl --user disable --now other.service"])


class SymlinkedDirectoryTests(PlannerCase):
    def test_a_user_unit_directory_that_is_a_link_into_the_profile_does_not_plan_a_reload_forever(self) -> None:
        self.unit_file("a.service")
        (self.home / ".config/systemd").mkdir(parents=True)
        (self.home / USER_DIR).symlink_to(self.profile_root / "files" / USER_DIR)
        self.systemd.load("a.service", state="enabled", active="active")
        self.declare('enable = ["a.service"]\n')
        self.assertEqual(len(self.plan()), 0)


class SystemEnabledDisableTests(PlannerCase):
    def test_a_unit_a_package_enables_system_wide_is_reported_as_failed_not_masked(self) -> None:
        self.systemd.load("pipewire.service", state="enabled", active="active", system=True)
        self.declare('disable = ["pipewire.service"]\n')
        plan = self.plan()
        self.assertEqual(self.commands(plan), ["systemctl --user disable --now pipewire.service"])
        result = plan.execute()
        self.assertEqual((result.changed, len(result.failed)), (0, 1))
        self.assertEqual(
            [words[2] for words in self.systemd.executed], ["disable"]
        )  # nothing is masked behind your back


class OddProfileFileNameTests(PlannerCase):
    def test_a_profile_file_with_a_name_systemctl_would_take_for_an_option_is_not_asked_about(self) -> None:
        self.unit_file("-odd.service")
        self.unit_file("fine.service")
        self.systemd.load("fine.service", state="enabled", active="active")
        plan = Plan.build(self.actions(with_files=True))
        result = plan.execute()
        self.assertEqual((result.failed, result.blocked), ((), ()))
        self.assertEqual(len(Plan.build(self.actions(with_files=True))), 0)


class TemplateDisableTests(PlannerCase):
    def test_disabling_an_instance_of_a_template_the_profile_links_keeps_the_template_link(self) -> None:
        source = self.unit_file("vpn@.service")
        link = self.home / USER_DIR / "vpn@.service"
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
        self.systemd.load("vpn@.service")
        self.systemd.load("vpn@a.service", state="enabled", active="active")
        self.declare('enable = ["vpn@a.service"]\n')
        self.declare('disable = ["vpn@a.service"]\n', overlay=True)
        plan = self.plan(with_files=True)
        wants = self.home / USER_DIR / "default.target.wants/vpn@a.service"
        self.assertEqual(
            self.commands(plan),
            [f"systemctl --user stop vpn@a.service; rm -f -- {wants}; systemctl --user daemon-reload"],
        )
        plan.execute()
        self.assertTrue(link.is_symlink())
        self.assertNotIn("disable", [words[2] for words in self.systemd.executed if words[0] == "systemctl"])


class LingerRefusedTests(PlannerCase):
    def test_a_refused_loginctl_is_a_manual_step_with_the_command_not_a_failure(self) -> None:
        class Refusing(FakeSystemd):
            def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
                raise CommandError("loginctl enable-linger alice: exited with status 1", 1)

        self.systemd = Refusing(self.home)
        self.declare("linger = true\n")
        result = self.plan().execute()
        self.assertEqual((result.changed, result.failed), (0, ()))
        self.assertEqual(len(result.manual), 1)
        self.assertIn("sudo loginctl enable-linger alice", result.manual[0][1])


class ReasonPerListTests(PlannerCase):
    def test_a_reason_for_enabling_is_not_shown_on_the_overlays_disable(self) -> None:
        self.systemd.load("a.service", state="enabled", active="active")
        self.declare('enable = [\n  "a.service",  # the web UI I want\n]\n')
        self.declare('disable = ["a.service"]\n', overlay=True)
        self.assertEqual(self.commands(self.plan()), ["systemctl --user disable --now a.service"])


class StoppedLinkedUnitTests(ProfileLinkedDisableTests):
    def test_a_unit_that_is_not_running_is_not_stopped_so_a_broken_one_can_still_be_disabled(self) -> None:
        self.link_unit("foo.service")
        self.systemd.load("foo.service", state="enabled", active="inactive")
        self.declare('enable = ["foo.service"]\n')
        self.declare('disable = ["foo.service"]\n', overlay=True)
        wants = self.home / USER_DIR / "default.target.wants/foo.service"
        self.assertEqual(
            self.commands(self.plan(with_files=True)),
            [f"rm -f -- {wants}; systemctl --user daemon-reload"],
        )

"""session-priority module against a scratch home and a stateful fake systemd: drop-ins, live apply, no shell restart."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.path.insert(0, str(MODULE))
sys.dont_write_bytecode = True

import session_priority_lib as lib  # noqa: E402

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.module_script import main  # noqa: E402
from ubuntu_tweaks.process import CommandError, CommandResult, Process  # noqa: E402

SHELL = "org.gnome.Shell@ubuntu.service"


class FakeSystemd(Process):
    """The user manager's view of the shell and the slice: ``daemon-reload`` re-reads the drop-in files into the live values
    (observed on the real manager); ``bus_down`` makes every call fail the way a missing session bus does."""

    def __init__(self, home: Path, active: set[str]) -> None:
        self.home = home
        self.active = set(active)
        self.bus_down = False
        self.override: dict[str, dict[str, str]] = {}  # a later foreign drop-in: unit -> property -> value
        self.live = {SHELL: {"MemorySwapMax": "infinity"}, "session.slice": {"CPUWeight": "100"}}
        self.groups = {SHELL: "/user.slice/session.slice/" + SHELL, "session.slice": "/user.slice/session.slice"}
        self.kernel = {SHELL: ("memory.swap.max", "max"), "session.slice": ("cpu.weight", "100")}
        self.cgroups = home.parent.parent / "cgroup"
        self.executed: list[list[str]] = []
        self.write_kernel()

    def write_kernel(self):
        for unit, (name, value) in self.kernel.items():
            target = self.cgroups / self.groups[unit].lstrip("/") / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value + "\n")

    def query(self, argv, *, timeout=60.0, cwd=None):
        assert argv[:2] == ["systemctl", "--user"], argv
        if self.bus_down:
            return CommandResult(1, "", "Failed to connect to bus: No medium found")
        command, rest = argv[2], argv[3:]
        if command == "is-active":
            state = "active" if rest[0] in self.active else "inactive"
            return CommandResult(0 if state == "active" else 3, state + "\n")
        if command == "list-units":
            return CommandResult(0, f"{SHELL} loaded active running GNOME Shell\n" if SHELL in self.active else "")
        if command == "show":
            unit = rest[0]
            keys = [item.removeprefix("--property=") for item in rest[1:]]
            values = {**self.live[unit], "ControlGroup": self.groups[unit]}
            return CommandResult(0, "".join(f"{key}={values.get(key, '')}\n" for key in keys))
        raise AssertionError(argv)

    def execute(self, argv, *, input_text=None, cwd=None):
        self.executed.append(list(argv))
        assert argv[2] == "daemon-reload", f"only a reload may change the manager: {argv}"
        for dropin in lib.DROPINS:
            unit = SHELL if dropin.unit == lib.SHELL_TEMPLATE else dropin.unit
            path = self.home / lib.USER_UNITS / f"{dropin.unit}.d" / lib.DROPIN_NAME
            if path.exists():
                self.live[unit][dropin.key] = dropin.value
                self.kernel[unit] = (dropin.cgroup_file, dropin.value)
            self.live[unit].update(self.override.get(unit, {}))
            if unit in self.override:
                self.kernel[unit] = (dropin.cgroup_file, self.override[unit][dropin.key])
        self.write_kernel()

    def elevate(self, argv):
        return list(argv)

    def reloads(self) -> int:
        return sum(1 for argv in self.executed if argv[2] == "daemon-reload")


class SessionPriority(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/alice"
        patcher = unittest.mock.patch.object(lib, "RELOAD_SETTLE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def systemd(self, active=(SHELL, "session.slice")) -> FakeSystemd:
        return FakeSystemd(self.home, set(active))

    def module(self, systemd):
        return lib.SessionPriority(self.home, systemd, systemd.cgroups)

    def test_install_converges_reloads_once_and_second_run_executes_nothing(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        findings = self.module(systemd).findings()
        self.assertTrue(all(f.ok for f in findings), [f.line() for f in findings if not f.ok])
        self.assertEqual(systemd.live[SHELL]["MemorySwapMax"], "0")
        self.assertEqual(systemd.live["session.slice"]["CPUWeight"], "300")
        self.assertEqual(systemd.kernel[SHELL], ("memory.swap.max", "0"))
        self.assertEqual(systemd.reloads(), 1)
        self.module(systemd).apply()
        self.assertEqual(systemd.reloads(), 1, "nothing changed, so nothing is reloaded")

    def test_dropins_land_where_systemd_reads_them(self):
        self.module(self.systemd()).apply()
        base = self.home / ".config/systemd/user"
        self.assertIn(
            "[Service]\nMemorySwapMax=0\n", (base / "org.gnome.Shell@.service.d" / lib.DROPIN_NAME).read_text()
        )
        self.assertIn("[Slice]\nCPUWeight=300\n", (base / "session.slice.d" / lib.DROPIN_NAME).read_text())

    def test_plan_names_every_change_and_says_nothing_restarts(self):
        text = "\n".join(self.module(self.systemd()).plan())
        self.assertIn("+MemorySwapMax=0", text)
        self.assertIn("daemon-reload", text)
        self.assertIn("nothing restarts", text)
        self.assertIn(f"live {SHELL}: MemorySwapMax infinity -> 0", text)

    def test_the_kernel_value_is_what_check_trusts_not_only_what_systemd_loaded(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        systemd.kernel[SHELL] = ("memory.swap.max", "max")  # written straight into the cgroup behind systemd's back
        systemd.write_kernel()
        unmet = [f for f in self.module(systemd).findings() if not f.ok]
        self.assertEqual([f.subject for f in unmet], [f"running {SHELL}"])
        self.assertIn("memory.swap.max is max, want 0", unmet[0].detail)
        self.assertFalse(unmet[0].manual, "one install (a reload) re-applies it")
        self.module(systemd).apply()  # a reload re-applies it
        self.assertTrue(all(f.ok for f in self.module(systemd).findings()))

    def test_a_missing_kernel_file_is_a_mismatch(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        (systemd.cgroups / systemd.groups["session.slice"].lstrip("/") / "cpu.weight").unlink()
        unmet = [f for f in self.module(systemd).findings() if not f.ok]
        self.assertIn("cpu.weight is missing, want 300", unmet[0].detail)

    def test_plan_is_empty_when_converged_so_it_promises_no_reload(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        self.assertEqual(self.module(systemd).plan(), [])

    def test_a_foreign_override_is_needs_manual_not_a_silent_pass(self):
        systemd = self.systemd()
        systemd.override = {"session.slice": {"CPUWeight": "50"}}
        with self.assertRaises(lib.NeedsManual) as raised:
            self.module(systemd).apply()
        self.assertIn("CPUWeight is 50, want 300", str(raised.exception))
        self.assertIn("overrides ours", str(raised.exception))
        self.assertNotIn("written behind its back", str(raised.exception))
        unmet = [f for f in self.module(systemd).findings() if not f.ok]
        self.assertEqual([f.subject for f in unmet], ["running session.slice"])

    def test_stopped_units_need_only_their_files_and_the_shell_absence_is_visible(self):
        systemd = self.systemd(active=())
        self.module(systemd).apply()
        findings = self.module(systemd).findings()
        self.assertTrue(all(f.ok for f in findings))
        self.assertTrue(any("no org.gnome.Shell@ instance" in f.detail for f in findings))

    def test_unreachable_user_manager_is_drift_exit_3_not_a_pass_and_not_a_crash(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        systemd.bus_down = True
        findings = self.module(systemd).findings()
        self.assertTrue(any(not f.ok and "Failed to connect to bus" in f.detail for f in findings))
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(main(self.module(systemd), ["check"], out=out, err=err), 3)
        plan = self.module(systemd).plan()
        self.assertIn("live state unreadable", plan[-1])

    def test_install_without_a_user_manager_is_needs_manual_after_writing_the_files(self):
        systemd = self.systemd()
        systemd.bus_down = True
        with self.assertRaises(lib.NeedsManual) as raised:
            self.module(systemd).apply()
        self.assertIn("daemon-reload", str(raised.exception))
        self.assertTrue((self.home / ".config/systemd/user/session.slice.d" / lib.DROPIN_NAME).exists())
        systemd.bus_down = False
        self.module(systemd).apply()
        self.assertTrue(all(f.ok for f in self.module(systemd).findings()))

    def test_a_failed_reload_is_needs_manual_not_a_crash(self):
        systemd = self.systemd()

        def refuse(argv, *, input_text=None, cwd=None):
            raise CommandError("Failed to reload daemon: Connection timed out")

        systemd.execute = refuse
        with self.assertRaises(lib.NeedsManual) as raised:
            self.module(systemd).apply()
        self.assertIn("daemon-reload", str(raised.exception))

    def test_an_empty_control_group_is_a_mismatch_not_a_pass(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        systemd.groups["session.slice"] = ""
        unmet = [f for f in self.module(systemd).findings() if not f.ok]
        self.assertIn("cpu.weight is missing", unmet[0].detail)

    def test_a_missing_controller_is_named_as_such_not_blamed_on_a_drop_in(self):
        systemd = self.systemd()
        original = systemd.execute
        weight = systemd.cgroups / systemd.groups["session.slice"].lstrip("/") / "cpu.weight"

        def no_controller(argv, *, input_text=None, cwd=None):
            original(argv, input_text=input_text, cwd=cwd)
            weight.unlink()  # the cpu controller is not delegated: the kernel file never exists

        systemd.execute = no_controller
        with self.assertRaises(lib.NeedsManual) as raised:
            self.module(systemd).apply()
        self.assertIn("controller is not delegated", str(raised.exception))

    def test_a_late_cgroup_write_after_the_reload_is_waited_for(self):
        systemd = self.systemd()
        original = systemd.execute
        polls_after_reload = {"count": 0, "reloaded": False}

        def lagging(argv, *, input_text=None, cwd=None):
            original(argv, input_text=input_text, cwd=cwd)
            polls_after_reload["reloaded"] = True
            systemd.kernel[SHELL] = ("memory.swap.max", "max")  # the manager has not written the cgroup yet
            systemd.write_kernel()

        real_query = systemd.query

        def query(argv, **kwargs):
            if polls_after_reload["reloaded"] and argv[2] == "show" and argv[3] == SHELL:
                polls_after_reload["count"] += 1
                if polls_after_reload["count"] == 3:  # lands on the third poll
                    systemd.kernel[SHELL] = ("memory.swap.max", "0")
                    systemd.write_kernel()
            return real_query(argv, **kwargs)

        systemd.execute = lagging
        systemd.query = query
        self.module(systemd).apply()  # converges without NeedsManual
        self.assertGreaterEqual(polls_after_reload["count"], 3, "the module polled until the write landed")

    def test_a_kernel_value_that_a_reload_does_not_undo_is_named_as_such(self):
        systemd = self.systemd()
        original = systemd.execute

        def sticky(argv, *, input_text=None, cwd=None):
            original(argv, input_text=input_text, cwd=cwd)
            systemd.kernel[SHELL] = ("memory.swap.max", "max")
            systemd.write_kernel()

        systemd.execute = sticky
        with self.assertRaises(lib.NeedsManual) as raised:
            self.module(systemd).apply()
        self.assertIn("kernel value differs from what systemd loaded", str(raised.exception))
        self.assertNotIn("overrides ours", str(raised.exception))

    def test_edited_dropin_is_drift_and_is_rewritten(self):
        systemd = self.systemd()
        self.module(systemd).apply()
        shell = self.home / ".config/systemd/user/org.gnome.Shell@.service.d" / lib.DROPIN_NAME
        shell.write_text("[Service]\nMemorySwapMax=infinity\n")
        self.assertFalse(all(f.ok for f in self.module(systemd).findings()))
        self.module(systemd).apply()
        self.assertIn("MemorySwapMax=0", shell.read_text())
        self.assertEqual(systemd.live[SHELL]["MemorySwapMax"], "0")


class BuildGuards(unittest.TestCase):
    SCRIPT = MODULE / "session-priority"

    def run_script(self, *, env_extra=None):
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run(
            [sys.executable, "-B", str(self.SCRIPT), "check"], env=env, capture_output=True, text=True
        )

    @unittest.skipIf(os.geteuid() == 0, "the script refuses root before it reads options")
    def test_options_are_rejected_with_exit_1(self):
        result = self.run_script(env_extra={"UBUNTU_TWEAK_MODULE_CONFIG": '{"x": 1}'})
        self.assertEqual(result.returncode, 1)
        self.assertIn("takes no options", result.stderr)


class Manifests(unittest.TestCase):
    def test_modules_are_valid_manifests(self):
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.name, "session-priority")

    def test_session_priority_is_user_level_and_claims_its_units(self):
        manifest = load_manifest(MODULE)
        self.assertFalse(manifest.source.root)
        self.assertEqual(
            [match for rule in manifest.coverage for match in rule.match],
            ["unit:org.gnome.Shell@.service", "unit:session.slice"],
        )


if __name__ == "__main__":
    unittest.main()

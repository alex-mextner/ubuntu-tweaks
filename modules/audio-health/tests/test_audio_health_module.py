"""audio-health module against a scratch home: installs the check, its service and timer, converges, sees drift."""

import importlib.machinery
import importlib.util
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.path.insert(0, str(MODULE))
sys.dont_write_bytecode = True

import audio_health_lib as lib  # noqa: E402

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.process import CommandResult, FakeProcess  # noqa: E402

TIMER = ("systemctl", "--user", "is-enabled", "audio-health.timer")
RUNNING = ("systemctl", "--user", "is-active", "audio-health.timer")


def load_script(name):
    """The extensionless script MODULE/name as a module."""
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(MODULE / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    script = importlib.util.module_from_spec(spec)
    loader.exec_module(script)
    return script


class SystemdProcess(FakeProcess):
    """A user manager that remembers `enable`, `start` and `restart` of the timer."""

    def __init__(self):
        super().__init__(effects=self._remember)
        self.enabled = False
        self.running = False
        self.no_manager = False  # no user session bus: systemctl says nothing on stdout and exits 1
        self.old_systemd = False  # a missing unit: empty stdout and "No such file or directory" instead of "not-found"

    def _remember(self, argv, _input):
        if argv[:3] == ("systemctl", "--user", "enable"):
            self.enabled = True
            self.running = "--now" in argv
        if argv[:3] == ("systemctl", "--user", "disable"):
            self.enabled = False
            self.running = self.running and "--now" not in argv
        if argv[:3] == ("systemctl", "--user", "stop"):
            self.running = False
        if argv[:3] == ("systemctl", "--user", "start") or argv[:3] == ("systemctl", "--user", "restart"):
            self.running = True

    def query(self, argv, **kwargs):
        if self.no_manager and tuple(argv[:2]) == ("systemctl", "--user"):
            return CommandResult(
                1, "", "Failed to connect to user scope bus via local transport: No such file or directory\n"
            )
        if tuple(argv) == TIMER and not self.enabled and self.old_systemd:
            return CommandResult(
                1, "", "Failed to get unit file state for audio-health.timer: No such file or directory\n"
            )
        if tuple(argv) == TIMER:
            return CommandResult(0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n", "")
        if tuple(argv) == RUNNING:
            return CommandResult(0 if self.running else 3, "active\n" if self.running else "inactive\n", "")
        return super().query(argv, **kwargs)


class AudioHealthModule(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/alice"
        self.process = SystemdProcess()

    def module(self):
        return lib.AudioHealth(self.home, self.process)

    def unmet(self):
        return [f.line() for f in self.module().findings() if not f.ok]

    def commands(self):
        return [argv for argv, _ in self.process.executed]

    def test_fresh_home_is_unsatisfied_install_converges_and_a_second_run_changes_nothing(self):
        self.assertTrue(self.unmet())
        self.module().apply()
        self.assertEqual(self.unmet(), [])
        self.assertEqual(
            self.commands(),
            [
                ("systemctl", "--user", "daemon-reload"),
                ("systemctl", "--user", "enable", "--now", "audio-health.timer"),
            ],
        )
        self.module().apply()
        self.assertEqual(len(self.commands()), 2, "nothing changed, so systemd is not touched again")
        self.assertEqual(self.module().plan(), [])

    def test_the_check_is_executable_and_the_units_run_it_at_login_and_every_few_minutes(self):
        self.module().apply()
        script = self.home / ".local/bin/audio-health-check"
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertEqual(script.read_text(), (MODULE / "audio-health-check").read_text())
        units = self.home / ".config/systemd/user"
        service = (units / "audio-health.service").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("ExecStart=%h/.local/bin/audio-health-check", service)
        timer = (units / "audio-health.timer").read_text()
        self.assertRegex(timer, r"(?m)^OnActiveSec=\d+")  # the login check: the timer starts with the user manager
        self.assertRegex(timer, r"(?m)^OnUnitActiveSec=\d+min$")
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_pending_window_of_the_check_covers_three_timer_periods(self):
        """The check forgets a sighting after PENDING_MAX_AGE_S: a copy of the timer's period, which must leave room for two
        periods (one run in between judged nothing) plus one of slack, or a slower timer would never confirm."""
        script = load_script("audio-health-check")
        period = re.search(r"(?m)^OnUnitActiveSec=(\d+)min$", (MODULE / "units/audio-health.timer").read_text())
        self.assertIsNotNone(period, "the timer period is written as OnUnitActiveSec=Nmin")
        self.assertGreaterEqual(script.PENDING_MAX_AGE_S, 3 * int(period.group(1)) * 60)

    def test_an_enabled_timer_that_was_stopped_is_drift_and_install_starts_it(self):
        self.module().apply()
        self.process.running = False
        self.assertTrue(self.unmet())
        self.assertIn("    systemctl --user enable --now audio-health.timer", self.module().plan())
        self.module().apply()
        self.assertEqual(self.unmet(), [])

    def test_a_script_that_is_not_text_is_drift_and_install_replaces_it(self):
        self.module().apply()
        (self.home / ".local/bin/audio-health-check").write_bytes(b"\xff\xfe\x00binary")
        self.assertTrue(self.unmet())
        self.module().plan()
        self.module().apply()
        self.assertEqual(self.unmet(), [])

    def test_a_changed_or_missing_piece_is_drift_and_install_repairs_it(self):
        self.module().apply()
        (self.home / ".local/bin/audio-health-check").write_text("#!/bin/sh\nexit 0\n")
        (self.home / ".config/systemd/user/audio-health.timer").unlink()
        self.process.enabled = False
        found = "\n".join(self.unmet())
        for needle in ("audio-health-check", "audio-health.timer", "enabled"):
            self.assertIn(needle, found)
        self.assertTrue(any("audio-health-check" in line for line in self.module().plan()))
        self.module().apply()
        self.assertEqual(self.unmet(), [])

    def test_the_plan_names_a_new_script_without_printing_it_and_shows_the_diff_of_a_changed_one(self):
        plan = "\n".join(self.module().plan())
        self.assertIn("write ~/.local/bin/audio-health-check (", plan)
        self.assertNotIn("def alsa_cards", plan)  # the script is not dumped into the preview
        self.assertIn("OnUnitActiveSec=5min", plan)  # the small unit files are
        self.module().apply()
        script = self.home / ".local/bin/audio-health-check"
        script.write_text(script.read_text().replace("COMMAND_TIMEOUT_S = 15", "COMMAND_TIMEOUT_S = 99"))
        script.chmod(0o755)
        changed = "\n".join(self.module().plan())
        self.assertIn("-COMMAND_TIMEOUT_S = 99", changed)
        self.assertIn("+COMMAND_TIMEOUT_S = 15", changed)

    def test_a_changed_timer_file_is_picked_up_by_a_running_timer(self):
        self.module().apply()
        self.process.executed.clear()
        (self.home / ".config/systemd/user/audio-health.timer").write_text("[Timer]\nOnUnitActiveSec=1h\n")
        self.module().apply()
        self.assertEqual(
            self.commands(),
            [("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "restart", "audio-health.timer")],
        )

    def left(self):
        return [f.line() for f in self.module().retired_findings() if not f.ok]

    def test_retiring_an_installed_module_stops_the_timer_removes_the_units_and_the_script_and_converges(self):
        self.module().apply()
        self.assertTrue(self.left())
        plan = self.module().retire_plan()
        self.assertIn("    systemctl --user disable --now audio-health.timer", plan)
        for piece in (
            "~/.local/bin/audio-health-check",
            "~/.config/systemd/user/audio-health.service",
            "~/.config/systemd/user/audio-health.timer",
        ):
            self.assertIn(f"    remove {piece}", plan)
        self.process.executed.clear()
        self.module().retire()
        self.assertEqual(self.left(), [])
        self.assertEqual(self.module().retire_plan(), [])
        self.assertFalse(self.process.enabled or self.process.running)
        for piece in lib.PIECES:
            self.assertFalse((self.home / piece.destination).exists(), piece.destination)
        self.assertEqual(
            self.commands(),
            [
                ("systemctl", "--user", "disable", "--now", "audio-health.timer"),
                ("systemctl", "--user", "daemon-reload"),
            ],
        )
        self.process.executed.clear()
        self.module().retire()
        self.assertEqual(self.commands(), [], "nothing left, so the user manager is not touched again")

    def test_retiring_a_machine_that_never_had_the_module_changes_nothing(self):
        self.assertEqual(self.left(), [])
        self.module().retire()
        self.assertEqual(self.commands(), [])

    def test_a_timer_that_is_enabled_again_or_a_script_that_came_back_is_drift_of_the_retirement(self):
        self.module().apply()
        self.module().retire()
        self.process.enabled = True
        self.assertIn("audio-health.timer", "\n".join(self.left()))
        self.process.enabled = False
        self.process.running = True
        self.assertIn("audio-health.timer", "\n".join(self.left()))
        self.process.running = False
        script = self.home / ".local/bin/audio-health-check"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\n")
        self.assertIn("audio-health-check", "\n".join(self.left()))
        self.module().retire()
        self.assertEqual(self.left(), [])

    def test_a_dangling_symlink_in_place_of_a_piece_is_removed_too(self):
        self.module().apply()
        self.module().retire()
        link = self.home / ".config/systemd/user/audio-health.timer"
        link.symlink_to(self.root / "gone")
        self.assertTrue(self.left())
        self.module().retire()
        self.assertFalse(link.is_symlink())

    def test_a_user_manager_that_does_not_answer_is_drift_then_a_manual_step_and_nothing_is_removed(self):
        """No answer is not "the timer is off": retiring blind would delete the units under a timer that still runs.
        The check stays inside the 0/3 protocol (a crash would abort the whole plan), the removal asks for a session."""
        self.module().apply()
        self.process.no_manager = True
        self.assertRegex("\n".join(self.left()), "user manager does not answer")
        with self.assertRaisesRegex(lib.ModuleError, "user manager does not answer"):
            self.module().retire_plan()
        with self.assertRaisesRegex(lib.NeedsManual, "user manager does not answer"):
            self.module().retire()
        for piece in lib.PIECES:
            self.assertTrue((self.home / piece.destination).exists(), piece.destination)

    def test_the_real_no_bus_message_is_silence_for_both_questions_even_though_it_says_no_such_file(self):
        """It ends in the words an older systemd uses for a missing unit; only that older wording is an answer."""
        self.process.no_manager = True
        for verb in ("is-enabled", "is-active"):
            with self.assertRaisesRegex(lib.ModuleError, "user manager does not answer"):
                self.module()._answered(verb)

    def test_an_enable_link_that_disable_leaves_behind_is_removed_in_the_same_run(self):
        self.module().apply()
        link = self.home / ".config/systemd/user/timers.target.wants/audio-health.timer"
        link.parent.mkdir(parents=True)
        link.write_text("not a symlink: systemd's disable leaves it alone")
        self.module().retire()
        self.assertEqual(self.left(), [])

    def test_with_nothing_on_disk_a_silent_user_manager_is_a_converged_retirement(self):
        self.process.no_manager = True
        self.assertEqual(self.left(), [])
        self.assertEqual(self.module().retire_plan(), [])
        self.module().retire()
        self.assertEqual(self.commands(), [])

    def test_an_older_systemd_that_words_a_missing_unit_on_stderr_is_an_answer_not_silence(self):
        self.process.old_systemd = True
        self.assertEqual(self.left(), [])
        self.module().apply()
        self.module().retire()
        self.assertEqual(self.left(), [])
        self.module().retire()

    def test_the_plan_does_not_list_the_enable_link_that_disable_removes_itself(self):
        self.module().apply()
        link = self.home / ".config/systemd/user/timers.target.wants/audio-health.timer"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.home / ".config/systemd/user/audio-health.timer")
        plan = "\n".join(self.module().retire_plan())
        self.assertIn("systemctl --user disable --now audio-health.timer", plan)
        self.assertNotIn("timers.target.wants", plan)

    def test_a_running_timer_whose_unit_file_is_gone_is_stopped_because_disable_needs_the_file(self):
        self.module().apply()
        (self.home / ".config/systemd/user/audio-health.timer").unlink()
        self.process.enabled = False  # systemd: not-found; the timer is still loaded and running
        self.assertIn("    systemctl --user stop audio-health.timer", self.module().retire_plan())
        self.process.executed.clear()
        self.module().retire()
        self.assertEqual(self.left(), [])
        self.assertEqual(
            self.commands(),
            [("systemctl", "--user", "stop", "audio-health.timer"), ("systemctl", "--user", "daemon-reload")],
        )

    def test_an_enable_link_left_without_its_unit_file_is_drift_and_removed_too(self):
        link = self.home / ".config/systemd/user/timers.target.wants/audio-health.timer"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.home / ".config/systemd/user/audio-health.timer")  # dangling: the unit file is gone
        self.assertIn("timers.target.wants/audio-health.timer", "\n".join(self.left()))
        self.assertIn(
            "    remove ~/.config/systemd/user/timers.target.wants/audio-health.timer", self.module().retire_plan()
        )
        self.module().retire()
        self.assertFalse(link.is_symlink())
        self.assertEqual(self.left(), [])
        self.assertEqual(self.commands(), [("systemctl", "--user", "daemon-reload")])

    def test_manifest_declares_a_disabled_by_default_retirement(self):
        manifest = load_manifest(MODULE)
        self.assertFalse(manifest.enabled)
        self.assertEqual(manifest.source.retire, ("audio-health", "retire"))
        self.assertEqual(manifest.source.retire_check, ("audio-health", "retire-check"))
        self.assertEqual(manifest.source.retire_preview, ("audio-health", "retire-plan"))

    def test_manifest_declares_a_user_level_system_module_that_needs_the_tools_it_runs(self):
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.source.kind, "system")
        self.assertFalse(manifest.source.root)
        self.assertEqual(manifest.source.check, ("audio-health", "check"))
        for package in ("alsa-utils", "wireplumber", "libnotify-bin"):
            self.assertIn(package, manifest.deps.apt)


class ScriptEntry(unittest.TestCase):
    def test_script_loads(self):
        script = load_script("audio-health")
        self.assertTrue(callable(script.build))


if __name__ == "__main__":
    unittest.main()

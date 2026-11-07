"""stray-gsettings-keyfile against a scratch HOME (HOME and the XDG variables point into a temp dir)."""

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "stray-gsettings-keyfile"
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402

CONTENT = (
    "[org/gnome/shell]\nfavorite-apps=['a.desktop']\n\n[org/gnome/desktop/interface]\ntoolkit-accessibility=false\n"
)


def load_script():
    loader = importlib.machinery.SourceFileLoader("stray_gsettings_keyfile_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


class StrayKeyfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.settings = self.home / ".config/glib-2.0/settings"
        self.keyfile = self.settings / "keyfile"
        self.backups = self.home / ".local/state/ubuntu-tweaks/backups"

    def put_keyfile(self) -> None:
        self.settings.mkdir(parents=True)
        self.keyfile.write_text(CONTENT)

    def run_module(self, command: str, options: dict | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        env["HOME"] = str(self.home)
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        if options is not None:
            env["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps(options)
        return subprocess.run([sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True)

    def test_manifest_is_off_by_default_and_user_level(self) -> None:
        manifest = load_manifest(MODULE)
        self.assertFalse(manifest.enabled)
        self.assertFalse(manifest.source.root)

    def test_an_absent_file_is_no_drift(self) -> None:
        self.assertEqual(self.run_module("check").returncode, 0)
        self.assertEqual(self.run_module("plan").stdout, "")

    def test_a_present_file_is_drift_and_plan_lists_backup_remove_rmdir(self) -> None:
        self.put_keyfile()

        check = self.run_module("check")
        plan = self.run_module("plan").stdout.splitlines()

        self.assertEqual(check.returncode, 3)
        self.assertIn(str(self.keyfile), check.stdout)
        self.assertEqual(len(plan), 3)
        self.assertIn(str(self.backups / "glib-keyfile-"), plan[0])
        self.assertTrue(plan[0].split()[2].endswith("Z.keyfile"))
        self.assertEqual(plan[1], f"rm {self.keyfile}")

    def test_install_backs_up_with_mode_0600_removes_file_and_empty_dir(self) -> None:
        self.put_keyfile()

        self.assertEqual(self.run_module("install").returncode, 0)

        (backup,) = self.backups.glob("glib-keyfile-*.keyfile")
        self.assertEqual(backup.read_text(), CONTENT)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertFalse(self.keyfile.exists())
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.run_module("check").returncode, 0)

    def test_a_second_install_changes_nothing(self) -> None:
        self.put_keyfile()
        self.run_module("install")

        self.assertEqual(self.run_module("install").returncode, 0)

        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_a_directory_with_other_files_is_kept(self) -> None:
        self.put_keyfile()
        (self.settings / "other").write_text("x")

        self.run_module("install")

        self.assertFalse(self.keyfile.exists())
        self.assertTrue((self.settings / "other").exists())

    def test_xdg_variables_are_honoured(self) -> None:
        config, state = self.home / "cfg", self.home / "st"
        (config / "glib-2.0/settings").mkdir(parents=True)
        (config / "glib-2.0/settings/keyfile").write_text(CONTENT)
        env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(config), XDG_STATE_HOME=str(state))
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)

        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "install"], env=env, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(list((state / "ubuntu-tweaks/backups").glob("glib-keyfile-*"))), 1)

    def stray(self, script):
        return script.StrayKeyfile(self.home / ".config", self.backups, NOW)

    def test_two_removals_in_the_same_second_keep_both_backups(self) -> None:
        script = load_script()
        self.put_keyfile()
        self.stray(script).apply()
        self.settings.mkdir(parents=True)
        self.keyfile.write_text("[second]\nk=1\n")

        self.stray(script).apply()

        backups = sorted(path.name for path in self.backups.iterdir())
        self.assertEqual(backups, ["glib-keyfile-20261101T120000Z-1.keyfile", "glib-keyfile-20261101T120000Z.keyfile"])
        self.assertEqual((self.backups / "glib-keyfile-20261101T120000Z.keyfile").read_text(), CONTENT)
        self.assertEqual((self.backups / "glib-keyfile-20261101T120000Z-1.keyfile").read_text(), "[second]\nk=1\n")

    def test_a_file_written_while_the_backup_is_made_is_not_deleted(self) -> None:
        script = load_script()
        self.put_keyfile()
        module = self.stray(script)
        original = module.write_backup

        def write_then_writer_recreates(data: bytes) -> None:
            original(data)
            self.keyfile.write_text("[new]\nk=2\n")  # GLib replaces the keyfile after it was moved aside

        module.write_backup = write_then_writer_recreates
        module.apply()

        self.assertEqual(self.keyfile.read_text(), "[new]\nk=2\n")
        (backup,) = self.backups.glob("glib-keyfile-*.keyfile")
        self.assertEqual(backup.read_text(), CONTENT)
        self.assertEqual(self.run_module("check").returncode, 3)

    def test_a_failed_backup_puts_the_file_back(self) -> None:
        script = load_script()
        self.put_keyfile()
        module = self.stray(script)

        def fail(data: bytes) -> None:
            raise script.ModuleError("disk full")

        module.write_backup = fail
        with self.assertRaises(script.ModuleError):
            module.apply()

        self.assertEqual(self.keyfile.read_text(), CONTENT)
        self.assertEqual(sorted(path.name for path in self.settings.iterdir()), ["keyfile"])

    def test_a_leftover_staging_directory_is_drift_and_is_backed_up_by_the_next_run(self) -> None:
        script = load_script()
        self.settings.mkdir(parents=True)
        leftover = self.settings / "keyfile.removing-abc"
        leftover.mkdir()
        (leftover / "keyfile").write_text("only copy of an interrupted run")

        self.assertEqual(self.run_module("check").returncode, 3)
        self.stray(script).apply()

        self.assertEqual(self.run_module("check").returncode, 0)
        self.assertEqual([p.read_text() for p in self.backups.iterdir()], ["only copy of an interrupted run"])

    def test_an_interrupt_during_the_backup_puts_the_file_back(self) -> None:
        script = load_script()
        self.put_keyfile()
        module = self.stray(script)

        def interrupt(data: bytes) -> None:
            raise KeyboardInterrupt

        module.write_backup = interrupt
        with self.assertRaises(KeyboardInterrupt):
            module.apply()

        self.assertEqual(self.keyfile.read_text(), CONTENT)
        self.assertEqual(sorted(p.name for p in self.settings.iterdir()), ["keyfile"])

    def test_a_dangling_symlink_is_removed_with_a_backup_recording_its_target(self) -> None:
        script = load_script()
        for target in ("missing-target", str(self.home / "absent")):
            with self.subTest(target=target):
                self.settings.mkdir(parents=True, exist_ok=True)
                self.keyfile.symlink_to(target)

                self.assertEqual(self.run_module("check").returncode, 3)
                self.stray(script).apply()

                self.assertFalse(self.keyfile.is_symlink())
                self.assertTrue(any(f"dangling symlink to {target}" in p.read_text() for p in self.backups.iterdir()))

    def test_a_relative_symlink_is_backed_up_from_where_it_lives(self) -> None:
        script = load_script()
        self.settings.mkdir(parents=True)
        store = self.home / ".config/glib-2.0/store"
        store.write_text("ORIGINAL")
        self.keyfile.symlink_to("../store")

        self.stray(script).apply()

        self.assertFalse(self.keyfile.is_symlink())
        self.assertEqual(store.read_text(), "ORIGINAL")
        (backup,) = self.backups.iterdir()
        self.assertEqual(backup.read_text(), "ORIGINAL")

    def test_a_directory_or_fifo_is_refused_untouched(self) -> None:
        script = load_script()
        self.settings.mkdir(parents=True)
        self.keyfile.mkdir()
        (self.keyfile / "inner").write_text("x")
        with self.assertRaises(script.ModuleError):
            self.stray(script).apply()
        self.assertEqual((self.keyfile / "inner").read_text(), "x")

        (self.keyfile / "inner").unlink()
        self.keyfile.rmdir()
        os.mkfifo(self.keyfile)
        with self.assertRaises(script.ModuleError):
            self.stray(script).apply()
        self.assertTrue(self.keyfile.exists())
        self.assertEqual(sorted(p.name for p in self.settings.iterdir()), ["keyfile"])

    def test_a_symlink_replaced_by_a_regular_file_before_the_move_backs_up_the_new_file(self) -> None:
        script = load_script()
        self.settings.mkdir(parents=True)
        old = self.home / "old-target"
        old.write_text("OLD")
        self.keyfile.symlink_to(old)
        module = self.stray(script)
        real_rename = os.rename

        def replace_then_rename(src, dst):
            os.unlink(src)  # gsettings writes a new regular file in its place
            Path(src).write_text("NEW")
            real_rename(src, dst)

        original = script.os.rename
        script.os.rename = replace_then_rename
        try:
            module.apply()
        finally:
            script.os.rename = original

        (backup,) = self.backups.iterdir()
        self.assertEqual(backup.read_text(), "NEW")
        self.assertEqual(old.read_text(), "OLD")

    def test_a_symlink_to_a_fifo_is_refused_without_blocking(self) -> None:
        script = load_script()
        self.settings.mkdir(parents=True)
        fifo = self.home / "fifo"
        os.mkfifo(fifo)
        self.keyfile.symlink_to(fifo)

        with self.assertRaises(script.ModuleError):
            self.stray(script).apply()

        self.assertTrue(self.keyfile.is_symlink())

    def test_a_failed_backup_of_a_symlink_puts_the_link_back(self) -> None:
        script = load_script()
        self.settings.mkdir(parents=True)
        self.keyfile.symlink_to("missing-target")
        module = self.stray(script)

        def fail(data: bytes) -> None:
            raise script.ModuleError("disk full")

        module.write_backup = fail
        with self.assertRaises(script.ModuleError) as caught:
            module.apply()

        self.assertIn("put back", str(caught.exception))
        self.assertTrue(self.keyfile.is_symlink())

    def test_unknown_options_are_refused(self) -> None:
        self.assertEqual(self.run_module("check", {"keep": True}).returncode, 1)


if __name__ == "__main__":
    unittest.main()

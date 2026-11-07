"""inotify-limits: the staged scripts (INOTIFY_LIMITS_ROOT) and the real converge step with a fake sysctl."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "inotify-limits"
sys.path.insert(0, str(MODULE.parents[1]))

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.module_script import ModuleError  # noqa: E402
from ubuntu_tweaks.process import CommandError  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("inotify_limits_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


script = load_script()


class FakeSysctl:
    """Records `sysctl --system`; the kernel takes the file's value unless `ignores` (a later file overrides it)."""

    def __init__(self, root, *, ignores=False, fails=False):
        self.root, self.ignores, self.fails, self.calls = root, ignores, fails, []

    def execute(self, argv, **_):
        self.calls.append(list(argv))
        if self.fails:
            raise CommandError("sysctl --system: exited with status 255")
        if not self.ignores:
            text = (self.root / script.DROP_IN).read_text()
            value = [line for line in text.splitlines() if line.startswith("fs.")][0].split("=")[1]
            (self.root / script.LIVE_VALUE).write_text(value + "\n")


class Staged(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "proc/sys/fs/inotify").mkdir(parents=True)
        (self.root / script.LIVE_VALUE).write_text("128\n")

    def run_script(self, command, **options):
        env = {
            **os.environ,
            script.ROOT_ENV: str(self.root),
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(options),
        }
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True, timeout=30
        )

    @property
    def drop_in(self):
        return self.root / script.DROP_IN


class StagedScript(Staged):
    def test_check_reports_the_missing_file_and_the_kernel_value(self):
        result = self.run_script("check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing /etc/sysctl.d/60-inotify.conf", result.stdout)
        self.assertIn("the kernel allows 2048 inotify instances per user: now 128", result.stdout)

    def test_install_writes_the_drop_in_with_the_value(self):
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fs.inotify.max_user_instances=2048\n", self.drop_in.read_text())
        self.assertEqual(oct(self.drop_in.stat().st_mode & 0o777), "0o644")

    def test_the_option_changes_the_value(self):
        self.run_script("install", max_user_instances=4096)
        self.assertIn("fs.inotify.max_user_instances=4096\n", self.drop_in.read_text())

    def test_a_staged_run_with_a_matching_kernel_value_is_clean_and_a_second_install_changes_nothing(self):
        (self.root / script.LIVE_VALUE).write_text("2048\n")
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual(self.run_script("check").returncode, 0)
        before = self.drop_in.stat().st_mtime_ns
        second = self.run_script("install")
        self.assertIn("nothing to change", second.stdout)
        self.assertEqual(self.drop_in.stat().st_mtime_ns, before)

    def test_a_foreign_file_is_backed_up_before_it_is_replaced(self):
        self.drop_in.parent.mkdir(parents=True)
        self.drop_in.write_text("fs.inotify.max_user_instances=1024\n")
        self.assertEqual(self.run_script("install").returncode, 0)
        backups = list((self.root / "var/backups/ubuntu-tweaks/inotify-limits").glob("*/60-inotify.conf"))
        self.assertEqual([b.read_text() for b in backups], ["fs.inotify.max_user_instances=1024\n"])

    def test_our_own_file_is_replaced_without_a_backup(self):
        self.run_script("install", max_user_instances=512)
        self.run_script("install")
        self.assertFalse((self.root / "var/backups").exists())

    def test_a_drifted_file_is_found_and_plan_shows_the_diff_and_sysctl(self):
        self.run_script("install")
        self.drop_in.write_text(self.drop_in.read_text().replace("2048", "64"))
        self.assertEqual(self.run_script("check").returncode, 3)
        plan = self.run_script("plan").stdout
        self.assertIn("-fs.inotify.max_user_instances=64", plan)
        self.assertIn("+fs.inotify.max_user_instances=2048", plan)
        self.assertIn("sysctl --system", plan)

    def test_bad_options_are_refused_before_anything_is_written(self):
        for options in (
            {"max_user_instances": 0},
            {"max_user_instances": "2048"},
            {"max_user_instances": True},
            {"bogus": 1},
        ):
            with self.subTest(options=options):
                result = self.run_script("install", **options)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(self.drop_in.exists())

    def test_install_without_root_is_refused(self):
        env = {**os.environ, "UBUNTU_TWEAK_MODULE_CONFIG": "{}"}
        if os.geteuid() == 0:
            self.skipTest("running as root")
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "install"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("must run as root", result.stderr)


class Converge(Staged):
    """The real (non-staged) path: `sysctl --system` runs when the file changed or the kernel disagrees with it."""

    def module(self, process):
        return script.InotifyLimits(self.root, 2048, process, is_root=True, staged=False)

    def apply(self, module):
        with redirect_stdout(io.StringIO()) as out:
            module.apply()
        return out.getvalue()

    def test_sysctl_runs_after_the_file_is_written_and_the_kernel_follows(self):
        sysctl = FakeSysctl(self.root)
        output = self.apply(self.module(sysctl))
        self.assertEqual(sysctl.calls, [["sysctl", "--system"]])
        self.assertIn("is now 2048", output)
        self.assertEqual(self.module(sysctl).findings()[-1].ok, True)

    def test_a_clean_file_with_a_wrong_kernel_value_runs_sysctl_again(self):
        sysctl = FakeSysctl(self.root)
        self.apply(self.module(sysctl))
        (self.root / script.LIVE_VALUE).write_text("128\n")  # a reboot that did not read the file
        sysctl.calls.clear()
        self.apply(self.module(sysctl))
        self.assertEqual(sysctl.calls, [["sysctl", "--system"]])

    def test_a_clean_machine_runs_nothing(self):
        sysctl = FakeSysctl(self.root)
        self.apply(self.module(sysctl))
        sysctl.calls.clear()
        self.assertIn("nothing to change", self.apply(self.module(sysctl)))
        self.assertEqual(sysctl.calls, [])

    def test_a_later_sysctl_file_that_wins_is_reported(self):
        with self.assertRaisesRegex(ModuleError, "sets it again"):
            self.apply(self.module(FakeSysctl(self.root, ignores=True)))

    def test_a_failing_sysctl_is_reported(self):
        with self.assertRaises(CommandError):
            self.apply(self.module(FakeSysctl(self.root, fails=True)))


class Manifest(unittest.TestCase):
    def test_the_manifest_names_the_script_and_tracks_the_file(self):
        manifest = load_manifest(MODULE)
        self.assertTrue(manifest.enabled)
        self.assertTrue(any(rule.coverage.value == "tracked" for rule in manifest.coverage))
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()

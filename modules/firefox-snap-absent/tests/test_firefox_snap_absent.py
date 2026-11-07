"""firefox-snap-absent against a fake `snap` on PATH and a scratch home (no real snapd, no root)."""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "firefox-snap-absent"
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.module_script import main  # noqa: E402
from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess  # noqa: E402

# STATE (a file) holds "installed" while the snap is there; every call is logged.
FAKE_SNAP = r"""#!/usr/bin/python3
import os, sys
state = os.environ["FAKE_SNAP_STATE"]
open(state + ".log", "a").write(" ".join(sys.argv[1:]) + "\n")
installed = os.path.exists(state)
if sys.argv[1:] == ["list", "firefox"]:
    if installed:
        print("Name Version Rev\nfirefox 1 1"); sys.exit(0)
    sys.stderr.write("error: no matching snaps installed\n"); sys.exit(1)
if sys.argv[1:3] == ["get", "system"]:
    sys.stderr.write('error: snap "core" has no "snapshots.automatic.retention" configuration option\\n'); sys.exit(1)
if sys.argv[1:] == ["remove", "firefox"]:
    os.unlink(state); sys.exit(0)
sys.exit(64)
"""
DEB_LAUNCHER = "userapp-Firefox-RMY9W3.desktop"
MIMEAPPS = f"[Default Applications]\nx-scheme-handler/http={DEB_LAUNCHER}\n"


def load_script():
    loader = importlib.machinery.SourceFileLoader("firefox_snap_absent_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


LIST = ("env", "LC_ALL=C", "LC_MESSAGES=C", "LANG=C", "snap", "list", "firefox")
INSTALLED = CommandResult(0, "Name Version Rev\nfirefox 1 1\n", "")
RETENTION = ("snap", "get", "system", "snapshots.automatic.retention")
UNSET = CommandResult(1, "", 'error: snap "core" has no "snapshots.automatic.retention" configuration option\n')
NOT_INSTALLED = CommandResult(1, "", "error: no matching snaps installed\n")


class RootBranchTests(unittest.TestCase):
    """`install` as root and the discovery errors, against a FakeProcess (the subprocess tests run as the test user)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.launcher = self.home / ".local/share/applications/firefox_firefox.desktop"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("[Desktop Entry]\n")
        self.script = load_script()

    def module(self, process: FakeProcess, *, is_root: bool):
        return self.script.FirefoxSnapAbsent(process, self.home, is_root=is_root)

    def run_main(self, module, command: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        return main(module, [command], out=out, err=err), out.getvalue(), err.getvalue()

    def test_root_install_runs_exactly_snap_remove_firefox_without_purge_and_drops_the_launcher(self) -> None:
        process = FakeProcess({LIST: INSTALLED, RETENTION: UNSET})

        status, _, _ = self.run_main(self.module(process, is_root=True), "install")

        self.assertEqual(status, 0)
        self.assertEqual([argv for argv, _ in process.executed], [("snap", "remove", "firefox")])
        self.assertFalse(self.launcher.exists())

    def test_disabled_automatic_snapshots_get_an_explicit_snapshot_before_the_removal(self) -> None:
        process = FakeProcess({LIST: INSTALLED, RETENTION: CommandResult(0, "no\n", "")})
        module = self.module(process, is_root=True)

        plan = self.run_main(module, "plan")[1].splitlines()
        self.run_main(module, "install")

        self.assertTrue(plan[0].startswith("snap save firefox"))
        self.assertEqual(process.queries.count(RETENTION), 1)  # the plan itself asks nothing privileged
        self.assertEqual(
            [argv for argv, _ in process.executed], [("snap", "save", "firefox"), ("snap", "remove", "firefox")]
        )

    def test_an_unreadable_snapshot_policy_stops_the_removal(self) -> None:
        process = FakeProcess({LIST: INSTALLED, RETENTION: CommandResult(124, "", "snap: timed out after 60 s")})

        status, _, err = self.run_main(self.module(process, is_root=True), "install")

        self.assertEqual(status, 1)
        self.assertIn("snapshots.automatic.retention", err)
        self.assertEqual(process.executed, [])

    def test_an_unset_snapshot_policy_is_the_default_and_removes_without_a_save(self) -> None:
        unset = CommandResult(1, "", 'error: snap "core" has no "snapshots.automatic.retention" configuration option\n')
        process = FakeProcess({LIST: INSTALLED, RETENTION: unset})

        self.run_main(self.module(process, is_root=True), "install")

        self.assertEqual([argv for argv, _ in process.executed], [("snap", "remove", "firefox")])

    def test_absence_is_recognised_whatever_the_locale_because_the_query_forces_C(self) -> None:
        process = FakeProcess({LIST: NOT_INSTALLED})
        self.launcher.unlink()

        self.assertEqual(self.run_main(self.module(process, is_root=True), "check")[0], 0)
        self.assertEqual(process.queries, [LIST])

    def test_a_failed_snap_remove_is_an_error(self) -> None:
        def fail(argv, _input):
            raise CommandError("snap remove firefox: exited with status 1")

        process = FakeProcess({LIST: INSTALLED, RETENTION: UNSET}, effects=fail)

        status, _, err = self.run_main(self.module(process, is_root=True), "install")

        self.assertEqual(status, 1)
        self.assertIn("snap remove firefox", err)

    def test_non_root_never_runs_snap_remove(self) -> None:
        process = FakeProcess({LIST: INSTALLED, RETENTION: UNSET})

        status, out, _ = self.run_main(self.module(process, is_root=False), "install")

        self.assertEqual(status, 10)
        self.assertIn("ubuntu-tweak apply --only firefox-snap-absent --commit", out)
        self.assertNotIn("sudo ubuntu-tweak", out)
        self.assertEqual(process.executed, [])

    def test_snap_failures_other_than_not_installed_are_errors(self) -> None:
        for result in (
            CommandResult(1, "", "error: cannot communicate with server: connection refused"),
            CommandResult(124, "", "snap: timed out after 60 s"),
            CommandResult(126, "", "snap: cannot run: Permission denied"),
        ):
            for command in ("check", "plan", "install"):
                with self.subTest(code=result.returncode, command=command):
                    process = FakeProcess({LIST: result})
                    status, out, err = self.run_main(self.module(process, is_root=True), command)
                    self.assertEqual((status, out), (1, ""))
                    self.assertIn("snap list firefox", err)
                    self.assertEqual(process.executed, [])
                    self.assertTrue(self.launcher.exists())

    def test_no_snap_binary_and_no_matching_snap_are_both_absent(self) -> None:
        self.launcher.unlink()
        for result in (CommandResult(127, "", "snap: command not found"), NOT_INSTALLED):
            process = FakeProcess({LIST: result})
            self.assertEqual(self.run_main(self.module(process, is_root=True), "check"), (0, "", ""))


class FirefoxSnapAbsentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "bin").mkdir()
        fake = self.tmp / "bin/snap"
        fake.write_text(FAKE_SNAP)
        fake.chmod(0o755)
        self.state = self.tmp / "snap-installed"
        self.home = self.tmp / "home"
        self.apps = self.home / ".local/share/applications"
        self.apps.mkdir(parents=True)
        self.launcher = self.apps / "firefox_firefox.desktop"
        self.deb_launcher = self.apps / DEB_LAUNCHER
        self.deb_launcher.write_text("[Desktop Entry]\nExec=/usr/bin/firefox\n")
        self.mimeapps = self.home / ".config/mimeapps.list"
        self.mimeapps.parent.mkdir()
        self.mimeapps.write_text(MIMEAPPS)

    def run_module(self, command: str, options: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            PATH=f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            FAKE_SNAP_STATE=str(self.state),
            FIREFOX_SNAP_ABSENT_HOME=str(self.home),
        )
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        if options is not None:
            env["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps(options)
        return subprocess.run([sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True)

    def snap_calls(self) -> list[str]:
        log = Path(str(self.state) + ".log")
        return log.read_text().splitlines() if log.exists() else []

    def test_manifest_is_off_by_default_root_with_a_rootless_fallback(self) -> None:
        manifest = load_manifest(MODULE)
        self.assertFalse(manifest.enabled)
        self.assertTrue(manifest.source.root)

    def test_no_snap_and_no_launcher_is_no_drift(self) -> None:
        result = self.run_module("check")

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.run_module("plan").stdout, "")

    def test_an_installed_snap_is_drift_and_the_plan_removes_without_purge(self) -> None:
        self.state.write_text("installed")
        self.launcher.write_text("[Desktop Entry]\n")

        check = self.run_module("check")
        plan = self.run_module("plan").stdout.splitlines()

        self.assertEqual(check.returncode, 3)
        self.assertIn("the firefox snap is not installed", check.stdout)
        self.assertTrue(plan[0].startswith("snap save firefox "))
        self.assertEqual(plan[1].split("#")[0].split(), ["snap", "remove", "firefox"])
        self.assertIn("snap restore", plan[1])
        self.assertEqual(plan[2], f"rm {self.launcher}")

    def test_a_stale_launcher_alone_is_drift(self) -> None:
        self.launcher.write_text("[Desktop Entry]\n")

        self.assertEqual(self.run_module("check").returncode, 3)

    def test_the_tests_run_as_non_root_so_install_removes_the_launcher_and_asks_for_root_for_the_snap(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the rootless path needs a non-root caller")
        self.state.write_text("installed")
        self.launcher.write_text("[Desktop Entry]\n")

        result = self.run_module("install")

        self.assertEqual(result.returncode, 10)
        self.assertIn("ubuntu-tweak apply --only firefox-snap-absent --commit", result.stdout)
        self.assertFalse(self.launcher.exists())
        self.assertTrue(self.state.exists())
        self.assertEqual([c for c in self.snap_calls() if c.startswith("remove")], [])

    def test_a_rootless_install_is_complete_when_only_the_launcher_is_left(self) -> None:
        self.launcher.write_text("[Desktop Entry]\n")

        self.assertEqual(self.run_module("install").returncode, 0)
        self.assertEqual(self.run_module("check").returncode, 0)

    def test_the_deb_launcher_and_the_default_browser_are_never_touched(self) -> None:
        self.launcher.write_text("[Desktop Entry]\n")

        self.run_module("install")

        self.assertTrue(self.deb_launcher.exists())
        self.assertEqual(self.mimeapps.read_text(), MIMEAPPS)

    def test_as_a_non_root_user_only_snap_list_is_ever_run(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("the rootless path needs a non-root caller")
        self.state.write_text("installed")
        self.run_module("check")
        self.run_module("plan")
        self.run_module("install")

        self.assertEqual({c for c in self.snap_calls()}, {"list firefox"})

    def test_unknown_options_are_refused(self) -> None:
        self.assertEqual(self.run_module("check", {"purge": True}).returncode, 1)

    def test_a_machine_without_snap_is_satisfied(self) -> None:
        (self.tmp / "bin/snap").unlink()
        env = dict(os.environ, PATH=str(self.tmp / "bin"), FIREFOX_SNAP_ABSENT_HOME=str(self.home))
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)

        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "check"], env=env, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

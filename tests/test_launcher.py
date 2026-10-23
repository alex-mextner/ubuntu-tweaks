"""bin/ubuntu-tweak: re-exec under the system interpreter when `gi` is missing; cli.main's message.

The launcher is run as a subprocess. `gi` is made unimportable for the interpreter under test
with a ``PYTHONPATH`` directory holding a ``gi`` package that raises ImportError, and
``UBUNTU_TWEAK_SYSTEM_PYTHON`` points at a stub "system interpreter" (a shell script that
echoes its argv), so the tests do not depend on what this machine has installed.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks.cli import main
from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.errors import ExitCode

LAUNCHER = REPO_ROOT / "bin" / "ubuntu-tweak"


class LauncherTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        write(self.tmp / "nogi" / "gi" / "__init__.py", 'raise ImportError("no gi here")\n')
        stub = write(self.tmp / "system-python", '#!/bin/sh\necho "stub:$*"\n')
        stub.chmod(0o755)
        self.stub = stub

    def run_launcher(self, *args: str, script: Path = LAUNCHER, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {k: v for k, v in os.environ.items() if k != "UBUNTU_TWEAK_REEXEC"}
        full_env["PYTHONPATH"] = str(self.tmp / "nogi")
        full_env.update(env)
        return subprocess.run(
            [sys.executable, "-B", str(script), *args], env=full_env, capture_output=True, text=True, check=False
        )

    def test_missing_gi_reexecs_under_the_system_interpreter(self) -> None:
        result = self.run_launcher("list", "--only", "x", UBUNTU_TWEAK_SYSTEM_PYTHON=str(self.stub))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"stub:{LAUNCHER} list --only x")

    def test_reexec_through_a_symlink_keeps_the_invoked_path(self) -> None:
        link = self.tmp / "bin" / "ubuntu-tweak"
        link.parent.mkdir()
        link.symlink_to(LAUNCHER)
        result = self.run_launcher("list", script=link, UBUNTU_TWEAK_SYSTEM_PYTHON=str(self.stub))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"stub:{link} list")

    def test_without_gi_anywhere_the_launcher_still_runs_gi_free_commands(self) -> None:
        result = self.run_launcher("--help", UBUNTU_TWEAK_SYSTEM_PYTHON=str(self.tmp / "no-such-python"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_a_system_interpreter_without_gi_ends_in_the_command_not_a_loop(self) -> None:
        again = write(self.tmp / "again-python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        again.chmod(0o755)  # a "system python" that is just as gi-less as the first one
        result = self.run_launcher("--help", UBUNTU_TWEAK_SYSTEM_PYTHON=str(again))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_a_stale_guard_value_from_the_shell_does_not_block_the_reexec(self) -> None:
        result = self.run_launcher("list", UBUNTU_TWEAK_SYSTEM_PYTHON=str(self.stub), UBUNTU_TWEAK_REEXEC="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"stub:{LAUNCHER} list")


class MissingGiMessageTest(unittest.TestCase):
    def test_a_command_that_needs_gi_ends_with_the_package_to_install(self) -> None:
        parser = mock.Mock()
        parser.parse_args.side_effect = ModuleNotFoundError("No module named 'gi'", name="gi")
        stderr = io.StringIO()
        with mock.patch("ubuntu_tweaks.cli.build_parser", return_value=parser), contextlib.redirect_stderr(stderr):
            code = main(["capture"])
        self.assertEqual(code, int(ExitCode.INTERNAL))
        self.assertIn("sudo apt install python3-gi", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_another_missing_module_is_still_a_traceback(self) -> None:
        parser = mock.Mock()
        parser.parse_args.side_effect = ModuleNotFoundError("No module named 'other'", name="other")
        stderr = io.StringIO()
        with mock.patch("ubuntu_tweaks.cli.build_parser", return_value=parser), contextlib.redirect_stderr(stderr):
            code = main(["capture"])
        self.assertEqual(code, int(ExitCode.INTERNAL))
        self.assertIn("Traceback", stderr.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("gi"), "PyGObject is not installed for this interpreter")
    def test_with_gi_it_runs_in_place(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, "-B", str(LAUNCHER), "--help"], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""The test package keeps scratch directories out of the real HOME (``tests/__init__.py``).

A path under the real home directory is shown as ``~/...`` by the planners, so a test that compares the text of a path
it built in a temporary directory fails when ``TMPDIR`` is inside the home (a server checkout under ``~/work``, a
developer's ``~/tmp``). Each case imports the package in a child interpreter with its own HOME and TMPDIR.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ubuntu_tweaks.context import REPO_ROOT


class ScratchOutsideHome(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="scratch-outside-home-")).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.home = self.scratch / "home"
        self.home.mkdir()

    def temp_dir_seen_by_the_test_package(self, tmpdir: Path) -> Path:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TMPDIR": str(tmpdir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        done = subprocess.run(
            [sys.executable, "-B", "-c", "import tests, tempfile; print(tempfile.gettempdir())"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        self.assertEqual(done.returncode, 0, done.stderr)
        return Path(done.stdout.strip())

    def test_a_tmpdir_inside_the_home_is_replaced_by_one_outside_it(self) -> None:
        inside = self.home / "work/tmp"
        inside.mkdir(parents=True)
        seen = self.temp_dir_seen_by_the_test_package(inside).resolve()
        self.assertFalse(seen.is_relative_to(self.home), f"{seen} is inside {self.home}")
        self.assertTrue(seen.is_dir())

    def test_a_tmpdir_outside_the_home_is_kept(self) -> None:
        outside = self.scratch / "elsewhere"
        outside.mkdir()
        self.assertEqual(self.temp_dir_seen_by_the_test_package(outside).resolve(), outside)


if __name__ == "__main__":
    unittest.main()

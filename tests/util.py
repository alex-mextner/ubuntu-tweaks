"""Helpers shared by the test modules."""

import tempfile
import unittest
from pathlib import Path


def write(path: Path, text: str = "") -> Path:
    """Write ``text`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TempDirCase(unittest.TestCase):
    """A test case with ``self.tmp``, a fresh temporary directory removed afterwards."""

    def setUp(self) -> None:
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.tmp = Path(handle.name)

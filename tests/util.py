"""Helpers shared by the test modules."""

import contextlib
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from ubuntu_tweaks.identity import Location


@contextlib.contextmanager
def no_dotfiles_discovery() -> Iterator[None]:
    """``cli.main`` finds the default profile through ``gh`` and the real $HOME: tests that run it without
    ``--profile`` must not."""
    with mock.patch("ubuntu_tweaks.identity.locate", return_value=Location(Path("/nonexistent/.files/ubuntu"))):
        yield


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

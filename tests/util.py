"""Helpers shared by the test modules."""

import contextlib
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.elevation import Host
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


def host(
    *,
    euid: int = 1000,
    terminal: bool = False,
    askpass: bool = False,
    graphical: bool = False,
    no_new_privs: bool = False,
    tools: frozenset[str] = frozenset({"sudo"}),
) -> Host:
    """A non-root machine with sudo and nothing else; ``euid=0`` makes it root."""
    return Host(
        euid=euid, terminal=terminal, askpass=askpass, graphical=graphical, no_new_privs=no_new_privs, tools=tools
    )


class Said:
    """Collects what a ``RootSession`` prints before it asks."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)


class RootStep(Action):
    """A root action that converges and notes ``run:<name>`` in ``events`` when it runs. With ``ready`` it is
    satisfied (so absent from a plan) until that path exists, like a step that depends on a cloned profile."""

    kind = "system"
    phase = Phase.SYSTEM
    needs_root = True

    def __init__(
        self, events: list[str], name: str = "timezone", *, ready: Path | None = None, scope: str | None = None
    ) -> None:
        self._events = events
        self._name = name
        self._ready = ready
        self._done = False
        self.scope = scope

    def describe(self) -> str:
        return f"set {self._name}"

    def check(self) -> bool:
        if self._ready is not None and not self._ready.exists():
            return True
        return self._done

    def run(self) -> None:
        self._events.append(f"run:{self._name}")
        self._done = True


class TempDirCase(unittest.TestCase):
    """A test case with ``self.tmp``, a fresh temporary directory removed afterwards."""

    def setUp(self) -> None:
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.tmp = Path(handle.name)

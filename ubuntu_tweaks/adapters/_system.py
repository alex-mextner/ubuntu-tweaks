"""The machine an adapter reads: one seam for the process runner, ``$HOME``, ``/`` and the settings backend.

Accessed via: every ``adapters/<name>.py`` (``system()``) and the tests (``use(System(...))``).

Assumptions: adapters are discovered as plain modules and called as ``discover(ctx)``, so what they read cannot be a
parameter; it is this module's current ``System``. The default is the real machine (``Process``, ``Path.home()``,
``/``, the backend ``reconcile.open_backend`` opens); a test installs a scratch one for the duration of a block, so
nothing real is read or written. Names starting with ``_`` are skipped by adapter discovery.

Public API: ``System(process, home, root, backend)`` with ``settings(ctx)`` (the backend, opened on first use),
``system() -> System`` and ``use(system)`` (a context manager that installs ``system``; the coverage command
installs the current ``System`` for a whole scan).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.process import Process
from ubuntu_tweaks.settings import SettingsBackend


@dataclass
class System:
    process: Process = field(default_factory=Process)
    home: Path = field(default_factory=Path.home)
    root: Path = Path("/")
    backend: SettingsBackend | None = None

    def settings(self, ctx: Context) -> SettingsBackend:
        if self.backend is None:
            from ubuntu_tweaks import reconcile

            self.backend = reconcile.open_backend(ctx)
        return self.backend


_current: System | None = None


def system() -> System:
    """The installed ``System``; outside ``use`` a fresh real one (a scan installs one so its adapters share it)."""
    return _current if _current is not None else System()


@contextlib.contextmanager
def use(replacement: System) -> Iterator[System]:
    global _current
    previous = _current
    _current = replacement
    try:
        yield replacement
    finally:
        _current = previous

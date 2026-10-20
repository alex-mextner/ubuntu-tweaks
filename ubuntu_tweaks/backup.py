"""Backups taken before a symlink replaces something in ``$HOME`` (safety invariant 5).

Accessed via: ``planners/files.py`` before it replaces a real file (or a foreign symlink) with a
link into the profile; slice A's extension-link action uses the same layout.

Assumptions: the original stays where it is until the caller replaces it; ``backup`` only copies.
The copy lives at ``<state>/ubuntu-tweaks/backup/<UTC timestamp>/<absolute original path>``
where ``<state>`` is ``$XDG_STATE_HOME`` or ``~/.local/state``. A symlink is stored as a
symlink (its target is not followed, so a link to a huge tree is cheap and still recorded); a
directory is copied recursively with its links preserved. When the same path is backed up twice
in the same second the second copy gets a ``.1``, ``.2``… suffix on the timestamp directory.

Public API: ``backup(path, *, now, state_home=None) -> Path`` (the copy), ``BACKUP_RELPATH``
(``ubuntu-tweaks/backup``), ``default_state_home(env=None)``.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

BACKUP_RELPATH = Path("ubuntu-tweaks/backup")


def default_state_home(env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get("XDG_STATE_HOME")
    return Path(configured) if configured else Path.home() / ".local/state"


def backup(path: Path, *, now: datetime, state_home: Path | None = None) -> Path:
    original = Path(os.path.abspath(path))
    if not (original.is_symlink() or original.exists()):
        raise FileNotFoundError(f"nothing to back up at {original}")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = (state_home if state_home is not None else default_state_home()) / BACKUP_RELPATH
    destination = root / stamp / original.relative_to(original.anchor)
    counter = 0
    while destination.exists() or destination.is_symlink():
        counter += 1
        destination = root / f"{stamp}.{counter}" / original.relative_to(original.anchor)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if original.is_dir() and not original.is_symlink():
        shutil.copytree(original, destination, symlinks=True)
    else:
        shutil.copy2(original, destination, follow_symlinks=False)
    return destination

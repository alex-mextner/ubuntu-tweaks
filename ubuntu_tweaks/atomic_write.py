"""Atomic file replacement: the one helper every profile writer uses.

Accessed via: ``capture/commit.py`` (``capture --commit``) and ``commands/bundle_move.py`` /
``commands/note.py``.

Assumptions: the target's directory may not exist yet (it is created); the temporary file lives in the
same directory so ``os.replace`` is atomic and an interrupted write never leaves a truncated file; the
mode of ``mode_from`` (usually the file being replaced) is kept.

Public API: ``atomic_write(path, data, mode_from=None)``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes, mode_from: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        if mode_from is not None:
            shutil.copymode(mode_from, temp_name)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise

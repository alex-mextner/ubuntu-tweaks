"""Writes that survive a power cut: content synced, name moved into place atomically, directory synced.

Accessed via: ``archive`` (the gzipped report must be on disk before the original is removed) and ``publisher``
(a pending event must be on disk before the report is archived).

Public API: ``fsync_dir(path)`` and ``write_durably(path, data, mode=0o600)`` (temporary file next to ``path``,
``fsync``, ``rename``, ``fsync`` of the directory; a failure leaves no temporary file behind).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def short_temporary(target: Path) -> Path:
    """A temporary name next to ``target`` that is short whatever the length of the target's name."""
    return target.with_name(".tmp-" + hashlib.sha1(os.fsencode(target.name)).hexdigest()[:16])


def write_durably(path: Path, data: bytes, mode: int = 0o600) -> None:
    temporary = short_temporary(path)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)

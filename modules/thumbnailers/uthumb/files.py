"""Bounded reads: a thumbnailer must never load an unbounded file or hang on a hostile one."""

from __future__ import annotations

from pathlib import Path

from .errors import Unsupported

MAX_TEXT_BYTES = 96 * 1024 * 1024
MAX_BINARY_BYTES = 256 * 1024 * 1024


def read_bytes(path: str | Path, limit: int = MAX_TEXT_BYTES) -> bytes:
    path = Path(path)
    if path.stat().st_size > limit:
        raise Unsupported(f"larger than {limit >> 20} MiB")
    return path.read_bytes()


def read_text(path: str | Path, limit: int = MAX_TEXT_BYTES) -> str:
    return read_bytes(path, limit).decode("utf-8", "replace")


def read_head(path: str | Path, count: int) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(count)


def read_tail(path: str | Path, count: int) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - count))
        return handle.read(count)


def read_member(package, name: str, limit: int = MAX_BINARY_BYTES) -> bytes:
    """A zip member's bytes, never more than ``limit``: the size a zip declares is not trusted, the stream is bounded."""
    try:
        with package.open(name) as member:
            data = member.read(limit + 1)
    except KeyError as error:
        raise Unsupported(f"no {name} in the archive") from error
    if len(data) > limit:
        raise Unsupported(f"{name} is larger than {limit >> 20} MiB")
    return data

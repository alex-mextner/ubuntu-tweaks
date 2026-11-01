"""Append-only JSONL audit of every event the ingesters handled.

Accessed via: ``apport_source``, ``spool``, ``publisher`` and ``cli`` append; ``cli list`` / ``replay`` read.

One line per decision, ASCII JSON: ``{"ts", "decision", ...}`` and, for an event, ``event_id`` (the agent-sub id,
or a ``pending-`` id while it waits in the local retry queue), ``report`` (the file name), ``status`` and the
sanitized ``event`` payload that ``replay`` re-publishes. The file is never rewritten or truncated; a write that
dies half way leaves one damaged line (the next append starts on a new line) that readers skip.

Public API: ``Audit(path, clock)`` with ``append(**fields) -> dict``, ``records() -> list[dict]`` (damaged lines
are skipped) and ``find(event_id) -> dict | None`` (exact id, else a unique prefix).
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

Record = dict[str, object]


class Audit:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock

    def append(self, **fields: object) -> Record:
        record: Record = {"ts": round(self._clock(), 3), **fields}
        # ensure_ascii: a file name from a shared directory may hold bytes that are not UTF-8 (lone surrogates)
        line = (json.dumps(record, default=str) + "\n").encode("ascii")
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._write_whole(fd, self._after_torn_tail(fd) + line)
            os.fsync(fd)
        finally:
            os.close(fd)
        return record

    @staticmethod
    def _after_torn_tail(fd: int) -> bytes:
        """A newline first when an earlier write died half way: the fragment stays one damaged line of its own."""
        size = os.fstat(fd).st_size
        return b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""

    @staticmethod
    def _write_whole(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]

    def records(self) -> list[Record]:
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found: list[Record] = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
        return found

    def find(self, event_id: str) -> Record | None:
        """The newest record carrying this event id; a unique prefix is accepted."""
        with_id = [r for r in self.records() if isinstance(r.get("event_id"), str)]
        exact = [r for r in with_id if r["event_id"] == event_id]
        if exact:
            return exact[-1]
        ids = {str(r["event_id"]) for r in with_id if str(r["event_id"]).startswith(event_id)}
        if len(ids) != 1:
            return None
        only = ids.pop()
        return [r for r in with_id if r["event_id"] == only][-1]

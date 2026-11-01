"""Parse the header of an apport ``.crash`` report without ever holding its core dump.

Accessed via: ``apport_source`` (one parse per readable report) and the tests.

An apport report is ``Key: value`` lines, a value continuing on following lines that start with one space. A big
report (96 MB here) is mostly ``CoreDump`` (base64 of gzip), ``ProcMaps``, ``ShellJournal`` and ``Stacktrace``:
the file is streamed in bounded chunks and only the fields in ``WANTED`` are kept (each capped); every other
field, the core dump above all, is read past and dropped. The file is opened with ``O_NOATIME`` where the kernel
allows it so that reading does not make apport consider the report "seen" (``atime > mtime``).

Public API:

- ``CrashReport(fields, journal_errors, shell_journal, skipped)``; ``fields`` maps a wanted key to its text.
- ``parse_report(path) -> CrashReport``; ``ReportError`` when the file is not an apport report.
- ``first_function(stacktrace_top) -> str``: the top frame's function (``unknown`` when none is named).
- ``open_noatime(path) -> BinaryIO``.
"""

from __future__ import annotations

import errno
import os
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

WANTED = frozenset(
    {
        "ProblemType",
        "ExecutablePath",
        "Signal",
        "SignalName",
        "SegvReason",
        "SegvAnalysis",
        "StacktraceTop",
        "ProcCmdline",
        "Package",
        "PackageArchitecture",
        "UnreportableReason",
        "Date",
        "Title",
        "Tags",
        "monitors.xml",
    }
)
JOURNAL_FIELDS = ("JournalErrors", "ShellJournal")
FIELD_CAP = 16_384  # characters kept of one wanted field
JOURNAL_LINES = 300  # the last lines of a journal field kept for the excerpt
JOURNAL_LINE_CAP = 4096  # the sanitizer masks a whole line and only then clips it to what it shows
CHUNK = 65_536
_FRAME = re.compile(r"^\s*(?:#\d+\s+)?(?:0x[0-9a-fA-F]+\s+in\s+)?(?P<function>[^\s(]+)")
_NO_NAME = frozenset({"???", "??", "?", "()"})


class ReportError(Exception):
    """The file is not an apport crash report (no ``ProblemType`` header)."""


@dataclass(frozen=True)
class CrashReport:
    fields: dict[str, str]
    journal_errors: str
    shell_journal: str
    skipped: int  # number of other fields read past and dropped


def open_noatime(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(path, flags)
    except PermissionError:  # O_NOATIME is only allowed on files the caller owns
        fd = os.open(path, flags & ~getattr(os, "O_NOATIME", 0))
    except OSError as error:
        if error.errno != errno.EPERM:
            raise
        fd = os.open(path, flags & ~getattr(os, "O_NOATIME", 0))
    return os.fdopen(fd, "rb")


def _lines(stream: BinaryIO) -> Iterator[tuple[bytes, bool]]:
    """Chunks of at most ``CHUNK`` bytes; the flag is True when the chunk starts a line."""
    at_start = True
    while True:
        chunk = stream.readline(CHUNK)
        if not chunk:
            return
        yield chunk, at_start
        at_start = chunk.endswith(b"\n")


def _decode(chunk: bytes) -> str:
    return chunk.decode("utf-8", "replace").rstrip("\n")


class _Collector:
    def __init__(self) -> None:
        self.parts: dict[str, list[str]] = {}
        self.sizes: dict[str, int] = {}
        self.journals: dict[str, deque[str]] = {name: deque(maxlen=JOURNAL_LINES) for name in JOURNAL_FIELDS}
        self.skipped = 0
        self.current: str | None = None

    def header(self, key: str, first: str) -> None:
        self.current = key
        if key in WANTED:
            self.parts[key] = [first]
            self.sizes[key] = len(first)
        elif key in self.journals:
            self.journals[key].clear()
            if first.strip():
                self.journals[key].append(first[:JOURNAL_LINE_CAP])
        else:
            self.skipped += 1

    def line(self, text: str, starts_line: bool) -> None:
        key = self.current
        if key in self.journals:
            if starts_line:
                self.journals[key].append(text[:JOURNAL_LINE_CAP])
            return
        if key not in self.parts or self.sizes[key] >= FIELD_CAP:
            return
        self.sizes[key] += len(text) + 1
        if starts_line:
            self.parts[key].append(text)
        else:
            self.parts[key][-1] += text

    def report(self) -> CrashReport:
        fields = {key: "\n".join(parts).strip()[:FIELD_CAP] for key, parts in self.parts.items()}
        journals = {name: "\n".join(lines) for name, lines in self.journals.items()}
        return CrashReport(fields, journals["JournalErrors"], journals["ShellJournal"], self.skipped)


def parse_report(path: Path) -> CrashReport:
    collector = _Collector()
    with open_noatime(path) as stream:
        for chunk, starts_line in _lines(stream):
            if starts_line and not chunk.startswith(b" ") and b":" in chunk:
                key, _, rest = _decode(chunk).partition(":")
                collector.header(key, rest[1:] if rest.startswith(" ") else rest)
            elif chunk.startswith(b" ") and starts_line:
                collector.line(_decode(chunk)[1:], True)
            else:
                collector.line(_decode(chunk), starts_line)
    report = collector.report()
    if not report.fields.get("ProblemType"):
        raise ReportError(f"{path.name}: no ProblemType header, not an apport report")
    return report


def first_function(stacktrace_top: str) -> str:
    for line in stacktrace_top.splitlines():
        match = _FRAME.match(line)
        if match and match["function"] not in _NO_NAME:
            return match["function"]
    return "unknown"

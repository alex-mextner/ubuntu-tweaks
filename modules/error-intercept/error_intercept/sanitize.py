"""What may leave a crash report: no environment, no memory maps, no secrets, no home directory, bounded sizes.

Accessed via: ``apport_source`` (every report), ``spool`` (window titles) and the tests.

The parser already keeps only a few fields; this is the second line: it drops the fields that must never travel
(``DROP_FIELDS``), masks tokens and secret-looking ``name=value`` pairs, writes ``/home/<user>/...`` as ``~/...``
(the rest of the path stays: a path is often the point of a crash), strips control characters and bounds every
field. The monitors.xml symlink-loop error (``[Errno 40] Too many levels of symbolic links``) is repeated in every
shell report and says nothing about the crash, so it is replaced by ONE fact (``facts``) whatever number of lines
carried it.

Public API:

- ``DROP_FIELDS``: names never copied (``ProcEnviron``, ``ProcMaps``, ``CoreDump``, ...).
- ``mask(text, home=None) -> str``, ``clip(text, limit) -> str``, ``clean_line(text, limit) -> str``,
  ``printable(name) -> str`` (a file name with non-UTF-8 bytes escaped).
- ``sanitize_report(report, home) -> SafeReport`` with ``fields`` (str to str), ``journal`` (bounded excerpt) and
  ``facts`` (one-line facts such as the monitors.xml loop).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from error_intercept.crash_report import CrashReport

DROP_FIELDS = frozenset(
    {
        "ProcEnviron",
        "ProcMaps",
        "ProcStatus",
        "ProcCwd",
        "ProcCpuinfoMinimal",
        "OpenFds",
        "Dependencies",
        "CoreDump",
        "Registers",
        "Disassembly",
        "Stacktrace",
        "ThreadStacktrace",
        "GsettingsChanges",
        "UserGroups",
        "Uname",
        "InstallationDate",
        "InstallationMedia",
        "CurrentDesktop",
        "DisplayManager",
    }
)
FIELD_LIMITS = {
    "SegvAnalysis": 600,
    "StacktraceTop": 800,
    "ProcCmdline": 400,
    "UnreportableReason": 400,
    "Title": 200,
    "Tags": 200,
}
DEFAULT_LIMIT = 200
JOURNAL_LIMIT = 2000
JOURNAL_LINE_LIMIT = 400
MAX_JOURNAL_LINES = 15
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HOME = re.compile(r"/home/[^/\s'\"):;,]+")
_MONITORS_LOOP = re.compile(r"(?i)monitors\.xml.*errno 40|errno 40.*monitors\.xml")
MONITORS_FACT = "monitors.xml is unreadable: symlink loop (Errno 40)"
# A value is masked whole: a quoted string (spaces and escaped quotes included), a gdb ``0xADDR "string"`` pair, or
# a word whose spaces are escaped with backslashes (apport writes ``--password correct\\ horse``).
_VALUE = (
    r"""(?:0x[0-9a-fA-F]+\s+(?=["']))?"""
    r"""(?:"(?:[^"\\\n]|\\.)*(?:"|(?=\n)|\Z)"""  # a quote that is never closed (a clipped line) runs to the end of the line
    r"""|'(?:[^'\\\n]|\\.)*(?:'|(?=\n)|\Z)"""
    r"""|(?:\\.|[^\s\\])+)"""
)
_DONE = r"(?!\[masked)"  # a value an earlier pattern masked is not masked again (it would eat what follows)
_SECRET_WORDS = r"(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
_BLOB = re.compile(r"[A-Za-z0-9+/]{48,}={0,2}")  # key material or an opaque token on a journal line
_SECRET_PATTERNS = (
    # whole PEM blocks first: later patterns would eat the BEGIN marker and leave the body behind
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
        "[masked-private-key]",
    ),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@"), r"\1[masked]@"),  # user:password@ of any URL
    # curl -u user:pass, -uuser:pass, -U, --user user:pass, --proxy-user ...
    (re.compile(rf"(?i)(\s(?:-[uU]|--(?:proxy-)?user)(?:=|\s+|(?=[^\s-])))(?!\[masked\]){_VALUE}"), r"\1[masked]"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\n]+"), r"\1: [masked]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 [masked]"),
    (re.compile(rf"(?i)(--?[a-z0-9_-]*{_SECRET_WORDS}[a-z0-9_-]*)(?:=|\s+){_DONE}{_VALUE}"), r"\1=[masked]"),
    (re.compile(rf"(?i)\b([a-z0-9_]*{_SECRET_WORDS}[a-z0-9_]*)([\"']?)\s*[=:]\s*{_DONE}{_VALUE}"), r"\1\2=[masked]"),
    (re.compile(rf"(?i)\b(password|passwd|pwd)([\"']?)\s*[=:]\s*{_DONE}{_VALUE}"), r"\1\2=[masked]"),
    (
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b"
        ),
        "[masked-token]",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[masked-jwt]"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "[masked-bot-token]"),
)


@dataclass(frozen=True)
class SafeReport:
    fields: dict[str, str]
    journal: str
    facts: list[str]


def printable(name: str) -> str:
    """A file name safe to store and send: bytes that are not UTF-8 (a shared directory) become ``\\udcNN`` escapes."""
    return name.encode("utf-8", "backslashreplace").decode("utf-8")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def mask(text: str, home: Path | None = None) -> str:
    """Secrets and the home directory masked; control characters removed."""
    text = _CONTROL.sub("", text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if home is not None and str(home) not in ("", "/"):
        text = text.replace(str(home), "~")
    return _HOME.sub("~", text)


def clean_line(text: str, limit: int) -> str:
    return clip(" ".join(mask(text).split()), limit)


def _without_monitors_loop(text: str) -> tuple[str, int]:
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        if _MONITORS_LOOP.search(line):
            dropped += 1
        else:
            kept.append(line)
    return "\n".join(kept), dropped


def _excerpt(journal: str, home: Path) -> tuple[str, int]:
    """The last lines of a journal field, secrets masked over the WHOLE field (a PEM block spans lines) first."""
    text, dropped = _without_monitors_loop(mask(journal, home))
    lines = [
        clip(_BLOB.sub("[masked-blob]", line).strip(), JOURNAL_LINE_LIMIT) for line in text.splitlines() if line.strip()
    ]
    return clip("\n".join(lines[-MAX_JOURNAL_LINES:]), JOURNAL_LIMIT), dropped


def sanitize_report(report: CrashReport, home: Path) -> SafeReport:
    fields: dict[str, str] = {}
    looped = 0
    for name, value in report.fields.items():
        if name in DROP_FIELDS:
            continue
        text, dropped = _without_monitors_loop(value)
        looped += dropped
        if text.strip():
            limit = FIELD_LIMITS.get(name, DEFAULT_LIMIT)
            fields[name] = clip(mask(text, home).strip(), limit)
    pieces: list[str] = []
    for journal in (report.journal_errors, report.shell_journal):
        piece, dropped = _excerpt(journal, home)
        looped += dropped
        if piece:
            pieces.append(piece)
    excerpt = clip("\n".join(pieces)[-JOURNAL_LIMIT:], JOURNAL_LIMIT)
    return SafeReport(fields, excerpt, [MONITORS_FACT] if looped else [])

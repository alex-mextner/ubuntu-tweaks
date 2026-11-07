"""Severity, kind and dedup key of a crash report.

Accessed via: ``apport_source`` after ``sanitize``; the tests.

Rules, first match wins:

1. a crash of the desktop itself (gnome-shell, mutter, Xwayland, gnome-session, gdm): ``critical``;
2. any other crash, and every report that has only an ``UnreportableReason`` or a package problem: ``warning``.

The key is ``<executable>|<signal>|<top frame function>``: no pid, no address, no time, so that the library's
dedup folds the same crash into one event. A nested test shell (gnome-shell or mutter with ``--headless``, ``--virtual-monitor`` or ``--nested`` as a whole
argument of ProcCmdline) is NOT downgraded: it keeps the severity above, gets the tag ``nested-test-shell`` (context for
the agent only) and ``|nested`` appended to the key, so its event never absorbs a crash of the real session shell.
Dropping nested shells to the audit log is the optional ``nested_test_shell.exempt`` rule (``rules.py``), applied by
``apport_source``, not here.

Public API: ``Classification(kind, severity, source, key, signal, function, tags, nested)`` and
``classify(fields) -> Classification`` (the PARSED fields: routing must not depend on how much the sanitizer clips); ``NESTED_FLAGS``, ``NESTED_CAPABLE``, ``DESKTOP_PROCESSES``.
"""

from __future__ import annotations

import os
import re
import signal as signal_module
from dataclasses import dataclass

from error_intercept.crash_report import first_function
from error_intercept.sanitize import clean_line, clip

NESTED_FLAGS = ("--headless", "--virtual-monitor", "--nested")
NESTED_CAPABLE = frozenset({"gnome-shell", "mutter"})
NESTED_TAG = "nested-test-shell"
DESKTOP_PROCESSES = frozenset(
    {"gnome-shell", "mutter", "Xwayland", "gnome-session-binary", "gdm-wayland-session", "gdm"}
)
KINDS = {"Crash": "crash", "Package": "package-problem", "KernelCrash": "kernel-crash", "KernelOops": "kernel-crash"}
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")


@dataclass(frozen=True)
class Classification:
    kind: str
    severity: str
    source: str
    key: str
    signal: str
    function: str
    tags: tuple[str, ...]
    nested: bool


def signal_name(fields: dict[str, str]) -> str:
    named = fields.get("SignalName", "").strip()
    if named:
        return named
    number = fields.get("Signal", "").strip()
    if number.isdigit():
        try:
            return signal_module.Signals(int(number)).name
        except ValueError:
            return f"SIG{number}"
    return number or "none"


def arguments(cmdline: str) -> list[str]:
    """ProcCmdline as an argument list, decoded the way apport wrote it: the arguments are joined with a space, and
    only a backslash and a space inside one are escaped with a backslash (``session\\ --nested`` is ONE argument).
    Quotes are ordinary characters: this is not shell quoting."""
    result: list[str] = []
    current: list[str] = []
    started = False
    index = 0
    while index < len(cmdline):
        char = cmdline[index]
        if char == "\\" and cmdline[index + 1 : index + 2] in ("\\", " "):
            current.append(cmdline[index + 1])
            started = True
            index += 2
            continue
        if char == " ":
            if started:
                result.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        result.append("".join(current))
    return result


def is_nested(exe: str, cmdline: str) -> bool:
    """A test instance of the shell: only a shell takes these flags (``firefox --headless`` is a real crash)."""
    if exe not in NESTED_CAPABLE:
        return False
    return any(token == flag or token.startswith(flag + "=") for token in arguments(cmdline) for flag in NESTED_FLAGS)


def executable(fields: dict[str, str]) -> str:
    path = fields.get("ExecutablePath", "").strip()
    return os.path.basename(path) if path else "unknown"


def classify(fields: dict[str, str]) -> Classification:
    exe = executable(fields)
    name = signal_name(fields)
    function = _ADDRESS.sub("", first_function(fields.get("StacktraceTop", ""))).strip() or "unknown"
    nested = is_nested(exe, fields.get("ProcCmdline", ""))
    crashed = name != "none"
    if crashed and exe in DESKTOP_PROCESSES:
        severity = "critical"
    else:
        severity = "warning"
    # routing above used the raw values; every value below is published (event, audit log) and so is masked first
    tags = tuple(tag for tag in (NESTED_TAG if nested else "", *clean_line(fields.get("Tags", ""), 200).split()) if tag)
    kind = KINDS.get(fields.get("ProblemType", ""), "report")
    source, signal_text, function = clean_line(exe, 200), clean_line(name, 200), clean_line(function, 200)
    key = clip(f"{source}|{signal_text}|{function}", 300) + ("|nested" if nested else "")
    return Classification(kind, severity, source, key, signal_text, function, tags, nested)

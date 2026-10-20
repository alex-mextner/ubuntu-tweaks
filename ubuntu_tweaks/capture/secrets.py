"""Value-pattern secret scan for everything `capture` would put into the profile.

Accessed via: ``settings_scan.py`` (every dconf value), ``files_scan.py`` (every adopted file) and
``commit.py`` (the serialized output, as the last line of defence).

Assumptions: a finding never carries the matched text, only its kind and position, so a report
built from findings cannot leak what it reports. Two classes: ``secret`` (PEM blocks, API tokens,
JWTs, e-mail addresses, credentials in URLs, long base64/hex runs) and ``identity`` (private LAN
addresses).

Public API: ``Finding(kind, cls, line)``, ``scan_text(text, *, exempt_email_keys=()) ->
tuple[Finding, ...]`` (line-oriented; an e-mail match is ignored on a ``key = value`` line whose
key is in ``exempt_email_keys``, because GNOME extension uuids look like addresses),
``scan_value(text)`` (one GVariant value, no key context), ``key_names(text, patterns)`` (names of
secret-looking keys in a config text; never their values).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

type FindingClass = Literal["secret", "identity"]


@dataclass(frozen=True)
class Finding:
    kind: str
    cls: FindingClass
    line: int


_PATTERNS: tuple[tuple[str, FindingClass, re.Pattern[str]], ...] = (
    ("pem-block", "secret", re.compile(r"-----BEGIN [A-Z0-9 ]+-----")),
    (
        "api-token",
        "secret",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
            r"|xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|npm_[A-Za-z0-9]{36})"
        ),
    ),
    ("jwt", "secret", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("url-credentials", "secret", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]+@")),
    ("email", "secret", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("hex-run", "secret", re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{40,}(?![0-9A-Za-z])")),
    (
        "base64-run",
        "secret",
        re.compile(
            r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])"
            r"[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])"
        ),
    ),
    (
        "lan-ip",
        "identity",
        re.compile(
            r"(?<![0-9.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![0-9])"
        ),
    ),
)
_ASSIGNMENT = re.compile(r"""^\s*["']?([A-Za-z0-9_.@/-]+)["']?\s*[=:]""")


_MAX_BASE64_SLASHES = 2  # a longer "run" is a file path such as /home/a/Pictures/b/c, not a token


def _is_secret_match(kind: str, matched: str) -> bool:
    return kind != "base64-run" or matched.count("/") <= _MAX_BASE64_SLASHES


def scan_text(text: str, *, exempt_email_keys: Iterable[str] = ()) -> tuple[Finding, ...]:
    exempt = frozenset(exempt_email_keys)
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        assignment = _ASSIGNMENT.match(line)
        key = assignment.group(1) if assignment else None
        for kind, cls, pattern in _PATTERNS:
            if kind == "email" and key in exempt:
                continue
            if any(_is_secret_match(kind, match.group()) for match in pattern.finditer(line)):
                found.append(Finding(kind, cls, number))
    return tuple(found)


def scan_value(text: str) -> tuple[Finding, ...]:
    return scan_text(text)


def key_names(text: str, patterns: Iterable[str]) -> tuple[str, ...]:
    """Distinct names of keys that look credential-bearing and carry a non-empty value."""
    matcher = re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE)
    assignment = re.compile(r"""^\s*["']?([A-Za-z0-9_.@/-]+)["']?\s*[=:]\s*["']?([^\s"',#]+)""")
    names: dict[str, None] = {}
    for line in text.splitlines():
        found = assignment.match(line)
        if found and matcher.search(found.group(1)):
            names[found.group(1)] = None
    return tuple(names)

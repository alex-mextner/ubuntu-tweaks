"""Path globs for the capture rules.

Accessed via: ``rules.py`` (dconf key paths), ``files_scan.py`` (``$HOME``-relative paths).

Syntax: ``*`` is one path segment (or part of it), ``**`` any number of segments (``/**/`` also
matches a single ``/``), ``?`` one character. A pattern must match the whole string.

Public API: ``compile_glob(pattern) -> re.Pattern[str]``, ``GlobSet(patterns)`` with
``first(value) -> str | None`` (the first matching pattern) and ``matches(value) -> bool``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=None)
def compile_glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("/**/", index):
            out.append("/(?:.*/)?")
            index += 4
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out), re.DOTALL)


class GlobSet:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = tuple(patterns)

    def first(self, value: str) -> str | None:
        for pattern in self.patterns:
            if compile_glob(pattern).fullmatch(value):
                return pattern
        return None

    def matches(self, value: str) -> bool:
        return self.first(value) is not None

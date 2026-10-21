"""A layout-preserving keyfile document: sections, ``key=value`` entries and the comments above them.

Accessed via: ``bundles.py`` (reading bundles with their reasons, ``bundle-move``, ``note``) and
``capture/commit.py`` (updating a bundle in place). Pure text work: no GLib, no I/O, so the
``list`` / ``machine`` / ``bundles`` commands stay free of PyGObject.

Assumptions:

- The syntax is the one ``dconf dump`` writes and GKeyFile reads for these files: ``[section]``
  headers, ``key=value`` lines (spaces around ``=`` are accepted and kept), ``#`` full-line comments,
  blank lines. A value is the rest of the line, trimmed; there are no continuation lines.
- The comment block of a key (or section) is the run of ``#`` lines directly above it, with no
  blank line in between: its text is the *reason* the key is there. A comment separated by a blank
  line belongs to nobody and is never touched.
- Edits change the least text: ``set_value`` rewrites one value, ``add`` appends after the last
  key of a section (or a new section at the end), ``remove`` drops one key with its comment block.
  Nothing is reordered, reformatted or re-sorted. Section names compare without surrounding
  slashes (``org/gnome/x`` is ``/org/gnome/x/``); the written header keeps the file's own spelling.
- A key outside any section, a line that is none of the above, or the same key twice in one
  section (also across two blocks of that section) is ``ConfigError`` naming file and line.

Public API: ``KeyfileDoc.parse(text, where)``, ``KeyfileDoc.empty(where)``, ``doc.text()``,
``doc.entries()``, ``doc.find(section, key)``, ``doc.section_comment(section)``,
``doc.set_value`` / ``add`` / ``remove`` / ``set_comment``, ``Entry``, ``Removed``,
``normalize_section(name)``, ``comment_text(lines)``, ``comment_lines(text)``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ubuntu_tweaks.errors import ConfigError

_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")
_ENTRY = re.compile(r"^(\s*)([^=\s#\[][^=]*?)(\s*=\s*)(.*?)\s*$")


def normalize_section(name: str) -> str:
    """``org/gnome/x`` and ``/org/gnome/x/`` are the same dconf directory; the root is ``/``."""
    stripped = name.strip().strip("/")
    return stripped or "/"


def comment_text(lines: Sequence[str]) -> str:
    """The reason in a comment block: each line without its ``#`` and one space, joined by spaces."""
    parts = []
    for line in lines:
        text = line.strip().removeprefix("#")
        parts.append(text.removeprefix(" ").strip())
    return " ".join(part for part in parts if part)


def comment_lines(text: str) -> list[str]:
    """``# `` lines for ``text`` (one per line of the text; empty text means no comment)."""
    return [f"# {line}".rstrip() for line in text.splitlines() if line.strip()]


@dataclass(frozen=True)
class Entry:
    section: str  # normalized
    key: str
    value: str
    line: int  # 0-based index into the document's lines
    comment_start: int  # first line of the attached comment block; equals ``line`` when there is none

    @property
    def number(self) -> int:
        """The 1-based line number shown in messages."""
        return self.line + 1


@dataclass(frozen=True)
class Removed:
    """What ``remove`` took out, so the caller can put it somewhere else without losing a word."""

    value: str
    comment: tuple[str, ...]  # the key's comment lines, verbatim
    section_comment: tuple[str, ...]  # the section's comment lines when the section emptied and went, else ()


@dataclass(frozen=True)
class _Header:
    name: str  # normalized
    line: int
    comment_start: int


class KeyfileDoc:
    def __init__(self, lines: list[str], where: str, *, trailing_newline: bool = True) -> None:
        self._lines = lines
        self.where = where
        self._trailing_newline = trailing_newline
        self._entries: list[Entry] = []
        self._headers: list[_Header] = []
        self._reparse()

    @classmethod
    def parse(cls, text: str, where: str) -> KeyfileDoc:
        return cls(text.splitlines(), where, trailing_newline=text.endswith("\n") or not text)

    @classmethod
    def empty(cls, where: str) -> KeyfileDoc:
        return cls([], where)

    def text(self) -> str:
        if not self._lines:
            return ""
        return "\n".join(self._lines) + ("\n" if self._trailing_newline else "")

    def _reparse(self) -> None:
        entries: list[Entry] = []
        headers: list[_Header] = []
        seen: dict[tuple[str, str], Entry] = {}
        section: str | None = None
        pending: int | None = None
        for index, line in enumerate(self._lines):
            if not line.strip():
                pending = None
            elif line.lstrip().startswith("#"):
                pending = index if pending is None else pending
            elif header := _SECTION.match(line):
                section = normalize_section(header.group(1))
                headers.append(_Header(section, index, index if pending is None else pending))
                pending = None
            elif entry := _ENTRY.match(line):
                if section is None:
                    raise ConfigError(f"{self.where}:{index + 1}: key {entry.group(2)!r} is outside any [section]")
                key = entry.group(2).strip()
                found = Entry(section, key, entry.group(4), index, index if pending is None else pending)
                if (section, key) in seen:
                    raise ConfigError(
                        f"{self.where}:{index + 1}: duplicate key {key!r} in [{section}] "
                        f"(first at line {seen[(section, key)].number})"
                    )
                seen[(section, key)] = found
                entries.append(found)
                pending = None
            else:
                raise ConfigError(f"{self.where}:{index + 1}: not a section header, key=value line or comment")
        self._entries, self._headers = entries, headers

    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def find(self, section: str, key: str) -> Entry | None:
        name = normalize_section(section)
        return next((e for e in self._entries if e.section == name and e.key == key), None)

    def reason(self, entry: Entry) -> tuple[str, ...]:
        """The key's comment lines, verbatim."""
        return tuple(self._lines[entry.comment_start : entry.line])

    def _section_headers(self, name: str) -> list[_Header]:
        return [h for h in self._headers if h.name == name]

    def section_comment(self, section: str) -> tuple[str, ...]:
        headers = self._section_headers(normalize_section(section))
        return tuple(self._lines[headers[0].comment_start : headers[0].line]) if headers else ()

    def set_value(self, section: str, key: str, value: str) -> None:
        entry = self.find(section, key)
        if entry is None:
            raise KeyError(f"[{section}] {key}")
        matched = _ENTRY.match(self._lines[entry.line])
        assert matched is not None
        self._lines[entry.line] = f"{matched.group(1)}{matched.group(2)}{matched.group(3)}{value}"
        self._trailing_newline = True
        self._reparse()

    def add(
        self,
        section: str,
        key: str,
        value: str,
        comment: Sequence[str] = (),
        section_comment: Sequence[str] = (),
    ) -> None:
        """Append ``key=value`` (with its comment lines above) to ``section``, creating the section at
        the end of the file when it is missing. ``section_comment`` is merged into the section's own
        comment block (below the lines already there)."""
        name = normalize_section(section)
        if self.find(name, key) is not None:
            raise KeyError(f"[{name}] {key} is already present")
        new_lines = [*comment, f"{key}={value}"]
        headers = self._section_headers(name)
        if headers:
            last = headers[-1]
            in_section = [e for e in self._entries if e.section == name]
            at = max((e.line for e in in_section), default=last.line) + 1
            self._lines[at:at] = new_lines
            if section_comment:
                first = headers[0]
                self._lines[first.line : first.line] = list(section_comment)
        else:
            if self._lines and self._lines[-1].strip():
                self._lines.append("")
            self._lines += [*section_comment, f"[{section.strip()}]", *new_lines]
        self._trailing_newline = True
        self._reparse()

    def remove(self, section: str, key: str) -> Removed:
        """Drop ``key`` with its comment block. A section left without keys is dropped too (its
        comment block is returned, not lost)."""
        entry = self.find(section, key)
        if entry is None:
            raise KeyError(f"[{section}] {key}")
        removed = Removed(entry.value, self.reason(entry), ())
        del self._lines[entry.comment_start : entry.line + 1]
        self._reparse()
        name = entry.section
        if not any(e.section == name for e in self._entries):
            section_comment: list[str] = []
            for header in reversed(self._section_headers(name)):
                section_comment = [*self._lines[header.comment_start : header.line], *section_comment]
                start = header.comment_start
                end = header.line + 1
                del self._lines[start:end]
                self._close_gap(start)
            removed = Removed(removed.value, removed.comment, tuple(section_comment))
            self._reparse()
        self._trailing_newline = True
        return removed

    def _close_gap(self, at: int) -> None:
        """After lines were deleted at ``at``: drop the blank line a deletion left doubled, leading or
        trailing, and nothing else."""
        lines = self._lines
        while at < len(lines) and not lines[at].strip() and (at == 0 or not lines[at - 1].strip()):
            del lines[at]
        while lines and at >= len(lines) and not lines[-1].strip():
            lines.pop()
            at = len(lines)

    def append_section_comment(self, section: str, lines: Sequence[str]) -> None:
        """Add ``lines`` to the end of ``section``'s own comment block (directly above its header)."""
        headers = self._section_headers(normalize_section(section))
        if not headers:
            raise KeyError(section)
        self._lines[headers[0].line : headers[0].line] = list(lines)
        self._trailing_newline = True
        self._reparse()

    def set_comment(self, section: str, key: str, text: str) -> None:
        """Replace the comment block above ``key`` with ``text`` (one ``#`` line per line of text);
        empty ``text`` removes the block."""
        entry = self.find(section, key)
        if entry is None:
            raise KeyError(f"[{section}] {key}")
        self._lines[entry.comment_start : entry.line] = comment_lines(text)
        self._trailing_newline = True
        self._reparse()

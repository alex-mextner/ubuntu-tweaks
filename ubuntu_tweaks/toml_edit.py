"""Comment- and order-preserving edits of the small TOML files the profile keeps
(``packages.toml``, ``system.toml``): stdlib only, no TOML writer.

Accessed via: ``capture/commit.py`` (adding captured packages and system values to an existing file)
and the ``note`` command (reasons for packages and system values).

Assumptions:

- The files are the shapes ``profile.py`` accepts: top-level keys, ``[table]`` headers, string
  values and arrays of strings. The scanner is tolerant (comments, multi-line arrays, literal and
  basic strings, inline tables and other scalars are stepped over) but it edits only what it
  understands: a string value or an array of strings.
- An edit changes the least text. New array items are appended after the last one (an existing
  order is never sorted); a new key goes after the last key of its table (a new top-level key before
  the first ``[table]``); a new table goes to the end of the file. Everything else, comments and blank
  lines included, stays byte for byte.
- The *reason* of an array item is its trailing ``# comment`` on the same line, else the full-line
  comment block directly above it; ``set_item_comment`` replaces whichever exists, else writes a
  trailing comment. The reason of a scalar key is the full-line comment block directly above it. An
  array that holds several items per line is first spread over one line per item, which is refused
  (``ConfigError``) when the array has comments, so a comment is never lost.
- Callers verify the result: ``commit.py`` re-parses the new text with ``tomllib`` and compares it
  with the data it intended, so a scanner bug fails loudly instead of writing a wrong file.

Public API: ``TomlDoc.parse(text, where)``, ``doc.text()``, ``doc.array(table, key)``,
``doc.append_to_array(table, key, items)``, ``doc.set_string(table, key, value)``,
``doc.item_reason(table, key, item)``, ``doc.set_item_comment(table, key, item, text)``,
``doc.key_reason(table, key)``, ``doc.set_key_comment(table, key, text)``. ``table`` is a tuple of
table names (``()`` for the top level, ``("keyboard",)`` for ``[keyboard]``).
"""

from __future__ import annotations

import json
import re
import tomllib
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.keyfile_doc import comment_lines, comment_text

_BARE = re.compile(r"[A-Za-z0-9_-]+")
_MULTILINE_ARRAY_FROM = 5  # a new array longer than this is written one item per line


@dataclass(frozen=True)
class _Item:
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class _Key:
    path: tuple[str, ...]  # table path + dotted key parts
    start: int
    value_start: int
    value_end: int
    items: tuple[_Item, ...] | None  # for an array of strings
    close: int | None  # offset of the closing ``]`` of an array


@dataclass(frozen=True)
class _Header:
    path: tuple[str, ...]
    start: int


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


class TomlDoc:
    def __init__(self, text: str, where: str) -> None:
        self._text = text
        self.where = where
        self._keys: list[_Key] = []
        self._headers: list[_Header] = []
        self._starts: list[int] = []
        self._scan()

    @classmethod
    def parse(cls, text: str, where: str) -> TomlDoc:
        return cls(text, where)

    def text(self) -> str:
        return self._text

    # ------------------------------------------------------------------ scanning

    def _fail(self, offset: int, message: str) -> ConfigError:
        line = self._text.count("\n", 0, offset) + 1
        return ConfigError(f"{self.where}:{line}: {message}")

    def _string_end(self, at: int) -> int:
        text = self._text
        quote = text[at]
        triple = text.startswith(quote * 3, at)
        pos = at + (3 if triple else 1)
        while pos < len(text):
            char = text[pos]
            if quote == '"' and char == "\\":
                pos += 2
                continue
            if triple and text.startswith(quote * 3, pos):
                return pos + 3
            if not triple and char == quote:
                return pos + 1
            if not triple and char == "\n":
                break
            pos += 1
        raise self._fail(at, "unterminated string")

    def _skip_blank(self, pos: int, *, newlines: bool) -> int:
        text = self._text
        while pos < len(text):
            if text[pos] in " \t\r" or (newlines and text[pos] == "\n"):
                pos += 1
            elif text[pos] == "#":
                while pos < len(text) and text[pos] != "\n":
                    pos += 1
            else:
                break
        return pos

    def _dotted(self, pos: int, closer: str) -> tuple[tuple[str, ...], int]:
        text = self._text
        parts: list[str] = []
        while True:
            pos = self._skip_blank(pos, newlines=False)
            if pos < len(text) and text[pos] in "\"'":
                end = self._string_end(pos)
                parts.append(str(tomllib.loads(f"v = {text[pos:end]}")["v"]))
                pos = end
            else:
                match = _BARE.match(text, pos)
                if match is None:
                    raise self._fail(pos, "cannot parse this key")
                parts.append(match.group(0))
                pos = match.end()
            pos = self._skip_blank(pos, newlines=False)
            if text.startswith(closer, pos):
                return tuple(parts), pos
            if pos < len(text) and text[pos] == ".":
                pos += 1
                continue
            raise self._fail(pos, "cannot parse this key")

    def _array(self, pos: int) -> tuple[tuple[_Item, ...] | None, int, int]:
        """``(items or None when some element is not a string, offset of ']', offset after it)``."""
        text = self._text
        items: list[_Item] = []
        plain = True
        pos += 1
        while True:
            pos = self._skip_blank(pos, newlines=True)
            if pos >= len(text):
                raise self._fail(pos, "unterminated array")
            if text[pos] == "]":
                return (tuple(items) if plain else None), pos, pos + 1
            if text[pos] in "\"'":
                end = self._string_end(pos)
                items.append(_Item(str(tomllib.loads(f"v = {text[pos:end]}")["v"]), pos, end))
            else:
                plain = False
                end = self._scalar_end(pos, in_array=True)
            pos = self._skip_blank(end, newlines=True)
            if pos < len(text) and text[pos] == ",":
                pos += 1

    def _scalar_end(self, pos: int, *, in_array: bool) -> int:
        """End of a value that is not a plain string: nested arrays/tables are skipped by depth."""
        text = self._text
        depth = 0
        while pos < len(text):
            char = text[pos]
            if char in "\"'":
                pos = self._string_end(pos)
                continue
            if char in "[{":
                depth += 1
            elif char in "]}":
                if depth == 0:
                    return pos
                depth -= 1
            elif depth == 0 and (char == "\n" or char == "#" or (in_array and char == ",")):
                return pos
            pos += 1
        return pos

    def _scan(self) -> None:
        text = self._text
        self._starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
        table: tuple[str, ...] = ()
        pos = 0
        keys: list[_Key] = []
        headers: list[_Header] = []
        while True:
            pos = self._skip_blank(pos, newlines=True)
            if pos >= len(text):
                break
            start = pos
            if text[pos] == "[":
                array_of_tables = text.startswith("[[", pos)
                path, close = self._dotted(pos + (2 if array_of_tables else 1), "]]" if array_of_tables else "]")
                table = path
                headers.append(_Header(path, start))
                pos = close + (2 if array_of_tables else 1)
                continue
            path, equals = self._dotted(pos, "=")
            pos = self._skip_blank(equals + 1, newlines=False)
            if pos >= len(text) or text[pos] == "\n":
                raise self._fail(start, "a key has no value")
            if text[pos] in "\"'":
                end = self._string_end(pos)
                keys.append(_Key((*table, *path), start, pos, end, None, None))
            elif text[pos] == "[":
                items, close, end = self._array(pos)
                keys.append(_Key((*table, *path), start, pos, end, items, close))
            else:
                end = self._scalar_end(pos, in_array=False)
                keys.append(_Key((*table, *path), start, pos, end, None, None))
            pos = end
        self._keys, self._headers = keys, headers

    def _line_of(self, offset: int) -> int:
        return bisect_right(self._starts, offset) - 1

    def _line_bounds(self, line: int) -> tuple[int, int]:
        """``(start offset, offset of the newline or end of text)`` of a 0-based line."""
        start = self._starts[line]
        end = self._starts[line + 1] - 1 if line + 1 < len(self._starts) else len(self._text)
        return start, end

    def _find(self, table: tuple[str, ...], key: str) -> _Key | None:
        return next((k for k in self._keys if k.path == (*table, key)), None)

    def _replace(self, start: int, end: int, replacement: str) -> None:
        self._text = self._text[:start] + replacement + self._text[end:]
        self._scan()

    # ------------------------------------------------------------------ reading

    def has(self, table: tuple[str, ...], key: str) -> bool:
        return self._find(table, key) is not None

    def array(self, table: tuple[str, ...], key: str) -> tuple[str, ...] | None:
        """The strings of the array ``key``, ``None`` when it is missing; ``ConfigError`` when it is no
        array of strings."""
        found = self._find(table, key)
        if found is None:
            return None
        if found.items is None:
            raise self._fail(found.start, f"{key} must be an array of strings")
        return tuple(item.value for item in found.items)

    # ------------------------------------------------------------------ writing: values

    def _insert_key(self, table: tuple[str, ...], key: str, rendered: str) -> None:
        """Add ``key = rendered`` at the end of ``table`` (top level: before the first ``[table]``); a
        table that does not exist yet is created at the end of the file."""
        text = self._text
        body = f"{key if _BARE.fullmatch(key) else _quote(key)} = {rendered}"
        in_table = [k for k in self._keys if k.path[:-1] == table]
        header = next((h for h in self._headers if h.path == table), None)
        if in_table or header is not None:
            if in_table:
                offset = self._line_bounds(self._line_of(in_table[-1].value_end - 1))[1]
            else:
                offset = self._line_bounds(self._line_of(header.start))[1]  # type: ignore[union-attr]
            self._replace(offset, offset, f"\n{body}" + ("\n" if offset == len(text) else ""))
        elif not table:
            first = self._headers[0] if self._headers else None
            if first is not None:
                at = self._line_bounds(self._comment_above(first.start)[0])[0]  # above the header's own comment
                self._replace(at, at, f"{body}\n\n")
            else:
                self._replace(len(text), len(text), ("" if not text or text.endswith("\n") else "\n") + f"{body}\n")
        else:
            names = ".".join(part if _BARE.fullmatch(part) else _quote(part) for part in table)
            lead = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
            self._replace(len(text), len(text), f"{lead}[{names}]\n{body}\n")

    def set_string(self, table: tuple[str, ...], key: str, value: str) -> None:
        found = self._find(table, key)
        if found is None:
            self._insert_key(table, key, _quote(value))
            return
        if found.items is not None or self._text[found.value_start] not in "\"'":
            raise self._fail(found.start, f"{key} must be a string")
        self._replace(found.value_start, found.value_end, _quote(value))

    def _render_array(self, items: Sequence[str]) -> str:
        if len(items) >= _MULTILINE_ARRAY_FROM:
            return "[\n" + "".join(f"  {_quote(item)},\n" for item in items) + "]"
        return "[" + ", ".join(_quote(item) for item in items) + "]"

    def append_to_array(self, table: tuple[str, ...], key: str, items: Sequence[str]) -> None:
        """Add ``items`` (not already present) after the last element, creating the array when missing."""
        found = self._find(table, key)
        if found is None:
            if items:
                self._insert_key(table, key, self._render_array(items))
            return
        if found.items is None or found.close is None:
            raise self._fail(found.start, f"{key} must be an array of strings")
        present = {item.value for item in found.items}
        new = [item for item in dict.fromkeys(items) if item not in present]
        if not new:
            return
        text = self._text
        close = found.close
        open_line, close_line = self._line_of(found.value_start), self._line_of(close)
        if not found.items:
            if not text[found.value_start + 1 : close].strip():
                self._replace(found.value_start, close + 1, self._render_array(new))
                return
            # only comments inside `[ ... ]`: add the items above the closing bracket and keep them
            line_start = self._line_bounds(close_line)[0]
            rendered = "".join(f"  {_quote(item)},\n" for item in new)
            if text[line_start:close].strip():
                self._replace(close, close, "\n" + rendered)
            else:
                self._replace(line_start, line_start, rendered)
            return
        last = found.items[-1]
        if open_line == close_line:  # `["a", "b"]`: stay on the line
            self._replace(last.end, close, "".join(f", {_quote(item)}" for item in new))
            return
        before_close = text[self._line_bounds(close_line)[0] : close]
        if before_close.strip():  # the `]` follows an item on its own line: `"b"]`
            self._replace(last.end, close, "".join(f", {_quote(item)}" for item in new))
            return
        indent = re.match(r"[ \t]*", text[self._line_bounds(self._line_of(last.start))[0] :]).group(0)  # type: ignore[union-attr]
        after_last = self._skip_blank(last.end, newlines=False)
        comma = "" if text.startswith(",", last.end) or text.startswith(",", after_last) else ","
        rendered = "".join(f"{indent}{_quote(item)},\n" for item in new)
        insert_at = self._line_bounds(close_line)[0]
        if comma:
            self._replace(last.end, last.end, comma)
            insert_at += len(comma)
        self._replace(insert_at, insert_at, rendered)

    # ------------------------------------------------------------------ reasons

    def _comment_above(self, offset: int) -> tuple[int, int]:
        """``(first line, line)`` of the full-line comment block directly above the line of ``offset``."""
        line = self._line_of(offset)
        first = line
        while first > 0:
            start, end = self._line_bounds(first - 1)
            if self._text[start:end].lstrip().startswith("#"):
                first -= 1
            else:
                break
        return first, line

    def _lines(self, first: int, last: int) -> list[str]:
        return [self._text[slice(*self._line_bounds(i))] for i in range(first, last)]

    def key_reason(self, table: tuple[str, ...], key: str) -> str:
        found = self._find(table, key)
        if found is None:
            return ""
        first, line = self._comment_above(found.start)
        return comment_text(self._lines(first, line))

    def set_key_comment(self, table: tuple[str, ...], key: str, text: str) -> None:
        found = self._find(table, key)
        if found is None:
            raise KeyError(key)
        first, line = self._comment_above(found.start)
        start = self._line_bounds(first)[0]
        end = self._line_bounds(line)[0]
        indent = re.match(r"[ \t]*", self._text[end:]).group(0)  # type: ignore[union-attr]
        block = "".join(f"{indent}{c}\n" for c in comment_lines(text))
        self._replace(start, end, block)

    def _trailing_comment(self, item: _Item) -> tuple[int, int] | None:
        """Offsets of the ``# ...`` after ``item`` on its line (past the comma), if any."""
        end_of_line = self._line_bounds(self._line_of(item.end))[1]
        rest = self._text[item.end : end_of_line]
        hash_at = rest.find("#")
        if hash_at < 0 or rest[:hash_at].strip(" \t,"):
            return None
        return item.end + hash_at, end_of_line

    def item_reason(self, table: tuple[str, ...], key: str, item: str) -> str:
        found = self._find(table, key)
        if found is None or found.items is None:
            return ""
        target = next((i for i in found.items if i.value == item), None)
        if target is None:
            return ""
        trailing = self._trailing_comment(target)
        if trailing is not None:
            return comment_text([self._text[trailing[0] : trailing[1]]])
        own_line = self._line_of(target.start)
        shares = own_line == self._line_of(found.value_start) or any(
            other is not target and self._line_of(other.start) == own_line for other in found.items
        )
        if shares:
            return ""  # the comment above belongs to the whole array line, not to this item
        first, line = self._comment_above(target.start)
        return comment_text(self._lines(first, line))

    def _one_item_per_line(self, table: tuple[str, ...], key: str) -> None:
        found = self._find(table, key)
        assert found is not None and found.items is not None and found.close is not None
        lines = [self._line_of(i.start) for i in found.items]
        bracket_lines = {self._line_of(found.value_start), self._line_of(found.close)}
        if len(set(lines)) == len(lines) and not bracket_lines & set(lines):
            return
        body = self._text[found.value_start : found.close + 1]
        if "#" in re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", "", body):
            raise self._fail(
                found.start, f"cannot attach a reason inside {key}: put one item per line first (it has comments)"
            )
        rendered = "[\n" + "".join(f"  {_quote(i.value)},\n" for i in found.items) + "]"
        self._replace(found.value_start, found.close + 1, rendered)

    def set_item_comment(self, table: tuple[str, ...], key: str, item: str, text: str) -> None:
        found = self._find(table, key)
        if found is None or found.items is None or not any(i.value == item for i in found.items):
            raise KeyError(f"{key}: {item}")
        self._one_item_per_line(table, key)
        found = self._find(table, key)
        assert found is not None and found.items is not None
        target = next(i for i in found.items if i.value == item)
        trailing = self._trailing_comment(target)
        first, line = self._comment_above(target.start)
        if trailing is not None:
            start, end = trailing
            while start > 0 and self._text[start - 1] in " \t":
                start -= 1
            self._replace(start, end, f"  # {text}" if text else "")
        elif first != line:
            start = self._line_bounds(first)[0]
            end = self._line_bounds(line)[0]
            indent = re.match(r"[ \t]*", self._text[end:]).group(0)  # type: ignore[union-attr]
            if text:
                self._replace(start, end, "".join(f"{indent}{c}\n" for c in comment_lines(text)))
            else:
                self._replace(start, end, "")
        elif text:
            end_of_line = self._line_bounds(self._line_of(target.end))[1]
            self._replace(end_of_line, end_of_line, f"  # {text}")

"""Read and write the small TOML files `capture --commit` updates (``packages.toml``, ``system.toml``).

Accessed via: ``plan.py`` (raw layer tables), ``commit.py`` (regenerating a file).

Assumptions: values are strings, booleans, numbers and lists of those; tables nest. Keys are
written bare when they are valid bare keys and quoted otherwise, so a key such as ``my.key``
round-trips as one key. Comments and ordering of an existing file are not preserved (ticket #29).
Reading is ``profile.read_toml`` (missing file = empty table, bad TOML = ``ConfigError``).

Public API: ``dump_toml(table) -> str``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from ubuntu_tweaks.errors import ConfigError

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else json.dumps(name, ensure_ascii=False)


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise ConfigError(f"cannot write {value!r} to TOML")


def dump_toml(table: Mapping[str, object], path: tuple[str, ...] = ()) -> str:
    """Scalars and arrays first (under ``[a.b]`` when nested), then each sub-table."""
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    lines: list[str] = []
    if path and (scalars or not tables):
        lines.append("[" + ".".join(_key(part) for part in path) + "]")
    for key, value in scalars:
        if isinstance(value, list) and len(value) > 4:
            lines.append(f"{_key(key)} = [")
            lines += [f"  {_value(item)}," for item in value]
            lines.append("]")
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    text = "\n".join(lines) + ("\n" if lines else "")
    for key, value in tables:
        text += ("\n" if text else "") + dump_toml(value, (*path, key))
    return text

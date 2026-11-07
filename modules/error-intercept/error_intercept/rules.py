"""Routing rules read from data: the shipped ``data/intercept-rules.json`` merged with the user's override.

Accessed via: ``apport_source.Ingester`` (the exemption decision) and ``cli status`` (problems found in the files).

The user override is ``$XDG_CONFIG_HOME/error-intercept/intercept-rules.json``; its keys win over the shipped file
field by field. Schema: ``{"version": 1, "nested_test_shell": {"exempt": bool, "mode": "drop", "note": str}}``.
``exempt`` false (the default) routes a nested test shell crash like any other and only tags it; true drops it to
the audit log. Any invalid value falls back to the default (off) and is reported in ``Rules.problems``.
The journal source (branch error-intercept-journal) reads the same key from the same file names.

Public API: ``load_rules(paths, shipped=SHIPPED) -> Rules(nested_exempt, problems)``, ``SHIPPED``, ``RULES_FILE``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from error_intercept.paths import Paths

RULES_FILE = "intercept-rules.json"
SHIPPED = Path(__file__).resolve().parent.parent / "data" / RULES_FILE
SUPPORTED_VERSION = 1
MODES = ("drop",)


@dataclass(frozen=True)
class Rules:
    nested_exempt: bool = False
    problems: tuple[str, ...] = ()


def _read(path: Path, label: str, problems: list[str]) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        problems.append(f"{label}: unreadable ({error})")
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as error:
        problems.append(f"{label}: not valid JSON ({error})")
        return {}
    if not isinstance(data, dict):
        problems.append(f"{label}: top level must be an object")
        return {}
    if data.get("version") != SUPPORTED_VERSION:
        problems.append(f"{label}: version must be {SUPPORTED_VERSION}")
        return {}
    return data


def _nested(data: dict[str, object], label: str, problems: list[str]) -> dict[str, object]:
    section = data.get("nested_test_shell", {})
    if not isinstance(section, dict):
        problems.append(f"{label}: nested_test_shell must be an object")
        return {}
    kept: dict[str, object] = {}
    before = len(problems)
    for key, value in section.items():
        if key == "exempt" and not isinstance(value, bool):
            problems.append(f"{label}: nested_test_shell.exempt must be true or false, got {value!r}; using false")
            kept[key] = False
        elif key == "mode" and value not in MODES:
            problems.append(f"{label}: nested_test_shell.mode must be one of {MODES}, got {value!r}; exemption off")
            kept[key] = MODES[0]
        elif key == "note" and not isinstance(value, str):
            problems.append(f"{label}: nested_test_shell.note must be a string")
        else:
            kept[key] = value
    if len(problems) > before:
        kept["exempt"] = False  # a section that is not fully valid never switches the exemption on
    return kept


def load_rules(paths: Paths, shipped: Path = SHIPPED) -> Rules:
    problems: list[str] = []
    merged: dict[str, object] = {}
    for label, path in (("shipped rules", shipped), ("user rules", paths.config / RULES_FILE)):
        before = len(problems)
        merged.update(_nested(_read(path, label, problems), label, problems))
        if len(problems) > before:
            merged["exempt"] = False  # any problem in a file, whole-file ones included, leaves the exemption off
    return Rules(nested_exempt=merged.get("exempt") is True, problems=tuple(problems))

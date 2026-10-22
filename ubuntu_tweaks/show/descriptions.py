"""The hand-written Russian texts of ``ubuntu-tweak show`` (``descriptions_ru.toml``).

Accessed via: ``show.collect`` asks ``Descriptions.lookup`` for the explanation and the value spec
(unit, zero text, enum meanings, keybinding flag) of every key, ``Descriptions.panel`` for the Russian
title of a Settings panel.

File layout (all TOML, validated at load: a wrong type is ``ConfigError``, never a silent miss):

``[panels.<id>]``  ``title`` and ``about`` of a Settings panel (ids from ``panels.toml``, plus ``other``).
``[keys."<schema id>"."<key>"]``  ``ru`` (the explanation), optional ``unit`` / ``zero`` / ``accel``.
``[enums."<schema id>"."<key>"]``  enum nick = its meaning.
``[[rule]]``  ``paths`` (dconf key globs, like ``panels.toml``), then the same fields as a key entry:
for keys without a schema on the machine and for families of keys (window sizes, per-application
notification settings). An exact ``keys`` entry beats a rule; the first matching rule wins.

Public API: ``load_descriptions(path=DESCRIPTIONS_TOML) -> Descriptions``; ``Descriptions.panel(id)``,
``Descriptions.lookup(schema, key, full) -> KeyText``; ``KeyText(ru, spec)``.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.capture.globs import GlobSet
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.show.values import ValueSpec

DESCRIPTIONS_TOML = Path(__file__).resolve().parent / "descriptions_ru.toml"
_UNITS = frozenset({"seconds", "minutes", "ms", "percent", "pixels", "size", "unix", "bytes"})
_ENTRY_FIELDS = frozenset({"ru", "unit", "zero", "accel"})


@dataclass(frozen=True)
class PanelText:
    title: str
    about: str


@dataclass(frozen=True)
class KeyText:
    ru: str | None
    spec: ValueSpec


@dataclass(frozen=True)
class _Entry:
    ru: str | None
    unit: str | None
    zero: str | None
    accel: bool


@dataclass(frozen=True)
class _Rule:
    paths: GlobSet
    entry: _Entry


@dataclass(frozen=True)
class Descriptions:
    panels: Mapping[str, PanelText]
    keys: Mapping[tuple[str, str], _Entry]
    enums: Mapping[tuple[str, str], Mapping[str, str]]
    rules: tuple[_Rule, ...]

    def panel(self, panel_id: str) -> PanelText | None:
        return self.panels.get(panel_id)

    def lookup(self, schema: str | None, key: str, full: str) -> KeyText:
        entry = self.keys.get((schema, key)) if schema is not None else None
        rule = next((rule.entry for rule in self.rules if rule.paths.matches(full)), None)
        enums = self.enums.get((schema, key), {}) if schema is not None else {}
        parts = [part for part in (entry, rule) if part is not None]  # an exact entry first: it wins per field
        accel = any(part.accel for part in parts) or (
            schema is not None and any(word in schema.lower() for word in ("keybindings", "shortcuts"))
        )
        ru = next((part.ru for part in parts if part.ru), None)
        unit = next((part.unit for part in parts if part.unit), None)
        zero = next((part.zero for part in parts if part.zero is not None), None)
        return KeyText(ru, ValueSpec(unit, zero, enums, accel))


def _table(parent: Mapping[str, object], name: str, where: str) -> Mapping[str, object]:
    value = parent.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: {name} must be a table")
    return value


def _entry(raw: object, where: str) -> _Entry:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a table")
    unknown = sorted(set(raw) - _ENTRY_FIELDS - {"paths"})
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) {', '.join(unknown)}")
    ru, unit, zero, accel = raw.get("ru"), raw.get("unit"), raw.get("zero"), raw.get("accel", False)
    if ru is not None and not isinstance(ru, str):
        raise ConfigError(f"{where}: ru must be a string")
    if unit is not None and unit not in _UNITS:
        raise ConfigError(f"{where}: unit must be one of {', '.join(sorted(_UNITS))}")
    if zero is not None and not isinstance(zero, str):
        raise ConfigError(f"{where}: zero must be a string")
    if not isinstance(accel, bool):
        raise ConfigError(f"{where}: accel must be true or false")
    return _Entry(ru, unit, zero, accel)


def _two_level(data: Mapping[str, object], name: str, where: str) -> dict[tuple[str, str], object]:
    found: dict[tuple[str, str], object] = {}
    for schema, keys in _table(data, name, where).items():
        if not isinstance(keys, dict):
            raise ConfigError(f"{where}: [{name}.{schema!r}] must be a table of keys")
        for key, raw in keys.items():
            found[(schema, key)] = raw
    return found


def load_descriptions(path: Path = DESCRIPTIONS_TOML) -> Descriptions:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error
    where = str(path)
    panels: dict[str, PanelText] = {}
    for panel_id, raw in _table(data, "panels", where).items():
        if not isinstance(raw, dict) or not isinstance(raw.get("title"), str) or not isinstance(raw.get("about"), str):
            raise ConfigError(f"{where}: [panels.{panel_id}] needs string title and about")
        panels[panel_id] = PanelText(raw["title"], raw["about"])
    keys = {
        pair: _entry(raw, f"{where}: keys {pair[0]} {pair[1]}") for pair, raw in _two_level(data, "keys", where).items()
    }
    enums: dict[tuple[str, str], Mapping[str, str]] = {}
    for pair, raw in _two_level(data, "enums", where).items():
        if not isinstance(raw, dict) or not all(isinstance(item, str) for item in raw.values()):
            raise ConfigError(f"{where}: enums {pair[0]} {pair[1]} must map nick to a string")
        enums[pair] = dict(raw)
    rules: list[_Rule] = []
    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise ConfigError(f"{where}: [[rule]] must be an array of tables")
    for index, raw in enumerate(raw_rules):
        paths = raw.get("paths") if isinstance(raw, dict) else None
        if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
            raise ConfigError(f"{where}: rule #{index + 1} needs a list of paths")
        rules.append(_Rule(GlobSet(paths), _entry(raw, f"{where}: rule #{index + 1}")))
    return Descriptions(panels, keys, enums, tuple(rules))

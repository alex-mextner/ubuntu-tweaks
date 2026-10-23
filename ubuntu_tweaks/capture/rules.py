"""The capture rules: the panel table, the built-in defaults and the profile's ``[capture]`` table.

Accessed via: ``plan.py`` builds one ``Rules`` per run. Pure parsing, no system access.

Sources (all TOML): ``ubuntu_tweaks/panels.toml`` (panels, relocatable schemas, panel rules),
``ubuntu_tweaks/capture/defaults.toml`` (exclude/machine rules incl. the ``auto`` class, file allow-list,
scanner and classifier settings) and the profile's merged ``[capture]`` table (``exclude``, ``machine``,
``include``, ``bundles``, ``include_files``, ``machine_files``; anything else is ``ConfigError``). Key
globs there may omit the leading ``/``. Profile ``exclude`` is checked first and always wins; ``machine``
and ``include_files`` extend the defaults; ``include`` lifts the ``state`` and ``auto`` skips for the keys
it matches; ``bundles`` (bundle name -> globs, first match wins) routes new keys to a bundle. A profile
cannot weaken the secret/identity/hardware excludes: they are evaluated before anything it adds.

Public API: ``load_rules(profile_capture=None) -> Rules``; ``Rules`` and its parts
(``PanelInfo``, ``PanelRule``, ``ExcludeRule``, ``MachineRule``, ``FileRules``, ``PackageRules``);
``relocatable_schemas()`` (the ``[[relocatable]]`` table, also read by ``setkey.SchemaIndex``);
``PANEL_IDS`` (the contract's panel file ids).
"""

from __future__ import annotations

import functools
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ubuntu_tweaks.bundles import validate_bundle_name
from ubuntu_tweaks.capture.globs import GlobSet, compile_glob
from ubuntu_tweaks.errors import ConfigError

PANELS_TOML = Path(__file__).resolve().parent.parent / "panels.toml"
DEFAULTS_TOML = Path(__file__).resolve().parent / "defaults.toml"

PANEL_IDS = (
    "network", "bluetooth", "background", "appearance", "notifications", "search", "multitasking",
    "apps", "privacy", "online-accounts", "sharing", "sound", "power", "displays", "mouse-touchpad",
    "keyboard", "printers", "removable-media", "color", "region-language", "accessibility", "users",
    "date-time", "about", "ubuntu-desktop", "wellbeing",
)  # fmt: skip
PANEL_SELECTORS = (*PANEL_IDS, "shell-extensions")  # what `--only` accepts besides the section names
SKIP_CLASSES = ("secret", "identity", "hardware", "state", "auto", "no-schema", "excluded-by-rule")
type Mark = Literal["+", "~", "="]
_HARD_CLASSES = frozenset({"secret", "identity", "hardware"})

type Table = dict[str, object]


@dataclass(frozen=True)
class PanelInfo:
    id: str
    title: str
    disposition: str
    note: str


@dataclass(frozen=True)
class PanelRule:
    panel: str
    to: Literal["shared", "machine", "skip"]
    cls: str | None
    reason: str
    globs: GlobSet


@dataclass(frozen=True)
class ExcludeRule:
    cls: str
    reason: str
    globs: GlobSet


@dataclass(frozen=True)
class MachineRule:
    reason: str
    globs: GlobSet


@dataclass(frozen=True)
class Relocatable:
    globs: GlobSet
    schema: str


@dataclass(frozen=True)
class FileRules:
    include: tuple[str, ...]
    machine: GlobSet
    max_file_bytes: int
    max_text_bytes: int
    large_dir_entries: int
    max_depth: int
    secret_key_names: tuple[str, ...]
    secret_names: GlobSet
    secret_dirs: GlobSet
    cache_dirs: GlobSet
    mixed_dirs: GlobSet
    cache_names: GlobSet
    portable_extensions: tuple[str, ...]
    email_exempt: tuple[str, ...]


@dataclass(frozen=True)
class PackageRules:
    ignore: GlobSet
    keep: GlobSet
    metapackages: tuple[str, ...]
    ignore_snaps: GlobSet


@dataclass(frozen=True)
class BundleRoute:
    """``[capture.bundles]``: keys matching ``globs`` are captured into bundle ``name``."""

    name: str
    globs: GlobSet


@dataclass(frozen=True)
class Rules:
    panels: Mapping[str, PanelInfo]
    panel_rules: tuple[PanelRule, ...]
    profile_exclude: GlobSet
    profile_machine: GlobSet
    profile_include: GlobSet
    profile_bundles: tuple[BundleRoute, ...]
    hard_excludes: tuple[ExcludeRule, ...]
    machine: tuple[MachineRule, ...]
    state: tuple[ExcludeRule, ...]
    auto: tuple[ExcludeRule, ...]
    state_keep: GlobSet
    files: FileRules
    packages: PackageRules
    email_exempt_keys: tuple[str, ...]

    def hard_exclude(self, full: str) -> ExcludeRule | None:
        return next((rule for rule in self.hard_excludes if rule.globs.matches(full)), None)

    def panel_rule(self, full: str) -> PanelRule | None:
        return next((rule for rule in self.panel_rules if rule.globs.matches(full)), None)

    def machine_rule(self, full: str) -> str | None:
        if self.profile_machine.matches(full):
            return "profile [capture] machine"
        return next((rule.reason for rule in self.machine if rule.globs.matches(full)), None)

    def state_rule(self, full: str) -> ExcludeRule | None:
        if self.state_keep.matches(full) or self.profile_include.matches(full):
            return None
        return next((rule for rule in self.state if rule.globs.matches(full)), None)

    def auto_rule(self, full: str) -> ExcludeRule | None:
        """The ``auto`` rule that skips ``full`` (keys the system or an app registers by itself); a
        profile ``[capture] include`` match promotes the key back."""
        if self.profile_include.matches(full):
            return None
        return next((rule for rule in self.auto if rule.globs.matches(full)), None)

    def bundle_route(self, full: str) -> str | None:
        """The profile's ``[capture.bundles]`` bundle for ``full`` (first match wins), if any."""
        return next((route.name for route in self.profile_bundles if route.globs.matches(full)), None)


def _load(path: Path) -> Table:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error


def _strings(table: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: {key} must be a list of strings")
    return tuple(value)


def _text(table: Mapping[str, object], key: str, where: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string")
    return value


def _tables(table: Mapping[str, object], key: str, where: str) -> list[Table]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise ConfigError(f"{where}: [[{key}]] must be an array of tables")
    return list(value)


def _int(table: Mapping[str, object], key: str, where: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be an integer")
    return value


def _sub(table: Mapping[str, object], key: str, where: str) -> Table:
    value = table.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: {key} must be a table")
    return value


def _parse_panels(data: Table) -> tuple[dict[str, PanelInfo], tuple[PanelRule, ...], tuple[Relocatable, ...]]:
    where = str(PANELS_TOML)
    raw_panels = _sub(data, "panels", where)
    if not all(isinstance(entry, dict) for entry in raw_panels.values()):
        raise ConfigError(f"{where}: every [panels.<id>] must be a table")
    panels = {
        name: PanelInfo(
            name, _text(entry, "title", where), _text(entry, "disposition", where), _text(entry, "note", where, "")
        )
        for name, entry in raw_panels.items()
    }
    rules: list[PanelRule] = []
    for entry in _tables(data, "rule", where):
        panel, to = _text(entry, "panel", where), _text(entry, "to", where)
        if panel not in panels:
            raise ConfigError(f"{where}: rule for unknown panel {panel!r}")
        if to not in ("shared", "machine", "skip"):
            raise ConfigError(f"{where}: rule for {panel}: to must be shared, machine or skip, got {to!r}")
        cls = _text(entry, "class", where) if to == "skip" else None
        if cls is not None and cls not in SKIP_CLASSES:
            raise ConfigError(f"{where}: rule for {panel}: unknown class {cls!r}")
        rules.append(
            PanelRule(panel, to, cls, _text(entry, "reason", where, ""), GlobSet(_strings(entry, "paths", where)))
        )
    relocatable = tuple(
        Relocatable(GlobSet([_text(entry, "path", where)]), _text(entry, "schema", where))
        for entry in _tables(data, "relocatable", where)
    )
    return panels, tuple(rules), relocatable


@functools.cache
def relocatable_schemas() -> tuple[tuple[re.Pattern[str], str], ...]:
    """``(dconf path pattern, schema id)`` of every ``[[relocatable]]`` in ``panels.toml``.

    The one list of relocatable schemas: ``capture`` reads and ``apply`` writes through it
    (``setkey.SchemaIndex``), so what capture emits is always something apply can resolve.
    """
    relocatable = _parse_panels(_load(PANELS_TOML))[2]
    return tuple((compile_glob(entry.globs.patterns[0]), entry.schema) for entry in relocatable)


@dataclass(frozen=True)
class ProfileCapture:
    exclude: GlobSet
    machine: GlobSet
    include: GlobSet
    bundles: tuple[BundleRoute, ...]
    include_files: tuple[str, ...]
    machine_files: tuple[str, ...]


def _dconf_globs(values: tuple[str, ...]) -> GlobSet:
    """Key globs of the profile's ``[capture]``: ``org/gnome/x/**`` and ``/org/gnome/x/**`` mean the same."""
    return GlobSet(value if value.startswith("/") else f"/{value}" for value in values)


def _profile_capture(table: Mapping[str, object] | None) -> ProfileCapture:
    where = "profile [capture]"
    table = table or {}
    unknown = sorted(set(table) - {"exclude", "machine", "include", "bundles", "include_files", "machine_files"})
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
    raw_bundles = table.get("bundles", {})
    if not isinstance(raw_bundles, dict):
        raise ConfigError(f"{where}: bundles must be a table of bundle name = list of dconf path globs")
    routes = tuple(
        BundleRoute(validate_bundle_name(name), _dconf_globs(_strings(raw_bundles, name, f"{where} bundles")))
        for name in raw_bundles
    )
    return ProfileCapture(
        _dconf_globs(_strings(table, "exclude", where)),
        _dconf_globs(_strings(table, "machine", where)),
        _dconf_globs(_strings(table, "include", where)),
        routes,
        _strings(table, "include_files", where),
        _strings(table, "machine_files", where),
    )


def load_rules(profile_capture: Mapping[str, object] | None = None) -> Rules:
    panels, panel_rules, _relocatable = _parse_panels(_load(PANELS_TOML))
    defaults = _load(DEFAULTS_TOML)
    where = str(DEFAULTS_TOML)
    excludes = [
        ExcludeRule(
            _text(entry, "class", where), _text(entry, "reason", where), GlobSet(_strings(entry, "paths", where))
        )
        for entry in _tables(defaults, "exclude", where)
    ]
    capture, files, secrets, packages = (
        _sub(defaults, name, where) for name in ("capture", "files", "secrets", "packages")
    )
    profile = _profile_capture(profile_capture)
    file_rules = FileRules(
        include=tuple(
            dict.fromkeys(_strings(capture, "include_files", where) + profile.include_files + profile.machine_files)
        ),
        machine=GlobSet(dict.fromkeys(_strings(capture, "machine_files", where) + profile.machine_files)),
        max_file_bytes=_int(capture, "max_file_bytes", where),
        max_text_bytes=_int(files, "max_text_bytes", where),
        large_dir_entries=_int(files, "large_dir_entries", where),
        max_depth=_int(files, "max_depth", where),
        secret_key_names=_strings(files, "secret_key_names", where),
        secret_names=GlobSet(_strings(files, "secret_names", where)),
        secret_dirs=GlobSet(_strings(files, "secret_dirs", where)),
        cache_dirs=GlobSet(_strings(files, "cache_dirs", where)),
        mixed_dirs=GlobSet(_strings(files, "mixed_dirs", where)),
        cache_names=GlobSet(_strings(files, "cache_names", where)),
        portable_extensions=_strings(files, "portable_extensions", where),
        email_exempt=_strings(secrets, "email_exempt_keys", where),
    )
    return Rules(
        panels=panels,
        panel_rules=panel_rules,
        profile_exclude=profile.exclude,
        profile_machine=profile.machine,
        profile_include=profile.include,
        profile_bundles=profile.bundles,
        hard_excludes=tuple(rule for rule in excludes if rule.cls in _HARD_CLASSES),
        machine=tuple(
            MachineRule(_text(entry, "reason", where), GlobSet(_strings(entry, "paths", where)))
            for entry in _tables(defaults, "machine", where)
        ),
        state=tuple(rule for rule in excludes if rule.cls == "state"),
        auto=tuple(rule for rule in excludes if rule.cls == "auto"),
        state_keep=GlobSet(_strings(_sub(defaults, "state_keep", where), "paths", where)),
        files=file_rules,
        packages=PackageRules(
            ignore=GlobSet(_strings(packages, "ignore", where)),
            keep=GlobSet(_strings(packages, "keep", where)),
            metapackages=_strings(packages, "metapackages", where),
            ignore_snaps=GlobSet(_strings(packages, "ignore_snaps", where)),
        ),
        email_exempt_keys=_strings(secrets, "email_exempt_keys", where),
    )

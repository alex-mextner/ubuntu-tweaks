"""Turning "this dconf key should hold this value" into plan actions.

Accessed via: ``planners/settings.py`` (module presets, profile panel files, machine overlay),
``planners/seeds.py`` and ``planners/migrations.py``. While planning, nothing here runs a command
or touches a file except reading keyfiles, ``metadata.json`` and ``schemas/*.gschema.xml``; only
an action's ``run()`` writes (dconf through the backend, and a seed's directories).

Assumptions:

- A keyfile section is a dconf directory (``org/gnome/desktop/interface``); the schema that owns
  it is looked up from ``SettingsBackend.schemas()`` (fixed paths) or, for the few relocatable
  schemas GNOME Settings uses, from ``relocatable_schemas()``. A key whose directory has no installed
  schema is never guessed: the caller gets an ``Unresolved`` reason and shows it as a warning.
- Effective values come from Gio, so a key whose value is pinned by the session's keyfile
  override is "satisfied" when the override already holds the wanted value and is never frozen
  into dconf. Nothing here writes a value it read back.
- A module's extension schema may not be installed yet when the plan is built (its link and
  ``glib-compile-schemas`` actions run earlier in the same plan). Its actions then report
  ``check() == False`` and resolve the key type in ``run()``; the directory and key names come
  from the extension's ``schemas/*.gschema.xml``.
- Higher layers win per ``(directory, key)``: ``collapse`` keeps one entry for each, so a lower
  layer's satisfied action is never dropped while a higher one is kept (see ``actions.py``).

Public API:

- ``relocatable_schemas()`` (from ``capture.rules``): ``(compiled path pattern, schema id)`` pairs from ``panels.toml``'s ``[[relocatable]]``.
- ``MODULE_OWNED_KEYS``: dconf keys no keyfile (panel file or module preset) may set
  (``enabled-extensions``, ``disabled-extensions``: the toggle planner owns them as single-item edits
  of the user value).
- ``ResolvedKey(schema, path, directory, key)`` / ``Unresolved(reason)`` and
  ``SchemaIndex(backend).resolve(directory, key)``.
- ``SchemaFacts`` and ``module_schema_facts(ctx, manifest, index, schema_id)``;
  ``module_schema_id(ctx, manifest, settings_file)``; ``extension_dir(ctx, manifest)``.
- ``enabled_modules(ctx) -> (manifests, config)``.
- ``Entry`` (one wanted value from one layer) and ``collapse(entries)``.
- ``SetKeyAction`` (kind ``set-key``) and ``SeedKeyAction`` (kind ``seed-key``).
- ``Skips``: groups "skipped" warnings per file and section.
- ``short(text, limit)``; ``NO_SETTINGS_SCHEMA``: the skip reason for a module without a settings schema.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.capture.rules import relocatable_schemas
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.extension import ExtensionModule
from ubuntu_tweaks.manifest import Manifest, SettingsFile, SourceKind, discover_modules, module_defaults
from ubuntu_tweaks.profile import Config
from ubuntu_tweaks.settings import (
    SettingsBackend,
    SettingsError,
    Variant,
    format_value,
    is_satisfied,
    parse_value,
)

from gi.repository import GLib  # noqa: E402  (ubuntu_tweaks.settings has pinned the GLib typelib version)

MODULE_OWNED_KEYS = frozenset({"/org/gnome/shell/enabled-extensions", "/org/gnome/shell/disabled-extensions"})


@dataclass(frozen=True)
class ResolvedKey:
    schema: str
    path: str | None  # what the backend takes as ``path``: the directory for a relocatable schema, else None
    directory: str  # the dconf directory the key lives in, ``/a/b/``
    key: str
    pending_type: str | None = None  # the key's type string from schema XML when the schema is not installed

    @property
    def full(self) -> str:
        return self.directory + self.key


@dataclass(frozen=True)
class Unresolved:
    reason: str


class SchemaIndex:
    def __init__(self, backend: SettingsBackend) -> None:
        self.backend = backend
        self._fixed: dict[str, list[str]] = {}
        self._paths: dict[str, str | None] = {}
        for info in backend.schemas():
            self._paths[info.id] = info.path
            if info.path is not None:
                self._fixed.setdefault(info.path, []).append(info.id)

    def installed(self, schema: str) -> bool:
        return schema in self._paths

    def fixed_path(self, schema: str) -> str | None:
        return self._paths.get(schema)

    def candidates(self, directory: str) -> list[tuple[str, str | None]]:
        found: list[tuple[str, str | None]] = [(schema, None) for schema in self._fixed.get(directory, ())]
        for pattern, schema in relocatable_schemas():
            if schema in self._paths and self._paths[schema] is None and pattern.fullmatch(directory):
                found.append((schema, directory))
        return found

    def resolve(self, directory: str, key: str) -> ResolvedKey | Unresolved:
        candidates = self.candidates(directory)
        if not candidates:
            return Unresolved(f"no installed schema at {directory}")
        for schema, path in candidates:
            if self.backend.has_key(schema, key):
                return ResolvedKey(schema, path, directory, key)
        names = ", ".join(schema for schema, _ in candidates)
        return Unresolved(f"no key {key} in {names}")


@dataclass(frozen=True)
class SchemaFacts:
    """What is known about a module's settings schema, installed or not yet."""

    schema: str
    installed: bool
    directory: str | None  # fixed dconf directory when known
    relocatable: bool
    keys: Mapping[str, str | None] | None  # key name -> type string (None: enum/flags) from schema XML


def extension_dir(ctx: Context, manifest: Manifest) -> Path | None:
    path = manifest.source.path
    if manifest.source.kind not in (SourceKind.SUBMODULE, SourceKind.LOCAL) or path is None:
        return None
    return ctx.repo_root / path


def module_schema_id(ctx: Context, manifest: Manifest, settings_file: SettingsFile | None = None) -> str | None:
    """The settings schema of a module: the manifest's ``schema``, else the extension's
    ``metadata.json`` ``settings-schema`` (never hardcoded, so a renamed extension keeps working)."""
    if settings_file is not None and settings_file.schema:
        return settings_file.schema
    directory = extension_dir(ctx, manifest)
    if directory is None:
        return None
    try:
        metadata = ExtensionModule(manifest, directory).metadata()
    except ConfigError:
        return None  # a broken metadata.json is the extension planners' error; it must not stop settings planning
    if metadata is None:
        return None
    return metadata.settings_schema or None


def _read_schema_xml(schema_dir: Path, schema_id: str) -> tuple[str | None, dict[str, str | None]] | None:
    """``(path or None, {key name: type string or None})`` of ``schema_id`` from the ``*.gschema.xml``
    files in ``schema_dir``."""
    for xml_file in sorted(schema_dir.glob("*.gschema.xml")):
        try:
            root = ElementTree.parse(xml_file).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        for schema in root.iter("schema"):
            if schema.get("id") == schema_id:
                keys = {name: key.get("type") for key in schema.findall("key") if (name := key.get("name"))}
                return schema.get("path"), keys
    return None


def module_schema_facts(ctx: Context, manifest: Manifest, index: SchemaIndex, schema_id: str) -> SchemaFacts:
    if index.installed(schema_id):
        path = index.fixed_path(schema_id)
        return SchemaFacts(schema_id, True, path, path is None, None)
    directory = extension_dir(ctx, manifest)
    found = _read_schema_xml(directory / "schemas", schema_id) if directory is not None else None
    if found is None:
        return SchemaFacts(schema_id, False, None, False, None)
    path, keys = found
    return SchemaFacts(schema_id, False, path, path is None, keys)


def module_target(facts: SchemaFacts, backend: SettingsBackend, key: str) -> ResolvedKey | Unresolved:
    """``key`` of a module's own schema at its fixed path (the ``[/]`` section of a module file)."""
    if facts.relocatable:
        return Unresolved(f"{facts.schema} is relocatable, a module preset needs a schema with a fixed path")
    if facts.directory is None:
        return Unresolved(f"schema {facts.schema} is not installed and its schemas/*.gschema.xml was not found")
    if facts.installed:
        if not backend.has_key(facts.schema, key):
            return Unresolved(f"no key {key} in {facts.schema}")
        return ResolvedKey(facts.schema, None, facts.directory, key)
    if facts.keys is not None and key not in facts.keys:
        return Unresolved(f"no key {key} in {facts.schema}")
    pending_type = facts.keys.get(key) if facts.keys is not None else None
    return ResolvedKey(facts.schema, None, facts.directory, key, pending_type)


def enabled_modules(ctx: Context) -> tuple[tuple[Manifest, ...], Config]:
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    return tuple(manifest for manifest in manifests if config.module_enabled(manifest.name)), config


@dataclass(frozen=True)
class Entry:
    phase: Phase
    scope: str
    source: str
    target: ResolvedKey
    text: str
    seed: bool = False
    rank: int = 0  # inside a phase, a higher rank wins (a module override beats every module preset)
    reason: str = ""  # the bundle comment above the key, shown next to it in the preview


def collapse(entries: Iterable[Entry]) -> list[Entry]:
    """One entry per ``(directory, key)``: the highest phase wins, then the highest rank, then the
    last one."""
    winners: dict[tuple[str, str], Entry] = {}
    for entry in sorted(entries, key=lambda item: (item.phase, item.rank)):
        winners[(entry.target.directory, entry.target.key)] = entry
    return list(winners.values())


class Skips:
    """Keys that were left out, grouped per file and section so the preview stays readable."""

    def __init__(self) -> None:
        self._groups: dict[tuple[str, str, str], list[str]] = {}

    def add(self, source: str, section: str, key: str, reason: str) -> None:
        self._groups.setdefault((source, section, reason), []).append(key)

    def lines(self) -> list[str]:
        return [
            f"skipped {source}{f' [{section}]' if section else ''} {', '.join(keys)}: {reason}"
            for (source, section, reason), keys in self._groups.items()
        ]


NO_SETTINGS_SCHEMA = "no settings schema: metadata.json has no settings-schema"


def short(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_text(backend: SettingsBackend, target: ResolvedKey, text: str, where: str) -> None:
    """A value that cannot be parsed as the key's type is invalid config, not a skipped key. The type
    comes from the installed schema, else from the extension's schema XML (enum keys are not checked)."""
    try:
        if backend.has_key(target.schema, target.key):
            wanted = parse_value(backend, target.schema, target.key, text)
            if not backend.in_range(target.schema, target.key, wanted):
                raise ConfigError(f"{where}: {text} is outside the allowed range of {target.schema} {target.key}")
        elif target.pending_type:
            GLib.Variant.parse(GLib.VariantType.new(target.pending_type), text)
    except SettingsError as error:
        raise ConfigError(f"{where}: {error}") from error
    except GLib.Error as error:
        raise ConfigError(f"{where}: {text!r} is not a valid {target.pending_type!r} value: {error.message}") from error


class _KeyAction(Action):
    def __init__(self, backend: SettingsBackend, entry: Entry) -> None:
        self._backend = backend
        self.entry = entry
        self.phase = entry.phase
        self.scope = entry.scope

    @property
    def target(self) -> ResolvedKey:
        return self.entry.target

    def _installed(self) -> bool:
        return self._backend.has_key(self.target.schema, self.target.key)

    def _wanted(self) -> Variant:
        return parse_value(self._backend, self.target.schema, self.target.key, self.entry.text)

    def _wanted_text(self) -> str:
        if self._installed():
            return format_value(self._wanted())
        return self.entry.text

    def _tail(self) -> str:
        pending = "" if self._installed() else f" (schema {self.target.schema} is not installed yet)"
        reason = f"  # {short(self.entry.reason, 80)}" if self.entry.reason else ""
        return f"{pending} <- {self.entry.source}{reason}"


class SetKeyAction(_KeyAction):
    kind = "set-key"

    def check(self) -> bool:
        if not self._installed():
            return False
        target = self.target
        return is_satisfied(self._backend, target.schema, target.path, target.key, self._wanted())

    def describe(self) -> str:
        target = self.target
        text = f"{target.full} = {short(self._wanted_text())}"
        if self._installed():
            now = format_value(self._backend.effective(target.schema, target.path, target.key))
            text += f" (now {short(now, 60)})"
        return text + self._tail()

    def run(self) -> None:
        target = self.target
        self._backend.write(target.schema, target.path, target.key, self._wanted())


class SeedKeyAction(_KeyAction):
    """Write while the key has no user value; satisfied for good once it has one."""

    kind = "seed-key"

    def __init__(self, backend: SettingsBackend, entry: Entry, ensure_dirs: Sequence[Path] = ()) -> None:
        super().__init__(backend, entry)
        self._ensure_dirs = tuple(ensure_dirs)

    def check(self) -> bool:
        if not self._installed():
            return False
        target = self.target
        return self._backend.user_value(target.schema, target.path, target.key) is not None

    def describe(self) -> str:
        text = f"{self.target.full} = {short(self._wanted_text())} (only while unset)"
        if self._ensure_dirs:
            text += f"; creates {', '.join(str(path) for path in self._ensure_dirs)}"
        return text + self._tail()

    def run(self) -> None:
        if self.check():  # a user value appeared since the plan was built (an earlier action, another process)
            return
        for directory in self._ensure_dirs:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise SettingsError(f"cannot create {directory}: {error.strerror or error}") from error
        target = self.target
        self._backend.write(target.schema, target.path, target.key, self._wanted())

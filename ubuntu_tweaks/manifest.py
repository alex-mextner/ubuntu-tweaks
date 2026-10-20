"""Parse and validate ``modules/<name>/module.toml`` into frozen dataclasses.

Accessed via: every command that lists or applies modules (``list``, ``apply``, ``status``);
the engine calls ``discover_modules(ctx.modules_dir)``.

Assumptions: validation is strict (unknown keys, wrong types and kinds that lack the fields
they need are all ``ConfigError``, exit 2) so a typo in a manifest fails loudly instead of
silently dropping a setting. Nothing here touches the system.

Public API:

- ``SourceKind``: ``SUBMODULE``, ``LOCAL``, ``BUILTIN``, ``SYSTEM`` (string values
  ``"submodule"``, ``"local"``, ``"builtin"``, ``"system"``). ``ego`` is reserved and rejected
  with ``not supported yet (#20)``.
- ``SettingsMode``: ``ENFORCE`` (default) or ``SEED``.
- ``Source(kind, path, install, check)``: ``path`` is set for submodule/local (relative to
  the repo root); ``install`` and ``check`` are argv tuples, set for ``system`` only, whose
  ``argv[0]`` is a path inside the module directory: a runner must execute
  ``manifest.directory / argv[0]`` with the rest as arguments, never a ``$PATH`` lookup.
- ``ExtensionSpec(conflicts, retire)``, ``SettingsFile(file, schema, mode)``,
  ``Seed(key, provider)``, ``Migration(from_uuid, from_path)``, ``Deps(apt)``.
- ``Manifest(name, directory, description, enabled, source, extension, settings, seeds,
  migrations, deps)``; ``enabled`` is the module default that ``profile.toml`` /
  ``machine.toml`` can override.
- ``parse_manifest(name, text, directory) -> Manifest``.
- ``load_manifest(directory) -> Manifest`` (module name = directory name).
- ``discover_modules(modules_dir) -> tuple[Manifest, ...]``: sorted by name; directories
  without ``module.toml`` are not modules yet and are skipped.
- ``module_defaults(manifests) -> TomlTable``: ``{"modules": {name: {"enabled": bool}}}``, the
  lowest layer for ``Profile.load_config``.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.profile import TomlTable, TomlValue

MANIFEST_FILE = "module.toml"
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_EGO_MESSAGE = "source kind 'ego' is not supported yet (#20)"


class SourceKind(StrEnum):
    SUBMODULE = "submodule"
    LOCAL = "local"
    BUILTIN = "builtin"
    SYSTEM = "system"


class SettingsMode(StrEnum):
    ENFORCE = "enforce"
    SEED = "seed"


@dataclass(frozen=True)
class Source:
    kind: SourceKind
    path: str | None = None
    install: tuple[str, ...] = ()
    check: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtensionSpec:
    conflicts: tuple[str, ...] = ()
    retire: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettingsFile:
    file: str
    schema: str | None = None
    mode: SettingsMode = SettingsMode.ENFORCE


@dataclass(frozen=True)
class Seed:
    key: str
    provider: str


@dataclass(frozen=True)
class Migration:
    from_uuid: str | None = None
    from_path: str | None = None


@dataclass(frozen=True)
class Deps:
    apt: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    name: str
    directory: Path
    description: str
    enabled: bool
    source: Source
    extension: ExtensionSpec | None = None
    settings: tuple[SettingsFile, ...] = ()
    seeds: tuple[Seed, ...] = ()
    migrations: tuple[Migration, ...] = ()
    deps: Deps = Deps()


class _Reader:
    """Typed access to one TOML table; every error names the manifest and the table."""

    def __init__(self, table: Mapping[str, TomlValue], where: str) -> None:
        self.table = table
        self.where = where

    def fail(self, message: str) -> ConfigError:
        return ConfigError(f"{self.where}: {message}")

    def only(self, *allowed: str) -> None:
        unknown = sorted(set(self.table) - set(allowed))
        if unknown:
            raise self.fail(f"unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")

    def string(self, key: str) -> str | None:
        value = self.table.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise self.fail(f"'{key}' must be a non-empty string, got {value!r}")
        return value

    def required_string(self, key: str) -> str:
        value = self.string(key)
        if value is None:
            raise self.fail(f"missing required key '{key}'")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self.table.get(key, default)
        if not isinstance(value, bool):
            raise self.fail(f"'{key}' must be true or false, got {value!r}")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise self.fail(f"'{key}' must be a list of non-empty strings, got {value!r}")
        return tuple(str(item) for item in value)

    def sub(self, key: str) -> _Reader | None:
        value = self.table.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise self.fail(f"'{key}' must be a table, got {value!r}")
        return _Reader(value, f"{self.where} [{key}]")

    def array(self, key: str) -> list[_Reader]:
        value = self.table.get(key, [])
        if not isinstance(value, list):
            raise self.fail(f"'{key}' must be an array of tables ([[{key}]])")
        readers: list[_Reader] = []
        for index, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise self.fail(f"'{key}' must be an array of tables ([[{key}]])")
            readers.append(_Reader(item, f"{self.where} [[{key}]] #{index}"))
        return readers


def _relative_path(reader: _Reader, value: str, key: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise reader.fail(f"'{key}' must be a relative path without '..', got {value!r}")


def _command(reader: _Reader, key: str) -> tuple[str, ...]:
    text = reader.required_string(key)
    try:
        argv = tuple(shlex.split(text))
    except ValueError as error:
        raise reader.fail(f"'{key}' is not a valid command line: {error}") from error
    if not argv or not argv[0]:
        raise reader.fail(f"'{key}' must name a program inside the module, got {text!r}")
    _relative_path(reader, argv[0], key)
    return argv


def _parse_source(reader: _Reader) -> Source:
    kind_text = reader.required_string("kind")
    if kind_text == "ego":
        raise reader.fail(_EGO_MESSAGE)
    try:
        kind = SourceKind(kind_text)
    except ValueError:
        valid = ", ".join(kind.value for kind in SourceKind)
        raise reader.fail(f"unknown source kind {kind_text!r}; valid kinds: {valid}") from None
    if kind in (SourceKind.SUBMODULE, SourceKind.LOCAL):
        reader.only("kind", "path")
        path = reader.required_string("path")
        _relative_path(reader, path, "path")
        return Source(kind, path=path)
    if kind is SourceKind.SYSTEM:
        reader.only("kind", "install", "check")
        return Source(kind, install=_command(reader, "install"), check=_command(reader, "check"))
    reader.only("kind")
    return Source(kind)


def _parse_extension(reader: _Reader) -> ExtensionSpec:
    reader.only("conflicts", "retire")
    return ExtensionSpec(conflicts=reader.strings("conflicts"), retire=reader.strings("retire"))


def _parse_settings(reader: _Reader, kind: SourceKind) -> SettingsFile:
    reader.only("file", "schema", "mode")
    file = reader.required_string("file")
    _relative_path(reader, file, "file")
    schema = reader.string("schema")
    if schema is None and kind is SourceKind.BUILTIN:
        raise reader.fail("a builtin module has no extension metadata, so 'schema' is required")
    mode_text = reader.string("mode") or SettingsMode.ENFORCE.value
    try:
        mode = SettingsMode(mode_text)
    except ValueError:
        valid = ", ".join(mode.value for mode in SettingsMode)
        raise reader.fail(f"unknown mode {mode_text!r}; valid modes: {valid}") from None
    return SettingsFile(file, schema, mode)


def _parse_seed(reader: _Reader) -> Seed:
    reader.only("key", "provider")
    return Seed(reader.required_string("key"), reader.required_string("provider"))


def _parse_migration(reader: _Reader) -> Migration:
    reader.only("from_uuid", "from_path")
    from_uuid = reader.string("from_uuid")
    from_path = reader.string("from_path")
    if from_uuid is None and from_path is None:
        raise reader.fail("needs 'from_uuid' or 'from_path'")
    if from_path is not None and not (from_path.startswith("/") and from_path.endswith("/")):
        raise reader.fail(f"'from_path' must be a dconf path like /org/gnome/x/, got {from_path!r}")
    return Migration(from_uuid, from_path)


def parse_manifest(name: str, text: str, directory: Path) -> Manifest:
    where = str(directory / MANIFEST_FILE)
    if not _NAME.fullmatch(name):
        raise ConfigError(f"{where}: invalid module name {name!r}; use lowercase letters, digits, '.', '_' and '-'")
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{where}: invalid TOML: {error}") from error

    root = _Reader(table, where)
    root.only("description", "enabled", "source", "extension", "settings", "seeds", "migrations", "deps")
    source_reader = root.sub("source")
    if source_reader is None:
        raise root.fail("missing required table [source]")
    source = _parse_source(source_reader)

    extension_reader = root.sub("extension")
    has_code = source.kind in (SourceKind.SUBMODULE, SourceKind.LOCAL)
    if extension_reader is not None and not has_code:
        raise root.fail(f"[extension] is only for submodule and local modules, not {source.kind.value}")
    settings = tuple(_parse_settings(item, source.kind) for item in root.array("settings"))
    seeds = tuple(_parse_seed(item) for item in root.array("seeds"))
    migrations = tuple(_parse_migration(item) for item in root.array("migrations"))
    if source.kind is SourceKind.SYSTEM and (settings or seeds or migrations):
        raise root.fail("a system module takes no [[settings]], [[seeds]] or [[migrations]]")

    deps_reader = root.sub("deps")
    deps = Deps()
    if deps_reader is not None:
        deps_reader.only("apt")
        deps = Deps(apt=deps_reader.strings("apt"))

    return Manifest(
        name=name,
        directory=directory,
        description=root.string("description") or "",
        enabled=root.boolean("enabled", True),
        source=source,
        extension=_parse_extension(extension_reader) if extension_reader is not None else None,
        settings=settings,
        seeds=seeds,
        migrations=migrations,
        deps=deps,
    )


def load_manifest(directory: Path) -> Manifest:
    path = directory / MANIFEST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{path}: cannot read: {error.strerror or error}") from error
    return parse_manifest(directory.name, text, directory)


def discover_modules(modules_dir: Path) -> tuple[Manifest, ...]:
    if not modules_dir.is_dir():
        return ()
    return tuple(
        load_manifest(directory) for directory in sorted(modules_dir.iterdir()) if (directory / MANIFEST_FILE).is_file()
    )


def module_defaults(manifests: Sequence[Manifest]) -> TomlTable:
    return {"modules": {manifest.name: {"enabled": manifest.enabled} for manifest in manifests}}

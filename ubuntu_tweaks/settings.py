"""Typed access to GSettings/dconf, plus ``dconf dump`` keyfile helpers.

Accessed via: the settings, capture and status code paths. Command modules import this lazily
(inside ``run``) because it imports PyGObject (``gi``), which ``list`` / ``machine`` never need.

Assumptions:

- Values are ``GLib.Variant``s compared through the schema (``Variant.equal`` is type-strict),
  so ``5``, ``uint32 5`` and an unset key at its default compare correctly: parse wanted text
  with ``parse_value`` (the schema key's type), then compare with the *effective* value.
- Never write an *effective* value back: on Ubuntu the effective value can come from a session
  override (``~/.config/glib-2.0/settings/keyfile``) and writing it back would freeze the
  override into dconf. Edit list keys such as ``enabled-extensions`` as single-item changes of
  ``user_value``.
- ``path`` arguments are the dconf directory of a relocatable schema (``/a/b/``, with leading
  and trailing slash); pass ``None`` for a schema with a fixed path.

Public API:

- ``SettingsError`` (exit 1): missing schema or key, relocatable schema without a path, a
  value of the wrong type or outside the key's range.
- ``UserKey(path, key)``: one key that has a user value; ``path`` is the dconf directory
  (``/org/gnome/desktop/interface/``), ``full`` is ``path + key``.
- ``SettingsBackend`` (ABC): ``schema_dirs``, ``has_schema(schema)``, ``has_key(schema, key)``,
  ``key_type(schema, key) -> str``, ``effective(schema, path, key) -> Variant``,
  ``user_value(schema, path, key) -> Variant | None``, ``default(schema, path, key) ->
  Variant``, ``write(schema, path, key, value)``, ``reset(schema, path, key)``,
  ``list_user_keys() -> tuple[UserKey, ...]``.
- ``GioBackend(extra_schema_dirs=())``: the real backend (``Gio.SettingsSchemaSource`` chained
  over the system schemas and ``extra_schema_dirs``, a later directory taking precedence over
  an earlier one and over the system; ``Gio.Settings.new_full`` with a path for relocatable
  schemas; ``dconf dump /`` for ``list_user_keys``). ``GioBackend.with_extensions(home=None)``
  adds every installed GNOME Shell extension's ``schemas/`` directory, ``~/.local`` last so it
  wins like it does in the Shell.
- ``extension_schema_dirs(roots) -> tuple[Path, ...]`` (in ``roots`` order) and
  ``default_extension_roots(home)`` (ascending precedence). A dconf path must look like
  ``/a/b/`` (leading and trailing slash, no ``//``) or ``SettingsError`` is raised.
- ``FakeSchema(id, keys, path=None)``, ``FakeKey(type, default, minimum=None, maximum=None)`` and
  ``FakeBackend(schemas)``
  for tests (``set_user(schema, path, key, text)`` seeds a user value; ``writes`` records
  ``write`` calls as ``(schema, path, key, value)`` and ``resets`` the ``reset`` calls).
- ``parse_value(backend, schema, key, text) -> Variant`` and ``format_value(value) -> str``
  (GVariant text).
- ``is_satisfied(backend, schema, path, key, wanted) -> bool``: effective value equals
  ``wanted``.
- Keyfile: ``KeyFileData`` (``{section: {key: gvariant_text}}``), ``parse_keyfile(text)``,
  ``read_keyfile(path)``, ``dump_keyfile(data)`` (sorted, ``dconf dump`` syntax),
  ``write_keyfile(path, data)``; ``section_to_path(section)`` / ``path_to_section(path)``
  convert ``org/gnome/x`` <-> ``/org/gnome/x/`` (root: ``/``).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from ubuntu_tweaks.dconf_path import is_dconf_dir_path  # noqa: E402
from ubuntu_tweaks.errors import ConfigError, UbuntuTweakError  # noqa: E402

type Variant = GLib.Variant
type KeyFileData = dict[str, dict[str, str]]


class SettingsError(UbuntuTweakError):
    """A schema, key or value problem while reading or writing settings."""


@dataclass(frozen=True)
class UserKey:
    path: str
    key: str

    @property
    def full(self) -> str:
        return self.path + self.key


class SettingsBackend(ABC):
    @property
    @abstractmethod
    def schema_dirs(self) -> tuple[Path, ...]:
        """Extra schema directories searched besides the system ones."""

    @abstractmethod
    def has_schema(self, schema: str) -> bool: ...

    @abstractmethod
    def has_key(self, schema: str, key: str) -> bool: ...

    @abstractmethod
    def key_type(self, schema: str, key: str) -> str:
        """The key's GVariant type string, e.g. ``"u"`` or ``"as"``."""

    @abstractmethod
    def effective(self, schema: str, path: str | None, key: str) -> Variant:
        """User value if set, else the schema default (including system overrides)."""

    @abstractmethod
    def user_value(self, schema: str, path: str | None, key: str) -> Variant | None:
        """The value stored in the user's dconf, or ``None`` when the key is unset."""

    @abstractmethod
    def default(self, schema: str, path: str | None, key: str) -> Variant: ...

    @abstractmethod
    def write(self, schema: str, path: str | None, key: str, value: Variant) -> None: ...

    @abstractmethod
    def reset(self, schema: str, path: str | None, key: str) -> None:
        """Drop the user value so the key falls back to its default."""

    @abstractmethod
    def list_user_keys(self) -> tuple[UserKey, ...]:
        """Every key that has a user value, with or without an installed schema."""


def parse_value(backend: SettingsBackend, schema: str, key: str, text: str) -> Variant:
    type_string = backend.key_type(schema, key)
    try:
        return GLib.Variant.parse(GLib.VariantType.new(type_string), text)
    except GLib.Error as error:
        raise SettingsError(
            f"{schema} {key}: {text!r} is not a valid {type_string!r} value: {error.message}"
        ) from error


def format_value(value: Variant) -> str:
    return value.print_(True)


def is_satisfied(backend: SettingsBackend, schema: str, path: str | None, key: str, wanted: Variant) -> bool:
    return backend.effective(schema, path, key).equal(wanted)


def _resolve_dconf_path(schema: str, fixed: str | None, path: str | None) -> str:
    """The dconf directory a schema's keys live at: its fixed path, or ``path`` for a relocatable
    schema. Shared by both backends so the fake refuses exactly what the real one would."""
    if fixed is not None:
        if path is not None and path != fixed:
            raise SettingsError(f"schema {schema} has the fixed path {fixed}, not {path}")
        return fixed
    if path is None:
        raise SettingsError(f"schema {schema} is relocatable: a dconf path is required")
    if not is_dconf_dir_path(path):
        raise SettingsError(
            f"{path!r} is not a dconf directory path (/a/b/: slashes around, no empty, '.' or '..' part)"
        )
    return path


def _check_value_type(schema: str, key: str, expected: str, value: Variant) -> None:
    if value.get_type_string() != expected:
        raise SettingsError(f"{schema} {key}: expected type {expected!r}, got {value.get_type_string()!r}")


class GioBackend(SettingsBackend):
    def __init__(self, extra_schema_dirs: Sequence[Path] = ()) -> None:
        self._dirs = tuple(extra_schema_dirs)
        source = Gio.SettingsSchemaSource.get_default()
        for directory in self._dirs:
            try:
                source = Gio.SettingsSchemaSource.new_from_directory(str(directory), source, False)
            except GLib.Error as error:
                raise SettingsError(f"cannot load schemas from {directory}: {error.message}") from error
        self._source = source
        self._settings: dict[tuple[str, str | None], Gio.Settings] = {}

    @classmethod
    def with_extensions(cls, home: Path | None = None) -> GioBackend:
        return cls(extension_schema_dirs(default_extension_roots(home or Path.home())))

    @property
    def schema_dirs(self) -> tuple[Path, ...]:
        return self._dirs

    def _schema(self, schema: str) -> Gio.SettingsSchema:
        found = self._source.lookup(schema, True) if self._source is not None else None
        if found is None:
            raise SettingsError(f"schema {schema} is not installed")
        return found

    def has_schema(self, schema: str) -> bool:
        return self._source is not None and self._source.lookup(schema, True) is not None

    def has_key(self, schema: str, key: str) -> bool:
        return self.has_schema(schema) and self._schema(schema).has_key(key)

    def _key(self, schema: str, key: str) -> Gio.SettingsSchemaKey:
        definition = self._schema(schema)
        if not definition.has_key(key):
            raise SettingsError(f"schema {schema} has no key {key}")
        return definition.get_key(key)

    def key_type(self, schema: str, key: str) -> str:
        return self._key(schema, key).get_value_type().dup_string()

    def _open(self, schema: str, path: str | None, key: str) -> Gio.Settings:
        self._key(schema, key)
        cached = self._settings.get((schema, path))
        if cached is not None:
            return cached
        definition = self._schema(schema)
        fixed = definition.get_path()
        resolved = _resolve_dconf_path(schema, fixed, path)
        settings = Gio.Settings.new_full(definition, None, None if fixed is not None else resolved)
        self._settings[(schema, path)] = settings
        return settings

    def effective(self, schema: str, path: str | None, key: str) -> Variant:
        return self._open(schema, path, key).get_value(key)

    def user_value(self, schema: str, path: str | None, key: str) -> Variant | None:
        return self._open(schema, path, key).get_user_value(key)

    def default(self, schema: str, path: str | None, key: str) -> Variant:
        return self._open(schema, path, key).get_default_value(key)

    def write(self, schema: str, path: str | None, key: str, value: Variant) -> None:
        _check_value_type(schema, key, self.key_type(schema, key), value)
        if not self._key(schema, key).range_check(value):
            raise SettingsError(f"{schema} {key}: {format_value(value)} is outside the allowed range")
        if not self._open(schema, path, key).set_value(key, value):
            raise SettingsError(f"{schema} {key}: {format_value(value)} is read-only")
        Gio.Settings.sync()

    def reset(self, schema: str, path: str | None, key: str) -> None:
        self._open(schema, path, key).reset(key)
        Gio.Settings.sync()

    def list_user_keys(self) -> tuple[UserKey, ...]:
        try:
            dump = subprocess.run(["dconf", "dump", "/"], capture_output=True, text=True, check=True, timeout=60).stdout
        except FileNotFoundError as error:
            raise SettingsError("dconf is not installed (package dconf-cli)") from error
        except subprocess.TimeoutExpired as error:
            raise SettingsError("dconf dump timed out after 60 s") from error
        except subprocess.CalledProcessError as error:
            raise SettingsError(f"dconf dump failed: {error.stderr.strip()}") from error
        data = parse_keyfile(dump)
        return tuple(
            UserKey(section_to_path(section), key) for section, keys in sorted(data.items()) for key in sorted(keys)
        )


def default_extension_roots(home: Path, env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Ascending precedence: the user's copy (``$XDG_DATA_HOME``, else ``<home>/.local/share``; the
    one GNOME Shell loads) beats a system one."""
    environment = os.environ if env is None else env
    user_data = Path(environment.get("XDG_DATA_HOME") or home / ".local/share")
    return (
        Path("/usr/share/gnome-shell/extensions"),
        Path("/usr/local/share/gnome-shell/extensions"),
        user_data / "gnome-shell/extensions",
    )


def extension_schema_dirs(roots: Sequence[Path]) -> tuple[Path, ...]:
    """``<root>/<uuid>/schemas`` for every extension that ships a compiled schema."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for extension in sorted(root.iterdir()):
            schemas = extension / "schemas"
            if (schemas / "gschemas.compiled").is_file():
                found.append(schemas)
    return tuple(found)


@dataclass(frozen=True)
class FakeKey:
    type: str
    default: str
    minimum: str | None = None  # GVariant text, like ``default``; a key's <range min max>
    maximum: str | None = None


@dataclass(frozen=True)
class FakeSchema:
    id: str
    keys: Mapping[str, FakeKey]
    path: str | None = None


class FakeBackend(SettingsBackend):
    def __init__(self, schemas: Sequence[FakeSchema] = ()) -> None:
        self._schemas = {schema.id: schema for schema in schemas}
        self._user: dict[tuple[str, str], Variant] = {}
        self.writes: list[tuple[str, str | None, str, Variant]] = []
        self.resets: list[tuple[str, str | None, str]] = []

    @property
    def schema_dirs(self) -> tuple[Path, ...]:
        return ()

    def _schema(self, schema: str) -> FakeSchema:
        if schema not in self._schemas:
            raise SettingsError(f"schema {schema} is not installed")
        return self._schemas[schema]

    def _key(self, schema: str, key: str) -> FakeKey:
        keys = self._schema(schema).keys
        if key not in keys:
            raise SettingsError(f"schema {schema} has no key {key}")
        return keys[key]

    def _location(self, schema: str, path: str | None, key: str) -> tuple[str, str]:
        self._key(schema, key)
        return _resolve_dconf_path(schema, self._schema(schema).path, path), key

    def has_schema(self, schema: str) -> bool:
        return schema in self._schemas

    def has_key(self, schema: str, key: str) -> bool:
        return schema in self._schemas and key in self._schemas[schema].keys

    def key_type(self, schema: str, key: str) -> str:
        return self._key(schema, key).type

    def effective(self, schema: str, path: str | None, key: str) -> Variant:
        user = self.user_value(schema, path, key)
        return user if user is not None else self.default(schema, path, key)

    def user_value(self, schema: str, path: str | None, key: str) -> Variant | None:
        return self._user.get(self._location(schema, path, key))

    def default(self, schema: str, path: str | None, key: str) -> Variant:
        self._location(schema, path, key)
        return parse_value(self, schema, key, self._key(schema, key).default)

    def write(self, schema: str, path: str | None, key: str, value: Variant) -> None:
        location = self._location(schema, path, key)
        _check_value_type(schema, key, self.key_type(schema, key), value)
        definition = self._key(schema, key)
        for bound, text, outside in (
            ("minimum", definition.minimum, lambda low: value.unpack() < low),
            ("maximum", definition.maximum, lambda high: value.unpack() > high),
        ):
            if text is not None and outside(parse_value(self, schema, key, text).unpack()):
                raise SettingsError(
                    f"{schema} {key}: {format_value(value)} is outside the allowed range ({bound} {text})"
                )
        self._user[location] = value
        self.writes.append((schema, path, key, value))

    def reset(self, schema: str, path: str | None, key: str) -> None:
        self._user.pop(self._location(schema, path, key), None)
        self.resets.append((schema, path, key))

    def set_user(self, schema: str, path: str | None, key: str, text: str) -> None:
        """Seed a user value without recording it in ``writes``."""
        self._user[self._location(schema, path, key)] = parse_value(self, schema, key, text)

    def list_user_keys(self) -> tuple[UserKey, ...]:
        return tuple(UserKey(directory, key) for directory, key in sorted(self._user))


def section_to_path(section: str) -> str:
    return "/" if section == "/" else f"/{section.strip('/')}/"


def path_to_section(path: str) -> str:
    return "/" if path == "/" else path.strip("/")


def parse_keyfile(text: str) -> KeyFileData:
    keyfile = GLib.KeyFile()
    try:
        keyfile.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    except GLib.Error as error:
        raise ConfigError(f"invalid keyfile: {error.message}") from error
    data: KeyFileData = {}
    for group in keyfile.get_groups()[0]:
        data[group] = {key: keyfile.get_value(group, key) for key in keyfile.get_keys(group)[0]}
    return data


def read_keyfile(path: Path) -> KeyFileData:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{path}: cannot read: {error.strerror or error}") from error
    try:
        return parse_keyfile(text)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from error


def dump_keyfile(data: Mapping[str, Mapping[str, str]]) -> str:
    keyfile = GLib.KeyFile()
    for section in sorted(data):
        for key in sorted(data[section]):
            keyfile.set_value(section, key, data[section][key])
    return keyfile.to_data()[0]


def write_keyfile(path: Path, data: Mapping[str, Mapping[str, str]]) -> None:
    """Write atomically (unique temp file in the same directory, then rename) so an interrupted or
    concurrent write never leaves or publishes a truncated panel file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(dump_keyfile(data))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

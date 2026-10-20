"""Which GSettings schema owns a dconf key: the lookup `capture` needs and `SettingsBackend` lacks.

Accessed via: ``settings_scan.py``. ``SettingsBackend`` reads a key given its schema id, but
``dconf dump`` only yields dconf paths, so this maps ``/org/gnome/desktop/interface/`` back to
``org.gnome.desktop.interface``.

Assumptions: fixed-path schemas are found from the schema source (a directory may be shared by
several schemas; the one that has the key wins). A relocatable schema cannot be found from a path,
so ``panels.toml`` lists its path pattern (``Rules.relocatable_schema``); a path no schema claims
resolves to ``None`` (class ``no-schema``).

Public API: ``SchemaCatalog(fixed)`` (``fixed``: dconf directory -> schema ids),
``gio_catalog(extra_schema_dirs)`` (the real catalog, same chain as ``GioBackend``),
``fake_catalog(schemas)`` (from ``FakeSchema``s, for tests), ``resolve(backend, catalog, rules,
directory, key) -> ResolvedKey | None``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gio, GLib

from ubuntu_tweaks.capture.rules import Rules
from ubuntu_tweaks.settings import FakeSchema, SettingsBackend, SettingsError


@dataclass(frozen=True)
class ResolvedKey:
    schema: str
    path: str | None  # the dconf directory for a relocatable schema, else None


@dataclass(frozen=True)
class SchemaCatalog:
    fixed: Mapping[str, tuple[str, ...]]


def gio_catalog(extra_schema_dirs: Sequence[Path] = ()) -> SchemaCatalog:
    source = Gio.SettingsSchemaSource.get_default()
    for directory in extra_schema_dirs:
        try:
            source = Gio.SettingsSchemaSource.new_from_directory(str(directory), source, False)
        except GLib.Error as error:
            raise SettingsError(f"cannot load schemas from {directory}: {error.message}") from error
    fixed: dict[str, list[str]] = {}
    if source is not None:
        non_relocatable, _relocatable = source.list_schemas(True)
        for schema_id in sorted(non_relocatable):
            schema = source.lookup(schema_id, True)
            if schema is not None and (path := schema.get_path()) is not None:
                fixed.setdefault(path, []).append(schema_id)
    return SchemaCatalog({path: tuple(ids) for path, ids in fixed.items()})


def fake_catalog(schemas: Sequence[FakeSchema]) -> SchemaCatalog:
    fixed: dict[str, list[str]] = {}
    for schema in schemas:
        if schema.path is not None:
            fixed.setdefault(schema.path, []).append(schema.id)
    return SchemaCatalog({path: tuple(ids) for path, ids in fixed.items()})


def resolve(
    backend: SettingsBackend, catalog: SchemaCatalog, rules: Rules, directory: str, key: str
) -> ResolvedKey | None:
    for schema in catalog.fixed.get(directory, ()):
        if backend.has_key(schema, key):
            return ResolvedKey(schema, None)
    relocatable = rules.relocatable_schema(directory)
    if relocatable is not None and backend.has_key(relocatable, key):
        return ResolvedKey(relocatable, directory)
    return None

"""What the installed schemas say about their keys: the summary (the English fallback text of a key).

Accessed via: ``show.collect``. ``SettingsBackend`` answers types and values but not the schema's own
documentation, so ``show`` reads it here: ``GioCatalog`` from the compiled schemas (system plus the
extension schema directories the backend already knows), ``StaticCatalog`` for tests.

Public API: ``KeyMeta(summary)`` and the ``SchemaCatalog`` protocol:
``keys(schema) -> tuple[str, ...]`` and ``meta(schema, key) -> KeyMeta``. Read-only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class KeyMeta:
    summary: str = ""


class SchemaCatalog(Protocol):
    def keys(self, schema: str) -> tuple[str, ...]: ...

    def meta(self, schema: str, key: str) -> KeyMeta: ...


class StaticCatalog:
    def __init__(self, metas: Mapping[tuple[str, str], KeyMeta]) -> None:
        self._metas = dict(metas)

    def keys(self, schema: str) -> tuple[str, ...]:
        return tuple(sorted(key for (name, key) in self._metas if name == schema))

    def meta(self, schema: str, key: str) -> KeyMeta:
        return self._metas.get((schema, key), KeyMeta())


class GioCatalog:
    def __init__(self, extra_schema_dirs: Sequence[Path] = ()) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        source = Gio.SettingsSchemaSource.get_default()
        for directory in extra_schema_dirs:
            try:
                source = Gio.SettingsSchemaSource.new_from_directory(str(directory), source, False)
            except GLib.Error:
                continue  # the backend reports a broken directory; a catalog without it only loses texts
        self._source = source

    def _schema(self, schema: str):  # noqa: ANN202 (a Gio.SettingsSchema; gi stubs are not available)
        return self._source.lookup(schema, True) if self._source is not None else None

    def keys(self, schema: str) -> tuple[str, ...]:
        found = self._schema(schema)
        return tuple(sorted(found.list_keys())) if found is not None else ()

    def meta(self, schema: str, key: str) -> KeyMeta:
        found = self._schema(schema)
        if found is None or not found.has_key(key):
            return KeyMeta()
        return KeyMeta(found.get_key(key).get_summary() or "")

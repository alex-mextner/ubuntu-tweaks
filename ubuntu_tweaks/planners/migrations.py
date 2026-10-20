"""Migrations planner: a module's ``[[migrations]]`` (an old extension uuid / dconf path replaced by
the module's current one) as ``migrate`` actions, ahead of schemas and settings.

Accessed via: planner discovery (``build``).

One action per migration; it is satisfied when none of these three steps is pending:

1. **copy**: ``from_path`` holds user values, the module's schema directory holds none, so the old
   values are copied key by key (raw GVariant text through ``dconf dump`` / ``dconf load``, no
   typing: the old schema may be gone). Only keys the module's schema has are copied. The old
   path is left as it is. Needs ``from_path``.
2. **drop**: ``from_uuid`` is in the *user value* of ``enabled-extensions``; it is removed as a
   single-item edit of that value (an override-provided value is never frozen into dconf).
3. **unlink**: ``<user extension dir>/<from_uuid>`` is a symlink that points into this repo (a
   link to anywhere else, or a real directory, is not ours and is left alone).

Assumptions: it runs before the schemas and link actions, so the module's new schema may not be
installed yet; its key names then come from the extension's ``schemas/*.gschema.xml``. A migration
of a module whose schema cannot be located only does steps 2 and 3.

Public API: ``build(ctx, backend, *, process=None, home=None, extension_root=None)``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import Migration
from ubuntu_tweaks.process import Process
from ubuntu_tweaks.setkey import SchemaFacts, SchemaIndex, enabled_modules, module_schema_facts, module_schema_id, short
from ubuntu_tweaks.settings import (
    SettingsBackend,
    SettingsError,
    default_extension_roots,
    dump_keyfile,
    parse_keyfile,
)

from gi.repository import GLib  # noqa: E402  (settings.py has required the GLib typelib version)

SHELL_SCHEMA = "org.gnome.shell"
ENABLED_KEY = "enabled-extensions"


class MigrateAction(Action):
    phase = Phase.MIGRATE
    kind = "migrate"

    def __init__(
        self,
        module: str,
        migration: Migration,
        facts: SchemaFacts | None,
        *,
        backend: SettingsBackend,
        process: Process,
        extension_root: Path,
        repo_root: Path,
    ) -> None:
        self.scope = module
        self._migration = migration
        self._facts = facts
        self._backend = backend
        self._process = process
        self._link = extension_root / migration.from_uuid if migration.from_uuid else None
        self._repo_root = os.path.normpath(repo_root)

    def _dump(self, directory: str) -> dict[str, str]:
        result = self._process.query(["dconf", "dump", directory])
        if not result.ok:
            return {}
        try:
            return parse_keyfile(result.stdout).get("/", {})
        except ConfigError:
            return {}

    def _key_known(self, key: str) -> bool:
        facts = self._facts
        if facts is None:
            return False
        if facts.installed:
            return self._backend.has_key(facts.schema, key)
        return facts.keys is not None and key in facts.keys

    def _copy(self) -> dict[str, str]:
        facts, from_path = self._facts, self._migration.from_path
        if from_path is None or facts is None or facts.directory is None:
            return {}
        old = self._dump(from_path)
        if not old or self._dump(facts.directory):
            return {}
        return {key: text for key, text in old.items() if self._key_known(key)}

    def _enabled_user_value(self) -> list[str] | None:
        uuid = self._migration.from_uuid
        if uuid is None or not self._backend.has_key(SHELL_SCHEMA, ENABLED_KEY):
            return None
        current = self._backend.user_value(SHELL_SCHEMA, None, ENABLED_KEY)
        if current is None:
            return None
        names = list(current.unpack())
        return names if uuid in names else None

    def _stale_link(self) -> Path | None:
        link = self._link
        if link is None or not link.is_symlink():
            return None
        target = os.path.normpath(os.path.join(link.parent, os.readlink(link)))
        inside = target == self._repo_root or target.startswith(self._repo_root.rstrip(os.sep) + os.sep)
        return link if inside else None

    def check(self) -> bool:
        return not (self._copy() or self._enabled_user_value() or self._stale_link())

    def describe(self) -> str:
        migration = self._migration
        parts: list[str] = []
        copy = self._copy()
        if copy and migration.from_path and self._facts and self._facts.directory:
            parts.append(
                f"copy {len(copy)} value(s) {migration.from_path} -> {self._facts.directory} "
                f"({short(', '.join(sorted(copy)), 80)})"
            )
        if self._enabled_user_value():
            parts.append(f"drop {migration.from_uuid} from enabled-extensions")
        link = self._stale_link()
        if link is not None:
            parts.append(f"remove symlink {link} -> {os.readlink(link)}")
        origin = migration.from_uuid or migration.from_path
        return f"{origin}: " + "; ".join(parts)

    def run(self) -> None:
        copy = self._copy()
        if copy and self._facts and self._facts.directory:
            self._process.execute(["dconf", "load", self._facts.directory], input_text=dump_keyfile({"/": copy}))
        names = self._enabled_user_value()
        if names is not None:
            kept = [name for name in names if name != self._migration.from_uuid]
            self._backend.write(SHELL_SCHEMA, None, ENABLED_KEY, GLib.Variant("as", kept))
        link = self._stale_link()
        if link is not None:
            try:
                link.unlink()
            except OSError as error:
                raise SettingsError(f"cannot remove {link}: {error.strerror or error}") from error


def build(
    ctx: Context,
    backend: SettingsBackend,
    *,
    process: Process | None = None,
    home: Path | None = None,
    extension_root: Path | None = None,
) -> Sequence[Action]:
    runner = process or Process()
    root = extension_root or default_extension_roots(home if home is not None else Path.home())[-1]
    index = SchemaIndex(backend)
    manifests, _ = enabled_modules(ctx)
    actions: list[Action] = []
    for manifest in manifests:
        if not manifest.migrations:
            continue
        schema_id = module_schema_id(ctx, manifest, manifest.settings[0] if manifest.settings else None)
        facts = module_schema_facts(ctx, manifest, index, schema_id) if schema_id else None
        actions.extend(
            MigrateAction(
                manifest.name,
                migration,
                facts,
                backend=backend,
                process=runner,
                extension_root=root,
                repo_root=ctx.repo_root,
            )
            for migration in manifest.migrations
        )
    return actions

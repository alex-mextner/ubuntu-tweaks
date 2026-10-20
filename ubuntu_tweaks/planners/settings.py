"""Settings planner: module presets, profile panel files and the machine overlay as ``set-key``
(and, for ``mode = "seed"`` presets, ``seed-key``) actions.

Accessed via: planner discovery (``build``, ``warnings``).

Layers, lowest first: ``SETTINGS_MODULE`` (an enabled module's ``[[settings]]`` keyfiles, with the
profile's / machine's ``[modules.<m>.settings]`` overrides replacing single keys), ``SETTINGS_PROFILE``
(``<profile>/settings/<panel>.ini``), ``SETTINGS_MACHINE`` (``machines/<m>/settings/<panel>.ini``).
A key set in several layers is planned once, with the winning layer's value (``setkey.collapse``).

Assumptions:

- Only the keys a file lists are enforced; an absent key is never touched.
- Section ``/`` in a module file means "the module's schema at its own path"; every other section
  is a dconf directory resolved through the installed schemas (``SchemaIndex``).
- A key without an installed schema (or without that key in the schema) is skipped and reported by
  ``warnings``; it is never guessed. A value that does not parse as the key's type is invalid
  config (``ConfigError``, exit 2); for a schema that is not installed yet the type comes from the
  extension's schema XML.
- ``enabled-extensions`` / ``disabled-extensions`` are skipped with a warning wherever they come
  from: the toggle planner owns them as single-item edits of the user value.
- An override in ``[modules.<m>.settings]`` replaces the preset entry that has that key name (and
  keeps its ``mode``); when the preset has none, the key is looked up in the module's schema. Two
  preset entries with the same key name make the override ambiguous: it is skipped with a warning.
  An override stays in the module layer, so panel files still win over it.

Public API: ``build(ctx, backend)`` and ``warnings(ctx, backend)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import Manifest, SettingsMode
from ubuntu_tweaks.profile import Config
from ubuntu_tweaks.setkey import (
    NO_SETTINGS_SCHEMA,
    MODULE_OWNED_KEYS,
    Entry,
    ResolvedKey,
    SchemaFacts,
    SchemaIndex,
    SeedKeyAction,
    SetKeyAction,
    Skips,
    Unresolved,
    collapse,
    enabled_modules,
    module_schema_facts,
    module_schema_id,
    module_target,
    validate_text,
)
from ubuntu_tweaks.settings import SettingsBackend, read_keyfile, section_to_path


@dataclass
class _Resolution:
    backend: SettingsBackend
    entries: list[Entry] = field(default_factory=list)
    skips: Skips = field(default_factory=Skips)

    def accept(
        self,
        target: ResolvedKey | Unresolved,
        *,
        section: str,
        key: str,
        text: str,
        phase: Phase,
        scope: str,
        source: str,
        seed: bool = False,
        rank: int = 0,
    ) -> None:
        """The one path every wanted value takes: skip it with a reason, or validate and keep it."""
        if isinstance(target, Unresolved):
            self.skips.add(source, section, key, target.reason)
            return
        if target.full in MODULE_OWNED_KEYS:
            self.skips.add(source, section, key, "owned by the extension toggles (modules), not by settings files")
            return
        where = f"{source} [{section}] {key}" if section else f"{source} {key}"
        validate_text(self.backend, target, text, where)
        self.entries.append(Entry(phase, scope, source, target, text, seed, rank))


def _override_source(ctx: Context, config: Config, module: str, key: str) -> str:
    layer = config.origin("modules", module, "settings", key)
    if layer == "overlay":
        return f"machines/{ctx.profile.machine}/machine.toml [modules.{module}.settings]"
    return f"profile.toml [modules.{module}.settings]"


def _first_module_target(backend: SettingsBackend, facts: Iterable[SchemaFacts], key: str) -> ResolvedKey | Unresolved:
    last = Unresolved(NO_SETTINGS_SCHEMA)
    for item in facts:
        target = module_target(item, backend, key)
        if isinstance(target, ResolvedKey):
            return target
        last = target
    return last


def _module_entries(
    ctx: Context, index: SchemaIndex, manifest: Manifest, config: Config, resolution: _Resolution
) -> None:
    backend = resolution.backend
    facts_by_schema: dict[str, SchemaFacts] = {}
    presets = _Resolution(backend, skips=resolution.skips)
    for settings_file in manifest.settings:
        source = f"modules/{manifest.name}/{settings_file.file}"
        schema_id = module_schema_id(ctx, manifest, settings_file)
        if schema_id is None:
            resolution.skips.add(source, "/", "(all keys)", NO_SETTINGS_SCHEMA)
            continue
        if schema_id not in facts_by_schema:
            facts_by_schema[schema_id] = module_schema_facts(ctx, manifest, index, schema_id)
        facts = facts_by_schema[schema_id]
        seed = settings_file.mode is SettingsMode.SEED
        for section, keys in read_keyfile(manifest.directory / settings_file.file).items():
            for key, text in keys.items():
                target = (
                    module_target(facts, backend, key)
                    if section == "/"
                    else index.resolve(section_to_path(section), key)
                )
                presets.accept(
                    target,
                    section=section,
                    key=key,
                    text=text,
                    phase=Phase.SETTINGS_MODULE,
                    scope=manifest.name,
                    source=source,
                    seed=seed,
                )
    winners = collapse(presets.entries)  # a later file replaces an earlier one's entry for the same key
    resolution.entries.extend(winners)

    overrides = config.table("modules", manifest.name, "settings")
    metadata_schema = module_schema_id(ctx, manifest) if overrides else None
    if metadata_schema is not None and metadata_schema not in facts_by_schema:
        facts_by_schema[metadata_schema] = module_schema_facts(ctx, manifest, index, metadata_schema)
    for key, value in overrides.items():
        source = _override_source(ctx, config, manifest.name, key)
        if not isinstance(value, str):
            raise ConfigError(f"{source} {key}: must be a string of GVariant text, got {value!r}")
        matches = [entry for entry in winners if entry.target.key == key]
        if len(matches) > 1:
            directories = ", ".join(entry.target.directory for entry in matches)
            resolution.skips.add(source, "", key, f"ambiguous: the preset sets this key in {directories}")
            continue
        target = matches[0].target if matches else _first_module_target(backend, facts_by_schema.values(), key)
        resolution.accept(
            target,
            section="",
            key=key,
            text=value,
            phase=Phase.SETTINGS_MODULE,
            scope=manifest.name,
            source=source,
            seed=bool(matches and matches[0].seed),
            rank=1,  # an explicit override beats every module's preset, whatever the discovery order
        )


def _panel_entries(ctx: Context, index: SchemaIndex, resolution: _Resolution) -> None:
    profile = ctx.profile
    for panel in profile.settings_panels():
        for path, phase, source in (
            (panel.shared, Phase.SETTINGS_PROFILE, f"settings/{panel.panel}.ini"),
            (panel.overlay, Phase.SETTINGS_MACHINE, f"machines/{profile.machine}/settings/{panel.panel}.ini"),
        ):
            if path is not None:
                _file_entries(path, phase, panel.panel, source, index, resolution)


def _file_entries(
    path: Path, phase: Phase, scope: str, source: str, index: SchemaIndex, resolution: _Resolution
) -> None:
    for section, keys in read_keyfile(path).items():
        directory = section_to_path(section)
        for key, text in keys.items():
            resolution.accept(
                index.resolve(directory, key),
                section=section,
                key=key,
                text=text,
                phase=phase,
                scope=scope,
                source=source,
            )


def _resolve(ctx: Context, backend: SettingsBackend) -> _Resolution:
    resolution = _Resolution(backend)
    index = SchemaIndex(backend)
    manifests, config = enabled_modules(ctx)
    for manifest in manifests:
        _module_entries(ctx, index, manifest, config, resolution)
    _panel_entries(ctx, index, resolution)
    return resolution


def build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]:
    return [
        SeedKeyAction(backend, entry) if entry.seed else SetKeyAction(backend, entry)
        for entry in collapse(_resolve(ctx, backend).entries)
    ]


def warnings(ctx: Context, backend: SettingsBackend) -> Sequence[str]:
    return _resolve(ctx, backend).skips.lines()

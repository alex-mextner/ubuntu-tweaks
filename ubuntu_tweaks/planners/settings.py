"""Settings planner: module presets and the profile's settings bundles (shared, then machine overlay) as
``set-key`` (and, for ``mode = "seed"`` presets, ``seed-key``) actions.

Accessed via: planner discovery (``build``, ``warnings``).

Layers, lowest first: ``SETTINGS_MODULE`` (an enabled module's ``[[settings]]`` keyfiles, with the
profile's / machine's ``[modules.<m>.settings]`` overrides replacing single keys), ``SETTINGS_PROFILE``
(every active shared bundle, ``<profile>/settings/**/*.ini``), ``SETTINGS_MACHINE`` (every active overlay
bundle, ``machines/<m>/settings/**/*.ini``). A key set in several layers is planned once, with the
winning layer's value (``setkey.collapse``). Bundle loading, the ``[ubuntu-tweak]`` header, the
``machines`` / ``requires-module`` filters and conflicts (``ConfigError``) are ``bundles.py``; a bundle's
scope is its name, a key's comment becomes the reason shown next to its ``set-key``.

Assumptions:

- Only the keys a file lists are enforced; an absent key is never touched.
- Section ``/`` in a module file means "the module's schema at its own path"; every other section
  is a dconf directory resolved through the installed schemas (``SchemaIndex``).
- A key without an installed schema (or without that key in the schema) is skipped and reported by
  ``warnings`` (which also reports duplicate keys across bundles); it is never guessed. A value that
  does not parse as the key's type, or lies outside its schema range, is invalid config
  (``ConfigError``, exit 2) at planning time; for a schema that is not installed yet the type comes
  from the extension's schema XML.
- ``enabled-extensions`` / ``disabled-extensions`` are skipped with a warning wherever they come
  from: the toggle planner owns them as single-item edits of the user value.
- An override in ``[modules.<m>.settings]`` replaces the preset entry that has that key name (and
  keeps its ``mode``); when the preset has none, the key is looked up in the module's schema. Two
  preset entries with the same key name make the override ambiguous: it is skipped with a warning.
  An override stays in the module layer, so bundles still win over it.

Public API: ``build(ctx, backend)``, ``warnings(ctx, backend)`` and ``declared_keys(ctx, backend)`` (the keys the
profile declares, for the coverage scanner).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

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
from ubuntu_tweaks.bundles import BundleSet, load_bundles, module_gate


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
        reason: str = "",
    ) -> None:
        """The one path every wanted value takes: skip it with a reason, or validate and keep it."""
        if isinstance(target, Unresolved):
            self.skips.add(source, section, key, target.reason, staged=target.staged)
            return
        if target.full in MODULE_OWNED_KEYS:
            self.skips.add(source, section, key, "owned by the extension toggles (modules), not by settings files")
            return
        where = f"{source} [{section}] {key}" if section else f"{source} {key}"
        validate_text(self.backend, target, text, where)
        self.entries.append(Entry(phase, scope, source, target, text, seed, rank, reason))


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


def _bundle_entries(ctx: Context, index: SchemaIndex, resolution: _Resolution) -> BundleSet:
    profile = ctx.profile
    bundles = load_bundles(profile, module_gate(ctx.modules_dir, profile))
    bundles.require_valid()
    for bundle in bundles.active():
        phase = Phase.SETTINGS_PROFILE if bundle.origin == "shared" else Phase.SETTINGS_MACHINE
        for key in bundle.keys:
            resolution.accept(
                index.resolve(section_to_path(key.section), key.key),
                section=key.section,
                key=key.key,
                text=key.value,
                phase=phase,
                scope=bundle.name,
                source=bundle.source,
                reason=key.reason,
            )
    return bundles


def _resolve(ctx: Context, backend: SettingsBackend) -> tuple[_Resolution, BundleSet]:
    resolution = _Resolution(backend)
    index = SchemaIndex(backend)
    manifests, config = enabled_modules(ctx)
    for manifest in manifests:
        _module_entries(ctx, index, manifest, config, resolution)
    return resolution, _bundle_entries(ctx, index, resolution)


def build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]:
    return [
        SeedKeyAction(backend, entry) if entry.seed else SetKeyAction(backend, entry)
        for entry in collapse(_resolve(ctx, backend)[0].entries)
    ]


def declared_keys(ctx: Context, backend: SettingsBackend) -> dict[str, str]:
    """``dconf key path -> owner`` for every key a module preset or an active bundle declares: a module's name for a
    preset, the bundle's file for a bundle. A bundle key whose schema is not installed is declared all the same."""
    resolution, bundles = _resolve(ctx, backend)
    declared = {
        entry.target.full: entry.scope if entry.phase is Phase.SETTINGS_MODULE else entry.source
        for entry in collapse(resolution.entries)
    }
    for bundle in bundles.active():
        for key in bundle.keys:
            declared.setdefault(section_to_path(key.section) + key.key, bundle.source)
    return declared


def warnings(ctx: Context, backend: SettingsBackend) -> Sequence[str]:
    resolution, bundles = _resolve(ctx, backend)
    return [*bundles.duplicate_warnings(), *resolution.skips.lines()]

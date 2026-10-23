"""Seeds planner: a module's ``[[seeds]]`` as ``seed-key`` actions.

Accessed via: planner discovery (``build`` and ``warnings``).

Assumptions: the seeded key belongs to the module's settings schema (the first ``[[settings]]``
entry's ``schema``, else the extension's ``metadata.json``). A seed is written only while the key
has no user value and never drift-checked (``check()`` is true once any user value exists). A key
that cannot be placed (schema unknown) is skipped with a warning. An unknown provider name is
invalid config (exit 2).

Public API: ``build(ctx, backend, *, home=None, env=None, localize=None)`` and
``warnings(ctx, backend)``.
"""

from __future__ import annotations

import gettext
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.seeds import SeedContext, get_provider
from ubuntu_tweaks.setkey import (
    NO_SETTINGS_SCHEMA,
    Entry,
    SchemaIndex,
    SeedKeyAction,
    Skips,
    Unresolved,
    enabled_modules,
    module_schema_facts,
    module_schema_id,
    module_target,
    validate_text,
)
from ubuntu_tweaks.settings import SettingsBackend


def _resolve(
    ctx: Context,
    backend: SettingsBackend,
    home: Path | None,
    env: Mapping[str, str] | None,
    localize: Callable[[str, str], str] | None,
) -> tuple[list[Action], Skips]:
    skips = Skips()
    actions: list[Action] = []
    seed_context = SeedContext(
        home if home is not None else Path.home(),
        os.environ if env is None else env,
        localize or gettext.dgettext,
    )
    index = SchemaIndex(backend)
    manifests, _ = enabled_modules(ctx)
    for manifest in manifests:
        if not manifest.seeds:
            continue
        schema_id = module_schema_id(ctx, manifest, manifest.settings[0] if manifest.settings else None)
        for seed in manifest.seeds:
            source = f"seed {seed.provider} (modules/{manifest.name})"
            provider = get_provider(seed.provider)
            if schema_id is None:
                skips.add(source, manifest.name, seed.key, NO_SETTINGS_SCHEMA)
                continue
            target = module_target(module_schema_facts(ctx, manifest, index, schema_id), backend, seed.key)
            if isinstance(target, Unresolved):
                skips.add(source, manifest.name, seed.key, target.reason)
                continue
            value = provider(seed_context)
            validate_text(backend, target, value.text, source)
            entry = Entry(Phase.SEED, manifest.name, source, target, value.text, seed=True)
            actions.append(SeedKeyAction(backend, entry, value.ensure_dirs))
    return actions, skips


def build(
    ctx: Context,
    backend: SettingsBackend,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    localize: Callable[[str, str], str] | None = None,
) -> Sequence[Action]:
    return _resolve(ctx, backend, home, env, localize)[0]


def warnings(ctx: Context, backend: SettingsBackend) -> Sequence[str]:
    return _resolve(ctx, backend, None, None, None)[1].lines()

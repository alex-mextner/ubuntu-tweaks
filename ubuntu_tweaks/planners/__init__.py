"""Planner discovery: every ``planners/<name>.py`` contributes actions to ``apply`` / ``status``.

Accessed via: ``reconcile.py`` (and through it ``apply``, ``status``, ``init``, ``update``).

Contract for a planner module (names starting with ``_`` are skipped):

- ``build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]``: every candidate action,
  satisfied or not; ``Plan.build`` evaluates ``check()`` and keeps the pending ones.
- ``extras(ctx: Context, backend: SettingsBackend) -> Sequence[str]`` (optional): human-readable
  lines for things that are on the machine but declared nowhere. Reported by ``status``, never
  changed.
- ``warnings(ctx: Context, backend: SettingsBackend) -> Sequence[str]`` (optional): things the
  planner skipped (a key with no installed schema); printed under the plan by ``apply`` and
  ``status``.

Public API: ``discover_planners()``, ``build_actions(ctx, backend)``, ``collect_extras(ctx,
backend)``, ``collect_warnings(ctx, backend)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import ModuleType

from ubuntu_tweaks import planners
from ubuntu_tweaks.actions import DEFAULT_ORDER, Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.discovery import discover
from ubuntu_tweaks.manifest import Manifest, discover_modules
from ubuntu_tweaks.settings import SettingsBackend


def discover_planners() -> list[ModuleType]:
    return discover(planners, kind="planner", entry="build", signature="build(ctx, backend)")


INSTALL_PHASES = frozenset({Phase.BASE, Phase.PACKAGES, Phase.SYSTEM})


def apply_module_ordering(actions: Sequence[Action], manifests: Sequence[Manifest]) -> None:
    """Give a module's actions the ``order`` its ``module.toml`` declares (the init pipeline position;
    an action whose order is not ``DEFAULT_ORDER``, like a ``[[post]]`` right after its module, keeps it,
    so ``DEFAULT_ORDER`` itself means "unset") and move its install-time actions (phases ``BASE``,
    ``PACKAGES``, ``SYSTEM``) to its declared ``phase``. The submodule, migration, schema, link, toggle,
    settings and seed phases keep theirs: they depend on each other and on the extension code."""
    by_name = {manifest.name: manifest for manifest in manifests}
    for action in actions:
        manifest = by_name.get(action.scope or "")
        if manifest is None:
            continue
        if action.order == DEFAULT_ORDER:
            action.order = manifest.order
        if manifest.phase is not None and action.phase in INSTALL_PHASES:
            action.phase = manifest.phase


def build_actions(ctx: Context, backend: SettingsBackend) -> list[Action]:
    actions = [action for module in discover_planners() for action in module.build(ctx, backend)]
    apply_module_ordering(actions, discover_modules(ctx.modules_dir))
    return actions


def _collect(attribute: str, ctx: Context, backend: SettingsBackend) -> list[str]:
    lines: list[str] = []
    for module in discover_planners():
        hook = getattr(module, attribute, None)
        if callable(hook):
            lines.extend(hook(ctx, backend))
    return lines


def collect_extras(ctx: Context, backend: SettingsBackend) -> list[str]:
    return _collect("extras", ctx, backend)


def collect_warnings(ctx: Context, backend: SettingsBackend) -> list[str]:
    return _collect("warnings", ctx, backend)

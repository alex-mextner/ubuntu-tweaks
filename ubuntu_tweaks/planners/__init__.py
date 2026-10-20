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

import importlib
import pkgutil
from types import ModuleType

from ubuntu_tweaks import planners
from ubuntu_tweaks.actions import Action
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.settings import SettingsBackend


def discover_planners() -> list[ModuleType]:
    found: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(planners.__path__), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{planners.__name__}.{info.name}")
        if not callable(getattr(module, "build", None)):
            raise ConfigError(f"planner module {module.__name__} has no build(ctx, backend)")
        found.append(module)
    return found


def build_actions(ctx: Context, backend: SettingsBackend) -> list[Action]:
    return [action for module in discover_planners() for action in module.build(ctx, backend)]


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

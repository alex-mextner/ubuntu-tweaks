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

from types import ModuleType

from ubuntu_tweaks import planners
from ubuntu_tweaks.actions import Action
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.discovery import discover
from ubuntu_tweaks.settings import SettingsBackend


def discover_planners() -> list[ModuleType]:
    return discover(planners, kind="planner", entry="build", signature="build(ctx, backend)")


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

"""Build the plan from all planners, print it, and (with ``commit``) execute it.

Accessed via: ``commands/apply.py``, ``commands/status.py``, ``commands/init.py`` and
``commands/update.py``. Imports PyGObject (through the planners and the settings backend), so
commands import it lazily inside their handler.

Assumptions:

- The preview and the commit are the same ``Plan`` (invariants 1 and 2). The one extension is
  staging: actions of phase ``SUBMODULE`` .. ``TOGGLE`` can add schemas that the settings
  backend cannot see yet (a fresh submodule, a freshly compiled or linked schema). When the plan
  holds such an action (kinds ``submodule``, ``compile-schemas``, ``link``, ``link-replace``),
  ``apply`` runs only the actions before the settings phases, opens a **fresh** backend, rebuilds
  the whole plan and continues (at most ``MAX_PASSES`` passes), so the settings of a new extension
  land in the same invocation. The preview says so.
- ``--only`` names a module; an unknown name is ``UnknownModuleError`` (exit 4).

Public API:

- ``open_backend(ctx) -> SettingsBackend``: ``GioBackend`` over the system schemas, every
  installed user extension, and the compiled schemas inside each enabled module's source tree.
- ``build_plan(ctx, backend, extra_actions=()) -> Plan``.
- ``profile_lines(ctx) -> list[str]``: the ``machine:`` / ``profile:`` header lines.
- ``apply(ctx, *, commit, no_sudo, extra_actions=(), backend_factory=open_backend,
  relogin_hint=False) -> int``: prints the preview, or executes and prints ``N changed``.
- ``RELOGIN_HINT``.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from ubuntu_tweaks.actions import Action, ActionFailed, ExecutionResult, Phase, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import RELOGIN_HINT, extension_modules, tilde
from ubuntu_tweaks.planners import build_actions, collect_warnings
from ubuntu_tweaks.identity import Location, locate, missing_lines
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import (
    GioBackend,
    SettingsBackend,
    default_extension_roots,
    extension_schema_dirs,
)

type BackendFactory = Callable[[Context], SettingsBackend]

MAX_PASSES = 3
SCHEMA_AFFECTING = frozenset({"submodule", "compile-schemas", "link", "link-replace"})
CODE_CHANGING = frozenset({"submodule", "link", "link-replace"})


def open_backend(ctx: Context) -> SettingsBackend:
    module_dirs = [
        module.schemas_dir for module in extension_modules(ctx) if (module.schemas_dir / "gschemas.compiled").is_file()
    ]
    installed = extension_schema_dirs(default_extension_roots(Path.home()))
    return GioBackend((*module_dirs, *installed))


def build_plan(ctx: Context, backend: SettingsBackend, extra_actions: Iterable[Action] = ()) -> Plan:
    manifests = ctx.manifests()
    ctx.require_modules({manifest.name for manifest in manifests})
    return Plan.build([*extra_actions, *build_actions(ctx, backend)], only=ctx.only)


def profile_lines(ctx: Context) -> list[str]:
    profile = ctx.profile
    overlay = f"overlay {tilde(profile.overlay_dir)}" if profile.has_overlay else "no overlay: shared profile only"
    lines = [f"machine: {profile.machine} ({overlay})"]
    if profile.exists:
        lines.append(f"profile: {tilde(profile.root)}")
    else:
        lines.append(f"profile: {tilde(profile.root)} (not found: module defaults only)")
        lines += missing_lines(profile.location or Location(profile.root))
    return lines


def _is_staged(plan: Plan) -> bool:
    return any(action.kind in SCHEMA_AFFECTING for action in plan.actions)


def _before_settings(plan: Plan) -> Plan:
    """The actions before any settings write: they may add schemas the backend cannot see yet."""
    return Plan(tuple(a for a in plan.actions if a.phase < Phase.SETTINGS_MODULE), plan.satisfied)


def _print_preview(ctx: Context, plan: Plan, backend: SettingsBackend, *, no_sudo: bool) -> None:
    print("\n".join(profile_lines(ctx)))
    print(plan.render(no_sudo=no_sudo))
    warnings = collect_warnings(ctx, backend)
    if warnings:
        print("Warnings:")
        print("\n".join(f"  {line}" for line in warnings))
    if _is_staged(plan):
        print(
            "Note: this plan installs extension code or schemas. Settings that depend on them are planned "
            "after those actions ran; `apply --commit` does that in the same invocation."
        )


def _arrived_profile(ctx: Context, installers_ran: bool, relocate: Callable[[], Location] | None) -> Profile | None:
    """The profile this run created: when it was missing at the start and a system module ran (``dotfiles`` may
    have just cloned the repository that holds it), look for it again; the plan built before the install knew
    only the modules. ``None`` when nothing could have created it or it is still not there."""
    if not installers_ran:
        return None
    where = relocate() if relocate is not None else locate(home=Path.home(), env=os.environ)
    return dataclasses.replace(ctx.profile, root=where.root, location=where) if where.root.is_dir() else None


def _manual_key(action: Action) -> tuple[str, str | None, str]:
    """Identity of an action across plan rebuilds: its ``key``, else its text (which may embed live state)."""
    return (action.kind, action.scope, action.key or action.describe())


def apply(
    ctx: Context,
    *,
    commit: bool,
    no_sudo: bool,
    extra_actions: Iterable[Action] = (),
    backend_factory: BackendFactory = open_backend,
    relogin_hint: bool = False,
    relocate: Callable[[], Location] | None = None,
) -> int:
    extras = tuple(extra_actions)
    backend = backend_factory(ctx)
    plan = build_plan(ctx, backend, extras)
    _print_preview(ctx, plan, backend, no_sudo=no_sudo)
    if not commit:
        if len(plan):
            print("Preview only, nothing was changed. Run with --commit to apply.")
        return 0

    executed: list[str] = []

    def announce(action: Action) -> None:
        executed.append(action.kind)
        print(f"  -> [{action.kind}] {action.describe()}", flush=True)

    changed = 0
    number = 1
    manual: dict[tuple[str, str | None, str], tuple[Action, str]] = {}
    profile_missing = not ctx.profile.exists
    while True:
        staged = _is_staged(plan)
        if staged and number == MAX_PASSES:
            raise ActionFailed(f"the plan still installs extension code after {MAX_PASSES - 1} passes")
        stage = _before_settings(plan) if staged else plan
        result = stage.execute(no_sudo=no_sudo, on_start=announce)
        changed += result.changed
        manual.update({_manual_key(action): (action, message) for action, message in result.manual})
        arrived = _arrived_profile(ctx, profile_missing and "system-module" in executed, relocate)
        if not staged and arrived is None:
            break
        if arrived is not None:  # the dotfiles module cloned the profile: the rest of the run applies it
            ctx, profile_missing = dataclasses.replace(ctx, profile=arrived), False
            print(f"profile: {tilde(arrived.root)} (cloned by this run)", flush=True)
        number += 1
        rebuilt = build_plan(ctx, backend_factory(ctx), extras)
        # a manual step reported in an earlier pass is not run again (its installer may have done work)
        plan = Plan(tuple(a for a in rebuilt.actions if _manual_key(a) not in manual), rebuilt.satisfied)
    print(f"{changed} changed")
    final = ExecutionResult(changed, result.skipped, tuple(manual.values()))
    if final.skipped:
        print(final.render_skipped())
    if relogin_hint or any(kind in CODE_CHANGING for kind in executed):
        print(RELOGIN_HINT)
    if final.manual:
        print(final.render_manual())
    return 0

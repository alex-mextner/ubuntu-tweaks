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
- ``RELOGIN_HINT``, ``CLONE_PROFILE_COMMAND``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from ubuntu_tweaks.actions import Action, ActionFailed, Phase, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import RELOGIN_HINT, extension_modules, tilde
from ubuntu_tweaks.manifest import discover_modules
from ubuntu_tweaks.planners import build_actions, collect_warnings
from ubuntu_tweaks.profile import DEFAULT_PROFILE_RELPATH
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
CLONE_PROFILE_COMMAND = "gh repo clone alex-mextner/.files ~/.files"


def open_backend(ctx: Context) -> SettingsBackend:
    module_dirs = [
        module.schemas_dir for module in extension_modules(ctx) if (module.schemas_dir / "gschemas.compiled").is_file()
    ]
    installed = extension_schema_dirs(default_extension_roots(Path.home()))
    return GioBackend((*module_dirs, *installed))


def build_plan(ctx: Context, backend: SettingsBackend, extra_actions: Iterable[Action] = ()) -> Plan:
    manifests = discover_modules(ctx.modules_dir)
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
        if profile.root == Path.home() / DEFAULT_PROFILE_RELPATH:
            lines.append(f"  to use your personal settings: {CLONE_PROFILE_COMMAND}")
    return lines


def _is_staged(plan: Plan) -> bool:
    return any(action.kind in SCHEMA_AFFECTING for action in plan.actions)


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


def apply(
    ctx: Context,
    *,
    commit: bool,
    no_sudo: bool,
    extra_actions: Iterable[Action] = (),
    backend_factory: BackendFactory = open_backend,
    relogin_hint: bool = False,
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
    for number in range(1, MAX_PASSES + 1):
        staged = _is_staged(plan)
        stage = (
            Plan(tuple(a for a in plan.actions if a.phase < Phase.SETTINGS_MODULE), plan.satisfied) if staged else plan
        )
        result = stage.execute(no_sudo=no_sudo, on_start=announce)
        changed += result.changed
        if not staged:
            break
        if number == MAX_PASSES:
            raise ActionFailed(f"the plan did not converge after {MAX_PASSES} passes")
        backend = backend_factory(ctx)
        plan = build_plan(ctx, backend, extras)
    print(f"{changed} changed")
    skipped = result.render_skipped()
    if skipped:
        print(skipped)
    if relogin_hint or any(kind in CODE_CHANGING for kind in executed):
        print(RELOGIN_HINT)
    return 0

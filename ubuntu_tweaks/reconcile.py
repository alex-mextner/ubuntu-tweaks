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
- ``header_lines(ctx, title) -> list[str]``, ``engine_version(ctx)``, ``print_warnings(lines)``: the
  explained (``ui.rich()``) header and warnings block of ``apply`` / ``status``.
- ``apply(ctx, *, commit, no_sudo, extra_actions=(), backend_factory=open_backend,
  relogin_hint=False, title="apply", preview_footer="apply.footer") -> int``: prints the preview, or
  executes and prints ``N changed`` (interactive: numbered progress and a summary, see the spec's
  *Output*). ``title`` / ``preview_footer`` let ``init`` and ``update`` frame the same preview.
- ``RELOGIN_HINT``, ``CLONE_PROFILE_COMMAND``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from ubuntu_tweaks import ui
from ubuntu_tweaks.actions import Action, ActionFailed, ExecutionResult, Phase, Plan
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


def _before_settings(plan: Plan) -> Plan:
    """The actions before any settings write: they may add schemas the backend cannot see yet."""
    return Plan(tuple(a for a in plan.actions if a.phase < Phase.SETTINGS_MODULE), plan.satisfied)


def engine_version(ctx: Context) -> str:
    """``git describe`` of the checkout the engine runs from; ``unknown`` for a tree that is not a git checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(ctx.repo_root), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else "unknown"


def header_lines(ctx: Context, title: str) -> list[str]:
    """The interactive header: the command, then machine / profile / engine as aligned rows. When the profile
    is missing, the hint lines of ``profile_lines`` (how to get it) follow in yellow."""
    profile = ctx.profile
    overlay = (
        ui.t("header.overlay", path=tilde(profile.overlay_dir)) if profile.has_overlay else ui.t("header.no_overlay")
    )
    where = tilde(profile.root) if profile.exists else ui.t("header.no_profile", path=tilde(profile.root))
    rows = [
        (ui.t("header.machine"), f"{profile.machine} ({overlay})"),
        (ui.t("header.profile"), where),
        (ui.t("header.engine"), f"ubuntu-tweaks {engine_version(ctx)}"),
    ]
    lines = [ui.heading(f"ubuntu-tweak {title}")] if title else []
    lines += ui.kv(rows)
    lines += [ui.style(line, "yellow") for line in profile_lines(ctx)[2:]]
    return lines


def print_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    if ui.rich():
        print()
        print(ui.section(ui.t("warnings"), len(warnings), "need"))
        for line in warnings:
            print("\n".join(ui.wrap(line, first="  ", names=("yellow",))))
        return
    print("Warnings:")
    print("\n".join(f"  {line}" for line in warnings))


def _print_preview(ctx: Context, plan: Plan, backend: SettingsBackend, *, no_sudo: bool, title: str) -> None:
    if ui.rich():
        print("\n".join(header_lines(ctx, title)))
        print()
    else:
        print("\n".join(profile_lines(ctx)))
    print(plan.render(no_sudo=no_sudo))
    print_warnings(collect_warnings(ctx, backend))
    if _is_staged(plan):
        note = ui.t("apply.staged")
        if ui.rich():
            print("\n".join(ui.wrap(note, first=f"{ui.symbol('info')} ", names=("cyan",))))
        else:
            print(
                "Note: this plan installs extension code or schemas. Settings that depend on them are planned "
                "after those actions ran; `apply --commit` does that in the same invocation."
            )


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
    title: str = "apply",
    preview_footer: str = "apply.footer",
) -> int:
    extras = tuple(extra_actions)
    backend = backend_factory(ctx)
    plan = build_plan(ctx, backend, extras)
    _print_preview(ctx, plan, backend, no_sudo=no_sudo, title=title)
    if not commit:
        if len(plan):
            if ui.rich():
                print()
                print("\n".join(ui.wrap(ui.t(preview_footer), first=f"{ui.symbol('info')} ", names=("cyan",))))
            else:
                print("Preview only, nothing was changed. Run with --commit to apply.")
        return 0

    executed: list[str] = []

    def runnable(pending: Plan) -> int:
        return sum(not (a.needs_root and no_sudo) for a in pending.actions)

    progress = {"number": 0, "total": 0}

    def announce(action: Action) -> None:
        executed.append(action.kind)
        progress["number"] += 1
        if ui.rich():
            print(ui.progress(progress["number"], progress["total"], "run", action.describe()), flush=True)
        else:
            print(f"  -> [{action.kind}] {action.describe()}", flush=True)

    def done(_action: Action) -> None:
        print(ui.progress(progress["number"], progress["total"], "ok", ui.style("done", "green")), flush=True)

    if ui.rich():
        print()
    changed = 0
    number = 1
    manual: dict[tuple[str, str | None, str], tuple[Action, str]] = {}
    while True:
        staged = _is_staged(plan)
        if staged and number == MAX_PASSES:
            raise ActionFailed(f"the plan still installs extension code after {MAX_PASSES - 1} passes")
        stage = _before_settings(plan) if staged else plan
        progress["total"] = progress["number"] + runnable(stage)
        result = stage.execute(no_sudo=no_sudo, on_start=announce, on_done=done if ui.rich() else None)
        changed += result.changed
        manual.update({_manual_key(action): (action, message) for action, message in result.manual})
        if not staged:
            break
        number += 1
        rebuilt = build_plan(ctx, backend_factory(ctx), extras)
        # a manual step reported in an earlier pass is not run again (its installer may have done work)
        plan = Plan(tuple(a for a in rebuilt.actions if _manual_key(a) not in manual), rebuilt.satisfied)
    final = ExecutionResult(changed, result.skipped, tuple(manual.values()))
    relogin = relogin_hint or any(kind in CODE_CHANGING for kind in executed)
    if not ui.rich():
        print(f"{changed} changed")
        if final.skipped:
            print(final.render_skipped())
        if relogin:
            print(RELOGIN_HINT)
        if final.manual:
            print(final.render_manual())
        return 0
    print()
    print(ui.heading(ui.t("commit.summary")))
    counts = [f"{ui.symbol('ok')} {ui.style(ui.t('commit.changed', n=changed), 'green')}"]
    if final.manual:
        counts.append(f"{ui.symbol('manual')} {ui.style(ui.t('commit.manual', n=len(final.manual)), 'yellow')}")
    if final.skipped:
        counts.append(f"{ui.symbol('need')} {ui.style(ui.t('commit.skipped', n=len(final.skipped)), 'yellow')}")
    print("  " + "   ".join(counts))
    if final.skipped:
        print(final.render_skipped())
    if relogin:
        print("\n".join(ui.wrap(RELOGIN_HINT, first=f"{ui.symbol('need')} ", names=("yellow",))))
    if final.manual:
        print(final.render_manual())
    hint = "commit.rerun_pending" if final.skipped or final.manual else "commit.rerun"
    print("\n".join(ui.wrap(ui.t(hint), first=f"{ui.symbol('info')} ", names=("cyan",))))
    return 0

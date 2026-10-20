"""``ubuntu-tweak status``: drift both ways, grouped as Missing / Modified / Extra.

Accessed via: ``bin/ubuntu-tweak status [--only a,b]``.

Uses the same plan as ``apply`` (so the same ``check()``s): a pending action of kind ``set-key`` or
``link-replace`` is **Modified**, every other pending kind is **Missing**. **Extra** lines come from
the planners' optional ``extras()`` and are printed only when ``--only`` is not given. Exit 3 when
anything is Missing or Modified; extras alone exit 0. Read-only.

Public API: ``register(subparsers)``, ``run(ctx, args)``, ``classify(plan)``.
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks.actions import Action, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ExitCode

MODIFIED_KINDS = frozenset({"set-key", "link-replace"})


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="show drift: missing, modified and extra items (exit 3 on drift)")
    parser.set_defaults(handler=run)


def classify(plan: Plan) -> tuple[list[Action], list[Action]]:
    """``(missing, modified)`` in plan order."""
    modified = [action for action in plan.actions if action.kind in MODIFIED_KINDS]
    missing = [action for action in plan.actions if action.kind not in MODIFIED_KINDS]
    return missing, modified


def _section(title: str, lines: list[str]) -> None:
    print(f"{title} ({len(lines)}):")
    print("\n".join(f"  {line}" for line in lines) if lines else "  none")


def run(ctx: Context, args: argparse.Namespace) -> int:
    from ubuntu_tweaks.commands._support import require_gi

    require_gi()
    from ubuntu_tweaks import reconcile
    from ubuntu_tweaks.planners import collect_extras, collect_warnings

    backend = reconcile.open_backend(ctx)
    plan = reconcile.build_plan(ctx, backend)
    missing, modified = classify(plan)
    print("\n".join(reconcile.profile_lines(ctx)))
    _section("Missing", [f"[{a.kind}] {a.describe()}" for a in missing])
    _section("Modified", [f"[{a.kind}] {a.describe()}" for a in modified])
    extras = [] if ctx.only else collect_extras(ctx, backend)
    _section("Extra", extras)
    warnings = collect_warnings(ctx, backend)
    if warnings:
        print("Warnings:")
        print("\n".join(f"  {line}" for line in warnings))
    return int(ExitCode.DRIFT if missing or modified else ExitCode.OK)

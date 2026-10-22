"""``ubuntu-tweak status``: drift both ways, grouped as Missing / Modified / Extra / Manual.

Accessed via: ``bin/ubuntu-tweak status [--only a,b] [--explain|-v] [--json]``.

Uses the same plan as ``apply`` (so the same ``check()``s): a pending action of kind ``set-key`` or
``link-replace`` is **Modified**, every other pending kind is **Missing**. **Extra** lines come from
the planners' optional ``extras()`` and are printed only when ``--only`` is not given. **Manual** holds
the planners' warnings (things only the user can decide, e.g. a missing git identity); it never
changes the exit code. Exit 3 when anything is Missing or Modified; extras and manual alone exit 0.
Read-only.

Two layouts, same facts and same exit codes. The *plain* layout (pipes, ``--plain``) is the compact
``Missing (n):`` / ``Modified (n):`` / ``Extra (n):`` text with ``[kind]`` labels. The *interactive* layout
(a terminal, ``--color always``, or ``--explain``) is the explained one: a header, a one-line verdict
with the next command, then one section per class with a plain-language definition and what fixes it,
items grouped by module as ``badge + what`` with a dim ``why`` line, counts, a legend, and with
``--explain`` / ``-v`` the exit-code table. ``--json`` prints the same data machine-readably.

Public API: ``register(subparsers)``, ``run(ctx, args)``, ``classify(plan)``.
"""

from __future__ import annotations

import argparse
import json

from ubuntu_tweaks import ui
from ubuntu_tweaks.actions import Action, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ExitCode

MODIFIED_KINDS = frozenset({"set-key", "link-replace", "dotfiles-link-replace"})


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="show drift: missing, modified and extra items (exit 3 on drift)")
    parser.add_argument(
        "--explain",
        "--verbose",
        "-v",
        dest="explain",
        action="store_true",
        help="always use the explained layout and print what the exit codes mean",
    )
    parser.add_argument("--json", action="store_true", help="print the drift as JSON (same exit codes)")
    parser.set_defaults(handler=run)


def classify(plan: Plan) -> tuple[list[Action], list[Action]]:
    """``(missing, modified)`` in plan order."""
    modified = [action for action in plan.actions if action.kind in MODIFIED_KINDS]
    missing = [action for action in plan.actions if action.kind not in MODIFIED_KINDS]
    return missing, modified


def _section(title: str, lines: list[str]) -> None:
    print(f"{title} ({len(lines)}):")
    print("\n".join(f"  {line}" for line in lines) if lines else "  none")


def verdict(missing: int, modified: int, extra: int, manual: int) -> list[str]:
    """The one-line summary and the next command."""
    if missing or modified:
        parts = [ui.t("count.missing", n=missing)] if missing else []
        parts += [ui.t("count.modified", n=modified)] if modified else []
        parts += [ui.t("count.extra", n=extra)] if extra else []
        parts += [ui.t("count.manual", n=manual)] if manual else []
        head = ui.wrap(", ".join(parts), first=f"{ui.symbol('error')} ", names=("red", "bold"))
        return [*head, *ui.wrap(ui.t("verdict.next"), indent=2, names=("cyan",))]
    if manual:
        text = ui.t("verdict.sync_manual", n=manual) + (f" ({ui.t('count.extra', n=extra)})" if extra else "")
        return ui.wrap(text, first=f"{ui.symbol('manual')} ", names=("yellow", "bold"))
    text = ui.t("verdict.sync_extra", extra=extra) if extra else ui.t("verdict.sync")
    return ui.wrap(text, first=f"{ui.symbol('ok')} ", names=("green", "bold"))


def _group(actions: list[Action]) -> dict[str, list[Action]]:
    groups: dict[str, list[Action]] = {}
    for action in actions:
        groups.setdefault(action.scope or ui.t("group.general"), []).append(action)
    return groups


def _item_lines(action: Action, *, show_why: bool) -> list[str]:
    tag = "[sudo] " if action.needs_root else ""
    lines = ui.wrap(action.describe(), first=f"    {ui.symbol('need')} ", names=(), accent=tag)
    if show_why and action.why():
        lines += ui.wrap(f"{ui.t('why')} {action.why()}", indent=6)
    return lines


def _section_lines(key: str, kind: str, count: int, *, explain: bool) -> list[str]:
    lines = ["", ui.section(ui.t(f"section.{key}"), count, kind)]
    if count or explain:
        lines += ui.wrap(ui.t(f"section.{key}.what"), indent=2)
        lines += ui.wrap(ui.t(f"section.{key}.fix"), indent=2, names=("cyan",))
    if not count:
        lines.append("  " + ui.style(ui.t("none"), "dim"))
    return lines


def render_rich(
    header: list[str],
    missing: list[Action],
    modified: list[Action],
    extras: list[str],
    warnings: list[str],
    *,
    explain: bool,
) -> str:
    lines = [*header, ""]
    lines += verdict(len(missing), len(modified), len(extras), len(warnings))
    for key, actions in (("missing", missing), ("modified", modified)):
        lines += _section_lines(key, "error" if actions else "ok", len(actions), explain=explain)
        for group, members in _group(actions).items():
            lines.append("  " + ui.style(group, "bold"))
            seen: set[str] = set()
            for action in members:
                lines += _item_lines(action, show_why=action.kind not in seen)
                seen.add(action.kind)
    lines += _section_lines("extra", "extra", len(extras), explain=explain)
    for extra in extras:
        lines += ui.wrap(extra, first=f"    {ui.symbol('extra')} ", names=())
    lines += _section_lines("manual", "manual", len(warnings), explain=explain)
    for warning in warnings:
        lines += ui.wrap(warning, first=f"    {ui.symbol('manual')} ", names=("yellow",))
    lines += ["", *ui.legend()]
    if missing or modified:
        lines += ["", ui.heading(ui.t("next"))]
        for command, key in (("ubuntu-tweak apply", "next.apply"), ("ubuntu-tweak apply --commit", "next.commit")):
            lines += [f"  {ui.command(command)}", *ui.wrap(ui.t(key), indent=6)]
    if explain:
        lines += ["", ui.heading(ui.t("exit.title"))]
        for code in ("0", "3", "other"):
            lines += ui.wrap(ui.t(f"exit.{code}"), indent=2)
    return "\n".join(lines)


def _json(ctx: Context, missing: list[Action], modified: list[Action], extras: list[str], warnings: list[str]) -> str:
    def entry(action: Action) -> dict[str, object]:
        return {
            "kind": action.kind,
            "module": action.scope,
            "description": action.describe(),
            "why": ui.kind_why(action.kind, "en"),  # always English: a script must not depend on the language
            "needs_root": action.needs_root,
        }

    drift = bool(missing or modified)
    document = {
        "machine": ctx.profile.machine,
        "profile": str(ctx.profile.root),
        "in_sync": not drift,
        "missing": [entry(action) for action in missing],
        "modified": [entry(action) for action in modified],
        "extra": None if ctx.only else extras,  # not checked with --only: null, not an empty list
        "manual": warnings,
        "exit_code": int(ExitCode.DRIFT if drift else ExitCode.OK),
    }
    return json.dumps(document, ensure_ascii=False, indent=2)


def run(ctx: Context, args: argparse.Namespace) -> int:
    from ubuntu_tweaks.commands._support import require_gi

    require_gi()
    from ubuntu_tweaks import reconcile
    from ubuntu_tweaks.planners import collect_extras, collect_warnings

    backend = reconcile.open_backend(ctx)
    plan = reconcile.build_plan(ctx, backend)
    missing, modified = classify(plan)
    extras = [] if ctx.only else collect_extras(ctx, backend)
    warnings = collect_warnings(ctx, backend)
    code = int(ExitCode.DRIFT if missing or modified else ExitCode.OK)
    explain = getattr(args, "explain", False)  # direct callers (tests) pass a bare Namespace
    if getattr(args, "json", False):
        print(_json(ctx, missing, modified, extras, warnings))
        return code
    if ui.rich() or (explain and not ui.plain()):
        header = reconcile.header_lines(ctx, "status")
        print(render_rich(header, missing, modified, extras, warnings, explain=explain))
        return code
    print("\n".join(reconcile.profile_lines(ctx)))
    _section("Missing", [f"[{a.kind}] {a.describe()}" for a in missing])
    _section("Modified", [f"[{a.kind}] {a.describe()}" for a in modified])
    _section("Extra", extras)
    if warnings:
        print("Warnings:")
        print("\n".join(f"  {line}" for line in warnings))
    return code

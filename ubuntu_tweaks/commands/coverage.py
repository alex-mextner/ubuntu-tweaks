"""``ubuntu-tweak coverage``: every configuration location of the machine in one class (spec, *Coverage*).

Accessed via: ``bin/ubuntu-tweak coverage [--json] [--class untracked|tracked|excluded|secret] [--propose]``;
``commands/status.py`` imports ``scan``, ``counts`` and the summary renderers for its one-line summary.

Read-only (the global ``--dry-run`` is accepted and changes nothing, there is nothing to skip). Runs every adapter
(``adapters/<name>.py``) against the live machine, classifies each item with the layered rules (engine defaults <
modules < profile ``coverage.toml`` < machine ``coverage.toml``, ``coverage_rules.py``) and the profile's own
declarations, and prints one table per adapter: a count per class, then each item with what to know about it (the
owner for ``tracked``, the written reason for ``excluded`` and ``secret``, the discovered detail for ``untracked``).
A secret item shows its reason only, never a value. ``--class`` lists one class only (the counts stay complete);
``--propose`` prints what ``capture`` would add to the profile for the untracked items, grouped by purpose, and
writes nothing (``capture --commit`` writes).

The *plain* layout (pipes, ``--plain``) is compact aligned text; the *interactive* layout (a terminal,
``--color always``) explains the four classes and colors them. ``--json`` prints the same facts machine-readably.

Exit codes: 3 (``ExitCode.DRIFT``) while any ``untracked`` item WITHOUT a ticket remains (whatever ``--class``
shows), else 0: a ``[[coverage]] class = "untracked"`` rule with a ``ticket`` keeps the item untracked (counted as
``N untracked (M ticketed)``, listed with its ticket) but is somebody's job already, so it does not fail the gate.
``--check-profile [--online]`` is the profile CI's static gate (``profile_check.py``): it parses the coverage rules,
scans nothing, writes nothing and needs no PyGObject; exit 0 clean, 2 invalid.

Public API: ``register(subparsers)``, ``run(ctx, args)``, ``scan(ctx)``, ``counts(findings) -> Totals``,
``summary_text(counts)``, ``summary_plain(counts)``, ``summary_rich(counts)``.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence

from ubuntu_tweaks import ui
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Finding, Proposal, Totals, totals
from ubuntu_tweaks.errors import ConfigError, ExitCode

ORDER = (Coverage.UNTRACKED, Coverage.SECRET, Coverage.EXCLUDED, Coverage.TRACKED)
_BADGE = {
    Coverage.UNTRACKED: "need",
    Coverage.SECRET: "manual",
    Coverage.EXCLUDED: "info",
    Coverage.TRACKED: "ok",
}
_MEANING = {
    Coverage.TRACKED: "declared in the profile and reconciled by `apply`, or owned by a module",
    Coverage.EXCLUDED: "deliberately not tracked, with a written reason (cache, state, generated, machine-bound)",
    Coverage.SECRET: "credentials: kept in the keychain, never in the profile; only their existence is reported",
    Coverage.UNTRACKED: "configuration nobody declared: track it, exclude it with a reason, or remove it",
}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "coverage",
        help="classify every configuration location: tracked, excluded, secret or untracked (exit 3 while untracked)",
        description=(
            "Scan the machine (dconf, files, packages, systemd units, GNOME extensions, /etc) and put every item in one "
            "class. Read-only. Exit 3 while untracked items WITHOUT a ticket remain; --check-profile validates the "
            "profile's coverage rules without scanning."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the findings as JSON (same exit codes)")
    parser.add_argument(
        "--class",
        dest="coverage_class",
        choices=[item.value for item in Coverage],
        help="list only this class (the counts and the exit code still cover everything)",
    )
    parser.add_argument(
        "--propose",
        action="store_true",
        help="print what `capture` would add to the profile for the untracked items, grouped by purpose",
    )
    parser.add_argument(
        "--check-profile",
        action="store_true",
        help=(
            "static gate for the profile's CI: parse every coverage rule (an untracked rule needs a ticket, excluded and "
            "secret need a reason), flag patterns that can never match and duplicate or redundant ones; scans nothing, "
            "writes nothing, needs no PyGObject; exit 2 when invalid"
        ),
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="with --check-profile: also verify through `gh issue view` that every ticket exists and is open",
    )
    parser.set_defaults(handler=run)


def scan(ctx: Context) -> list[Finding]:
    """Every adapter's items, classified. The adapters of one scan share one ``System`` (the installed one, else the
    real machine)."""
    from ubuntu_tweaks.adapters import scan as scan_adapters
    from ubuntu_tweaks.adapters._system import system, use
    from ubuntu_tweaks.coverage_rules import load_rules

    rules = load_rules(ctx)
    with use(system()):
        return scan_adapters(ctx, rules)


def counts(findings: Sequence[Finding]) -> Totals:
    return totals(findings)


def summary_text(counted: Totals) -> str:
    """``N untracked (M ticketed), S secret, E excluded, T tracked``; the ticketed note only when one exists."""
    parts: list[str] = []
    for kind in ORDER:
        note = f" ({counted.ticketed} ticketed)" if kind is Coverage.UNTRACKED and counted.ticketed else ""
        parts.append(f"{counted[kind]} {kind.value}{note}")
    return ", ".join(parts)


def summary_plain(counted: Totals) -> str:
    return f"Coverage: {summary_text(counted)}" + (" (run `ubuntu-tweak coverage`)" if counted.open else "")


def summary_rich(totals: Totals) -> list[str]:
    if totals.open:
        head = ui.wrap(f"coverage: {summary_text(totals)}", first=f"{ui.symbol('need')} ", names=("yellow", "bold"))
        return [
            *head,
            *ui.wrap("`ubuntu-tweak coverage` lists them; `--propose` shows what capture would add", indent=2),
        ]
    return ui.wrap(f"coverage: {summary_text(totals)}", first=f"{ui.symbol('ok')} ", names=("green", "bold"))


def _what(finding: Finding) -> str:
    if finding.coverage is Coverage.TRACKED:
        return finding.owner
    if finding.coverage is Coverage.UNTRACKED:
        if not finding.ticket:
            return finding.item.detail
        return f"{finding.reason} ({finding.item.detail})" if finding.item.detail else finding.reason
    reason = finding.reason
    return f"{reason} ({finding.owner})" if finding.owner else reason  # a secret's value is never shown


def _by_adapter(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = defaultdict(list)
    for finding in sorted(findings, key=lambda f: (ORDER.index(f.coverage), f.item.id)):
        groups[finding.item.adapter].append(finding)
    return dict(sorted(groups.items()))


def _plain(ctx: Context, findings: Sequence[Finding], shown: Sequence[Finding]) -> str:
    from ubuntu_tweaks import reconcile

    lines = [*reconcile.profile_lines(ctx), summary_plain(counts(findings))]
    every = _by_adapter(findings)
    with_tickets = any(finding.ticket for finding in shown)
    for adapter, members in _by_adapter(shown).items():
        totals = counts(every[adapter])
        lines += ["", f"{adapter}: {summary_text(totals)}"]
        width = max(len(finding.item.id) for finding in members)
        ticket_width = max(len(finding.ticket) for finding in members)
        lines += [
            f"  {finding.coverage.value:<9}  {finding.item.id:<{width}}  "
            f"{f'{finding.ticket:<{ticket_width}}  ' if with_tickets else ''}{_what(finding)}".rstrip()
            for finding in members
        ]
    return "\n".join(lines)


def _rich(ctx: Context, findings: Sequence[Finding], shown: Sequence[Finding], chosen: Coverage | None) -> str:
    from ubuntu_tweaks import reconcile

    totals = counts(findings)
    lines = [*reconcile.header_lines(ctx, "coverage"), "", *summary_rich(totals), ""]
    lines += [ui.heading("Classes")]
    for kind in ORDER:
        lines += ui.wrap(f"{kind.value}: {_MEANING[kind]}", indent=2)
    every = _by_adapter(findings)
    with_tickets = any(finding.ticket for finding in shown)
    for adapter, members in _by_adapter(shown).items():
        adapter_totals = counts(every[adapter])
        badge = "need" if adapter_totals.open else "ok"
        lines += [
            "",
            ui.section(adapter, len(every[adapter]), badge),
            f"  {ui.style(summary_text(adapter_totals), 'dim')}",
        ]
        rows = [
            (
                ui.badge(_BADGE[finding.coverage], word=False) + " " + finding.coverage.value,
                finding.item.id,
                *((finding.ticket,) if with_tickets else ()),
                _what(finding),
            )
            for finding in members
        ]
        lines += ui.table(("CLASS", "ITEM", *(("TICKET",) if with_tickets else ()), "WHAT"), rows)
    if not shown:
        lines += ["", "  " + ui.style(f"no {chosen.value} items" if chosen else "nothing found", "dim")]
    return "\n".join(lines)


def _proposals(ctx: Context, findings: Sequence[Finding]) -> list[tuple[str, Proposal]]:
    from ubuntu_tweaks.adapters import discover_adapters
    from ubuntu_tweaks.adapters._system import system, use

    untracked = [f.item for f in findings if f.coverage is Coverage.UNTRACKED and not f.ticket]
    found: list[tuple[str, Proposal]] = []
    with use(system()):
        for module in discover_adapters():
            hook = getattr(module, "capture", None)
            members = tuple(item for item in untracked if item.adapter == getattr(module, "NAME", ""))
            if callable(hook) and members:
                found += [(module.NAME, proposal) for proposal in hook(ctx, members)]
    return found


def _proposals_text(proposals: Sequence[tuple[str, Proposal]]) -> str:
    if not proposals:
        return "Nothing to propose: no untracked item has a place in the profile to land in."
    lines = ["Proposals (nothing is written; `ubuntu-tweak capture --commit` writes the profile):"]
    for adapter, proposal in proposals:
        lines += ["", f"## {proposal.title} ({adapter}, {len(proposal.items)} item(s))", proposal.text.rstrip()]
    return "\n".join(lines)


def _json(
    ctx: Context,
    findings: Sequence[Finding],
    shown: Sequence[Finding],
    proposals: Sequence[tuple[str, Proposal]] | None,
    code: int,
) -> str:
    totals = counts(findings)
    document: dict[str, object] = {
        "machine": ctx.profile.machine,
        "profile": str(ctx.profile.root),
        "counts": {**{kind.value: totals[kind] for kind in ORDER}, "ticketed": totals.ticketed},
        "findings": [
            {
                "adapter": finding.item.adapter,
                "id": finding.item.id,
                "kind": finding.item.kind,
                "class": finding.coverage.value,
                "detail": "" if finding.coverage is Coverage.SECRET else finding.item.detail,
                "reason": finding.reason,
                "owner": finding.owner,
                "ticket": finding.ticket,
            }
            for finding in shown
        ],
        "exit_code": code,
    }
    if proposals is not None:
        document["proposals"] = [
            {"adapter": adapter, "title": p.title, "text": p.text, "items": [item.id for item in p.items]}
            for adapter, p in proposals
        ]
    return json.dumps(document, ensure_ascii=False, indent=2)


def run(ctx: Context, args: argparse.Namespace) -> int:
    online = getattr(args, "online", False)
    if getattr(args, "check_profile", False):
        from ubuntu_tweaks.profile_check import run_check

        return run_check(ctx, online=online, as_json=getattr(args, "json", False))
    if online:
        raise ConfigError("--online needs --check-profile")
    from ubuntu_tweaks.commands._support import require_gi

    require_gi()
    findings = scan(ctx)
    code = int(ExitCode.DRIFT if counts(findings).open else ExitCode.OK)
    chosen = Coverage(args.coverage_class) if getattr(args, "coverage_class", None) else None
    shown = [f for f in findings if chosen is None or f.coverage is chosen]
    proposals = _proposals(ctx, findings) if getattr(args, "propose", False) else None
    if getattr(args, "json", False):
        print(_json(ctx, findings, shown, proposals, code))
        return code
    print(_rich(ctx, findings, shown, chosen) if ui.rich() else _plain(ctx, findings, shown))
    if proposals is not None:
        print("\n" + _proposals_text(proposals))
    return code

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

Exit codes: 3 (``ExitCode.DRIFT``) while any ``untracked`` item remains (whatever ``--class`` shows), else 0.

Public API: ``register(subparsers)``, ``run(ctx, args)``, ``scan(ctx)``, ``counts(findings)``,
``summary_text(counts)``, ``summary_plain(counts)``, ``summary_rich(counts)``.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence

from ubuntu_tweaks import ui
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Finding, Proposal, summary
from ubuntu_tweaks.errors import ExitCode

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
            "class. Read-only. Exit 3 while untracked items remain."
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


def counts(findings: Sequence[Finding]) -> dict[Coverage, int]:
    return summary(findings)


def summary_text(totals: Mapping[Coverage, int]) -> str:
    return ", ".join(f"{totals[kind]} {kind.value}" for kind in ORDER)


def summary_plain(totals: Mapping[Coverage, int]) -> str:
    untracked = totals[Coverage.UNTRACKED]
    return f"Coverage: {summary_text(totals)}" + (" (run `ubuntu-tweak coverage`)" if untracked else "")


def summary_rich(totals: Mapping[Coverage, int]) -> list[str]:
    untracked = totals[Coverage.UNTRACKED]
    if untracked:
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
        return finding.item.detail
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
    for adapter, members in _by_adapter(shown).items():
        totals = counts(every[adapter])
        lines += ["", f"{adapter}: {summary_text(totals)}"]
        width = max(len(finding.item.id) for finding in members)
        lines += [
            f"  {finding.coverage.value:<9}  {finding.item.id:<{width}}  {_what(finding)}".rstrip()
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
    for adapter, members in _by_adapter(shown).items():
        adapter_totals = counts(every[adapter])
        badge = "need" if adapter_totals[Coverage.UNTRACKED] else "ok"
        lines += [
            "",
            ui.section(adapter, len(every[adapter]), badge),
            f"  {ui.style(summary_text(adapter_totals), 'dim')}",
        ]
        rows = [
            (
                ui.badge(_BADGE[finding.coverage], word=False) + " " + finding.coverage.value,
                finding.item.id,
                _what(finding),
            )
            for finding in members
        ]
        lines += ui.table(("CLASS", "ITEM", "WHAT"), rows)
    if not shown:
        lines += ["", "  " + ui.style(f"no {chosen.value} items" if chosen else "nothing found", "dim")]
    return "\n".join(lines)


def _proposals(ctx: Context, findings: Sequence[Finding]) -> list[tuple[str, Proposal]]:
    from ubuntu_tweaks.adapters import discover_adapters
    from ubuntu_tweaks.adapters._system import system, use

    untracked = [f.item for f in findings if f.coverage is Coverage.UNTRACKED]
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
        "counts": {kind.value: totals[kind] for kind in ORDER},
        "findings": [
            {
                "adapter": finding.item.adapter,
                "id": finding.item.id,
                "kind": finding.item.kind,
                "class": finding.coverage.value,
                "detail": "" if finding.coverage is Coverage.SECRET else finding.item.detail,
                "reason": finding.reason,
                "owner": finding.owner,
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
    from ubuntu_tweaks.commands._support import require_gi

    require_gi()
    findings = scan(ctx)
    code = int(ExitCode.DRIFT if counts(findings)[Coverage.UNTRACKED] else ExitCode.OK)
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

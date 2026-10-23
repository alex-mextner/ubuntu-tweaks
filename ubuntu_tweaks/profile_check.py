"""``ubuntu-tweak coverage --check-profile``: the static gate of a profile's coverage rules (spec, *Coverage*).

Accessed via: ``commands/coverage.py`` (``run`` hands over to ``run_check`` before it needs PyGObject).

What it checks, over every rule source (the engine's ``coverage_defaults.toml``, every module's ``[[coverage]]``
enabled or not, the profile's ``coverage.toml`` and the machine overlay's):

- every file and manifest parses, with ``coverage.parse_rules``'s own errors: an ``untracked`` rule has a valid
  ``ticket`` and a ``reason``, an ``excluded`` / ``secret`` rule has a ``reason``, no other class has a ``ticket``;
- every ``match`` glob can match some item id: its literal head must lead into a known scheme prefix
  (``coverage.ITEM_PREFIXES``: ``dconf:/``, ``file:~/``, ``etc:/``, ``apt:``...), so ``~/.config/x`` or
  ``file:/home/me/x`` are caught;
- no pattern is listed twice in one layer (the first match of a layer wins, so the second never applies), and no layer
  restates a lower layer's identical pattern with the same class (redundant; a different class is a real override);
- with ``online``, every ticket exists and is open, asked of ``gh issue view`` (skipped with a note when ``gh`` is
  not installed; ``#N`` is asked in the profile's own checkout, ``owner/repo#N`` with ``--repo``).

Assumptions: reads TOML files and the module manifests, optionally runs ``gh``; it never scans the machine, never
needs PyGObject and never writes.

Public API: ``Report``, ``check(ctx, *, online, process) -> Report`` and ``run_check(ctx, *, online, as_json,
process=None) -> int`` (0 clean, 2 invalid config).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import ITEM_PREFIXES, Layer, Rule, parse_rules
from ubuntu_tweaks.coverage_rules import COVERAGE_TOML, DEFAULTS_TOML, drop_ins, profile_layer_files, rule_entries
from ubuntu_tweaks.errors import ConfigError, ExitCode
from ubuntu_tweaks.manifest import module_defaults
from ubuntu_tweaks.process import Process

_WILDCARDS = "*?["
_NOT_FOUND = ("could not resolve", "not found", "404", "no issue")


@dataclass(frozen=True)
class Report:
    problems: tuple[str, ...]
    notes: tuple[str, ...]
    rules: int
    tickets: tuple[str, ...]


@dataclass(frozen=True)
class _Located:
    """One rule and where it was written, as the message names it: ``<source> [[coverage]] #<n>``."""

    rule: Rule
    where: str


type Sources = list[tuple[Layer, list[_Located]]]


def _parse_file(layer: Layer, label: str, path: Path, problems: list[str]) -> list[_Located]:
    """The rules of one file; an entry that does not parse is a problem and is left out."""
    try:
        entries = rule_entries(path)
    except ConfigError as error:
        problems.append(str(error))
        return []
    located: list[_Located] = []
    for number, entry in enumerate(entries, 1):
        try:
            (rule,) = parse_rules([entry], layer, label, first=number)
        except ConfigError as error:
            problems.append(str(error))
            continue
        located.append(_Located(rule, f"{label} [[coverage]] #{number}"))
    return located


def _sources(ctx: Context, problems: list[str]) -> tuple[Sources, Sources]:
    """``(live, other)``: the rule sources that apply on this machine (the engine's, the enabled modules', the
    profile's, this machine's overlay) and the ones that do not (disabled modules, the overlays of other machines).
    Both are checked for syntax and patterns; only the live ones can duplicate or override each other."""
    live: Sources = []
    other: Sources = []
    files = [(Layer.ENGINE, "engine coverage_defaults.toml", DEFAULTS_TOML), *profile_layer_files(ctx)]
    for layer, label, path in files:
        live.append((layer, _parse_file(layer, label, path, problems)))
    machines = ctx.profile.root / "machines"
    for directory in sorted(machines.iterdir()) if machines.is_dir() else []:
        if directory.name != ctx.profile.machine and directory.is_dir():
            for path in [directory / COVERAGE_TOML, *drop_ins(directory)]:
                label = f"machines/{directory.name}/{path.relative_to(directory).as_posix()}"
                other.append((Layer.MACHINE, _parse_file(Layer.MACHINE, label, path, problems)))
    try:
        manifests = ctx.manifests()
        config = ctx.profile.load_config(module_defaults(manifests))
    except ConfigError as error:
        problems.append(str(error))
        return live, other
    for manifest in manifests:
        label = f"module {manifest.name}"
        rules = [_Located(rule, f"{label} [[coverage]] #{n}") for n, rule in enumerate(manifest.coverage, 1)]
        (live if config.module_enabled(manifest.name) else other).append((Layer.MODULE, rules))
    return live, other


def unmatchable(pattern: str) -> bool:
    """True when no item id can ever match ``pattern``: its literal head cannot start any known id."""
    head = pattern
    for position, char in enumerate(pattern):
        if char in _WILDCARDS:
            head = pattern[:position]
            break
    if head == pattern:
        return not any(pattern.startswith(prefix) for prefix in ITEM_PREFIXES)
    return not any(prefix.startswith(head) or head.startswith(prefix) for prefix in ITEM_PREFIXES)


def _pattern_problems(located: Sequence[_Located]) -> list[str]:
    forms = ", ".join(ITEM_PREFIXES)
    return [
        f"{item.where}: match {pattern!r} can never match any item (ids start with one of {forms})"
        for item in located
        for pattern in item.rule.match
        if unmatchable(pattern)
    ]


def _duplicate_problems(sources: Sequence[tuple[Layer, list[_Located]]]) -> list[str]:
    by_layer: dict[Layer, list[_Located]] = {layer: [] for layer in Layer}
    for layer, located in sources:
        by_layer[layer] += located
    problems: list[str] = []
    for layer in Layer:
        first: dict[str, _Located] = {}
        for item in by_layer[layer]:
            for position, pattern in enumerate(item.rule.match):
                earlier = first.setdefault(pattern, item)
                if earlier is not item:
                    problems.append(
                        f"{item.where}: match {pattern!r} repeats {earlier.where}; the first matching rule of a layer "
                        "wins, so this one never applies"
                    )
                elif pattern in item.rule.match[:position]:
                    problems.append(
                        f"{item.where}: match {pattern!r} is listed twice in the same rule; the second never applies"
                    )
    for layer in Layer:
        for item in by_layer[layer]:
            for pattern in dict.fromkeys(item.rule.match):
                nearest = next(
                    (
                        lower
                        for lower_layer in reversed(Layer)
                        if lower_layer < layer
                        for lower in by_layer[lower_layer]
                        if pattern in lower.rule.match
                    ),
                    None,
                )
                if nearest is not None and nearest.rule.coverage is item.rule.coverage:
                    problems.append(
                        f"{item.where}: match {pattern!r} is redundant: {nearest.where} already says "
                        f"{item.rule.coverage.value}"
                    )
    return problems


def _verify_tickets(
    ctx: Context, tickets: Sequence[str], process: Process, problems: list[str], notes: list[str]
) -> None:
    root = ctx.profile.root
    for ticket in tickets:
        repo, _, number = ticket.rpartition("#")
        argv = ["gh", "issue", "view", number, *(["--repo", repo] if repo else []), "--json", "state"]
        result = process.query(argv, timeout=30.0, cwd=root if root.is_dir() else None)
        if result.returncode == 127:
            notes.append("online check skipped: gh is not installed, so no ticket was verified")
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            said = detail[0] if detail else f"exit {result.returncode}"
            if any(text in (result.stderr + result.stdout).lower() for text in _NOT_FOUND):
                problems.append(f"ticket {ticket} does not exist ({said})")
            else:
                problems.append(f"cannot verify {ticket}: gh said: {said}")
            continue
        try:
            state = str(json.loads(result.stdout)["state"])
        except (ValueError, KeyError, TypeError):
            problems.append(f"cannot verify {ticket}: gh printed no state")
            continue
        if state.upper() != "OPEN":
            problems.append(
                f"ticket {ticket} is {state.lower()}: close the rule with it, or point it at an open ticket"
            )


def check(ctx: Context, *, online: bool, process: Process) -> Report:
    problems: list[str] = []
    notes: list[str] = []
    live, other = _sources(ctx, problems)
    everything = [*live, *other]
    for _layer, located in everything:
        problems += _pattern_problems(located)
    problems += _duplicate_problems(live)
    tickets = sorted({item.rule.ticket for _layer, located in everything for item in located if item.rule.ticket})
    if online:
        _verify_tickets(ctx, tickets, process, problems, notes)
    elif tickets:
        notes.append(f"{len(tickets)} ticket(s) not verified: add --online to check that they exist and are open")
    rules = sum(len(located) for _layer, located in everything)
    return Report(tuple(problems), tuple(notes), rules, tuple(tickets))


def _text(ctx: Context, report: Report) -> str:
    lines = [
        f"profile check: {ctx.profile.root}",
        f"  {report.rules} coverage rule(s) read, {len(report.tickets)} ticket(s)",
    ]
    lines += [f"  note: {note}" for note in report.notes]
    if report.problems:
        lines += [f"{len(report.problems)} problem(s):", *(f"  - {problem}" for problem in report.problems)]
    else:
        lines.append("no problems")
    return "\n".join(lines)


def run_check(ctx: Context, *, online: bool, as_json: bool, process: Process | None = None) -> int:
    report = check(ctx, online=online, process=process or Process())
    code = int(ExitCode.INVALID_CONFIG if report.problems else ExitCode.OK)
    if as_json:
        document = {
            "profile": str(ctx.profile.root),
            "rules": report.rules,
            "tickets": list(report.tickets),
            "problems": list(report.problems),
            "notes": list(report.notes),
            "exit_code": code,
        }
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(_text(ctx, report))
    return code

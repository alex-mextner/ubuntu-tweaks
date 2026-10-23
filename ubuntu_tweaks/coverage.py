"""The coverage model: what an adapter discovers and how the scanner classifies it (spec, *Coverage*).

Accessed via: ``adapters/<name>.py`` (they build ``Item`` / ``Finding`` / ``Proposal``), ``commands/coverage.py``
(the scan), ``commands/status.py`` (the summary line) and ``commands/capture.py`` (proposals).

Assumptions: pure data and matching, no I/O; adapters read the machine, this module only classifies.

Public API:

- ``Coverage`` (``StrEnum``): ``TRACKED``, ``EXCLUDED``, ``SECRET``, ``UNTRACKED``.
- ``Item(adapter, id, kind, detail="")``: one configuration location. ``id`` is ``<scheme>:<value>``
  (``dconf:/org/gnome/x/key``, ``file:~/.config/foo``, ``apt:curl``, ``unit:foo.service``...); ``kind`` is a short
  label for the table (``key``, ``dir``, ``file``, ``package``, ``unit``); ``detail`` is one human line (never a secret value).
- ``Rule(match, coverage, reason, layer, owner, ticket)``: ``match`` are globs against ``Item.id`` (``fnmatch``,
  case-sensitive); ``layer`` is ``engine`` < ``module`` < ``profile`` < ``machine``; ``ticket`` is set only on an
  ``untracked`` rule.
- ``Rules``: ``Rules(rules).classify(item) -> Rule | None``: the first matching rule of the highest layer.
- ``Finding(item, coverage, reason, owner, ticket)``: ``owner`` names the module or profile file that tracks the
  item (``""`` when untracked or excluded); ``ticket`` is the issue an ``untracked`` item is waiting on.
- ``Proposal(title, text, items)``: what ``capture`` would add to the profile for some untracked items.
- ``parse_rules(table, layer, where) -> list[Rule]``: the ``[[coverage]]`` tables of a ``module.toml`` /
  ``coverage.toml`` (``match`` list, ``class``; ``reason`` required for every class but ``tracked``; an
  ``untracked`` rule also needs a ``ticket`` (``#N`` or ``owner/repo#N``), no other class may have one).
- ``valid_ticket(text) -> bool``: the ticket reference format.
- ``classify(item, rules, owned) -> Finding``: ``owned`` maps item ids to the owner the profile already declares
  (bundle keys, linked files, ``packages.toml`` names): those are ``TRACKED`` without a rule; a rule beats an
  absent declaration (a declaration beats an ``untracked`` rule); an item with neither is ``UNTRACKED``.
- ``summary(findings) -> dict[Coverage, int]``; ``totals(findings) -> Totals``: the same counts plus ``ticketed``
  (untracked with a ticket) and ``open`` (untracked without one, the items that make ``coverage`` exit 3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from fnmatch import fnmatchcase

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.profile import TomlValue


class Coverage(StrEnum):
    TRACKED = "tracked"
    EXCLUDED = "excluded"
    SECRET = "secret"
    UNTRACKED = "untracked"


class Layer(IntEnum):
    ENGINE = 0
    MODULE = 1
    PROFILE = 2
    MACHINE = 3


@dataclass(frozen=True)
class Item:
    adapter: str
    id: str
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class Rule:
    match: tuple[str, ...]
    coverage: Coverage
    reason: str
    layer: Layer
    owner: str = ""
    ticket: str = ""

    def matches(self, item: Item) -> bool:
        return any(fnmatchcase(item.id, pattern) for pattern in self.match)


@dataclass(frozen=True)
class Finding:
    item: Item
    coverage: Coverage
    reason: str = ""
    owner: str = ""
    ticket: str = ""


@dataclass(frozen=True)
class Proposal:
    title: str
    text: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...] = ()

    def classify(self, item: Item) -> Rule | None:
        best: Rule | None = None
        for rule in self.rules:
            if rule.matches(item) and (best is None or rule.layer > best.layer):
                best = rule
        return best


# How an item id starts, one entry per scheme the adapters produce (spec, *Coverage*, *Items*): what a ``match`` glob
# must be able to lead into, and what ``coverage --check-profile`` holds a pattern against.
ITEM_PREFIXES = (
    "dconf:/",
    "file:~/",
    "etc:/",
    "apt:",
    "snap:",
    "flatpak:",
    "brew:",
    "pipx:",
    "uv:",
    "npm:",
    "bun:",
    "cargo:",
    "unit:",
    "autostart:",
    "extension:",
    "cron:",
    "ssh:",
    "git:",
    "disk:",
)

TICKET = re.compile(r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+")


def valid_ticket(ticket: str) -> bool:
    """``#N`` or ``owner/repo#N`` (the issue of this repository, or of another one)."""
    return TICKET.fullmatch(ticket) is not None


def parse_rules(
    table: Sequence[Mapping[str, TomlValue]], layer: Layer, where: str, owner: str = "", *, first: int = 1
) -> list[Rule]:
    """Validate the ``[[coverage]]`` entries of one file; every error names ``where`` and the entry's number (the
    first entry of ``table`` is number ``first``)."""
    rules: list[Rule] = []
    for index, entry in enumerate(table, first):
        label = f"{where} [[coverage]] #{index}"
        unknown = sorted(set(entry) - {"match", "class", "reason", "ticket"})
        if unknown:
            raise ConfigError(f"{label}: unknown key(s) {', '.join(unknown)}; allowed: match, class, reason, ticket")
        match = entry.get("match")
        if not isinstance(match, list) or not match or not all(isinstance(item, str) and item for item in match):
            raise ConfigError(f"{label}: 'match' must be a non-empty list of item-id globs")
        try:
            coverage = Coverage(str(entry.get("class")))
        except ValueError:
            raise ConfigError(f"{label}: 'class' must be tracked, excluded, secret or untracked") from None
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            raise ConfigError(f"{label}: 'reason' must be a string")
        if coverage is not Coverage.TRACKED and not reason:
            article = "an" if coverage.value[0] in "aeiou" else "a"
            raise ConfigError(f"{label}: {article} {coverage.value} rule needs a 'reason'")
        ticket = entry.get("ticket")
        if ticket is not None and coverage is not Coverage.UNTRACKED:
            raise ConfigError(f"{label}: 'ticket' is only for an untracked rule, not {coverage.value}")
        if coverage is Coverage.UNTRACKED:
            if ticket is None:
                raise ConfigError(f"{label}: an untracked rule needs a 'ticket' (#N or owner/repo#N)")
            if not isinstance(ticket, str) or not valid_ticket(ticket):
                raise ConfigError(f"{label}: 'ticket' must be #N or owner/repo#N, not {ticket!r}")
        rules.append(Rule(tuple(str(item) for item in match), coverage, reason, layer, owner, ticket or ""))
    return rules


def classify(item: Item, rules: Rules, owned: Mapping[str, str]) -> Finding:
    rule = rules.classify(item)
    if (
        rule is not None
        and rule.layer > Layer.ENGINE
        and not (rule.coverage is Coverage.UNTRACKED and item.id in owned)
    ):
        return Finding(item, rule.coverage, rule.reason, rule.owner, rule.ticket)
    if item.id in owned:
        return Finding(item, Coverage.TRACKED, "", owned[item.id])
    if rule is not None:
        return Finding(item, rule.coverage, rule.reason, rule.owner, rule.ticket)
    return Finding(item, Coverage.UNTRACKED)


def summary(findings: Iterable[Finding]) -> dict[Coverage, int]:
    counts = dict.fromkeys(Coverage, 0)
    for finding in findings:
        counts[finding.coverage] += 1
    return counts


@dataclass(frozen=True)
class Totals:
    """A count per class plus how many of the untracked items already carry a ticket."""

    classes: Mapping[Coverage, int]
    ticketed: int = 0

    def __getitem__(self, kind: Coverage) -> int:
        return self.classes[kind]

    @property
    def open(self) -> int:
        """Untracked items without a ticket: the ones that make ``coverage`` exit 3."""
        return self.classes[Coverage.UNTRACKED] - self.ticketed


def totals(findings: Sequence[Finding]) -> Totals:
    return Totals(summary(findings), sum(1 for f in findings if f.coverage is Coverage.UNTRACKED and f.ticket))

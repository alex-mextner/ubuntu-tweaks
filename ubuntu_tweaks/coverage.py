"""The coverage model: what an adapter discovers and how the scanner classifies it (spec, *Coverage*).

Accessed via: ``adapters/<name>.py`` (they build ``Item`` / ``Finding`` / ``Proposal``), ``commands/coverage.py``
(the scan), ``commands/status.py`` (the summary line) and ``commands/capture.py`` (proposals).

Assumptions: pure data and matching, no I/O; adapters read the machine, this module only classifies.

Public API:

- ``Coverage`` (``StrEnum``): ``TRACKED``, ``EXCLUDED``, ``SECRET``, ``UNTRACKED``.
- ``Item(adapter, id, kind, detail="")``: one configuration location. ``id`` is ``<scheme>:<value>``
  (``dconf:/org/gnome/x/key``, ``file:~/.config/foo``, ``apt:curl``, ``unit:foo.service``...); ``kind`` is a short
  label for the table (``key``, ``dir``, ``file``, ``package``, ``unit``); ``detail`` is one human line (never a secret value).
- ``Rule(match, coverage, reason, layer)``: ``match`` are globs against ``Item.id`` (``fnmatch``, case-sensitive);
  ``layer`` is ``engine`` < ``module`` < ``profile`` < ``machine``.
- ``Rules``: ``Rules(rules).classify(item) -> Rule | None``: the first matching rule of the highest layer.
- ``Finding(item, coverage, reason, owner)``: ``owner`` names the module or profile file that tracks the item
  (``""`` when untracked or excluded).
- ``Proposal(title, text, items)``: what ``capture`` would add to the profile for some untracked items.
- ``parse_rules(table, layer, where) -> list[Rule]``: the ``[[coverage]]`` tables of a ``module.toml`` /
  ``coverage.toml`` (``match`` list, ``class``, ``reason`` required for ``excluded`` and ``secret``).
- ``classify(item, rules, owned) -> Finding``: ``owned`` maps item ids to the owner the profile already declares
  (bundle keys, linked files, ``packages.toml`` names): those are ``TRACKED`` without a rule; a rule beats an
  absent declaration; an item with neither is ``UNTRACKED``.
- ``summary(findings) -> dict[Coverage, int]``.
"""

from __future__ import annotations

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

    def matches(self, item: Item) -> bool:
        return any(fnmatchcase(item.id, pattern) for pattern in self.match)


@dataclass(frozen=True)
class Finding:
    item: Item
    coverage: Coverage
    reason: str = ""
    owner: str = ""


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


def parse_rules(table: Sequence[Mapping[str, TomlValue]], layer: Layer, where: str, owner: str = "") -> list[Rule]:
    """Validate the ``[[coverage]]`` entries of one file; every error names ``where``."""
    rules: list[Rule] = []
    for index, entry in enumerate(table, 1):
        label = f"{where} [[coverage]] #{index}"
        unknown = sorted(set(entry) - {"match", "class", "reason"})
        if unknown:
            raise ConfigError(f"{label}: unknown key(s) {', '.join(unknown)}; allowed: match, class, reason")
        match = entry.get("match")
        if not isinstance(match, list) or not match or not all(isinstance(item, str) and item for item in match):
            raise ConfigError(f"{label}: 'match' must be a non-empty list of item-id globs")
        try:
            coverage = Coverage(str(entry.get("class")))
        except ValueError:
            raise ConfigError(f"{label}: 'class' must be tracked, excluded or secret") from None
        if coverage is Coverage.UNTRACKED:
            raise ConfigError(f"{label}: 'class' must be tracked, excluded or secret")
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            raise ConfigError(f"{label}: 'reason' must be a string")
        if coverage in (Coverage.EXCLUDED, Coverage.SECRET) and not reason:
            raise ConfigError(f"{label}: an {coverage.value} rule needs a 'reason'")
        rules.append(Rule(tuple(str(item) for item in match), coverage, reason, layer, owner))
    return rules


def classify(item: Item, rules: Rules, owned: Mapping[str, str]) -> Finding:
    rule = rules.classify(item)
    if rule is not None and rule.layer > Layer.ENGINE:
        return Finding(item, rule.coverage, rule.reason, rule.owner)
    if item.id in owned:
        return Finding(item, Coverage.TRACKED, "", owned[item.id])
    if rule is not None:
        return Finding(item, rule.coverage, rule.reason, rule.owner)
    return Finding(item, Coverage.UNTRACKED)


def summary(findings: Iterable[Finding]) -> dict[Coverage, int]:
    counts = dict.fromkeys(Coverage, 0)
    for finding in findings:
        counts[finding.coverage] += 1
    return counts

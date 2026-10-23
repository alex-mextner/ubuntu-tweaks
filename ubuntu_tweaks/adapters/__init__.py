"""Adapter discovery: every ``adapters/<name>.py`` covers one area of configuration (spec, *Coverage*).

Accessed via: ``commands/coverage.py``, ``commands/status.py`` and ``commands/capture.py``.

Contract for an adapter module (names starting with ``_`` are skipped):

- ``NAME: str``: the item-id scheme the adapter owns (``dconf``, ``file``, ``apt``...).
- ``discover(ctx: Context) -> Iterable[Item]`` (required): what exists on the machine; read-only, no root, never
  a secret value; an area that cannot be read (needs root, tool missing) yields one ``Item`` whose ``detail``
  says so instead of raising.
- ``classify(ctx, item, rules) -> Finding | None`` (optional): an opinion of the adapter (the dconf adapter knows
  which keys the profile's bundles declare); ``None`` leaves it to the generic rules.
- ``owned(ctx) -> Mapping[str, str]`` (optional): item id -> owner for everything the profile and the modules
  already track.
- ``capture(ctx, items) -> Iterable[Proposal]`` (optional): live -> profile text for untracked items, only
  non-default values, with reasons, grouped by purpose.

Public API: ``discover_adapters()``, ``scan(ctx) -> list[Finding]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import ModuleType

from ubuntu_tweaks import adapters
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Finding, Rules, classify
from ubuntu_tweaks.discovery import discover


def discover_adapters() -> list[ModuleType]:
    return discover(adapters, kind="adapter", entry="discover", signature="discover(ctx)")


def scan(ctx: Context, rules: Rules) -> list[Finding]:
    findings: list[Finding] = []
    for module in discover_adapters():
        hook = getattr(module, "owned", None)
        owned: Mapping[str, str] = hook(ctx) if callable(hook) else {}
        opinion = getattr(module, "classify", None)
        for item in module.discover(ctx):
            finding = opinion(ctx, item, rules) if callable(opinion) else None
            findings.append(finding or classify(item, rules, owned))
    return findings

"""``ubuntu-tweak bundles``: the settings bundles of the profile and how they merge. Read-only.

Accessed via: ``bin/ubuntu-tweak bundles [--show NAME] [--machine NAME]``.

Without ``--show``: one row per bundle (shared and overlay) with origin, key count, whether it applies
on this machine (``machines`` / ``requires-module``) and its description; then the keys an overlay
bundle shadows, the duplicated keys (same value in two bundles of one layer: a warning) and the
conflicts (different values: invalid config, exit 2, the same error ``apply`` gives). ``--show NAME``
prints the merged result of that bundle name: the shared bundle with the machine overlay's keys on
top, each key with its reason and where it comes from. Public API: ``register``, ``run``,
``render_list(ctx)``, ``render_show(ctx, name)``.
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks.bundles import Bundle, BundleSet, load_bundles, module_gate
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError, ExitCode


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "bundles",
        help="list the settings bundles (purpose-grouped keyfiles) and how they merge; --show NAME prints one merged",
    )
    parser.add_argument("--show", metavar="NAME", help="print the merged result of one bundle name")
    parser.set_defaults(handler=run)


def _load(ctx: Context) -> BundleSet:
    return load_bundles(ctx.profile, module_gate(ctx.modules_dir, ctx.profile))


def _state(bundle: Bundle) -> str:
    return "active" if bundle.active else f"inactive: {bundle.inactive}"


def render_list(ctx: Context) -> tuple[str, bool]:
    """``(text, ok)``; ``ok`` is False when two bundles of one layer set the same key to different values."""
    bundles = _load(ctx)
    lines = [f"bundles of {ctx.profile.root} for machine {ctx.profile.machine}:"]
    if not bundles.bundles:
        lines.append("  (none)")
    else:
        rows = [("NAME", "ORIGIN", "KEYS", "DESCRIPTION")] + [
            (b.name, b.origin, str(len(b.keys)), b.description or "-") for b in bundles.bundles
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(3)]
        for index, (name, origin, keys, description) in enumerate(rows):
            line = f"  {name:<{widths[0]}}  {origin:<{widths[1]}}  {keys:>{widths[2]}}  {description}"
            lines.append(line)
            if index and not bundles.bundles[index - 1].active:
                lines.append(f"      {_state(bundles.bundles[index - 1])}")
    if bundles.shadows:
        lines += ["", "shadowed (an overlay key beats a shared one):"]
        lines += [
            f"  {s.shared.key.full}: {s.shared.where} = {s.shared.key.value}"
            f" shadowed by {s.overlay.where} = {s.overlay.key.value}" + (" (same value)" if s.same_value else "")
            for s in bundles.shadows
        ]
    if bundles.duplicates:
        lines += ["", "duplicated (same value in two bundles of one layer; warning):"]
        lines += [f"  {c.first.key.full}: {c.first.where} and {c.second.where}" for c in bundles.duplicates]
    if bundles.conflicts:
        lines += ["", "CONFLICTS (different values in two bundles of one layer; apply refuses with exit 2):"]
        lines += [
            f"  {c.first.key.full}: {c.first.where} = {c.first.key.value} vs {c.second.where} = {c.second.key.value}"
            for c in bundles.conflicts
        ]
    return "\n".join(lines), not bundles.conflicts


def render_show(ctx: Context, name: str) -> str:
    bundles = _load(ctx)
    named = [b for b in bundles.bundles if b.name == name]
    if not named:
        raise ConfigError(f"no bundle named {name}; `ubuntu-tweak bundles` lists them")
    sources = " + ".join(b.source for b in named)
    lines = [f"# bundle {name} merged for machine {ctx.profile.machine}: {sources}"]
    if len(named) > 1:
        lines.append("# the overlay's keys win over the shared bundle's, key by key")
    for bundle in named:
        if not bundle.active:
            lines.append(f"# {bundle.source} does not apply here ({bundle.inactive}); shown anyway")
        if bundle.description:
            lines.append(f"# {bundle.origin} description: {bundle.description}")
    merged: dict[tuple[str, str], list[tuple[Bundle, int]]] = {}
    for bundle in sorted(named, key=lambda b: b.origin != "shared"):
        for index, key in enumerate(bundle.keys):
            merged.setdefault((key.section, key.key), []).append((bundle, index))
    by_section: dict[str, list[list[tuple[Bundle, int]]]] = {}
    for (section, _key_name), holders in merged.items():
        by_section.setdefault(section, []).append(holders)
    for section, groups in by_section.items():
        lines += ["", f"[{section}]"]
        for holders in groups:
            bundle, index = holders[-1]
            key = bundle.keys[index]
            reason = next((h.keys[i].reason for h, i in reversed(holders) if h.keys[i].reason), "")
            if reason:
                lines.append(f"# {reason}")
            lines.append(f"{key.key}={key.value}")
            origin = f"# from {bundle.where(key)}"
            if len(holders) > 1:
                old_bundle, old_index = holders[0]
                old = old_bundle.keys[old_index]
                origin += f", overrides {old_bundle.where(old)} = {old.value}"
            lines.append(origin)
    return "\n".join(lines)


def run(ctx: Context, args: argparse.Namespace) -> int:
    if args.show:
        print(render_show(ctx, args.show))
        return int(ExitCode.OK)
    text, ok = render_list(ctx)
    print(text)
    return int(ExitCode.OK if ok else ExitCode.INVALID_CONFIG)

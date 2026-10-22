"""``ubuntu-tweak list``: modules with kind, enabled state and source. Read-only.

Accessed via: ``bin/ubuntu-tweak list [--only a,b]``.

Public API: ``register(subparsers)``, ``run(ctx, args) -> int`` and ``render(ctx) -> str``
(the table, for tests). "Enabled" is the merged value: module default < profile.toml <
machine.toml. A name in ``--only`` that is not a module is ``UnknownModuleError`` (exit 4).
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks import ui
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.manifest import Manifest, SourceKind, discover_modules, module_defaults


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("list", help="list modules with kind, enabled state and source")
    parser.set_defaults(handler=run)


def _source_label(manifest: Manifest) -> str:
    source = manifest.source
    if source.kind is SourceKind.SYSTEM:
        return f"modules/{manifest.name}/{source.install[0]} ({'root' if source.root else 'user'})"
    return source.path or "(settings only)"


def _render_rich(rows: list[tuple[str, str, str, str]]) -> str:
    enabled = sum(row[2] == "yes" for row in rows)
    shown = [
        (
            ui.style(name, "bold"),
            ui.style(kind, "dim"),
            (ui.badge("ok", word=False) + " on") if on == "yes" else ui.style("- off", "dim"),
            ui.style(source, "dim"),
        )
        for name, kind, on, source in rows
    ]
    lines = [ui.heading(f"ubuntu-tweak modules ({len(rows)})")]
    lines += ui.wrap(
        "A module is one tweak (an extension, a settings preset or a system installer). "
        "Enabled comes from the module default, overridden by profile.toml, then machine.toml.",
        indent=2,
    )
    lines.append("")
    lines += ui.table(("MODULE", "KIND", "ENABLED", "SOURCE"), shown)
    lines += ["", f"  {ui.style(f'{enabled} enabled, {len(rows) - enabled} disabled', 'dim')}"]
    return "\n".join(lines)


def render(ctx: Context) -> str:
    manifests = discover_modules(ctx.modules_dir)
    ctx.require_modules({manifest.name for manifest in manifests})
    config = ctx.profile.load_config(module_defaults(manifests))
    rows = [
        (
            manifest.name,
            manifest.source.kind.value,
            "yes" if config.module_enabled(manifest.name) else "no",
            _source_label(manifest),
        )
        for manifest in manifests
        if ctx.selects(manifest.name)
    ]
    if not rows:
        return "No modules."
    if ui.rich():
        return _render_rich(rows)
    table = [("MODULE", "KIND", "ENABLED", "SOURCE"), *rows]
    widths = [max(len(row[column]) for row in table) for column in range(3)]
    return "\n".join(
        f"{name:<{widths[0]}}  {kind:<{widths[1]}}  {enabled:<{widths[2]}}  {source}"
        for name, kind, enabled, source in table
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    print(render(ctx))
    return 0

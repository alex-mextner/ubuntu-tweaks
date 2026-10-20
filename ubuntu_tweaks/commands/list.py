"""``ubuntu-tweak list``: modules with kind, enabled state and source. Read-only.

Accessed via: ``bin/ubuntu-tweak list [--only a,b]``.

Public API: ``register(subparsers)``, ``run(ctx, args) -> int`` and ``render(ctx) -> str``
(the table, for tests). "Enabled" is the merged value: module default < profile.toml <
machine.toml. A name in ``--only`` that is not a module is ``UnknownModuleError`` (exit 4).
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import UnknownModuleError
from ubuntu_tweaks.manifest import Manifest, SourceKind, discover_modules, module_defaults


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("list", help="list modules with kind, enabled state and source")
    parser.set_defaults(handler=run)


def _source_label(manifest: Manifest) -> str:
    source = manifest.source
    if source.kind is SourceKind.SYSTEM:
        return f"modules/{manifest.name}/{source.install[0]} (root)"
    return source.path or "(settings only)"


def render(ctx: Context) -> str:
    manifests = discover_modules(ctx.modules_dir)
    unknown = sorted(ctx.only - {manifest.name for manifest in manifests})
    if unknown:
        raise UnknownModuleError(f"unknown module(s): {', '.join(unknown)}")
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
    table = [("MODULE", "KIND", "ENABLED", "SOURCE"), *rows]
    widths = [max(len(row[column]) for row in table) for column in range(3)]
    return "\n".join(
        f"{name:<{widths[0]}}  {kind:<{widths[1]}}  {enabled:<{widths[2]}}  {source}"
        for name, kind, enabled, source in table
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    print(render(ctx))
    return 0

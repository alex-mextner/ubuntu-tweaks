"""``ubuntu-tweak bundle-move <bundle> <key-or-glob>... [--commit]``: group keys into one bundle.

Accessed via: ``bin/ubuntu-tweak bundle-move``.

Moves every key of the active bundles that matches a dconf key path or glob (``org/gnome/x/key``,
``org/gnome/desktop/wm/keybindings/**``; a leading ``/`` is optional) into ``<bundle>``, together with the
comment above it, so grouping settings by purpose is one command instead of hand editing. Preview by
default (the moves and the files they touch); ``--commit`` writes them atomically. A key stays in its
layer: shared keys go to ``settings/<bundle>.ini``, overlay keys to
``machines/<machine>/settings/<bundle>.ini``, and it must keep applying on the same machines (a move
between bundles with different ``machines`` / ``requires-module`` headers is refused, exit 2). A pattern
that matches nothing is invalid (exit 2); an unsafe destination (symlink, outside the profile) is
refused (exit 5). Public API: ``register``, ``run``.
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks import ui
from ubuntu_tweaks.bundle_edit import plan_move, write_edits
from ubuntu_tweaks.bundles import module_gate
from ubuntu_tweaks.context import Context


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "bundle-move",
        help="move keys (with their comments) from whichever bundle holds them into one bundle",
    )
    parser.add_argument(
        "bundle", help="destination bundle name, e.g. dev-keyboard or laptop/power (created when missing)"
    )
    parser.add_argument(
        "keys",
        nargs="+",
        metavar="KEY_OR_GLOB",
        help="dconf key path or glob, e.g. org/gnome/desktop/wm/keybindings/**",
    )
    parser.add_argument("--commit", action="store_true", help="write the moves (default: preview only)")
    parser.set_defaults(handler=run)


def run(ctx: Context, args: argparse.Namespace) -> int:
    plan = plan_move(ctx.profile, args.bundle, args.keys, module_gate(ctx.modules_dir, ctx.profile))
    if ui.rich():
        suffix = "" if args.commit else " " + ui.style("(--commit writes it)", "dim")
        print(ui.heading("ubuntu-tweak bundle-move") + suffix)
    print("\n".join(plan.lines))
    if not plan.writes:
        return 0
    if not args.commit:
        print(ui.say("info", f"preview: {len(plan.writes)} file(s) would change; --commit writes it"))
        return 0
    print("\n".join(ui.say("ok", line) for line in write_edits(plan, ctx.profile)))
    return 0

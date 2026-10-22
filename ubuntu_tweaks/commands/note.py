"""``ubuntu-tweak note <target> "reason" [--commit]``: add, change or remove the reason of a setting.

Accessed via: ``bin/ubuntu-tweak note``.

``<target>`` is ``<bundle>:<dconf key path>`` (``dev-keyboard:org/gnome/desktop/wm/keybindings/close``),
``packages:<apt|snap|flatpak>:<name>`` or ``system:<key>`` (``system:timezone``,
``system:keyboard.layout``). The reason is the comment above a bundle or system key, or the trailing
comment of a package; an empty reason removes it. Preview by default; ``--commit`` writes the one
changed file atomically. ``--origin shared|overlay`` picks the layer when the target is in both
(otherwise exit 2). The target must exist (exit 2); a symlinked profile file is refused (exit 5).
Public API: ``register``, ``run``.
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks import ui
from ubuntu_tweaks.bundle_edit import plan_note, write_edits
from ubuntu_tweaks.context import Context


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("note", help="add, change or remove the reason of a setting, key or package")
    parser.add_argument("target", help="<bundle>:<dconf key path> | packages:<apt|snap|flatpak>:<name> | system:<key>")
    parser.add_argument("reason", help="the reason text (empty removes it)")
    parser.add_argument("--origin", choices=("shared", "overlay"), help="the layer to edit when the target is in both")
    parser.add_argument("--commit", action="store_true", help="write the change (default: preview only)")
    parser.set_defaults(handler=run)


def run(ctx: Context, args: argparse.Namespace) -> int:
    plan = plan_note(ctx.profile, args.target, args.reason, args.origin)
    if ui.rich():
        suffix = "" if args.commit else " " + ui.style("(--commit writes it)", "dim")
        print(ui.heading("ubuntu-tweak note") + suffix)
    print("\n".join(plan.lines))
    if not args.commit:
        print(ui.say("info", "preview: nothing was written; --commit writes it"))
        return 0
    print("\n".join(ui.say("ok", line) for line in write_edits(plan, ctx.profile)))
    return 0

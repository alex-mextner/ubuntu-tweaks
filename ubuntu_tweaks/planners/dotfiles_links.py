"""Dotfiles planner: link the shell rc files of the user's dotfiles repository into ``$HOME``.

Accessed via: planner discovery (``build``).

Assumptions: ``dotfiles_rc.provided`` decides which files (the checkout's ``.zprofile``, ``.zshrc``, ``.profile``
that the profile's ``files/`` does not link). Each becomes a ``SymlinkAction`` of kind ``dotfiles-link`` (nothing at
``~/<name>``) or ``dotfiles-link-replace`` (a real file or another link is there: copied to the backup directory
first, spec invariant 5). The actions run in the BASE phase right after the ``dotfiles`` module's clone, so they
precede the brew steps; they are not extension code (no re-login hint, no staged settings pass).

Public API: ``build(ctx, backend, *, home=None)``.
"""

from __future__ import annotations

from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.dotfiles_rc import DOTFILES_MODULE, provided
from ubuntu_tweaks.planners.base import POST_ORDER_OFFSET
from ubuntu_tweaks.settings import SettingsBackend
from ubuntu_tweaks.symlink import SymlinkAction


def build(ctx: Context, backend: SettingsBackend, *, home: Path | None = None) -> list[Action]:
    files = provided(ctx)
    if not files:
        return []
    order = next((m.order for m in ctx.manifests() if m.name == DOTFILES_MODULE), 100) + POST_ORDER_OFFSET
    root = home if home is not None else Path.home()
    actions: list[Action] = []
    for name, source in files.items():
        action = SymlinkAction(root / name, source, scope=DOTFILES_MODULE, kind_prefix="dotfiles-")
        action.phase = Phase.BASE
        action.order = order
        action.key = f"dotfiles-link:{name}"
        actions.append(action)
    return actions

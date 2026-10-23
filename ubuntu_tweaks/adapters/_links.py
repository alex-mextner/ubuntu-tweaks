"""Who a symlink belongs to: the profile, the dotfiles checkout or the engine checkout.

Accessed via: ``adapters/files.py`` and ``adapters/units.py`` (``owned``). Names starting with ``_`` are skipped by
adapter discovery.

Public API: ``link_owner(ctx, path) -> str | None``: ``"profile (symlink)"`` and so on when ``path`` is a symlink whose
resolved target lies inside one of the three trees, else ``None``.
"""

from __future__ import annotations

import os
from pathlib import Path

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.dotfiles_rc import checkout as dotfiles_checkout


def _within(target: str, root: Path | None) -> bool:
    if root is None:
        return False
    base = os.path.realpath(root)
    return target == base or target.startswith(base + os.sep)


def link_owner(ctx: Context, path: Path) -> str | None:
    if not path.is_symlink():
        return None
    target = os.path.realpath(path)
    for root, name in (
        (ctx.profile.root, "profile"),
        (dotfiles_checkout(ctx), "dotfiles checkout"),
        (ctx.repo_root, "engine checkout"),
    ):
        if _within(target, root):
            return f"{name} (symlink)"
    return None

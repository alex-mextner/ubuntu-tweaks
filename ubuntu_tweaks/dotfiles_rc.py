"""The shell rc files the user's dotfiles repository provides (spec, *rc files from the repository*).

Accessed via: ``planners/dotfiles_links.py`` (links them into ``$HOME``) and ``planners/base.py`` (the generic
``shell-rc`` step and the brew ``shellenv`` post leave out what the repository provides).

The repository's own convention is symlinks from ``$HOME`` to files at its root (``installer.sh``:
``ln -s .files/.zprofile ...``), so a file named like an rc file at the root of the checkout is linked, never
copied. The profile wins: a name its ``files/`` links is not taken from the repository. ``--only`` drops the
links (they carry the ``dotfiles`` scope), and so does switching the ``dotfiles`` module off.

Public API: ``SHELL_RC_FILES``, ``DOTFILES_MODULE``, ``checkout(ctx)``, ``provided(ctx)``.
"""

from __future__ import annotations

from pathlib import Path

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.manifest import module_defaults

SHELL_RC_FILES = (".zprofile", ".zshrc", ".profile")
DOTFILES_MODULE = "dotfiles"


def checkout(ctx: Context) -> Path | None:
    """The dotfiles checkout when it exists on disk (a profile found through ``gh`` names it before it is cloned)."""
    location = ctx.profile.location
    where = location.checkout if location is not None else None
    return where if where is not None and where.is_dir() else None


def provided(ctx: Context) -> dict[str, Path]:
    """``name -> file in the checkout`` of each shell rc file the repository carries and the profile does not link."""
    where = checkout(ctx)
    if where is None or ctx.only:
        return {}
    manifests = ctx.manifests()
    if not ctx.profile.load_config(module_defaults(manifests)).module_enabled(DOTFILES_MODULE):
        return {}
    linked = {str(entry.rel) for entry in ctx.profile.files()}
    return {name: where / name for name in SHELL_RC_FILES if name not in linked and (where / name).is_file()}

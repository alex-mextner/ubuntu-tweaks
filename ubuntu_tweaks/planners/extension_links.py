"""Planner: symlink each enabled extension module into the user's GNOME Shell extensions directory.

Accessed via: planner discovery (``planners/__init__.py``).

Assumptions: the link name is the uuid from the extension's ``metadata.json`` (resolved lazily, so
the action can be planned before a submodule is initialized). A real directory, a file or a symlink
elsewhere in the way makes the action kind ``link-replace``: it is copied to
``~/.local/state/ubuntu-tweaks/backup/<UTC timestamp>/`` and removed first (``symlink.replace_with_symlink``).
``extras()`` lists symlinks that point into this repository under a name no module declares.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import ExtensionModule, extension_modules, tilde, user_extensions_dir
from ubuntu_tweaks.settings import SettingsBackend
from ubuntu_tweaks.symlink import Clock, describe_link, is_linked, link_kind, replace_with_symlink, utc_now


class ExtensionLink(Action):
    phase = Phase.LINK

    def __init__(self, module: ExtensionModule, extensions_dir: Path, *, now: Clock = utc_now) -> None:
        self.module = module
        self.extensions_dir = extensions_dir
        self.scope = module.name
        self._now = now

    def _dest(self) -> Path | None:
        metadata = self.module.metadata()
        return None if metadata is None else self.extensions_dir / metadata.uuid

    @property
    def kind(self) -> str:
        dest = self._dest()
        return "link" if dest is None else link_kind(dest, self.module.source_dir)

    def describe(self) -> str:
        dest = self._dest()
        if dest is None:
            return (
                f"link {self.module.name} into {tilde(self.extensions_dir)}/<uuid from metadata.json, "
                f"known once the submodule is initialized> -> {tilde(self.module.source_dir)}"
            )
        return describe_link(dest, self.module.source_dir)

    def check(self) -> bool:
        dest = self._dest()
        return dest is not None and is_linked(dest, self.module.source_dir)

    def run(self) -> None:
        metadata = self.module.require_metadata()
        replace_with_symlink(self.extensions_dir / metadata.uuid, self.module.source_dir, now=self._now)


def build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]:
    directory = user_extensions_dir()
    return [ExtensionLink(module, directory) for module in extension_modules(ctx)]


def extras(ctx: Context, backend: SettingsBackend) -> Sequence[str]:
    directory = user_extensions_dir()
    if not directory.is_dir():
        return []
    declared = {
        metadata.uuid for module in extension_modules(ctx, enabled_only=False) if (metadata := module.metadata())
    }
    repo = os.path.realpath(ctx.repo_root)
    lines: list[str] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_symlink() or entry.name in declared:
            continue
        target = os.path.realpath(entry)
        if target == repo or target.startswith(repo + os.sep):
            lines.append(
                f"extension link {tilde(entry)} -> {tilde(Path(os.readlink(entry)))} into this repo, no module declares {entry.name}"
            )
    return lines

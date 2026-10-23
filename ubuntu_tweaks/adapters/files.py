"""files adapter: the top-level entries of the places apps keep their configuration (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``, ``capture``.

Assumptions:

- Items are ``file:~/<path>``: each entry of ``~/.config``, ``~/.local/share`` and ``~/.local/bin``, and every dotfile
  of ``$HOME`` (``.config`` and ``.local`` themselves are expanded, not listed). Only the top level is listed, so one
  app is one item however many files it keeps; ``detail`` is the kind of thing (``directory, 12 entries``, a size,
  ``symlink -> target``), never content. Nothing is opened for reading.
- ``owned``: the top-level entry of every file the profile's ``files/`` (and the machine overlay) declares, the shell rc
  files of the dotfiles checkout, an entry that is a symlink into the profile, the dotfiles checkout or the engine
  checkout, and ``~/.local/share/gnome-shell`` while an extension module exists (its link lives below it).
- ``capture`` proposes a ``[capture] include_files`` list for the profile: the entries as ``$HOME``-relative globs
  (``<dir>/**`` for a directory); what ``capture --commit`` finds secret-bearing it refuses by itself.

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``, ``capture(ctx, items)``, ``item_id(rel)`` (the id of a
``$HOME``-relative path) and ``entry_id(rel)`` (the id of the top-level entry a path belongs to, or ``None``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from ubuntu_tweaks.adapters._links import link_owner
from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item, Proposal
from ubuntu_tweaks.dotfiles_rc import provided as dotfiles_provided
from ubuntu_tweaks.manifest import SourceKind

NAME = "files"
_PREFIX = "file:~/"
_EXPANDED = (PurePosixPath(".config"), PurePosixPath(".local/share"), PurePosixPath(".local/bin"))
_SKIPPED_HOME = frozenset({".config", ".local"})


def item_id(rel: PurePosixPath | str) -> str:
    return _PREFIX + str(rel)


def entry_id(rel: PurePosixPath) -> str | None:
    """The item a ``$HOME``-relative path belongs to: its top-level entry, or ``None`` outside the scanned places."""
    parts = rel.parts
    for base in _EXPANDED:
        depth = len(base.parts)
        if parts[:depth] == base.parts and len(parts) > depth:
            return item_id(PurePosixPath(*parts[: depth + 1]))
    if len(parts) >= 1 and parts[0].startswith(".") and parts[0] not in _SKIPPED_HOME:
        return item_id(parts[0])
    return None


def _scan(home: Path) -> list[tuple[PurePosixPath, Path]]:
    found: list[tuple[PurePosixPath, Path]] = []
    for base in _EXPANDED:
        found += _children(home, base)
    found += [
        pair
        for pair in _children(home, PurePosixPath())
        if pair[0].name.startswith(".") and pair[0].name not in _SKIPPED_HOME
    ]
    return sorted(found, key=lambda pair: str(pair[0]))


def _children(home: Path, base: PurePosixPath) -> list[tuple[PurePosixPath, Path]]:
    directory = home.joinpath(*base.parts)
    try:
        names = sorted(os.listdir(directory))
    except OSError:  # absent, or not readable: nothing to list
        return []
    return [(base / name, directory / name) for name in names]


def _describe(path: Path) -> tuple[str, str]:
    """``(kind, detail)`` of one entry without opening it."""
    try:
        if path.is_symlink():
            return "symlink", f"-> {os.readlink(path)}"
        if path.is_dir():
            count = len(os.listdir(path))
            return "dir", f"directory, {count} {'entry' if count == 1 else 'entries'}"
        size = path.stat().st_size
    except OSError as error:
        return "file", f"cannot stat: {error.strerror or error}"
    return "file", f"{size} B" if size < 1024 else f"{size / 1024:.1f} KiB"


def discover(ctx: Context) -> Iterable[Item]:
    for rel, path in _scan(system().home):
        kind, detail = _describe(path)
        yield Item(NAME, item_id(rel), kind, detail)


def owned(ctx: Context) -> Mapping[str, str]:
    profile = ctx.profile
    found: dict[str, str] = {}
    for entry in profile.files():
        target = entry_id(entry.rel)
        if target is not None:
            found.setdefault(target, entry.source.relative_to(profile.root).as_posix())
    for name in dotfiles_provided(ctx):
        found.setdefault(item_id(name), "dotfiles checkout")
    extension_modules = sorted(
        manifest.name
        for manifest in ctx.manifests()
        if manifest.source.kind in (SourceKind.SUBMODULE, SourceKind.LOCAL)
    )
    if extension_modules:
        found.setdefault(item_id(".local/share/gnome-shell"), "module " + ", ".join(extension_modules))
    for rel, path in _scan(system().home):
        label = link_owner(ctx, path)
        if label is not None:
            found.setdefault(item_id(rel), label)
    return found


def capture(ctx: Context, items: Sequence[Item]) -> Iterable[Proposal]:
    entries: list[str] = []
    members: list[Item] = []
    for item in items:
        if item.kind == "symlink":
            continue
        rel = item.id.removeprefix(_PREFIX)
        entries.append(f"{rel}/**" if item.kind == "dir" else rel)
        members.append(item)
    if not entries:
        return
    lines = ["[capture]", "include_files = [", *(f'  "{entry}",' for entry in entries), "]"]
    yield Proposal("profile.toml [capture] include_files", "\n".join(lines) + "\n", tuple(members))

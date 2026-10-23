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

- ``classify`` (``discover`` itself still opens nothing): asks capture's file classifier what an unclaimed entry
  holds, depth- and size-limited and reading only the small text files the classifier reads (never a secret-named
  file, never a socket or pipe). Every file credential-bearing -> ``secret`` (``credential store by name`` /
  ``content matches: <kinds>``, never the match); only caches, state, logs, sockets or bytecode -> ``excluded``
  (``cache-state: ...``); an empty directory -> ``excluded``; a symlink into a package manager's tree
  (``~/.local/share/uv/tools``, ``~/.local/lib/node_modules``, ``~/.local/opt``, ``~/Applications``) -> ``excluded``
  (``link created by <installer>; the package is declared elsewhere``); portable config only -> ``untracked`` with
  ``portable config: capture can adopt`` appended to the detail; portable config next to secrets or state, or a
  large data directory or an unreadable file -> ``untracked`` with ``mixed: needs a rule``.

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``, ``classify(ctx, item, rules)``, ``capture(ctx, items)``,
``item_id(rel)`` (the id of a
``$HOME``-relative path) and ``entry_id(rel)`` (the id of the top-level entry a path belongs to, or ``None``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ubuntu_tweaks.adapters._capture_view import capture_view
from ubuntu_tweaks.adapters._links import link_owner
from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.capture.files_scan import Candidate, scan_entry
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Finding, Item, Proposal, Rules
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.dotfiles_rc import provided as dotfiles_provided
from ubuntu_tweaks.manifest import SourceKind

NAME = "files"
_PREFIX = "file:~/"
_EXPANDED = (PurePosixPath(".config"), PurePosixPath(".local/share"), PurePosixPath(".local/bin"))
_SKIPPED_HOME = frozenset({".config", ".local"})
_NAME_REASONS = frozenset({"name matches a credential pattern", "credential store or browser profile by name"})


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


# Trees a package manager keeps its installs in, relative to $HOME, and who to name for a link into each.
_INSTALLER_TREES = (
    (PurePosixPath(".local/share/uv/tools"), "uv"),
    (PurePosixPath(".local/lib/node_modules"), "npm"),
    (PurePosixPath(".local/opt"), "a manual install under ~/.local/opt"),
    (PurePosixPath("Applications"), "an AppImage installer"),
)
_EMPTY = "empty directory: nothing to track"


def _listable(path: Path) -> bool:
    """False for a directory this user cannot list (it is not empty, only unreadable)."""
    try:
        os.listdir(path)
    except NotADirectoryError:
        return True
    except OSError:
        return False
    return True


def _installer_of(path: Path, home: Path) -> str | None:
    """Who created the symlink ``path`` when it points into one of the package-manager trees (never followed)."""
    try:
        target = os.path.normpath(os.path.join(path.parent, os.readlink(path)))
    except OSError:
        return None
    for tree, installer in _INSTALLER_TREES:
        root = str(home.joinpath(*tree.parts))
        if target == root or target.startswith(root + os.sep):
            return installer
    return None


def _secret_reason(candidates: Iterable[Candidate]) -> str:
    """Why every credential-bearing file counts: by name, or the kinds of pattern found (never the match)."""
    reasons = {
        "credential store by name" if c.reason in _NAME_REASONS else c.reason
        for c in candidates
        if c.cls == "secret-bearing"
    }
    return "; ".join(sorted(reasons))


def classify(ctx: Context, item: Item, rules: Rules) -> Finding | None:
    """Apply capture's file classifier to the entry: credentials -> ``secret``, only caches and state -> ``excluded``,
    a link a package manager made -> ``excluded``; portable config and mixed entries stay ``untracked`` with a detail
    saying which. Reads only what the classifier reads (small text files, never a secret-named one)."""
    home = system().home
    path = home / item.id.removeprefix(_PREFIX)
    if item.kind == "symlink":
        installer = _installer_of(path, home)
        if installer is None:
            return None
        return Finding(item, Coverage.EXCLUDED, f"link created by {installer}; the package is declared elsewhere")
    try:
        file_rules = capture_view(ctx).rules.files
    except ConfigError:
        return None  # an invalid [capture] table is capture's to report, not a reason to abort the scan
    scanned = scan_entry(home, path, file_rules)
    found = scanned.candidates
    if not found:
        if scanned.symlinks_ignored or not _listable(path):
            return None  # something is there that was not looked at: it stays a finding
        return Finding(item, Coverage.EXCLUDED, _EMPTY)
    portable = any(c.cls == "portable-config" for c in found)
    mixed = any(c.cls == "mixed" or c.reason == "unreadable" for c in found)
    if portable or mixed:
        pure = portable and all(c.cls == "portable-config" for c in found)
        note = "portable config: capture can adopt" if pure else "mixed: needs a rule"
        return Finding(replace(item, detail=f"{item.detail}; {note}" if item.detail else note), Coverage.UNTRACKED)
    if any(c.cls == "secret-bearing" for c in found):
        return Finding(item, Coverage.SECRET, _secret_reason(found))
    reasons = sorted({c.reason for c in found})
    return Finding(
        item, Coverage.EXCLUDED, "cache-state: " + "; ".join(reasons[:3]) + ("; ..." if len(reasons) > 3 else "")
    )


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

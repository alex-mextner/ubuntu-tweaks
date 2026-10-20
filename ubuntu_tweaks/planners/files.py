"""Files planner: every leaf under ``<profile>/files`` (and the machine overlay) becomes a symlink
at the same relative path under ``$HOME``.

Accessed via: planner discovery (``build`` and ``extras``).

Assumptions:

- Only leaves are linked; directories stay real so apps can add siblings. Parent directories of a
  link are created when missing.
- Kind ``link``: nothing at the destination. Kind ``link-replace``: a real file, or a symlink
  that points elsewhere (the plan says ``backup+replace``; identical content is called
  ``adopt``). Either way the original goes through ``backup.backup`` before it is replaced. The
  kind is computed from the live destination, so it is always what ``run()`` would do.
- A real directory in the way is never touched: the action says so and ``run()`` raises
  ``RefusedError``.
- Unsafe destinations are refused up front (``RefusedError``, exit 5), before any plan exists: a
  relative path with ``..`` and a destination whose nearest existing parent resolves outside
  ``$HOME`` (a symlinked ``~/.config`` that leads elsewhere, for instance).
- ``extras`` looks for symlinks into the profile whose file was removed from it. It scans the
  directories the profile manages plus ``$HOME``, ``~/.config`` and the first level below
  ``~/.config``; a deeper orphan is not found. It never removes anything.

Public API: ``build(ctx, backend, *, home=None, clock=None, state_home=None)``,
``extras(ctx, backend, *, home=None)``, ``FileLinkAction``.
"""

from __future__ import annotations

import difflib
import filecmp
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.backup import backup
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import RefusedError, UbuntuTweakError
from ubuntu_tweaks.profile import Profile, ProfileFile
from ubuntu_tweaks.settings import SettingsBackend

_DIFF_LIMIT = 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _size(count: int) -> str:
    return f"{count} B" if count < 1024 else f"{count / 1024:.1f} KiB"


def _tilde(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _profile_label(profile: Profile, source: Path) -> str:
    try:
        return source.relative_to(profile.root).as_posix()
    except ValueError:
        return str(source)


def _destination(home: Path, rel: PurePosixPath) -> Path:
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise RefusedError(f"refused: files/{rel} would link outside $HOME (absolute path or '..')")
    destination = home.joinpath(*rel.parts)
    ancestor = destination.parent
    while not os.path.lexists(ancestor) and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not _within(os.path.realpath(ancestor), os.path.realpath(home)):
        raise RefusedError(
            f"refused: {destination} would be created outside $HOME "
            f"({ancestor} resolves to {os.path.realpath(ancestor)})"
        )
    return destination


def _changed_lines(disk: Path, profile_copy: Path) -> str:
    """``+a/-b`` lines the profile copy would add to / drop from the file on disk, or a note."""
    try:
        if disk.stat().st_size > _DIFF_LIMIT or profile_copy.stat().st_size > _DIFF_LIMIT:
            return "too large to diff"
        old = disk.read_text(encoding="utf-8").splitlines()
        new = profile_copy.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "binary or unreadable, not diffed"
    added = removed = 0
    for line in difflib.unified_diff(old, new, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return f"+{added}/-{removed} lines"


class FileLinkAction(Action):
    phase = Phase.FILES
    scope = None

    def __init__(
        self,
        entry: ProfileFile,
        destination: Path,
        *,
        profile: Profile,
        home: Path,
        clock: Callable[[], datetime],
        state_home: Path | None,
    ) -> None:
        self._entry = entry
        self._destination = destination
        self._profile = profile
        self._home = home
        self._clock = clock
        self._state_home = state_home

    @property
    def _source(self) -> Path:
        return self._entry.source

    def _state(self) -> str:
        destination = self._destination
        if destination.is_symlink():
            target = os.readlink(destination)
            resolved = os.path.normpath(os.path.join(destination.parent, target))
            return "linked" if resolved == os.path.normpath(self._source) else "symlink"
        if not os.path.lexists(destination):
            return "missing"
        return "directory" if destination.is_dir() else "file"

    @property
    def kind(self) -> str:
        return "link" if self._state() == "missing" else "link-replace"

    def check(self) -> bool:
        return self._state() == "linked"

    def describe(self) -> str:
        state = self._state()
        where = _tilde(self._destination, self._home)
        what = f"{where} -> {_profile_label(self._profile, self._source)}"
        if self._entry.origin == "overlay":
            what += " [machine overlay]"
        if state == "missing":
            return f"link {what}"
        if state == "directory":
            return f"REFUSED {what}: {where} is a directory, move it away first"
        if state == "symlink":
            return f"backup+replace {what} (now a symlink to {os.readlink(self._destination)})"
        if filecmp.cmp(self._destination, self._source, shallow=False):
            return f"adopt {what} (identical content; the original is backed up first)"
        disk, profile_size = self._destination.stat().st_size, self._source.stat().st_size
        return (
            f"backup+replace {what} (differs: {_size(disk)} on disk, {_size(profile_size)} in profile, "
            f"{_changed_lines(self._destination, self._source)})"
        )

    def run(self) -> None:
        state = self._state()
        if state == "directory":
            raise RefusedError(f"refused: {self._destination} is a directory; move it away and run apply again")
        try:
            self._destination.parent.mkdir(parents=True, exist_ok=True)
            if state in ("file", "symlink"):
                backup(self._destination, now=self._clock(), state_home=self._state_home)
            temporary = self._destination.with_name(f".{self._destination.name}.ubuntu-tweak.tmp")
            temporary.unlink(missing_ok=True)
            os.symlink(self._source, temporary)
            os.replace(temporary, self._destination)
        except OSError as error:
            raise UbuntuTweakError(f"cannot link {self._destination}: {error.strerror or error}") from error


def build(
    ctx: Context,
    backend: SettingsBackend,
    *,
    home: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    state_home: Path | None = None,
) -> Sequence[Action]:
    base = home if home is not None else Path.home()
    return [
        FileLinkAction(
            entry,
            _destination(base, entry.rel),
            profile=ctx.profile,
            home=base,
            clock=clock or _now,
            state_home=state_home,
        )
        for entry in ctx.profile.files()
    ]


def _scan_directories(home: Path, declared: set[Path]) -> list[Path]:
    found: dict[Path, None] = {home: None}
    for destination in declared:
        for parent in destination.parents:
            if parent == home or home not in parent.parents:
                break
            found[parent] = None
    config = home / ".config"
    found[config] = None
    if config.is_dir() and not config.is_symlink():
        for child in sorted(config.iterdir()):
            if child.is_dir() and not child.is_symlink():
                found[child] = None
    return list(found)


def extras(ctx: Context, backend: SettingsBackend, *, home: Path | None = None) -> Sequence[str]:
    base = home if home is not None else Path.home()
    profile = ctx.profile
    roots = [os.path.normpath(root) for root in (profile.files_dir, profile.overlay_files_dir)]
    declared = {base.joinpath(*entry.rel.parts) for entry in profile.files()}
    found: list[str] = []
    for directory in _scan_directories(base, declared):
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            continue
        for item in entries:
            path = Path(item.path)
            if not item.is_symlink() or path in declared:
                continue
            target = os.path.normpath(os.path.join(directory, os.readlink(path)))
            if any(_within(target, root) for root in roots):
                found.append(f"{_tilde(path, base)} -> {target}: the profile no longer has that file")
    return found

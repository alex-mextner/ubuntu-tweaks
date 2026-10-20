"""Symlink creation with a backup of whatever is in the way.

Accessed via: ``planners/extension_links.py`` (extension directories) and ``commands/init.py``
(the CLI link in ``~/.local/bin``).

Assumptions: replacing anything that exists at the destination first *moves* it to
``<state>/ubuntu-tweaks/backup/<UTC timestamp>/<absolute path>`` (``<state>`` is
``$XDG_STATE_HOME`` else ``~/.local/state``), so nothing is ever lost (safety invariant 5).

Public API:

- ``is_linked(dest, source) -> bool``: ``dest`` is a symlink resolving to ``source``.
- ``link_kind(dest, source) -> str``: ``"link"`` when ``dest`` does not exist (or is already
  right) and ``"link-replace"`` when something else is there.
- ``describe_link(dest, source) -> str``: the plan line; for a replacement it says
  ``backup+replace`` and whether the content is identical.
  ``SymlinkAction``'s ``kind_prefix`` (e.g. ``"cli-"``) keeps a link that is not extension code out of
  the ``link`` / ``link-replace`` kinds that ``reconcile`` treats as extension code.
- ``replace_with_symlink(dest, source, *, now=utc_now)``: back up what is at ``dest``, then
  create the symlink (parents are created).
- ``SymlinkAction(dest, source, *, scope=None, now=utc_now)``: an ``Action`` (phase ``LINK``)
  around the three functions above.
"""

from __future__ import annotations

import filecmp
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.extension import tilde

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def state_dir(env: dict[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    return Path(environment.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "ubuntu-tweaks"


def is_linked(dest: Path, source: Path) -> bool:
    return dest.is_symlink() and os.path.realpath(dest) == os.path.realpath(source)


def link_kind(dest: Path, source: Path) -> str:
    return "link-replace" if os.path.lexists(dest) and not is_linked(dest, source) else "link"


def _same_tree(first: Path, second: Path) -> bool:
    if first.is_dir() != second.is_dir():
        return False
    if not first.is_dir():
        return filecmp.cmp(first, second, shallow=False)
    comparison = filecmp.dircmp(first, second)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(first, second, comparison.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(_same_tree(first / name, second / name) for name in comparison.common_dirs)


def _content_note(dest: Path, source: Path) -> str:
    if not dest.exists() or not source.exists():
        return "content differs"
    try:
        return "identical content" if _same_tree(dest, source) else "content differs"
    except OSError:
        return "content differs"


def _what_is_there(dest: Path) -> str:
    if dest.is_symlink():
        return f"symlink to {os.readlink(dest)}"
    return "directory" if dest.is_dir() else "file"


def describe_link(dest: Path, source: Path) -> str:
    if link_kind(dest, source) == "link":
        return f"link {tilde(dest)} -> {tilde(source)}"
    return (
        f"backup+replace {tilde(dest)} ({_what_is_there(dest)}, {_content_note(dest, source)}) "
        f"with symlink -> {tilde(source)}"
    )


def replace_with_symlink(dest: Path, source: Path, *, now: Clock = utc_now) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dest):
        stamp = now().strftime("%Y%m%dT%H%M%SZ")
        saved = state_dir() / "backup" / stamp / str(dest.absolute()).lstrip(os.sep)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(dest, saved)
    os.symlink(source, dest)


class SymlinkAction(Action):
    phase = Phase.LINK

    def __init__(
        self, dest: Path, source: Path, *, scope: str | None = None, kind_prefix: str = "", now: Clock = utc_now
    ) -> None:
        self.dest = dest
        self.source = source
        self.scope = scope
        self.kind = kind_prefix + link_kind(dest, source)
        self._now = now

    def describe(self) -> str:
        return describe_link(self.dest, self.source)

    def check(self) -> bool:
        return is_linked(self.dest, self.source)

    def run(self) -> None:
        replace_with_symlink(self.dest, self.source, now=self._now)

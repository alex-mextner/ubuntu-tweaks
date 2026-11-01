"""Would a symlink ``destination -> source`` turn into a loop?

Accessed via: ``planners/files.py`` (profile file links) and ``symlink.py`` (extension, CLI and dotfiles links).

Assumptions: path resolution done by hand, component by component, the way the kernel does it (a ``..`` follows the
links before it); nothing is opened or changed. At most ``MAX_LINKS`` links are followed (the kernel's own limit is 40); a longer chain is reported as a loop.

Public API:

- ``leads_back_to(source, destination) -> bool``: resolving ``source`` to the end passes through ``destination`` (it is
  ``destination``, a link chain that reaches it, or a path that goes through it, also through a directory link). Making
  ``destination`` a link to ``source`` would then point at itself and every reader gets ``ELOOP`` ("Too many levels of
  symbolic links").
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_LINKS = 40


def _canonical_entry(destination: Path) -> str:
    """``destination`` with the directories above it resolved and its own last component kept (it may be a link)."""
    text = os.path.join(os.getcwd(), destination)
    return os.path.join(os.path.realpath(os.path.dirname(text)), os.path.basename(text))


def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def leads_back_to(source: Path, destination: Path) -> bool:
    wanted = _canonical_entry(destination)
    pending = [part for part in os.path.join(os.getcwd(), source).split("/") if part]
    pending.reverse()  # a stack: the next component is last
    current = "/"
    followed = 0
    while pending:
        part = pending.pop()
        if part in (".", ""):
            continue
        if part == "..":
            current = os.path.dirname(current)
            continue
        entry = os.path.join(current, part)
        if entry == wanted:
            return True
        target = _readlink(entry)
        if target is None:
            current = entry
            continue
        followed += 1
        if followed > MAX_LINKS:
            return True  # a chain this long is ELOOP for every reader anyway: unresolved counts as a loop
        if target.startswith("/"):
            current = "/"
        pending.extend(reversed([p for p in target.split("/") if p]))
    return False

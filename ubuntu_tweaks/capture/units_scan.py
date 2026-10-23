"""Section 3b of `capture`: the ``units.toml`` proposal, from the systemd user units that are enabled on this machine.

Accessed via: ``plan.build_plan``. Read-only: it lists the entries of ``~/.config/systemd/user/*.wants`` and
``*.requires`` (what ``systemctl --user enable`` creates) and keeps the units whose file lives in
``~/.config/systemd/user`` (an instance counts through its template). Units the system or the desktop ships are not
proposed: they are not the user's to declare. The reason comment of a proposed name is the unit's ``Description=``.
Marks: ``+`` not yet in the profile's ``units.toml`` ``enable`` list, ``=`` already there; a unit the profile
``disable``s is left out, and so is a unit whose file the files section did not adopt or that holds a secret (in the
file or a drop-in).

Public API: ``UnitItem``, ``scan_units(home, profile, not_adopted=())``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from collections.abc import Collection
from pathlib import Path

from ubuntu_tweaks.capture.rules import Mark
from ubuntu_tweaks.capture.secrets import scan_text
from ubuntu_tweaks.profile import UNIT_SUFFIXES, USER_UNIT_DIR, Profile, unit_template

_DESCRIPTION = re.compile(r"^Description=(.*)$", re.MULTILINE)
_READ_LIMIT = 65536
_REASON_LIMIT = 80


@dataclass(frozen=True)
class UnitItem:
    name: str
    mark: Mark
    reason: str


def _description(source: Path) -> str:
    try:
        with source.open("rb") as handle:
            text = handle.read(_READ_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return ""
    match = _DESCRIPTION.search(text)
    reason = " ".join(match.group(1).split()) if match else ""
    if scan_text(reason):  # a token in a description never becomes a comment of units.toml
        return ""
    return reason if len(reason) <= _REASON_LIMIT else reason[: _REASON_LIMIT - 1] + "…"


def _has_secret(path: Path) -> bool:
    try:
        return bool(scan_text(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def _tainted(source: Path) -> bool:
    """A secret in the unit file or in one of its drop-ins: the files section skips that file, and a unit without its
    file (or without the environment of its drop-in) is no use on another machine. Checked here too, so
    ``--only units`` does not propose it either."""
    return any(_has_secret(path) for path in (source, *sorted(source.parent.glob(f"{source.name}.d/*.conf"))))


def scan_units(home: Path, profile: Profile, not_adopted: Collection[str] = ()) -> list[UnitItem]:
    """``not_adopted``: ``$HOME``-relative unit files the files section skipped (a secret in them): a unit whose file
    the profile will not get is not proposed, ``apply`` could not enable it."""
    declared = profile.load_units()  # validates both layers before anything is proposed
    directory = home / USER_UNIT_DIR
    found: dict[str, str] = {}
    for suffix in (".wants", ".requires"):
        for group in sorted(directory.glob(f"*{suffix}")):
            if not group.is_dir():
                continue
            for entry in sorted(group.iterdir()):
                name = entry.name
                if name in found or name in declared.disable or not name.endswith(UNIT_SUFFIXES):
                    continue
                source = directory / name
                if not os.path.lexists(source):
                    source = directory / unit_template(name)
                if (
                    os.path.lexists(source)
                    and source.relative_to(home).as_posix() not in not_adopted
                    and not _tainted(source)
                ):
                    found[name] = _description(source)
    return [UnitItem(name, "=" if name in declared.enable else "+", reason) for name, reason in sorted(found.items())]

"""units adapter: the systemd user units and timers that run on their own (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``.

Assumptions:

- Items are ``unit:<name>``: a unit (or a ``<unit>.d`` drop-in directory) in ``~/.config/systemd/user`` and every unit
  ``systemctl --user list-unit-files --state=enabled`` reports. ``detail`` says which of the two holds (``enabled``,
  ``user unit file``). Without a user systemd (``systemctl`` missing, no session bus) only the directory is read.
- ``owned``: units whose file or drop-in the profile's ``files/`` declares (``.config/systemd/user/...``) and units
  that are symlinks into the profile, the dotfiles checkout or the engine checkout.
- ``*.wants`` / ``*.requires`` directories are enablement symlinks, not units: never items.

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from ubuntu_tweaks.adapters._links import link_owner
from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item

NAME = "units"
USER_DIR = PurePosixPath(".config/systemd/user")
_SUFFIXES = (".service", ".timer", ".socket", ".path", ".target", ".mount", ".slice")


def _unit_name(name: str) -> str | None:
    """``foo.service`` for ``foo.service`` and ``foo.service.d``; ``None`` for anything that is not a unit."""
    base = name.removesuffix(".d")
    return base if base.endswith(_SUFFIXES) else None


def _enabled() -> list[str]:
    result = system().process.query(
        ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"], timeout=30.0
    )
    if not result.ok:
        return []
    return [words[0] for line in result.stdout.splitlines() if (words := line.split())]


def _user_files() -> list[str]:
    try:
        names = sorted(os.listdir(system().home.joinpath(*USER_DIR.parts)))
    except OSError:
        return []
    return [unit for name in names if (unit := _unit_name(name))]


def discover(ctx: Context) -> Iterable[Item]:
    details: dict[str, list[str]] = {}
    for name in _enabled():
        details.setdefault(name, []).append("enabled")
    for name in _user_files():
        if "user unit file" not in details.setdefault(name, []):
            details[name].append("user unit file")
    for name in sorted(details):
        yield Item(NAME, f"unit:{name}", "unit", ", ".join(details[name]))


def owned(ctx: Context) -> Mapping[str, str]:
    profile = ctx.profile
    found: dict[str, str] = {}
    depth = len(USER_DIR.parts)
    for entry in profile.files():
        parts = entry.rel.parts
        if parts[:depth] == USER_DIR.parts and len(parts) > depth and (unit := _unit_name(parts[depth])):
            found.setdefault(f"unit:{unit}", entry.source.relative_to(profile.root).as_posix())
    for name in _user_files():
        label = link_owner(ctx, system().home.joinpath(*USER_DIR.parts) / name)
        if label is not None:
            found.setdefault(f"unit:{name}", label)
    return found

"""etc adapter: the machine-wide configuration that can be read without root (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``.

Assumptions:

- Items are ``etc:/etc/...``: conffiles of installed packages whose content no longer matches the md5 dpkg recorded
  (``dpkg-query -W -f='${Conffiles}'``, each file only hashed; ``dpkg --verify`` says the same but checks every file
  of every package and takes about half a minute), and the local entries of ``/etc/default``, ``/etc/systemd/system`` (``*.service``,
  ``*.timer``, ``*.socket``, ``*.d``), ``/etc/polkit-1/rules.d``, ``/etc/sudoers.d`` (names only, a file is never
  read) and ``/etc/apt/sources.list.d``. A file that is an unmodified package conffile is stock, not configuration, so
  it is skipped (a modified one is listed as a conffile); so is a systemd unit that only symlinks into the
  distribution's unit directories.
- A directory the user cannot list (``/etc/sudoers.d`` is root-only on Ubuntu) yields one item for the directory whose
  ``detail`` is ``needs root to read``: the area is reported, not silently skipped. A missing directory or tool yields
  nothing. Everything is read through ``Process.query`` (``dpkg-query``) and the filesystem under
  ``System.root``; nothing is written.
- ``owned``: ``/etc/default/locale`` and ``/etc/default/keyboard`` when the profile's ``system.toml`` sets a locale or
  a keyboard. Other system modules claim what they own with ``[[coverage]]`` ``class = "tracked"`` rules.

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path

from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item

NAME = "etc"
_MD5 = re.compile(r"[0-9a-f]{32}")
_UNREADABLE = "needs root to read"
_DISTRIBUTION_UNITS = ("/usr/lib/systemd", "/lib/systemd")

type Areas = tuple[tuple[str, tuple[str, ...]], ...]
_AREAS: Areas = (
    ("/etc/default", ()),
    ("/etc/systemd/system", (".service", ".timer", ".socket", ".d")),
    ("/etc/polkit-1/rules.d", ()),
    ("/etc/sudoers.d", ()),
    ("/etc/apt/sources.list.d", ()),
)


def _conffile_sums() -> dict[str, str]:
    """``path -> md5`` of the conffiles dpkg knows under ``/etc`` (obsolete ones and ones with no recorded sum left out)."""
    result = system().process.query(["dpkg-query", "-W", "-f=${Conffiles}\\n"], timeout=60.0)
    if not result.ok:
        return {}
    sums: dict[str, str] = {}
    for line in result.stdout.splitlines():
        words = line.split()
        if len(words) == 2 and words[0].startswith("/etc/") and _MD5.fullmatch(words[1]):
            sums[words[0]] = words[1]
    return sums


def _md5(path: Path) -> str | None:
    try:
        return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    except OSError:
        return None


def _modified_conffiles(sums: Mapping[str, str]) -> list[Item]:
    """Conffiles whose content no longer matches the package, read from ``System.root`` and only hashed. Computed
    here and not with ``dpkg --verify``, which checks every file of every package (about half a minute)."""
    root = system().root
    items: list[Item] = []
    unreadable = 0
    for path, expected in sums.items():
        location = root / path.lstrip("/")
        if not os.path.lexists(location):
            items.append(Item(NAME, f"etc:{path}", "conffile", "conffile deleted: differs from the package"))
            continue
        actual = _md5(location)
        if actual is None:
            unreadable += 1
        elif actual != expected:
            items.append(Item(NAME, f"etc:{path}", "conffile", "conffile modified: differs from the package"))
    if unreadable:
        items.append(Item(NAME, "etc:/etc/<conffiles>", "dir", f"{unreadable} conffile(s): {_UNREADABLE}"))
    return items


def _stock_unit_link(path: Path) -> bool:
    return path.is_symlink() and os.readlink(path).startswith(_DISTRIBUTION_UNITS)


def _listed(directory: str, suffixes: tuple[str, ...], conffiles: Collection[str]) -> Iterable[Item]:
    location = system().root / directory.lstrip("/")
    try:
        names = sorted(os.listdir(location))
    except FileNotFoundError:
        return
    except OSError:
        yield Item(NAME, f"etc:{directory}", "dir", _UNREADABLE)
        return
    for name in names:
        path = location / name
        identifier = f"{directory}/{name}"
        if identifier in conffiles or (suffixes and not name.endswith(suffixes)) or _stock_unit_link(path):
            continue
        is_directory = path.is_dir() and not path.is_symlink()
        yield Item(
            NAME,
            f"etc:{identifier}",
            "dir" if is_directory else "file",
            "local directory" if is_directory else "local file",
        )


def discover(ctx: Context) -> Iterable[Item]:
    sums = _conffile_sums()
    found = {item.id: item for item in _modified_conffiles(sums)}
    for directory, suffixes in _AREAS:
        for item in _listed(directory, suffixes, sums):
            found.setdefault(item.id, item)
    return [found[key] for key in sorted(found)]


def owned(ctx: Context) -> Mapping[str, str]:
    system_config = ctx.profile.load_system()
    found: dict[str, str] = {}
    if system_config.locale:
        found["etc:/etc/default/locale"] = "system.toml"
    if any(
        (
            system_config.keyboard.model,
            system_config.keyboard.layout,
            system_config.keyboard.variant,
            system_config.keyboard.options,
        )
    ):
        found["etc:/etc/default/keyboard"] = "system.toml"
    return found

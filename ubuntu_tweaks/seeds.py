"""Seed providers: values computed on the machine that a module's ``[[seeds]]`` write once.

Accessed via: ``planners/seeds.py`` looks a provider up by the manifest's ``provider`` name.

Assumptions: a provider is pure (it reads the environment and ``user-dirs.dirs``, creates
nothing); anything it wants created is returned in ``SeedValue.ensure_dirs`` and made by the
action's ``run()``. Its text is GVariant text for the key it seeds. A seed is written only while
the key has no user value and is never drift-checked, so a provider may return something
machine- or language-specific.

Public API:

- ``SeedContext(home, env, localize)``: ``localize(domain, message)`` translates a message the way
  ``gettext -d <domain>`` does (default ``gettext.dgettext``).
- ``SeedValue(text, ensure_dirs=())``.
- ``PROVIDERS``: provider name -> ``Provider``; ``get_provider(name)`` raises ``ConfigError``
  listing the known names.
- ``screenshots_stack`` (``"screenshots-stack"``): the dock's ``folder-stacks`` value for GNOME's
  Screenshots folder (``<Pictures>/<localized "Screenshots">``).
- ``pictures_dir(home, env)``: ``XDG_PICTURES_DIR`` from ``user-dirs.dirs``, else ``~/Pictures``.
"""

from __future__ import annotations

import gettext
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

from ubuntu_tweaks.errors import ConfigError  # noqa: E402


@dataclass(frozen=True)
class SeedContext:
    home: Path
    env: Mapping[str, str] = field(default_factory=dict)
    localize: Callable[[str, str], str] = gettext.dgettext


@dataclass(frozen=True)
class SeedValue:
    text: str
    ensure_dirs: tuple[Path, ...] = ()


type Provider = Callable[[SeedContext], SeedValue]

_USER_DIR = re.compile(r'^\s*XDG_PICTURES_DIR\s*=\s*"?([^"\n]*)"?\s*$', re.MULTILINE)


def pictures_dir(home: Path, env: Mapping[str, str]) -> Path:
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        match = _USER_DIR.search((config / "user-dirs.dirs").read_text(encoding="utf-8"))
    except OSError:
        match = None
    if match and match.group(1).strip():
        return Path(match.group(1).replace("$HOME", str(home)))
    return home / "Pictures"


def screenshots_stack(context: SeedContext) -> SeedValue:
    folder = pictures_dir(context.home, context.env) / context.localize("gnome-shell", "Screenshots")
    stacks = [
        {"path": str(folder), "icon": "folder-pictures", "view": "automatic", "sort": "date-added", "display": "stack"}
    ]
    return SeedValue(GLib.Variant("s", json.dumps(stacks)).print_(True), (folder,))


PROVIDERS: dict[str, Provider] = {"screenshots-stack": screenshots_stack}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ConfigError(f"unknown seed provider {name!r}; known: {', '.join(sorted(PROVIDERS))}") from None

"""Where the git identity of this person can be read from when the profile does not say (spec, *git and gh*).

Accessed via: ``steps.py`` (the ``git-identity`` step plans what is found) and ``planners/base.warnings`` (warns only
when nothing is found). Nothing here is invented or hardcoded: every value is read from the person's own data.

Per key (``name``, ``email``) the sources are tried in this order, the first that knows the key wins:

1. what ``git config --global`` already holds (``current``: left as it is, nothing to plan);
2. ``user.name`` / ``user.email`` of ``<dotfiles checkout>/.gitconfig`` (read with ``git config -f``; the file is
   not linked, only read);
3. ``gh api user`` when ``gh`` is logged in: the name (else the login) and the public e-mail (else the GitHub
   noreply address ``<id>+<login>@users.noreply.github.com``).

Public API: ``KEYS``, ``Known(value, source)``, ``current(process, key)``, ``discover(process, dotfiles, wanted)``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.process import Process

KEYS = ("name", "email")
GH_TIMEOUT_SECONDS = 20.0
GITCONFIG = ".gitconfig"


@dataclass(frozen=True)
class Known:
    value: str
    source: str  # shown in the plan: ``from ~/.files/.gitconfig``


def current(process: Process, key: str) -> str | None:
    """The global ``user.<key>`` as git has it (``None`` when unset or git cannot run)."""
    result = process.query(["git", "config", "--global", "--get", f"user.{key}"])
    return result.stdout.strip() or None if result.ok else None


def _from_gitconfig(process: Process, dotfiles: Path, wanted: Iterable[str]) -> dict[str, Known]:
    path = dotfiles / GITCONFIG
    if not path.is_file():
        return {}
    found: dict[str, Known] = {}
    for key in wanted:
        result = process.query(["git", "config", "-f", str(path), "--get", f"user.{key}"])
        if result.ok and result.stdout.strip():
            found[key] = Known(result.stdout.strip(), f"from {_shown(path)}")
    return found


def _shown(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _from_gh(process: Process) -> dict[str, Known]:
    result = process.query(["gh", "api", "user"], timeout=GH_TIMEOUT_SECONDS)
    if not result.ok:
        return {}
    try:
        user = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(user, dict):
        return {}
    login, ident = user.get("login"), user.get("id")
    if not isinstance(login, str) or not login:
        return {}
    name = user.get("name")
    email = user.get("email")
    noreply = f"{ident}+{login}@users.noreply.github.com" if isinstance(ident, int) else None
    found: dict[str, Known] = {}
    found["name"] = Known(name if isinstance(name, str) and name.strip() else login, "from gh api user")
    if isinstance(email, str) and email.strip():
        found["email"] = Known(email, "from gh api user")
    elif noreply is not None:
        found["email"] = Known(noreply, "from gh api user (GitHub noreply address)")
    return found


def discover(process: Process, dotfiles: Path | None, wanted: Iterable[str]) -> dict[str, Known]:
    """``key -> Known`` for the wanted keys that the dotfiles ``.gitconfig`` or ``gh`` can tell; ``gh`` is only
    asked for what the first source did not know."""
    keys = tuple(wanted)
    found = _from_gitconfig(process, dotfiles, keys) if dotfiles is not None else {}
    if any(key not in found for key in keys):
        for key, known in _from_gh(process).items():
            if key in keys:
                found.setdefault(key, known)
    return found

"""The user-level package managers of ``packages.toml``: ``uv``, ``npm``, ``bun``, ``cargo`` and ``pipx``.

Accessed via: ``profile.py`` (validation of the lists), ``planners/packages.py`` (install and installed-check) and
``adapters/packages.py`` (coverage listing, ownership and capture), so one definition of "what an entry means"
serves all three.

Assumptions:

- An entry is the argument the manager's install command takes: a PEP 508 requirement for ``uv`` / ``pipx``
  (``glances[web]``, ``esphome==2026.1``, ``name @ git+https://host/repo.git``), ``name`` / ``@scope/name`` with an
  optional ``@version`` for ``npm`` / ``bun``, a crate name with an optional ``@version`` for ``cargo``.
- Entries reach ``Process.execute`` as one argv element, never through a shell; they are still checked against a
  strict grammar, so a stray ``;``, ``$(...)``, space or leading ``-`` (an option) is a ``ConfigError`` at load time.
- The NAME of an entry is what the installed-check and coverage ownership compare: the normalized distribution name
  (PEP 503: lower case, runs of ``-_.`` become ``-``) for ``uv`` / ``pipx``, the package name for the others.
- Managers are never installed here: a missing binary is reported (``is_missing``, ``INSTALLED_BY``), not fixed.
- A listing is parsed from stdout whatever the exit status (``npm ls`` exits 1 on warnings) unless the binary
  is missing (127), not executable (126) or timed out (124).

Public API: ``TOOL_MANAGERS``, ``FALLBACK``, ``INSTALLED_BY``, ``parse_spec(manager, spec) -> name``,
``normalize(name)``, ``parse_listing(manager, text)``, ``list_argv(manager, home)``, ``install_argv(manager, spec, home)``,
``installed_names(process, manager, home)``, ``is_missing(process, manager)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ubuntu_tweaks.errors import ConfigError

if TYPE_CHECKING:
    from ubuntu_tweaks.process import Process

TOOL_MANAGERS = ("uv", "npm", "bun", "cargo", "pipx")
FALLBACK: Mapping[str, str] = {"uv": "pipx", "npm": "bun"}
INSTALLED_BY: Mapping[str, str] = {
    "uv": "the brew module (its `tools` post runs `brew install uv`) or a profile base tool",
    "pipx": "apt (`apt install pipx`), e.g. in packages.toml",
    "npm": "Node.js (apt `nodejs`/`npm`, or a profile module)",
    "bun": "the brew module (its `tools` post runs `brew install oven-sh/bun/bun`)",
    "cargo": "Rust (apt `cargo`, or rustup from a profile module)",
}
_TIMEOUT = 30.0
_NO_BINARY = (126, 127)  # not executable, not found
_NO_ANSWER = (124, *_NO_BINARY)  # ... or timed out

_DIST = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_EXTRAS = r"(?:\[[A-Za-z0-9._,-]*\])?"
_CLAUSE = r"(?:===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9._*+!-]+"
_PEP508 = re.compile(rf"(?P<name>{_DIST}){_EXTRAS}(?:{_CLAUSE}(?:,{_CLAUSE})*)?")
_PEP508_URL = re.compile(rf"(?P<name>{_DIST}){_EXTRAS} @ [A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9._~:/@%+=?#-]+")
_NODE = re.compile(r"(?P<name>(?:@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*)(?:@[A-Za-z0-9._~^*+=-]+)?")
_CRATE = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)(?:@[A-Za-z0-9._~^*+=-]+)?")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_spec(manager: str, spec: str) -> str:
    """The NAME of a ``packages.toml`` entry; ``ConfigError`` when it is not a valid spec for ``manager``."""
    if manager in ("uv", "pipx"):
        match = _PEP508.fullmatch(spec) or _PEP508_URL.fullmatch(spec)
        name = normalize(match.group("name")) if match else None
    elif manager in ("npm", "bun"):
        match = _NODE.fullmatch(spec)
        name = match.group("name") if match else None
    elif manager == "cargo":
        match = _CRATE.fullmatch(spec)
        name = match.group("name") if match else None
    else:
        raise ConfigError(f"{manager} is not a user-level package manager ({', '.join(TOOL_MANAGERS)})")
    if name is None:
        raise ConfigError(
            f"{manager}: {spec!r} is not a valid package spec (a name with an optional version"
            + (" or `name @ url`" if manager in ("uv", "pipx") else "")
            + "; no spaces, shell characters or leading '-')"
        )
    return name


def _uv(text: str) -> list[str]:
    return [
        normalize(line.split()[0]) for line in text.splitlines() if line and not line[0].isspace() and line[0] != "-"
    ]


def _npm(text: str) -> list[str]:
    marker = "node_modules/"
    return [line.strip().partition(marker)[2] for line in text.splitlines() if marker in line]


_BUN = re.compile(r"^\s*[├└]──\s+(@[^@\s]+/[^@\s]+|[^@\s]+)@\S+\s*$")
_CARGO = re.compile(r"^(\S+) v\S+.*:\s*$")


def _bun(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := _BUN.match(line))]


_CARGO_SOURCE = re.compile(r"^(\S+) v\S+ \(.+\):\s*$")


def cargo_off_registry(text: str) -> set[str]:
    """Crates of ``cargo install --list`` installed from a git URL or a path (the row carries the source in
    parentheses): ``cargo install <name>`` would fetch another crate of that name from the registry."""
    return {match.group(1) for line in text.splitlines() if (match := _CARGO_SOURCE.match(line))}


def _cargo(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := _CARGO.match(line))]


def _pipx(text: str) -> list[str]:
    return [normalize(words[0]) for line in text.splitlines() if (words := line.split())]


_PARSERS: Mapping[str, Callable[[str], list[str]]] = {
    "uv": _uv,
    "npm": _npm,
    "bun": _bun,
    "cargo": _cargo,
    "pipx": _pipx,
}


def parse_listing(manager: str, text: str) -> list[str]:
    """The installed names (already in the form ``parse_spec`` gives) out of the manager's listing."""
    return _PARSERS[manager](text)


def list_argv(manager: str, home: Path) -> list[str]:
    match manager:
        case "uv":
            return ["uv", "tool", "list"]
        case "npm":
            return ["npm", "ls", "-g", "--depth=0", "--parseable", "--prefix", str(home / ".local")]
        case "bun":
            return ["bun", "pm", "ls", "-g"]
        case "cargo":
            return ["cargo", "install", "--list"]
        case "pipx":
            return ["pipx", "list", "--short"]
        case _:
            raise ValueError(f"not a user-level package manager: {manager}")


def install_argv(manager: str, spec: str, home: Path) -> list[str]:
    match manager:
        case "uv":
            return ["uv", "tool", "install", spec]
        case "npm":
            return ["npm", "install", "-g", "--prefix", str(home / ".local"), spec]
        case "bun":
            return ["bun", "add", "-g", spec]
        case "cargo":
            return ["cargo", "install", spec]
        case "pipx":
            return ["pipx", "install", spec]
        case _:
            raise ValueError(f"not a user-level package manager: {manager}")


def installed_names(process: Process, manager: str, home: Path) -> set[str]:
    result = process.query(list_argv(manager, home), timeout=_TIMEOUT)
    if result.returncode in _NO_ANSWER:
        return set()
    return set(parse_listing(manager, result.stdout))


def is_missing(process: Process, manager: str) -> bool:
    return process.query([manager, "--version"], timeout=_TIMEOUT).returncode in _NO_BINARY

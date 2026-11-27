"""The one place that knows how the engine spells an apt install, and what to say about a conffile dpkg kept.

Accessed via: ``planners/base.py`` and ``planners/packages.py`` (the ``apt`` package actions), ``commands/doctor.py``
(``doctor --yes``) and ``module_script.apt_get`` (root module scripts). Every apt install is built here, so no
caller can forget a flag.

Why: on 26 Nov 2026 ``apt-get install -y zsh ...`` stopped in dpkg's conffile prompt for ``/etc/zsh/zshrc``
(``*** zshrc (Y/I/N/O/D/Z) [default=N] ?``) and waited for ever: ``-y`` does not answer dpkg. An apt step is
non-interactive by default: nothing it runs may ask a question.

Assumptions: ``sudo`` scrubs the environment, so the variables are carried by ``env`` *inside* the elevated command
(``sudo -- env DEBIAN_FRONTEND=noninteractive apt-get ...``), not set on our side. ``--force-confdef`` answers every
conffile question that has a default and ``--force-confold`` answers the rest by keeping the file on disk: a local edit
is never replaced silently, the package's version lands beside it as ``<file>.dpkg-dist``. The prompt is never
answered ``--force-confnew``.

Public API:

- ``APT_ENVIRONMENT``: what apt, dpkg, needrestart and apt-listchanges may not ask for or wait for.
- ``DPKG_OPTIONS``: the two ``-o Dpkg::Options::=...`` pairs.
- ``install_command(packages) -> list[str]``: ``env <VAR=...> apt-get install -y <DPKG_OPTIONS> <packages>``, to be
  elevated by the caller. ``noninteractive_arguments(arguments)`` adds the dpkg options to an ``apt-get`` argument
  list that installs or upgrades (other subcommands come back unchanged).
- ``kept_conffiles(since, log) -> list[str]``: the conffiles dpkg kept since ``since`` (best effort: dpkg's own log).
- ``conffile_note(path) -> str``: the one line the run prints for a kept conffile.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

APT_ENVIRONMENT: Mapping[str, str] = {
    "DEBIAN_FRONTEND": "noninteractive",
    "NEEDRESTART_MODE": "a",
    "NEEDRESTART_SUSPEND": "1",
    "APT_LISTCHANGES_FRONTEND": "none",
}
DPKG_OPTIONS: tuple[str, ...] = (
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
)
DPKG_LOG = Path("/var/log/dpkg.log")
_INSTALLING = frozenset({"install", "upgrade", "dist-upgrade", "full-upgrade", "reinstall", "build-dep", "satisfy"})
_VALUE_OPTIONS = frozenset(
    {"-o", "--option", "-c", "--config-file", "-t", "--target-release", "-a", "--host-architecture"}
)
_LOG_TIME = "%Y-%m-%d %H:%M:%S"
_CONFFILE_KEPT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d conffile (\S+) keep$")


def install_command(packages: Sequence[str]) -> list[str]:
    """The command (not yet elevated) that installs ``packages`` without a single question."""
    return [
        "env",
        *(f"{name}={value}" for name, value in APT_ENVIRONMENT.items()),
        "apt-get",
        "install",
        "-y",
        *DPKG_OPTIONS,
        *packages,
    ]


def noninteractive_arguments(arguments: Sequence[str]) -> list[str]:
    """``arguments`` of an ``apt-get`` call, with the dpkg options placed right after the subcommand when it installs
    or upgrades (``apt-get update``, ``download``, ``indextargets`` run no dpkg and are returned as they are)."""
    words = list(arguments)
    position = 0
    while position < len(words) and words[position].startswith("-"):  # options before the subcommand
        if words[position] == "--":  # nothing after it is an option: do not put ours among the operands
            return words
        position += 2 if words[position] in _VALUE_OPTIONS else 1
    if position >= len(words) or words[position] not in _INSTALLING:
        return words
    return [*words[: position + 1], *DPKG_OPTIONS, *words[position + 1 :]]


def kept_conffiles(since: datetime, log: Path = DPKG_LOG) -> list[str]:
    """Paths of the conffiles dpkg kept (``conffile <path> keep`` in its log) at or after ``since``, once each, in
    the order dpkg met them. An unreadable or missing log is no information: nothing is reported."""
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    earliest = since.replace(microsecond=0)  # the log is stamped in whole seconds
    kept: list[str] = []
    for line in reversed(lines):  # the log only grows: stop at the first line older than the run
        if _older(line, earliest):
            break
        found = _CONFFILE_KEPT.match(line)
        if found is not None and found.group(1) not in kept:
            kept.append(found.group(1))
    kept.reverse()
    return kept


def _older(line: str, earliest: datetime) -> bool:
    try:
        return datetime.strptime(line[:19], _LOG_TIME) < earliest
    except ValueError:  # a continuation line without a stamp
        return False


def conffile_note(path: str) -> str:
    """The line printed for a conffile dpkg kept: what happened to the file and where the package's own copy is."""
    shipped = Path(f"{path}.dpkg-dist")
    try:
        where = f"; the package's version is {shipped}" if shipped.exists() else ""
    except OSError:  # an unreadable directory: the note matters more than where the other copy is
        where = ""
    return f"apt: kept your {path} (dpkg conffile conflict, answered keep-old{where})"

"""A ``Runner`` that runs read-only probe commands on another machine over ``ssh``.

Accessed via: ``commands/show.py --ssh HOST``. Nothing is installed or copied to the remote machine: the
command line of each probe (``dconf dump /``, ``cat``, ``timedatectl show``, ...) is sent as one ssh
command and its stdout comes back. Authentication is whatever ``ssh`` does non-interactively
(``BatchMode=yes``: keys and agent only, never a password prompt).

Public API: ``ssh_runner(target, options=()) -> Runner``; ``target_host(target)`` (the host part, used as
the report label); ``validate_target``. ``options`` are ``-o KEY=VALUE`` pairs given as ``KEY=VALUE``.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence

from ubuntu_tweaks.capture.system_scan import Runner
from ubuntu_tweaks.errors import ConfigError

_TIMEOUT = 60


def validate_target(target: str) -> str:
    if not target or target.startswith("-") or any(char.isspace() for char in target):
        raise ConfigError(f"invalid --ssh target {target!r}: expected [user@]host")
    return target


def target_host(target: str) -> str:
    return validate_target(target).rsplit("@", 1)[-1]


def ssh_runner(target: str, options: Sequence[str] = ()) -> Runner:
    validate_target(target)
    for option in options:
        if "=" not in option or option.startswith("-"):
            raise ConfigError(f"invalid --ssh-option {option!r}: expected KEY=VALUE")
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    for option in options:
        base += ["-o", option]

    def run(argv: Sequence[str]) -> str | None:
        try:
            done = subprocess.run(
                [*base, target, shlex.join(argv)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout if done.returncode == 0 else None

    return run

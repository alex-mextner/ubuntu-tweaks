"""The graphical-session variables a ``finish`` step needs to open a window, taken from the user's systemd manager.

Accessed via: ``commands/finish.py`` (``run`` puts the result into its own environment, which every step command and the
child ``apply`` inherit).

Why: a shell that was started before the graphical login (a tmux server started at boot, an SSH session, a cron job)
has no ``DISPLAY`` / ``WAYLAND_DISPLAY``, so a step that opens a browser or a login window fails there, often without
a word (``xdg-open`` with its output discarded, a script that exits "no graphical session"). The user's systemd
manager knows the session: ``systemctl --user show-environment`` lists them.

Assumptions: systemd writes a value with a space or a special character as ``$'...'`` (C escapes); it is decoded.
Nothing is overridden: a variable the caller already has stays as it is, and when the caller already has
a display nothing is read at all. When ``systemctl`` is missing, fails, or its answer holds no display, the
environment is returned unchanged (never an exception: a headless machine is a valid place to run ``finish``).

Public API: ``GRAPHICAL_VARIABLES``, ``with_graphical_session(environ, process) -> dict[str, str]``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from ubuntu_tweaks.process import Process

DISPLAY_VARIABLES = ("WAYLAND_DISPLAY", "DISPLAY")
GRAPHICAL_VARIABLES = (*DISPLAY_VARIABLES, "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR")  # wayland-0 lives in it
SHOW_ENVIRONMENT = ("systemctl", "--user", "show-environment")
SYSTEMCTL_TIMEOUT_SECONDS = 10.0


_ESCAPE = re.compile(rb"\\(x[0-9a-fA-F]{2}|[0-7]{1,3}|.)", re.DOTALL)
_SIMPLE_ESCAPES = {b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t", b"v": b"\v"}


def _unescape(match: re.Match[bytes]) -> bytes:
    code = match.group(1)
    if code[:1] == b"x":
        return bytes([int(code[1:], 16)])
    if code[:1].isdigit():
        return bytes([int(code, 8) & 0xFF])
    return _SIMPLE_ESCAPES.get(code, code)  # \\ \' \" and any other escaped character stand for themselves


def _unquote(value: str) -> str:
    """What systemd wrote as a shell word: a value with a space or a special character comes as ``$'...'`` with C
    escapes (a path like ``/home/me/My Session/Xauthority``); anything else is taken as it is."""
    if len(value) < 3 or not (value.startswith("$'") and value.endswith("'")):
        return value
    return os.fsdecode(_ESCAPE.sub(_unescape, os.fsencode(value[2:-1])))


def _manager_environment(process: Process) -> dict[str, str]:
    """``NAME=value`` lines of ``systemctl --user show-environment`` (a value that systemd quotes is decoded)."""
    result = process.query(SHOW_ENVIRONMENT, timeout=SYSTEMCTL_TIMEOUT_SECONDS)
    if not result.ok:
        return {}
    found: dict[str, str] = {}
    for line in result.stdout.split("\n"):  # not splitlines(): U+2028 and the like are valid inside a path
        name, separator, value = line.partition("=")
        if separator and value:
            found[name] = _unquote(value)
    return found


def with_graphical_session(environ: Mapping[str, str], process: Process) -> dict[str, str]:
    """``environ`` plus the graphical-session variables of the user's systemd manager, when ``environ`` has no display
    of its own and the manager has one. A variable already in ``environ`` is never replaced."""
    result = dict(environ)
    if any(result.get(name) for name in DISPLAY_VARIABLES):
        return result
    manager = _manager_environment(process)
    if not any(name in manager for name in DISPLAY_VARIABLES):
        return result
    for name in GRAPHICAL_VARIABLES:
        if name in manager and not result.get(name):
            result[name] = manager[name]
    return result

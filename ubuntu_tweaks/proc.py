"""Run external commands (git, apt, glib-compile-schemas) and turn their failures into errors.

Accessed via: the submodule/schemas planners and the ``init`` / ``update`` / ``doctor`` commands.

Public API:

- ``CommandError`` (exit 1): the command is missing, timed out, or exited non-zero; the message
  carries the command line and the tail of its stderr.
- ``run(argv, *, cwd=None, capture=False, timeout=None) -> str``: runs ``argv`` (never through a
  shell) and returns its stdout when ``capture`` is true, else ``""``. Output of a non-captured
  command goes straight to the terminal, which is what ``apt``/``git`` progress needs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.errors import UbuntuTweakError


class CommandError(UbuntuTweakError):
    """An external command could not run or failed."""


def run(argv: Sequence[str], *, cwd: Path | None = None, capture: bool = False, timeout: float | None = None) -> str:
    line = " ".join(argv)
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise CommandError(f"{argv[0]}: command not found (while running: {line})") from error
    except subprocess.TimeoutExpired as error:
        raise CommandError(f"`{line}` timed out after {timeout} s") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise CommandError(f"`{line}` failed with exit code {error.returncode}{suffix}") from error
    return result.stdout or ""

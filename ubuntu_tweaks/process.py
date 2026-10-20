"""Running external commands on behalf of planners: the one seam tests replace.

Accessed via: ``planners/system_root.py``, ``planners/packages.py`` and ``planners/migrations.py``
take a ``Process`` (default: the real one) so their checks and installs are testable without
touching the machine.

Assumptions: ``query`` is for read-only checks (captured output, a timeout, never raises for a
missing binary); ``execute`` is for mutations (inherits the terminal so ``sudo`` can ask for a
password and ``apt`` can show progress) and raises ``CommandError`` on a non-zero exit.

Public API:

- ``CommandResult(returncode, stdout, stderr)`` with ``ok``.
- ``CommandError`` (exit 1).
- ``Process``: ``query(argv, *, timeout=60.0, cwd=None) -> CommandResult`` (127 when the binary is
  missing, 124 on timeout; stdin closed), ``execute(argv, *, input_text=None, cwd=None)`` and
  ``elevate(argv) -> list[str]`` (prefix with ``sudo --`` or ``pkexec``; unchanged when already
  root; ``CommandError`` when neither exists).
- Module-level ``elevate(argv)`` (the same logic, for callers without a ``Process``) and
  ``run(argv, *, cwd=None, capture=False, timeout=None) -> str`` (raises ``CommandError``; the
  helper behind ``git``/``apt``/``glib-compile-schemas`` calls in the commands and submodule planners).
- ``FakeProcess(responses, effects)``: records ``queries`` and ``executed``; ``responses`` maps an
  argv tuple to a ``CommandResult`` (anything else answers 127); ``effects`` is called with
  ``(argv, input_text)`` on every ``execute`` so a test can change the fake machine state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.errors import UbuntuTweakError


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(UbuntuTweakError):
    """An external command failed or a required helper (sudo, pkexec) is missing."""


def elevate(argv: Sequence[str]) -> list[str]:
    """Prefix ``argv`` so it runs as root: unchanged when already root, else ``sudo --`` or ``pkexec``."""
    if os.geteuid() == 0:
        return list(argv)
    if shutil.which("sudo"):
        return ["sudo", "--", *argv]
    if shutil.which("pkexec"):
        return ["pkexec", *argv]
    raise CommandError("root is required but neither sudo nor pkexec is installed")


def run(argv: Sequence[str], *, cwd: Path | None = None, capture: bool = False, timeout: float | None = None) -> str:
    """Run ``argv`` (never through a shell); return its stdout when ``capture`` else ``""``.

    Failures (missing binary, timeout, non-zero exit) raise ``CommandError`` carrying the command
    line and the last stderr line. Output of a non-captured command goes to the terminal.
    """
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


class Process:
    def query(self, argv: Sequence[str], *, timeout: float = 60.0, cwd: Path | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{argv[0]}: timed out after {timeout:g} s")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        try:
            completed = subprocess.run(list(argv), cwd=cwd, input=input_text, text=True, check=False)
        except FileNotFoundError as error:
            raise CommandError(f"{argv[0]}: command not found") from error
        if completed.returncode != 0:
            raise CommandError(f"{' '.join(argv)}: exited with status {completed.returncode}")

    def elevate(self, argv: Sequence[str]) -> list[str]:
        return elevate(argv)


class FakeProcess(Process):
    def __init__(
        self,
        responses: Mapping[tuple[str, ...], CommandResult] | None = None,
        effects: Callable[[tuple[str, ...], str | None], None] | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._effects = effects
        self.queries: list[tuple[str, ...]] = []
        self.executed: list[tuple[tuple[str, ...], str | None]] = []

    def query(self, argv: Sequence[str], *, timeout: float = 60.0, cwd: Path | None = None) -> CommandResult:
        key = tuple(argv)
        self.queries.append(key)
        return self._responses.get(key, CommandResult(127, "", f"{argv[0]}: command not found"))

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        key = tuple(argv)
        self.executed.append((key, input_text))
        if self._effects is not None:
            self._effects(key, input_text)

    def elevate(self, argv: Sequence[str]) -> list[str]:
        return ["sudo", "--", *argv]

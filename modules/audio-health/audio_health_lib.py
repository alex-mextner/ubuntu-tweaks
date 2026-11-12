"""Shared code of the ``audio-health`` module: the check, its service and its timer, installed for the desktop user.

Accessed via: ``modules/audio-health/audio-health`` (check / status / plan / install and the retirement's
retire-check / retire-plan / retire, as the desktop user).
Spec: docs/specs/ubuntu-tweak-cli.md, *Audio health*.

Everything lives under ``$HOME``: ``~/.local/bin/audio-health-check`` (the module's script, byte for byte),
``~/.config/systemd/user/audio-health.service`` and ``audio-health.timer``, and the timer enabled in the user manager.
The module is off by default; while it is disabled the retirement declared in ``module.toml`` leaves none of it: the
timer disabled and stopped, the units and the script removed (a machine that never had it changes nothing).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.module_script import (
    Finding,
    Module,
    ModuleError,
    NeedsManual,
    read_text_or_none,
    unified_diff_lines,
    write_file,
)
from ubuntu_tweaks.process import Process

HERE = Path(__file__).resolve().parent
SCRIPT = "audio-health-check"
TIMER = "audio-health.timer"
SERVICE = "audio-health.service"


@dataclass(frozen=True)
class Piece:
    source: Path
    destination: str  # relative to $HOME
    mode: int
    is_unit: bool


PIECES = (
    Piece(HERE / SCRIPT, f".local/bin/{SCRIPT}", 0o755, False),
    Piece(HERE / "units" / SERVICE, f".config/systemd/user/{SERVICE}", 0o644, True),
    Piece(HERE / "units" / TIMER, f".config/systemd/user/{TIMER}", 0o644, True),
)


# What the retirement leaves none of: the three installed files and the enable link that `systemctl enable` made.
ENABLE_LINK = f".config/systemd/user/timers.target.wants/{TIMER}"
SCRIPT_DESTINATION = PIECES[0].destination
LEFTOVERS = (*(piece.destination for piece in PIECES), ENABLE_LINK)


class AudioHealth(Module):
    name = "audio-health"

    def __init__(self, home: Path, process: Process) -> None:
        self._home = home
        self._process = process

    def _in_place(self, piece: Piece) -> bool:
        path = self._home / piece.destination
        return (
            path.is_file()
            and path.read_bytes() == piece.source.read_bytes()
            and (path.stat().st_mode & 0o777) == piece.mode
        )

    def _timer_enabled(self) -> bool:
        result = self._process.query(["systemctl", "--user", "is-enabled", TIMER])
        return result.returncode == 0 and result.stdout.splitlines()[:1] == ["enabled"]

    def _timer_running(self) -> bool:
        """``enabled`` only means from the next login on: a stopped timer would never run the check until then."""
        return self._process.query(["systemctl", "--user", "is-active", TIMER]).returncode == 0

    def _timer_up(self) -> bool:
        return self._timer_enabled() and self._timer_running()

    def _answered(self, verb: str) -> bool:
        """Whether ``systemctl --user VERB TIMER`` exits 0. A user manager that says nothing (no session bus) is a
        ``ModuleError``, never "off": retiring blind would delete the units under a timer that still runs. An older
        systemd words a missing unit file on stderr ("Failed to get unit file state ...") instead of ``not-found``: an answer;
        the no-bus error ends in the same "No such file or directory", so only that exact wording counts."""
        result = self._process.query(["systemctl", "--user", verb, TIMER])
        missing = verb == "is-enabled" and "Failed to get unit file state" in result.stderr
        if not result.stdout.strip() and not missing:
            detail = next((line for line in result.stderr.splitlines() if line.strip()), "no output")
            raise ModuleError(
                f"the user manager does not answer (systemctl --user {verb} {TIMER}: {detail.strip()}): "
                "run this in the desktop user's login session"
            )
        return result.returncode == 0

    def _retire_step(self, left: list[str]) -> tuple[str, ...] | None:
        """The systemctl arguments that stop the timer: ``disable --now`` while systemd still knows the unit file (any
        enabled state with exit 0), ``stop`` for a timer that runs on after its file is gone (disable needs the file),
        ``None`` for a timer that is off. A manager that does not answer matters only while something is left on disk:
        with nothing there the retirement counts as converged: a shell without a session has nothing to ask, and a timer
        that runs on after its files were deleted by hand is only found by the next run inside the session."""
        try:
            if self._answered("is-enabled"):
                return ("disable", "--now", TIMER)
            if self._answered("is-active"):
                return ("stop", TIMER)
        except ModuleError:
            if left:
                raise
        return None

    def _left(self) -> list[str]:
        return [destination for destination in LEFTOVERS if os.path.lexists(self._home / destination)]  # dangling too

    # ---- Module protocol ----

    def findings(self) -> list[Finding]:
        findings = [
            Finding(
                self._in_place(piece), f"~/{piece.destination}", "" if self._in_place(piece) else "missing or changed"
            )
            for piece in PIECES
        ]
        findings.append(Finding(self._timer_up(), f"{TIMER} is enabled and running in the user manager"))
        return findings

    def plan(self) -> list[str]:
        lines: list[str] = []
        for piece in PIECES:
            if self._in_place(piece):
                continue
            shown = f"~/{piece.destination}"
            new = piece.source.read_text()
            try:
                old = read_text_or_none(self._home / piece.destination)
            except UnicodeDecodeError:  # not text: the preview shows the new file only
                old = None
            if old is None and not piece.is_unit:  # a new script is named, not dumped into the preview
                lines.append(f"    write {shown} ({len(new.splitlines())} lines, mode {piece.mode:o})")
            else:
                lines.extend(unified_diff_lines(shown, old, new))
        if not self._timer_up():
            lines.append(f"    systemctl --user enable --now {TIMER}")
        return lines

    def apply(self) -> None:
        changed_units = False
        for piece in PIECES:
            if self._in_place(piece):
                continue
            write_file(self._home / piece.destination, piece.source.read_text(), piece.mode)
            changed_units = changed_units or piece.is_unit
        if changed_units:
            self._process.execute(["systemctl", "--user", "daemon-reload"])
        if not self._timer_up():
            self._process.execute(["systemctl", "--user", "enable", "--now", TIMER])
        elif changed_units:
            self._process.execute(["systemctl", "--user", "restart", TIMER])  # a running timer keeps its old schedule

    # ---- retirement (the module is disabled) ----

    @staticmethod
    def _to_remove(step: tuple[str, ...] | None, left: list[str]) -> list[str]:
        """What ``retire`` unlinks itself: ``disable`` already removes the enable link."""
        return [d for d in left if not (d == ENABLE_LINK and step and step[0] == "disable")]

    @staticmethod
    def _needs_reload(removed: list[str]) -> bool:
        return any(destination != SCRIPT_DESTINATION for destination in removed)

    def retired_findings(self) -> list[Finding]:
        left = self._left()
        findings = [
            Finding(destination not in left, f"~/{destination} is absent", "still there" if destination in left else "")
            for destination in LEFTOVERS
        ]
        try:
            off = self._retire_step(left) is None
            findings.append(Finding(off, f"{TIMER} is neither enabled nor running", "" if off else "still on"))
        except ModuleError as error:  # drift, not a crash: a crash would abort the plan of every other module
            findings.append(Finding(False, f"{TIMER} is neither enabled nor running", str(error)))
        return findings

    def retire_plan(self) -> list[str]:
        left = self._left()
        step = self._retire_step(left)
        removed = self._to_remove(step, left)
        lines = [f"    systemctl --user {' '.join(step)}"] if step else []
        lines.extend(f"    remove ~/{destination}" for destination in removed)
        if self._needs_reload(removed):
            lines.append("    systemctl --user daemon-reload")
        return lines

    def retire(self) -> None:
        left = self._left()
        try:
            step = self._retire_step(left)  # before the unit file goes, or systemd cannot name what it disables
        except ModuleError as error:
            raise NeedsManual(str(error)) from error
        if step:
            self._process.execute(["systemctl", "--user", *step])
        removed = self._left()  # read again: what `disable` removed itself is gone, whatever it left is removed here
        for destination in removed:
            (self._home / destination).unlink()
        if self._needs_reload(removed):
            self._process.execute(["systemctl", "--user", "daemon-reload"])

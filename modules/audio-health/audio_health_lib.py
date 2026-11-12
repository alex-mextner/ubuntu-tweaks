"""Shared code of the ``audio-health`` module: the check, its service and its timer, installed for the desktop user.

Accessed via: ``modules/audio-health/audio-health`` (check / status / plan / install, as the desktop user).
Spec: docs/specs/ubuntu-tweak-cli.md, *Audio health*.

Everything lives under ``$HOME``: ``~/.local/bin/audio-health-check`` (the module's script, byte for byte),
``~/.config/systemd/user/audio-health.service`` and ``audio-health.timer``, and the timer enabled in the user manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.module_script import Finding, Module, read_text_or_none, unified_diff_lines, write_file
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

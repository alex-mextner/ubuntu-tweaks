"""The progress watchdog and the heartbeat of every command the engine streams (spec, *Stalled steps*).

Accessed via: ``process.run_streamed`` builds one ``Watchdog`` per child, asks it to ``poll`` once a second while it
waits and lets the output readers call ``activity``; ``actions.Plan.execute`` opens a ``process.watching`` block per
action so the stall is named after the action and reaches the run journal.

Why it exists (ticket #126): Ubuntu 26.04's ``sudo`` is sudo-rs. A command whose stdout is a pipe (every command this
engine runs) starts in the *background* of sudo's pty; ``apt-get``, a grandchild of a root module script, changes the
pty's settings when it finishes and the kernel stops the process group (SIGTTOU). sudo-rs notices only its direct
child stopping and resumes that pid alone, so ``apt-get`` stays stopped for ever and the wizard shows nothing. Nothing
in the engine looked below its direct child, and a root process cannot be signalled by an unprivileged engine.

Assumptions: ``/proc/<pid>/stat`` of every process is readable (it is world-readable, root children included), so the
engine sees the whole tree below the child it started: the sudo front end, its monitor, the module script, ``apt-get``.
A stall is reported, never fixed by force: nothing is killed and nothing is hidden. The one recovery is SIGCONT to a
descendant that is *stopped* (state ``T``), sent by the engine itself, or through the open root session
(``sudo -n kill -CONT <pid>``) when the process belongs to root; when neither works the report carries the literal
command for another terminal.

Public API:

- ``Proc(pid, ppid, state, cpu, comm)``, ``read_processes(root=Path("/proc")) -> dict[int, Proc]``,
  ``tree(table, pid) -> list[Proc]`` (the pid and its descendants).
- ``Stall(kind, step, seconds, pid=None, comm="", resumed=False, command="")`` with ``detail()`` (one plain line, for the
  run journal) and ``render(stall) -> list[str]`` (the warning, Russian or English, coloured in a terminal).
- ``stall_seconds(environ=os.environ, override=None) -> float``: the step's own value, else ``UBUNTU_TWEAK_STALL_SECONDS``
  (5 to 86400), else 120.
- ``resume_stopped(pid) -> bool``: SIGCONT, as root through the session when the process is root's.
- ``Heartbeat(step, stream, clock=..., after=3.0)``: ``tick()``, ``activity()``, ``erase()``; writes only on a stream
  that is in the explained layout (``ui.rich``), one line rewritten in place.
- ``Watchdog(pid, step=, stall_seconds=, ...)``: ``activity()`` (output arrived), ``poll()``, ``lock``.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ubuntu_tweaks import sessionlog, ui

STALL_ENV = "UBUNTU_TWEAK_STALL_SECONDS"
DEFAULT_STALL_SECONDS = 120.0
MIN_STALL_SECONDS = 5.0
MAX_STALL_SECONDS = 86400.0
STOP_GRACE_SECONDS = 10.0  # a stop that lasts shorter is sudo bouncing a tty access: it resumes the command itself
SCAN_AFTER_SECONDS = 1.0  # the process table is read only while the step has been quiet at least this long
HEARTBEAT_AFTER_SECONDS = 3.0
HEARTBEAT_MAX_SECONDS = 1800  # a redrawn line the session log cannot collapse must not grow for ever
RESUME_TIMEOUT_SECONDS = 8
LOCK_WAIT_SECONDS = 0.2
EMIT_LOCK_WAIT_SECONDS = 2.0
RESCAN_SECONDS = 5.0  # the process tree is read at least this often even while the step prints
ERASE_LINE = "\r\x1b[2K"
STOPPED = "T"  # "t" is a tracing stop (a debugger): not ours to touch


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    state: str
    cpu: int  # clock ticks used by the process and the children it has waited for
    comm: str


def read_processes(root: Path = Path("/proc")) -> dict[int, Proc]:
    table: dict[int, Proc] = {}
    try:
        names = os.listdir(root)
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            text = (root / name / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:  # the process ended while we looked
            continue
        opening, closing = text.find("("), text.rfind(")")
        fields = text[closing + 2 :].split() if closing >= 0 else []
        if opening < 0 or len(fields) < 15:
            continue
        try:
            ppid = int(fields[1])
            cpu = sum(int(fields[index]) for index in (11, 12, 13, 14))  # utime stime cutime cstime
        except ValueError:
            continue
        pid = int(name)
        table[pid] = Proc(pid, ppid, fields[0], cpu, text[opening + 1 : closing])
    return table


def tree(table: Mapping[int, Proc], pid: int) -> list[Proc]:
    children: dict[int, list[int]] = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    found: list[Proc] = []
    seen: set[int] = set()
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in table:
            found.append(table[current])
        pending.extend(children.get(current, ()))
    return found


def stall_seconds(environ: Mapping[str, str] = os.environ, override: float | None = None) -> float:
    if override is not None:
        return float(override)
    try:
        value = float(environ.get(STALL_ENV, ""))
    except ValueError:
        return DEFAULT_STALL_SECONDS
    return value if MIN_STALL_SECONDS <= value <= MAX_STALL_SECONDS else DEFAULT_STALL_SECONDS


@dataclass(frozen=True)
class Stall:
    kind: str  # "stopped" (a descendant is in state T) or "quiet" (no output and no CPU)
    step: str
    seconds: float
    pid: int | None = None
    comm: str = ""
    resumed: bool = False  # the engine sent SIGCONT
    command: str = ""  # what a person types in another terminal to resume it

    def detail(self) -> str:
        if self.kind == "stopped":
            return (
                f"stalled: {self.comm} (pid {self.pid}) stopped for {math.floor(self.seconds)} s"
                + (", SIGCONT sent" if self.resumed else "")
                + f"; resume with: {self.command}"
            )
        return f"stalled: no output and no CPU for {math.floor(self.seconds)} s"


def render(stall: Stall) -> list[str]:
    seconds = math.floor(stall.seconds)
    if stall.kind == "stopped":
        lines = [
            ui.say("need", ui.t("stall.stopped", step=stall.step, comm=stall.comm, pid=stall.pid, seconds=seconds)),
        ]
        if stall.resumed:
            lines += ui.wrap(ui.t("stall.resumed"), indent=2)
        lines += ui.wrap(ui.t("stall.command"), indent=2)
        lines.append("    " + ui.command(stall.command))
        lines += ui.wrap(ui.t("stall.leave"), indent=2)
        return lines
    lines = [ui.say("need", ui.t("stall.quiet", step=stall.step, seconds=seconds))]
    lines += ui.wrap(ui.t("stall.quiet_hint", env=STALL_ENV), indent=2)
    return lines


def _resume_as_root(pid: int) -> bool:
    from ubuntu_tweaks import process  # process imports this module

    prefix = process.root_prefix()
    if not prefix or prefix[0] != "sudo":  # pkexec would pop up a dialog: not ours to ask for
        return False
    argv = ["sudo", "-n", *prefix[1:], "kill", "-CONT", str(pid)]
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=RESUME_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def resume_stopped(pid: int) -> bool:
    """SIGCONT to ``pid``; as root, through the open root session, when the process is not ours. A process that is
    already gone needs nothing."""
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        return True
    except PermissionError:
        return _resume_as_root(pid)
    except OSError:
        return False
    return True


class Heartbeat:
    """One line, ``… <step> (still running, mm:ss)``, rewritten in place while a step is quiet; erased before anything
    else is written. Never written on a stream that is not in the explained layout."""

    def __init__(
        self,
        step: str,
        stream: TextIO,
        *,
        clock: Callable[[], float] = time.monotonic,
        after: float = HEARTBEAT_AFTER_SECONDS,
    ) -> None:
        self._step = step
        self._stream = stream
        self._clock = clock
        self._after = after
        self._started = clock()
        self._last_output = self._started
        self._visible = False
        self._at_line_start = True  # a prompt or half-printed line is never overwritten

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):  # the terminal is gone: the run goes on
            self._visible = False

    def tick(self) -> None:
        if not ui.rich(self._stream) or not self._at_line_start:
            return
        now = self._clock()
        if now - self._last_output < self._after:
            return
        elapsed = int(now - self._started)
        if now - self._last_output > HEARTBEAT_MAX_SECONDS:
            return
        text = ui.t("heartbeat", step=self._step, time=f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        if len(text) > ui.width() - 1:
            text = text[: ui.width() - 2] + "…"
        self._write(ERASE_LINE + ui.style(text, "dim", stream=self._stream))
        self._visible = True

    def erase(self) -> None:
        if self._visible:
            self._visible = False
            self._write(ERASE_LINE)

    def activity(self, text: str = "") -> None:
        self.erase()
        self._last_output = self._clock()
        if text:
            self._at_line_start = text.endswith("\n")  # "45%\r" is a progress line still being drawn


class Watchdog:
    def __init__(
        self,
        pid: int,
        *,
        step: str,
        stall_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        processes: Callable[[], dict[int, Proc]] = read_processes,
        resume: Callable[[int], bool] = resume_stopped,
        report: Callable[[Stall], None] | None = None,
        on_stall: Callable[[Stall], None] | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.lock = threading.RLock()  # output readers and the report share the terminal
        self._pid = pid
        self._step = step
        self._stall = stall_seconds
        self._grace = min(STOP_GRACE_SECONDS, stall_seconds)
        self._scan_after = min(SCAN_AFTER_SECONDS, stall_seconds / 2)
        self._clock = clock
        self._processes = processes
        self._resume = resume
        self._report = report
        self._on_stall = on_stall
        self._heartbeat = heartbeat
        now = clock()
        self._last_output = now
        self._last_progress = now  # output or CPU used by the tree
        self._cpu: int | None = None
        self._last_scan = now
        self._closed = False  # the step ended: a poll still in flight reports nothing any more
        self._quiet_reported = False
        self._first_stopped: dict[int, float] = {}  # pid -> when it was first seen stopped
        self._grace_from: dict[int, float] = {}  # pid -> when the current grace period began
        self._attempted: set[int] = set()
        self._settled: set[int] = set()  # nothing more to say about these

    def activity(self, text: str = "") -> None:
        """Output arrived (the caller holds ``lock`` while it writes it); ``text`` is what it was."""
        with self.lock:
            now = self._clock()
            self._last_output = self._last_progress = now
            self._quiet_reported = False
            if self._heartbeat is not None:
                self._heartbeat.activity(text)

    def finish(self) -> None:
        """The step ended (or was interrupted): nothing of the heartbeat stays on the screen, and no stall is
        reported from now on (a poll still blocked in a slow write or a ``sudo`` call finds the step closed)."""
        self._closed = True
        if not self.lock.acquire(timeout=LOCK_WAIT_SECONDS):  # an output write that blocks must not block the end
            return
        try:
            if self._heartbeat is not None:
                self._heartbeat.erase()
        finally:
            self.lock.release()

    def poll(self) -> None:
        """One look. Only the redraw of the heartbeat and a report hold ``lock``; reading the process table and
        continuing a stopped process (a ``sudo`` call of up to seconds) do not, so output and the end of the step
        never wait for them."""
        if not self._locked(self._tick):
            return
        if self._closed:
            return
        now = self._clock()
        # Output keeps a step alive, but it says nothing of a *sibling* that stopped: the tree is read at least
        # every RESCAN_SECONDS whatever the output does.
        if now - self._last_output < self._scan_after and now - self._last_scan < RESCAN_SECONDS:
            return
        self._last_scan = now
        members = tree(self._processes(), self._pid)
        cpu = sum(proc.cpu for proc in members)
        if self._cpu is not None and cpu != self._cpu:
            self._last_progress = now
            self._quiet_reported = False
        self._cpu = cpu
        self._check_stopped({proc.pid: proc for proc in members if proc.state == STOPPED}, now)
        self._check_quiet(now)

    def _locked(self, action: Callable[[], None]) -> bool:
        if not self.lock.acquire(timeout=LOCK_WAIT_SECONDS):  # a blocked terminal write must not stall the caller
            return False
        try:
            action()
        finally:
            self.lock.release()
        return True

    def _tick(self) -> None:
        if self._heartbeat is not None and not self._closed:
            self._heartbeat.tick()

    def _check_stopped(self, stopped: dict[int, Proc], now: float) -> None:
        for pid in [pid for pid in self._first_stopped if pid not in stopped]:  # it moved again (or ended)
            for book in (self._first_stopped, self._grace_from):
                book.pop(pid, None)
            self._attempted.discard(pid)
            self._settled.discard(pid)
        for pid, proc in stopped.items():
            first = self._first_stopped.setdefault(pid, now)
            begun = self._grace_from.setdefault(pid, now)
            if pid in self._settled or now - begun < self._grace:
                continue
            command = f"sudo kill -CONT {pid}"
            if pid in self._attempted:  # still stopped a grace period after SIGCONT: say so once and stop asking
                self._settled.add(pid)
                self._emit(Stall("stopped", self._step, now - first, pid, proc.comm, False, command))
                continue
            self._attempted.add(pid)
            try:
                resumed = self._resume(pid)
            except Exception:  # a recovery that fails is reported below as not resumed, never a crash of the run
                resumed = False
            if resumed:
                self._grace_from[pid] = now  # give the process a moment to prove it moves
            else:
                self._settled.add(pid)
            self._emit(Stall("stopped", self._step, now - first, pid, proc.comm, resumed, command))

    def _check_quiet(self, now: float) -> None:
        if self._quiet_reported or self._first_stopped or now - self._last_progress < self._stall:
            return
        self._quiet_reported = True
        self._emit(Stall("quiet", self._step, now - self._last_progress))

    def _emit(self, stall: Stall) -> None:
        if self._closed:
            return
        # Ordered with the child's output when the lock can be had; a stall is never swallowed because it cannot.
        held = self.lock.acquire(timeout=EMIT_LOCK_WAIT_SECONDS)
        try:
            if self._closed:
                return
            if self._report is not None:
                self._report(stall)
            else:
                self._print(stall)
            if self._on_stall is not None:
                self._on_stall(stall)
        finally:
            if held:
                self.lock.release()

    def _print(self, stall: Stall) -> None:
        if self._heartbeat is not None:
            self._heartbeat.erase()
        try:
            sys.stdout.write("\n".join(render(stall)) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        sessionlog.note("stall", f"{stall.step}: {stall.detail()}")

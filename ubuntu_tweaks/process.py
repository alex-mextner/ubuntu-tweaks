"""Running external commands on behalf of planners: the one seam tests replace.

Accessed via: ``planners/system_root.py``, ``planners/packages.py`` and ``planners/migrations.py``
take a ``Process`` (default: the real one) so their checks and installs are testable without
touching the machine.

Assumptions: ``query`` is for read-only checks (captured output, a timeout, never raises for a
missing binary); ``execute`` is for mutations (stdin stays the terminal so a child can read an answer and ``sudo`` can
open ``/dev/tty`` for its password; stdout and stderr are pipes copied to ``sys.stdout`` / ``sys.stderr`` as they
arrive, so the session log sees what the child shows) and raises ``CommandError`` on a non-zero exit.

Public API:

- ``CommandResult(returncode, stdout, stderr)`` with ``ok``.
- ``CommandError`` (exit 1); ``returncode`` is the failed command's exit status (``None`` when it never ran).
- ``Process``: ``execute`` raises ``CommandError`` ``<argv>: exited with status N`` plus, when the child wrote to stderr, ``: <its last line>``
  (so a retry classifier can read ``could not resolve host``); ``query(argv, *, timeout=60.0, cwd=None) -> CommandResult`` (127 when the binary is
  missing, 126 when it cannot be executed, 124 on timeout; stdin closed), ``execute(argv, *, input_text=None, cwd=None)`` and
  ``elevate(argv) -> list[str]`` (prefix with ``sudo --`` or ``pkexec``; unchanged when already
  root; ``CommandError`` when neither exists; while an ``elevation.RootSession`` is open it uses the prefix
  that session found working, e.g. ``sudo -A --``).
- Module-level ``elevate(argv)`` (the same logic, for callers without a ``Process``),
  ``use_root_prefix(prefix)`` / ``root_prefix()`` (the session's prefix; ``None`` outside a session) and
  ``run(argv, *, cwd=None, capture=False, timeout=None) -> str`` (raises ``CommandError``; the
  helper behind ``git``/``apt``/``glib-compile-schemas`` calls in the commands and submodule planners) and
  ``run_streamed(argv, *, cwd=None, env=None, input_text=None, timeout=None, stderr_tail=None, last_line=None) -> int``
  (the child's exit status; its output is copied through the pipes described above; ``OSError`` when it cannot
  start, ``TimeoutExpired``).
- ``FakeProcess(responses, effects)``: records ``queries`` and ``executed``; ``responses`` maps an
  argv tuple to a ``CommandResult`` (anything else answers 127); ``effects`` is called with
  ``(argv, input_text)`` on every ``execute`` so a test can change the fake machine state.
"""

from __future__ import annotations

import atexit
import codecs
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ubuntu_tweaks import watchdog
from ubuntu_tweaks.errors import UbuntuTweakError
from ubuntu_tweaks.sessionlog import LineMasker

_CHUNK = 65536
_TAIL = 2000  # characters of a child's stderr kept for the error message of a failure
_LINE = 2000  # characters of the last line of a child's output kept for the reason of a failure (after masking)
_DETAIL = 400  # ... of which the last three non-empty lines are shown on one line, clipped to this many
_DRAIN_GRACE = 2.0  # seconds to wait for output left in the pipes once the child has exited
_INTERRUPT_GRACE = 0.25  # seconds a child gets to react to the Ctrl-C it received too, before it is stopped
_POLL_SECONDS = 1.0  # how often the watchdog looks at a running child
_STEP_LABEL = 100  # characters of a command line kept as the name of a step the engine knows nothing else about
_abandoned: list[IO[bytes]] = []  # pipes whose reader is still blocked: closing them would let the fd be reused


def _close_abandoned() -> None:
    for pipe in _abandoned:
        pipe.close()


atexit.register(_close_abandoned)  # at exit no fd can be reused any more


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(UbuntuTweakError):
    """An external command failed or a required helper (sudo, pkexec) is missing; ``returncode`` is the
    command's exit status when it ran and failed."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


_root_prefix: tuple[str, ...] | None = None


def use_root_prefix(prefix: Sequence[str] | None) -> None:
    """Make ``elevate`` use ``prefix`` (``None``: the default lookup again); ``elevation.RootSession`` sets it."""
    global _root_prefix
    _root_prefix = tuple(prefix) if prefix is not None else None


def root_prefix() -> tuple[str, ...] | None:
    return _root_prefix


def elevate(argv: Sequence[str]) -> list[str]:
    """Prefix ``argv`` so it runs as root: unchanged when already root, else the open session's prefix, else
    ``sudo --`` or ``pkexec``."""
    if os.geteuid() == 0:
        return list(argv)
    if _root_prefix is not None:
        return [*_root_prefix, *argv]
    if shutil.which("sudo"):
        return ["sudo", "--", *argv]
    if shutil.which("pkexec"):
        return ["pkexec", *argv]
    raise CommandError("root is required but neither sudo nor pkexec is installed")


@dataclass(frozen=True)
class Watch:
    """What ``run_streamed`` knows about the step it runs for: its name in a stall warning, its own stall time
    (``None``: ``UBUNTU_TWEAK_STALL_SECONDS``, then 120 s), who is told of a stall, and a heartbeat for it."""

    label: str = ""
    stall_seconds: float | None = None
    on_stall: Callable[[watchdog.Stall], None] | None = None
    resume: Callable[[int], bool] | None = None  # a test seam: what continues a stopped descendant
    poll_seconds: float = _POLL_SECONDS
    heartbeat_after: float = watchdog.HEARTBEAT_AFTER_SECONDS


_watch: Watch | None = None


def current_watch() -> Watch | None:
    return _watch


@contextlib.contextmanager
def watching(
    label: str = "",
    *,
    stall_seconds: float | None = None,
    on_stall: Callable[[watchdog.Stall], None] | None = None,
    resume: Callable[[int], bool] | None = None,
    poll_seconds: float = _POLL_SECONDS,
    heartbeat_after: float = watchdog.HEARTBEAT_AFTER_SECONDS,
) -> Iterator[None]:
    """Every command started inside the block belongs to the step ``label``: a stall is named after it, reaches
    ``on_stall`` and, in the explained layout, a quiet step shows an elapsed-time heartbeat."""
    global _watch
    previous = _watch
    _watch = Watch(label, stall_seconds, on_stall, resume, poll_seconds, heartbeat_after)
    try:
        yield
    finally:
        _watch = previous


class _LastLine:
    """The last non-blank line of a child's output, exactly as the session log shows it (``sessionlog.LineMasker``:
    plain text, secrets masked, a long line cut the same way). Fed with the chunks of ONE pipe, so the order is the
    child's own. Blank lines, however many or however long, never push a diagnostic out."""

    def __init__(self, target: list[str]) -> None:
        self._target = target
        self._lines = LineMasker()
        self._lock = threading.Lock()
        self._done = False

    def feed(self, text: str) -> None:
        with self._lock:
            if not self._done:
                self._keep(self._lines.feed(text))

    def flush(self) -> None:
        """The child ended: an unfinished last line counts too (a prompt, a message without a newline). Nothing is
        kept afterwards: a grandchild that holds the pipe open and writes later is not the reason the child failed."""
        with self._lock:
            if not self._done:
                self._keep(self._lines.rest())
            self._done = True

    def _keep(self, shown: list[tuple[str, str]]) -> None:
        for _raw, masked in shown:
            if masked.strip():
                self._target[:] = [masked.strip()[-_LINE:]]


def _pump(
    pipe: IO[bytes],
    stream_name: str,
    abandoned: threading.Event,
    monitor: watchdog.Watchdog | None,
    tail: list[str] | None = None,
    lines: _LastLine | None = None,
) -> None:
    """Copy ``pipe`` to ``sys.<stream_name>`` chunk by chunk (not by line, so a prompt without a newline shows).
    ``tail``, when given, keeps the last ``_TAIL`` characters of what was read (the reason a failure shows);
    ``lines`` keeps the last non-blank line instead.
    Every chunk tells the watchdog (``None`` outside a ``watching`` block) that the child is alive; the heartbeat
    line is erased before it is shown and never drawn over a line the child left unfinished.

    Once ``abandoned`` is set the caller has stopped waiting: the thread only drains, so it never writes to a
    stream the interpreter is about to tear down."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    fd = pipe.fileno()
    shown = True
    while True:
        try:
            chunk = os.read(fd, _CHUNK)
        except OSError:
            break
        text = decoder.decode(chunk, final=not chunk)
        if text and tail is not None:
            tail[:] = ["".join(tail + [text])[-_TAIL:]]
        if text and lines is not None:
            lines.feed(text)
        if text and not abandoned.is_set():
            with monitor.lock if monitor is not None else contextlib.nullcontext():
                if monitor is not None:
                    monitor.activity(text)
                if shown:
                    stream = getattr(sys, stream_name)
                    try:
                        try:
                            stream.write(text)
                        except UnicodeEncodeError:  # a character the terminal's encoding lacks: show a replacement
                            encoding = getattr(stream, "encoding", None) or "ascii"
                            stream.write(text.encode(encoding, "replace").decode(encoding))
                        stream.flush()
                    except (OSError, ValueError):  # the terminal or pipe we write to is gone: keep draining the child
                        shown = False
        if not chunk:
            break
    if lines is not None:
        lines.flush()
    pipe.close()  # an abandoned pipe is released as soon as the last writer lets go of it


def _poll_loop(monitor: watchdog.Watchdog, stop: threading.Event, poll: float) -> None:
    """Look at the child's process tree every ``poll`` seconds until ``stop`` is set. A thread of its own: a write of
    the heartbeat or a warning that blocks (a terminal paused with Ctrl-S) must not stop the caller from seeing the
    child exit or its timeout run out."""
    while not stop.wait(poll):
        try:
            monitor.poll()
        except Exception as error:  # the watchdog is an aid: its own failure never fails or hides the step
            print(f"watchdog: {type(error).__name__}: {error}", file=sys.stderr)


def _feed(pipe: IO[bytes], data: bytes) -> None:
    with contextlib.suppress(OSError):  # the child exited without reading its input
        pipe.write(data)
    with contextlib.suppress(OSError):
        pipe.close()


def _watchdog_for(pid: int, watch: Watch, argv: Sequence[str]) -> watchdog.Watchdog:
    step = (watch.label or shlex.join(map(str, argv)))[:_STEP_LABEL]
    return watchdog.Watchdog(
        pid,
        step=step,
        stall_seconds=watchdog.stall_seconds(os.environ, watch.stall_seconds),
        resume=watch.resume if watch.resume is not None else watchdog.resume_stopped,
        on_stall=watch.on_stall,
        heartbeat=watchdog.Heartbeat(step, sys.stdout, after=watch.heartbeat_after),
    )


def run_streamed(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
    stderr_tail: list[str] | None = None,
    last_line: list[str] | None = None,
) -> int:
    """Run ``argv`` (never through a shell) and return its exit status, showing its output as it arrives.

    stdout and stderr are pipes that reader threads copy to ``sys.stdout`` / ``sys.stderr`` (which may be the
    session log's tees); stdin is the terminal, or ``input_text`` when given. ``OSError`` when the program cannot
    start and ``subprocess.TimeoutExpired`` (the child is stopped first) propagate as ``subprocess.run`` raises them.
    ``env`` is the child's whole environment (default: ours). ``stderr_tail`` (a list) ends up holding one string:
    the last characters the child wrote to stderr. ``last_line`` (a list) ends up holding the last non-blank line the
    child wrote, secrets masked and escape sequences removed (nothing when it wrote none); asking for it merges stderr
    into the stdout pipe, so the order of the lines is exactly the child's and one pump sees them all (the screen and
    the session log then show the child's stderr lines as stdout lines).

    Inside a ``watching`` block (one per action) a watchdog looks at the child and every descendant once a second
    (``watchdog``): a stopped one is continued and reported, a step with no output and no CPU for the stall time is
    reported, and a quiet step shows a heartbeat. Outside a block nothing is watched, so a command nested in a
    watched one (the child ``apply`` of ``finish``, a module script's ``apt-get``) is never reported twice.
    Nothing is ever killed for being slow.
    """
    # A Python child on a pipe would hold its prompts back and mix its two streams: ask it not to buffer.
    environment = {**(os.environ if env is None else env), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if last_line is not None else subprocess.PIPE,
        env=environment,
    )
    lines = _LastLine(last_line) if last_line is not None else None
    watch = _watch
    monitor = _watchdog_for(process.pid, watch, argv) if watch is not None else None
    stop = threading.Event()
    poll_seconds = watch.poll_seconds if watch is not None else _POLL_SECONDS
    readers = [
        (
            threading.Thread(
                target=_pump,
                args=(pipe, name, abandoned, monitor, stderr_tail if name == "stderr" else None, lines),
                daemon=True,
            ),
            pipe,
            abandoned,
        )
        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr"))
        if pipe is not None
        for abandoned in (threading.Event(),)
    ]
    try:
        for reader, _, _ in readers:
            reader.start()
        if monitor is not None:
            threading.Thread(target=_poll_loop, args=(monitor, stop, poll_seconds), daemon=True).start()
        if input_text is not None and process.stdin is not None:
            threading.Thread(target=_feed, args=(process.stdin, input_text.encode()), daemon=True).start()
        returncode = process.wait(timeout)
    except SystemExit:  # SIGTERM to us: do not kill the child (it may die of SIGPIPE on its next write)
        raise
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):  # the child got the same Ctrl-C: let it wind down first
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(_INTERRUPT_GRACE)
        if process.poll() is None:
            try:
                process.kill()
            except PermissionError:  # a root child (sudo) cannot be signalled by us
                pass
            else:
                process.wait()
        raise
    finally:
        stop.set()
        if monitor is not None:
            monitor.finish()
        deadline = time.monotonic() + _DRAIN_GRACE
        for reader, pipe, abandoned in readers:
            if reader.ident is None:  # never started
                pipe.close()
                continue
            reader.join(max(0.05, deadline - time.monotonic()))  # the floor: a pump at EOF needs a moment to finish
        for reader, pipe, abandoned in readers:  # decided after every join: one slow pipe must not cut the other's tail
            if reader.ident is None:
                continue
            if reader.is_alive():  # a grandchild still holds the pipe open
                abandoned.set()  # its pump stops writing to the terminal: it may outlive the interpreter's streams
                if lines is not None:
                    lines.flush()  # the child is gone: what the pump has not read yet is not coming
                _abandoned.append(pipe)
            else:
                pipe.close()
    return returncode


def run(argv: Sequence[str], *, cwd: Path | None = None, capture: bool = False, timeout: float | None = None) -> str:
    """Run ``argv`` (never through a shell); return its stdout when ``capture`` else ``""``.

    Failures (missing binary, timeout, non-zero exit) raise ``CommandError`` carrying the command
    line and, for a captured command, the last stderr line. Output of a non-captured command is shown through
    ``sys.stdout`` / ``sys.stderr`` (``run_streamed``).
    """
    line = " ".join(argv)
    try:
        if not capture:
            returncode = run_streamed(argv, cwd=cwd, timeout=timeout)
            if returncode != 0:
                raise CommandError(f"`{line}` failed with exit code {returncode}", returncode)
            return ""
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise CommandError(f"{argv[0]}: command not found (while running: {line})") from error
    except subprocess.TimeoutExpired as error:
        raise CommandError(f"`{line}` timed out after {timeout} s") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise CommandError(f"`{line}` failed with exit code {error.returncode}{suffix}", error.returncode) from error
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
        except OSError as error:
            return CommandResult(126, "", f"{argv[0]}: cannot run: {error.strerror or error}")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        tail: list[str] = []
        try:
            returncode = run_streamed(argv, cwd=cwd, input_text=input_text, stderr_tail=tail)
        except FileNotFoundError as error:
            raise CommandError(f"{argv[0]}: command not found") from error
        except OSError as error:
            raise CommandError(f"{argv[0]}: cannot run: {error.strerror or error}") from error
        if returncode != 0:
            lines = [line.strip() for line in "".join(tail).splitlines() if line.strip()]
            detail = f": {' / '.join(lines[-3:])[-_DETAIL:]}" if lines else ""
            raise CommandError(f"{' '.join(argv)}: exited with status {returncode}{detail}", returncode)

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

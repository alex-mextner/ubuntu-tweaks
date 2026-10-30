"""The session log: everything a run showed the user, written to a file an agent can read afterwards.

Accessed via: ``cli.main`` wraps its whole body in ``run_logged``; ``cli._run`` calls ``set_command`` / ``note``
once it knows the command and the profile. No other module logs anything.

Assumptions: the tee wraps ``sys.stdout`` / ``sys.stderr`` and sees only what is written through them; a
child process that inherits the real descriptors bypasses it, so ``process.run_streamed`` gives children pipes and
copies them to these streams. The sudo/pkexec password prompt is opened by those programs on ``/dev/tty`` and an
answer typed into a child's own prompt goes straight to that child: neither passes through this process, and
neither is ever logged. A log write failure must never fail the command: logging is switched off for the rest of
the run and one warning goes to stderr.

Public API:

- ``KEEP_LOGS`` (50), ``LOG_DIR_ENV`` (``UBUNTU_TWEAK_LOG_DIR``, the test seam naming the directory) and
  ``SESSION_ENV`` (``UBUNTU_TWEAK_SESSION_LOG``, exported to children so a nested ``ubuntu-tweak`` writes no
  second log).
- ``log_dir(environ=None, home=None) -> Path``: ``$UBUNTU_TWEAK_LOG_DIR``, else
  ``<$XDG_STATE_HOME or ~/.local/state>/ubuntu-tweaks/logs``.
- ``run_logged(argv, command) -> int``: run ``command()`` (the CLI body, returning the exit code) with the tees
  installed and the log opened before anything is parsed; ``SystemExit`` (``--help``, usage errors),
  ``KeyboardInterrupt`` (exit 130) and SIGTERM (exit 143, best effort) are logged and then propagate as before.
- ``set_command(name)`` renames the log to ``<timestamp>-<name>.log`` (``none`` until then) and ``note(key, value)``
  adds a ``# key: value`` line to the footer; both do nothing outside a logged run.
- ``strip_terminal_text(text) -> str``: the escape-free, ``\\r``-collapsed form of one line.
- ``LineMasker``: one stream's chunks in, the plain, masked lines the log would write out (also used by
  ``process.run_streamed`` for the last line of a child's output, so a failure reason never shows what the log hides).
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import signal
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from ubuntu_tweaks.capture.secrets import SecretMasker, mask_secrets

KEEP_LOGS = 50
LOG_DIR_ENV = "UBUNTU_TWEAK_LOG_DIR"
SESSION_ENV = "UBUNTU_TWEAK_SESSION_LOG"
LATEST = "latest.log"
INTERRUPTED = 130
TERMINATED = 143
ERROR_PREFIX = "! "  # stderr lines carry it, stdout lines nothing: one text tells the two streams apart

_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: window titles, hyperlinks
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: colors, cursor moves, erase line
    r"|\x1b[()][0-9A-Za-z]"  # charset selection
    r"|\x1b[@-Z\\-_=>]"  # two-byte sequences
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # what is left after the escapes: BEL, backspace, a lone ESC
_SECRET_WORD = re.compile(r"token|password|secret", re.IGNORECASE)
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def log_dir(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    configured = env.get("XDG_STATE_HOME")
    state = Path(configured) if configured else (home if home is not None else Path.home()) / ".local/state"
    return state / "ubuntu-tweaks" / "logs"


def _collapse(text: str) -> str:
    """Keep the last state of a line rewritten with carriage returns (a progress bar); one trailing ``\\r`` stays
    because a ``\\r\\n`` pair may be split across writes."""
    if "\r" not in text:
        return text
    body = text.rstrip("\r")
    return body.rpartition("\r")[2] + ("\r" if len(body) != len(text) else "")


def strip_terminal_text(text: str) -> str:
    """``text`` (one line) as plain text: redraws collapsed, escape sequences and control characters removed."""
    return _CONTROL.sub("", _ANSI.sub("", _collapse(text).rstrip("\r")))


def masked_argv(argv: Iterable[str]) -> list[str]:
    """``argv`` with the value of every ``--...token|password|secret...`` flag replaced by ``***``."""
    shown: list[str] = []
    hide_next = False
    for element in argv:
        if hide_next and not element.startswith("--"):  # a value may start with "-": argparse reads it as a flag
            shown.append("***")
            hide_next = False
            continue
        hide_next = False
        if element.startswith("-"):
            name, equals, _ = element.partition("=")
            if _SECRET_WORD.search(name):
                if equals:
                    element = f"{name}=***"
                else:
                    hide_next = True
        shown.append(element)
    return shown


_MAX_PENDING = 262144  # characters of one unfinished line held back before it is logged as a line of its own


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class LineMasker:
    """One stream's text as the log shows it: the chunks written so far in, plain masked lines out. It holds an
    unfinished line until its newline arrives (a very long one is cut after a space so no token is split), collapses
    redraws, removes escape sequences, drops a line that only erased itself and masks each line with a ``SecretMasker``
    that has seen every earlier line of the stream. ``feed`` and ``rest`` return ``(raw, masked)`` pairs, ``raw`` being the
    line as written. The session log and the failure reason of a child (``process.run_streamed``) share it, so the
    reason can never show what the log hides. Not thread-safe: one writer at a time."""

    def __init__(self) -> None:
        self._pending: list[str] = []  # chunks: appending never recopies
        self._held = 0
        self._masker = SecretMasker()

    def feed(self, text: str) -> list[tuple[str, str]]:
        if "\n" in text:
            *complete, rest = (self.take() + text).split("\n")
            rest = _collapse(rest)
            if rest:
                self._pending.append(rest)
                self._held = len(rest)
        else:
            self._pending.append(text)
            self._held += len(text)
            if self._held <= _MAX_PENDING:
                return []
            # One endless line (a minified file): log it in pieces, cut after a space so no token is split.
            whole = self.take()
            cut = whole.rfind(" ") + 1 or len(whole)
            complete = [whole[:cut]]
            if cut < len(whole):
                self._pending.append(whole[cut:])
                self._held = len(whole) - cut
        return self._shown(complete)

    def rest(self) -> list[tuple[str, str]]:
        """The unfinished line, now that the stream has ended."""
        text = self.take()
        return self._shown([text]) if text else []

    def take(self) -> str:
        """The unfinished line as written, which is then empty."""
        text = "".join(self._pending)
        self._pending.clear()
        self._held = 0
        return text

    def pending(self) -> str:
        return "".join(self._pending)

    def _shown(self, raws: Iterable[str]) -> list[tuple[str, str]]:
        shown: list[tuple[str, str]] = []
        for raw in raws:
            line = strip_terminal_text(raw)
            if not line and raw.strip("\r"):  # a redraw that erased itself (spinner cleanup): not a blank line
                continue
            shown.append((raw, self._masker.line(line)))
        return shown


class _Sink:
    """The open log file. One lock orders every write of both streams, the reader threads of child output
    included; each stream keeps its unfinished line until the newline arrives."""

    def __init__(self, directory: Path, path: Path, file: TextIO, real_stderr: TextIO) -> None:
        self.directory = directory
        self.path = path
        self.lock = threading.RLock()
        self.notes: dict[str, str] = {}
        self._file = file
        self._real_stderr = real_stderr
        self._lines = {"stdout": LineMasker(), "stderr": LineMasker()}  # a PEM block never spans two streams
        self._broken = False
        self._last_secretive = False  # the line before the answer asked for a secret
        self._typing: str | None = None  # the answer being typed, once its first character was read
        self._typing_prompt = ""
        self._typing_secret = False

    def feed(self, stream: str, text: str) -> None:
        """Log ``text`` just written to ``stream``; the caller holds ``lock``."""
        if self._broken:
            return
        shown = self._lines[stream].feed(text)
        if not shown:
            return
        try:
            for raw, masked in shown:
                self._write(stream, raw, masked)
            self._file.flush()
        except (OSError, ValueError) as error:
            self.fail(error)

    def typed(self, text: str) -> None:
        """Log what was read from stdin as ``> <prompt><typed text>`` lines, the prompt being the unfinished stdout
        line (``Q? ``); characters read one at a time are joined until the newline. An answer to a question that
        mentions a token, password or secret (on the prompt line, the line before it or an unfinished stderr line,
        where ``getpass`` writes) is logged as ``***``: what the user types there need not look like a secret."""
        with self.lock:
            for piece in text.splitlines(keepends=True):
                if self._typing is None:
                    self._typing_prompt = self._lines["stdout"].take()
                    self._typing_secret = bool(
                        _SECRET_WORD.search(self._typing_prompt)
                        or self._last_secretive
                        or _SECRET_WORD.search(self._lines["stderr"].pending())
                    )
                    self._typing = ""
                self._typing += piece.rstrip("\r\n")
                if piece.endswith("\n"):
                    self._log_typed()

    def _log_typed(self) -> None:
        if self._typing is None:
            return
        answer = "***" if self._typing_secret else self._typing
        prompt, self._typing = self._typing_prompt, None
        self.feed("stdout", f"> {prompt}{answer}\n")
        self._last_secretive = False  # the echo line above is not a question: it must not mask the next answer

    def header(self, argv: Sequence[str], started: datetime) -> None:
        engine = Path(__file__).resolve().parent.parent
        python = f"{sys.version.split()[0]} {sys.executable}"
        lines = (
            "# session",
            f"# argv: ubuntu-tweak {shlex.join(masked_argv(argv))}".rstrip(),
            f"# started: {_iso(started)}",
            f"# host: {socket.gethostname()}",
            f"# cwd: {_cwd()}",
            f"# engine: {engine}",
            f"# python: {python}",
        )
        self._write_raw(lines)

    def footer(self, started: datetime, began: float, exit_code: int, outcome: str | None) -> None:
        """Flush the unfinished lines, then the end marker; the outcome line says how the run did not end normally."""
        with self.lock:
            self._log_typed()
            for stream in ("stdout", "stderr"):
                if not self._broken:
                    try:
                        for raw, masked in self._lines[stream].rest():
                            self._write(stream, raw, masked)
                    except (OSError, ValueError) as error:
                        self.fail(error)
            ended = datetime.now(UTC)
            lines = [f"# end: {_iso(ended)}, {time.monotonic() - began:.2f} s, exit {exit_code}"]
            if outcome:
                lines.append(f"# {outcome}")
            lines += [f"# {key}: {value}" for key, value in self.notes.items()]
            self._write_raw(lines)
            self._broken = True  # a reader thread of a child that outlived the run must not write to the closed file
            with contextlib.suppress(OSError, ValueError):
                self._file.close()

    def fail(self, error: Exception) -> None:
        """Stop logging for the rest of the run and say so once; never raises."""
        if self._broken:
            return
        self._broken = True
        with contextlib.suppress(OSError, ValueError):
            self._real_stderr.write(
                f"ubuntu-tweak: cannot write the session log {self.path} ({error}); continuing without it\n"
            )
            self._real_stderr.flush()

    def _write_raw(self, lines: Iterable[str]) -> None:
        if self._broken:
            return
        try:
            self._file.write("".join(f"{mask_secrets(line)}\n" for line in lines))
            self._file.flush()
        except (OSError, ValueError) as error:
            self.fail(error)

    def _write(self, stream: str, raw: str, masked: str) -> None:
        self._last_secretive = bool(_SECRET_WORD.search(raw))
        prefix = ERROR_PREFIX if stream == "stderr" else ""
        self._file.write(f"{prefix}{masked}\n")


def _cwd() -> str:
    try:
        return " ".join(str(Path.cwd()).splitlines())
    except OSError:  # the directory the user is in was deleted
        return "(unavailable)"


class _Delegating(io.TextIOBase):
    """The real stream in every way a tee does not change (``isatty``, ``encoding``, ``buffer``...), so ``ui``
    decides color and layout exactly as before; closing it must not close the terminal underneath."""

    def __init__(self, real: TextIO, sink: _Sink) -> None:
        super().__init__()
        self._real = real
        self._sink = sink

    def close(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._real.isatty()

    def seekable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._real.closed

    @property
    def encoding(self) -> str:
        return self._real.encoding

    @property
    def errors(self) -> str | None:
        return self._real.errors

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class _Tee(_Delegating):
    """An output stream that writes to the real one first and then to the log."""

    def __init__(self, real: TextIO, name: str, sink: _Sink) -> None:
        super().__init__(real, sink)
        self._name = name

    def write(self, text: str) -> int:
        written = self._real.write(text)  # outside the lock: a stalled terminal must not stall the other stream
        with self._sink.lock:
            self._sink.feed(self._name, text)
        return written

    def flush(self) -> None:
        self._real.flush()

    def fileno(self) -> int:
        return self._real.fileno()

    def writable(self) -> bool:
        return self._real.writable()

    def readable(self) -> bool:
        return False


class _StdinTee(_Delegating):
    """The input side: what this process reads from the terminal is logged as ``> <what was typed>``, preceded by
    the prompt that is still the unfinished stdout line (``> Q? y``). ``fileno`` is refused on purpose: with a real
    descriptor ``input()`` talks to the C-level terminal (readline) and bypasses both tees, with none it goes
    through ``sys.stdout.write`` and ``sys.stdin.readline``, which is what is logged. Only this process's reads
    pass here: a child's own prompts are read by the child from the terminal and never logged."""

    def _flush_prompt(self) -> None:
        """A prompt without a newline sits in the terminal's line buffer: show it before blocking on the answer."""
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()

    def readline(self, size: int = -1) -> str:
        self._flush_prompt()
        line = self._real.readline(size)  # blocks: the lock is not held
        self._sink.typed(line)
        return line

    def read(self, size: int | None = -1) -> str:
        self._flush_prompt()
        text = self._real.read(size)
        self._sink.typed(text)
        return text

    def readlines(self, hint: int = -1) -> list[str]:
        self._flush_prompt()
        lines = self._real.readlines(hint)
        self._sink.typed("".join(lines))
        return lines

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno: the session log reads stdin through Python")

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False


class _Terminated(SystemExit):
    """SIGTERM turned into a normal exit so the footer is written and ``finally`` blocks run."""

    def __init__(self) -> None:
        super().__init__(TERMINATED)


def _on_sigterm(_signum: int, _frame: object) -> None:
    raise _Terminated


_active: _Sink | None = None


def _unique_name(directory: Path, stamp: str, command: str) -> Iterable[Path]:
    yield directory / f"{stamp}-{command}.log"
    number = 2
    while True:
        yield directory / f"{stamp}-{command}-{number}.log"
        number += 1


def _create(directory: Path, stamp: str, command: str) -> tuple[Path, TextIO]:
    for path in _unique_name(directory, stamp, command):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            continue
        return path, os.fdopen(fd, "w", encoding="utf-8", errors="replace")
    raise AssertionError("unreachable: _unique_name never ends")


def _point_latest(directory: Path, path: Path) -> None:
    """``latest.log`` -> ``path``, replaced in one rename so a reader never finds it missing."""
    temporary = directory / f".{LATEST}.{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    temporary.symlink_to(path.name)
    os.replace(temporary, directory / LATEST)


def _prune(directory: Path, keep_path: Path) -> None:
    """Delete the oldest ``*.log`` files beyond ``KEEP_LOGS``; the names sort by time. Never ``latest.log``, its
    target, the new log or anything that is not a regular ``*.log`` file."""
    logs = sorted(p for p in directory.glob("*.log") if p.name != LATEST and p.is_file() and not p.is_symlink())
    latest = directory / LATEST
    target = os.readlink(latest) if latest.is_symlink() else None  # a name, relative to ``directory``
    for old in logs[: max(0, len(logs) - KEEP_LOGS)]:
        if old != keep_path and old.name != target:
            with contextlib.suppress(OSError):
                old.unlink()


def _open(real_stderr: TextIO) -> _Sink | None:
    """Create the log for this run; ``None`` (with one warning) when that is impossible."""
    try:
        directory = log_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path, file = _create(directory, _timestamp(datetime.now(UTC)), "none")
        sink = _Sink(directory, path, file, real_stderr)
        with contextlib.suppress(OSError):  # a filesystem without symlinks: the log works, `latest.log` is absent
            _point_latest(directory, path)
        with contextlib.suppress(OSError):
            _prune(directory, path)
    except (OSError, RuntimeError) as error:  # RuntimeError: no $HOME and no passwd entry
        with contextlib.suppress(OSError, ValueError):
            real_stderr.write(f"ubuntu-tweak: cannot create the session log ({error}); continuing without it\n")
        return None
    return sink


def _nested() -> bool:
    """True inside a child ``ubuntu-tweak`` of a logged run: its output already reaches the parent's log."""
    parent = os.environ.get(SESSION_ENV)
    return bool(parent) and os.access(Path(parent).parent, os.W_OK)


def set_command(name: str) -> None:
    """Name the log after the command (``<timestamp>-<name>.log``); a no-op outside a logged run."""
    sink = _active
    if sink is None:
        return
    safe = _UNSAFE_NAME.sub("_", str(name or "none")) or "none"  # never fail the command over a name
    with sink.lock:
        stamp = sink.path.name.split("-", 1)[0]
        for path in _unique_name(sink.directory, stamp, safe):
            if path == sink.path:
                return
            try:
                os.link(sink.path, path)  # unlike rename, never replaces an existing log
            except FileExistsError:
                continue
            except OSError:  # no hard links on this filesystem: the name is cosmetic, keep the provisional one
                return
            break
        try:
            _point_latest(sink.directory, path)  # before the old name goes: a reader of latest.log never loses it
        except OSError:
            with contextlib.suppress(OSError):
                path.unlink()
            return
        old, sink.path = sink.path, path
        os.environ[SESSION_ENV] = str(path)
        with contextlib.suppress(OSError):
            old.unlink()


def note(key: str, value: str) -> None:
    """Add ``# key: value`` to the footer of this run's log (the profile directory, the machine...)."""
    if _active is not None:
        with _active.lock:
            _active.notes[key] = " ".join(str(value).splitlines())  # a newline in a path must not forge footer lines


def _exit_status(code: object) -> int:
    if code is None:
        return 0
    return code if isinstance(code, int) else 1  # SystemExit("message") exits 1 after printing it


def run_logged(argv: Sequence[str], command: Callable[[], int]) -> int:
    """Run ``command`` with this run's session log open; return its exit code, or let what it raised propagate."""
    global _active
    if _nested():
        return command()
    real_out, real_err, real_in = sys.stdout, sys.stderr, sys.stdin
    sink = _open(real_err)
    if sink is None:
        return command()
    started, began = datetime.now(UTC), time.monotonic()
    with sink.lock:
        sink.header(argv, started)
    previous_env = os.environ.get(SESSION_ENV)
    os.environ[SESSION_ENV] = str(sink.path)
    sys.stdout = cast(TextIO, _Tee(real_out, "stdout", sink))
    sys.stderr = cast(TextIO, _Tee(real_err, "stderr", sink))
    if real_in is not None:
        sys.stdin = cast(TextIO, _StdinTee(real_in, sink))
    previous_active, _active = _active, sink
    sigterm_installed = _install_sigterm()
    exit_code = 1
    outcome: str | None = None
    try:
        exit_code = command()
        return exit_code
    except _Terminated:
        exit_code, outcome = TERMINATED, "terminated (SIGTERM)"
        raise
    except SystemExit as error:
        exit_code = _exit_status(error.code)
        raise
    except KeyboardInterrupt:
        exit_code, outcome = INTERRUPTED, "interrupted (Ctrl-C)"
        raise
    except BaseException as error:
        outcome = f"exception: {type(error).__name__}"
        with sink.lock:  # the interpreter prints it for the terminal once this propagates
            sink.feed("stderr", traceback.format_exc())
        raise
    finally:
        if sigterm_installed:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        _active = previous_active
        sys.stdout, sys.stderr, sys.stdin = real_out, real_err, real_in
        if previous_env is None:
            os.environ.pop(SESSION_ENV, None)
        else:
            os.environ[SESSION_ENV] = previous_env
        sink.footer(started, began, exit_code, outcome)


def _install_sigterm() -> bool:
    """Route SIGTERM through ``SystemExit`` (main thread only, and only over the default action); True when the
    handler was installed and has to be put back."""
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGTERM) != signal.SIG_DFL:
        return False
    signal.signal(signal.SIGTERM, _on_sigterm)
    return True

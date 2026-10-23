"""Obtain root once, up front, or fail with the cause and the exact command to run instead.

Accessed via: ``reconcile.apply`` (``apply`` / ``init`` / ``update`` / ``fix`` commits) and
``commands/doctor.py`` (``--yes``) call ``RootSession.require`` before the first root action; the commands
create the session through ``commands/_support.root_session``. Spec: *Elevation*.

Assumptions:

- One ``RootSession`` per run. ``require(count, what)`` does nothing for ``count == 0`` and after the first
  success, so a plan that is rebuilt mid-run (a cloned profile, a staged pass) can call it again and still ask
  at most once. While the session is open ``process.elevate`` uses the prefix the strategy found working.
- The strategy is, first match wins: root; ``NoNewPrivs: 1`` (sudo and pkexec cannot work, whatever the
  sudoers say: ``RootUnavailable``); neither sudo nor pkexec (``RootUnavailable``); ``sudo -n true`` (NOPASSWD or
  a valid ticket); a controlling terminal: one explanatory line, then ``sudo -v`` once; ``SUDO_ASKPASS``:
  ``sudo -A -v``; pkexec and a graphical session or a terminal: ``pkexec true`` (polkit keeps no ticket, so it may ask per
  action); else ``RootUnavailable``. A refused sudo or pkexec (wrong password, timeout, not allowed) is
  ``RootUnavailable`` too, with no retry. Every sudo path then runs a thread that executes ``sudo -n -v`` every
  ``refresh_seconds``, so the one password (or a ticket that was about to expire) outlives long steps.
- ``RootUnavailable`` is raised before the first root action. It says "Nothing was changed" unless the caller
  passed ``ran_before=True`` (a rebuilt plan that gained root actions after earlier steps ran).

Public API:

- ``Host``: what the strategy looks at (``euid``, ``terminal``, ``askpass``, ``graphical``, ``no_new_privs``,
  ``tools``); ``Host.detect(environ=None)`` reads the real machine, tests build one by hand.
  ``parse_no_new_privs(status_text)``.
- ``Cause`` (``no-new-privs``, ``no-tool``, ``no-prompt``, ``rejected``) and ``RootUnavailable`` (exit code
  ``ExitCode.NO_ROOT``; ``cause``, ``fix`` = the command to run from a normal terminal).
- ``RootSession(*, command, skip=None, process=None, host=None, say=None, refresh_seconds=60.0)``: ``require``,
  ``prefix``, ``close``, and use as a context manager. ``command`` is what the user is told to run on failure,
  ``skip`` the alternative that skips the root actions (omitted from the message when ``None``).
- ``SUDO_CHECK``, ``SUDO_REFRESH``, ``SUDO_ASK``, ``SUDO_ASKPASS_ASK``, ``PKEXEC_CHECK``: the argv the strategy
  runs (tests answer them through ``FakeProcess``).
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from ubuntu_tweaks import ui
from ubuntu_tweaks.errors import ExitCode, UbuntuTweakError
from ubuntu_tweaks.process import CommandError, Process, use_root_prefix

SUDO_PROMPT = "[sudo] password for %p (ubuntu-tweak asks once for the whole run): "
SUDO_CHECK = ("sudo", "-n", "true")
SUDO_REFRESH = ("sudo", "-n", "-v")
SUDO_ASK = ("sudo", "-p", SUDO_PROMPT, "-v")
SUDO_ASKPASS_ASK = ("sudo", "-A", "-v")
PKEXEC_CHECK = ("pkexec", "true")

SUDO_PREFIX = ("sudo", "--")
SUDO_ASKPASS_PREFIX = ("sudo", "-A", "--")
PKEXEC_PREFIX = ("pkexec",)

NOTHING_CHANGED = "Nothing was changed."
EARLIER_STEPS_RAN = "Steps of this run that need no root already ran; running the command again is safe and goes on."
REFRESH_JOIN_SECONDS = 1.0
PROC_STATUS = "/proc/self/status"


class Cause(StrEnum):
    NO_NEW_PRIVS = "no-new-privs"
    NO_TOOL = "no-tool"
    NO_PROMPT = "no-prompt"
    REJECTED = "rejected"


class RootUnavailable(UbuntuTweakError):
    """Root is needed and cannot be obtained; ``fix`` is the command to run from a normal terminal."""

    exit_code = ExitCode.NO_ROOT

    def __init__(self, cause: Cause, message: str, fix: str) -> None:
        super().__init__(message)
        self.cause = cause
        self.fix = fix


def parse_no_new_privs(status_text: str) -> bool:
    for line in status_text.splitlines():
        name, _, value = line.partition(":")
        if name == "NoNewPrivs":
            return value.strip() == "1"
    return False


def _read_no_new_privs() -> bool:
    try:
        with open(PROC_STATUS, encoding="utf-8") as handle:
            return parse_no_new_privs(handle.read())
    except OSError:
        return False


def _has_terminal() -> bool:
    """A controlling terminal is what sudo prompts on (it opens ``/dev/tty``), whatever stdin is."""
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(descriptor)
    return True


@dataclass(frozen=True)
class Host:
    euid: int
    terminal: bool
    askpass: bool
    graphical: bool
    no_new_privs: bool
    tools: frozenset[str]  # which of "sudo" / "pkexec" are on PATH

    @classmethod
    def detect(cls, *, environ: Mapping[str, str] | None = None) -> Host:
        env = os.environ if environ is None else environ
        return cls(
            euid=os.geteuid(),
            terminal=_has_terminal(),
            askpass=bool(env.get("SUDO_ASKPASS")),
            graphical=bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")),
            no_new_privs=_read_no_new_privs(),
            tools=frozenset(tool for tool in ("sudo", "pkexec") if shutil.which(tool)),
        )


def _announce(text: str) -> None:
    print(ui.say("info", text), flush=True)


class RootSession:
    def __init__(
        self,
        *,
        command: str,
        skip: str | None = None,
        process: Process | None = None,
        host: Host | None = None,
        say: Callable[[str], None] | None = None,
        refresh_seconds: float = 60.0,
    ) -> None:
        self._command = command
        self._skip = skip
        self._process = process or Process()
        self._host = host
        self._say = say or _announce
        self._refresh_seconds = refresh_seconds
        self._prefix: tuple[str, ...] | None = None
        self._acquired = False
        self._ran_before = False
        self._stop = threading.Event()
        self._refresher: threading.Thread | None = None

    @property
    def prefix(self) -> tuple[str, ...] | None:
        return self._prefix

    def require(self, count: int, what: str, *, ran_before: bool = False) -> None:
        """Make sure ``count`` root actions (of the kinds in ``what``) can run; asks at most once per session.
        ``ran_before``: actions of this run already changed something (the error then says so)."""
        if count <= 0 or self._acquired:
            return
        self._ran_before = ran_before
        self._acquire(count, what)
        self._acquired = True

    def close(self) -> None:
        self._stop.set()
        if self._refresher is not None:
            self._refresher.join(timeout=REFRESH_JOIN_SECONDS)  # a hung `sudo -n -v` must not hold the exit
            self._refresher = None
        if self._prefix is not None:
            use_root_prefix(None)
            self._prefix = None

    def __enter__(self) -> RootSession:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()

    def _use(self, prefix: tuple[str, ...]) -> None:
        self._prefix = prefix
        use_root_prefix(prefix)

    def _acquire(self, count: int, what: str) -> None:
        host = self._host or Host.detect()
        if host.euid == 0:
            return
        if host.no_new_privs:
            raise self._unavailable(
                Cause.NO_NEW_PRIVS,
                "root is not available: this process runs with no_new_privs set (NoNewPrivs: 1 in "
                "/proc/self/status), so sudo and pkexec cannot work from here (a sandbox, a container or a "
                "restricted agent shell).",
            )
        if not host.tools:
            raise self._unavailable(
                Cause.NO_TOOL,
                "root is needed but neither sudo nor pkexec is installed. As root, run `apt-get install -y sudo` "
                "(for example `su -c 'apt-get install -y sudo'`).",
            )
        ahead = f"{count} root action(s) ahead ({what})."
        if "sudo" in host.tools and self._via_sudo(host, ahead):
            return
        if "pkexec" in host.tools and (host.graphical or host.terminal):
            self._say(f"{ahead} pkexec asks for your password (polkit may ask per action).")
            self._ask(PKEXEC_CHECK, "pkexec", PKEXEC_PREFIX)
            return
        raise self._unavailable(Cause.NO_PROMPT, self._no_prompt_reason(host))

    @staticmethod
    def _no_prompt_reason(host: Host) -> str:
        """Only reached when nothing could ask: sudo (if installed) has no terminal and no askpass, and pkexec is
        missing or has neither a terminal nor a graphical session; the reason says exactly that."""
        gaps = (
            ["sudo is not passwordless", "there is no terminal", "there is no SUDO_ASKPASS"]
            if "sudo" in host.tools
            else ["sudo is not installed"]
        )
        gaps.append(
            "pkexec has no terminal or graphical session to ask on"
            if "pkexec" in host.tools
            else "pkexec is not installed"
        )
        return "root is needed but nothing here can ask for a password: " + ", ".join(gaps) + "."

    def _via_sudo(self, host: Host, ahead: str) -> bool:
        """``True`` when sudo works now (NOPASSWD, a valid ticket, or the one password it just got)."""
        if self._process.query(SUDO_CHECK).ok:
            self._use(SUDO_ASKPASS_PREFIX if host.askpass and not host.terminal else SUDO_PREFIX)
            self._keep_alive()  # a ticket that is still valid may expire mid-run; harmless for NOPASSWD
            return True
        if host.terminal:
            self._say(f"{ahead} sudo asks for your password once; it covers the whole run.")
            self._ask(SUDO_ASK, "sudo", SUDO_PREFIX)
        elif host.askpass:
            self._say(f"{ahead} sudo asks for your password once through SUDO_ASKPASS.")
            self._ask(SUDO_ASKPASS_ASK, "sudo", SUDO_ASKPASS_PREFIX)
        else:
            return False
        self._keep_alive()
        return True

    def _ask(self, argv: tuple[str, ...], tool: str, prefix: tuple[str, ...]) -> None:
        try:
            self._process.execute(argv)
        except CommandError as error:
            raise self._unavailable(
                Cause.REJECTED,
                f"{tool} did not give root: wrong password, a timeout, or this user may not use {tool} ({error}).",
            ) from error
        self._use(prefix)

    def _keep_alive(self) -> None:
        self._stop.clear()
        self._refresher = threading.Thread(target=self._refresh, name="ubuntu-tweak-sudo-refresh", daemon=True)
        self._refresher.start()

    def _refresh(self) -> None:
        while not self._stop.wait(self._refresh_seconds):
            if not self._process.query(SUDO_REFRESH).ok:
                return  # the ticket is gone: a later sudo asks for itself

    def _unavailable(self, cause: Cause, reason: str) -> RootUnavailable:
        state = EARLIER_STEPS_RAN if self._ran_before else NOTHING_CHANGED
        lines = [f"{reason} {state}", f"Run this in a normal terminal: {self._command}"]
        if self._skip is not None:
            lines.append(f"Or skip the root actions (they are listed): {self._skip}")
        return RootUnavailable(cause, "\n".join(lines), self._command)

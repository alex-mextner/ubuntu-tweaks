"""Shared code of the ``earlyoom`` module: typed options, the rendered defaults file, the system and the user-level install.

Accessed via: ``modules/earlyoom/earlyoom`` (check / status / plan / install as root) and
``modules/earlyoom/earlyoom-user`` (the rootless fallback). Spec: docs/specs/earlyoom.md.

Assumptions: earlyoom reads ``EARLYOOM_ARGS`` from ``/etc/default/earlyoom`` through the systemd
``EnvironmentFile=`` of the Ubuntu package and matches ``--prefer`` / ``--avoid`` (POSIX extended regular
expressions compiled by libc ``regcomp``) against the process *name* (``/proc/PID/comm``, at most 15
characters), not the command line. Options reach the scripts as JSON in ``UBUNTU_TWEAK_MODULE_CONFIG``.

Public API:

- ``EarlyoomOptions`` and ``parse_options(raw)``: the typed ``[modules.earlyoom]`` table; ``ModuleError`` on an
  unknown key, a wrong type, an out-of-range number, an unsafe character or an invalid regex.
- ``posix_ere_error(pattern) -> str | None``: libc's own verdict on an expression (``None`` = valid).
- ``render_args(options, *, user_level=False)`` and ``render_defaults(options, *, user_level=False)``: the
  ``EARLYOOM_ARGS`` value and the whole file, byte-stable.
- ``SystemEarlyoom``: the root install (apt package, defaults file, service); its ``findings`` judge the rootless
  copy when that is all the machine has. ``StagedEarlyoom``: only the defaults file under ``EARLYOOM_ROOT``.
- ``UserEarlyoom``: the rootless copy (the archive's deb extracted under ``~/.local/opt/earlyoom`` plus a user unit).
- ``run(name, build, argv)``: the entry point both scripts share.
"""

from __future__ import annotations

import contextlib
import ctypes
import difflib
import functools
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.module_script import Finding, Module, ModuleError, apt_get, main, module_config, write_file
from ubuntu_tweaks.process import CommandError, Process

PACKAGE = "earlyoom"
SERVICE = "earlyoom.service"
SYSTEM_BINARY = Path("/usr/bin/earlyoom")
DEFAULTS_FILE = "etc/default/earlyoom"
USER_PREFIX = ".local/opt/earlyoom"
USER_ENV = ".config/earlyoom/earlyoom.env"
USER_UNIT = ".config/systemd/user/earlyoom.service"
ROOT_ENV = "EARLYOOM_ROOT"
CONFIG_ENV = "UBUNTU_TWEAK_MODULE_CONFIG"

# Processes whose death ends the desktop session or the machine's reachability. The names are the kernel's
# `comm` (15 characters: gnome-session-binary is gnome-session-b). The profile adds its own on top.
DEFAULT_AVOID = (
    "(^|/)(systemd|dbus-daemon|dbus-broker|gnome-shell|gnome-session-b|Xwayland|Xorg|gdm3|pipewire|wireplumber|sshd)$"
)

OPTION_NAMES = ("mem_percent", "swap_percent", "report_interval", "prefer", "avoid", "notify", "extra_args")
# Flags the typed options own. A second copy in `extra_args` would silently override the typed value: the short
# flags in any spelling (`-m5`, `-M 100`), the long ones with a value (`--prefer=x`).
# getopt_long, which earlyoom uses, also accepts a long option by any unambiguous prefix (`--pref=x`) and grouped
# short flags (`-gm5`): so every spelling that can set one of them is refused.
_MANAGED_SHORT = re.compile(r"-[^-]*[mMsSrn].*")
_MANAGED_LONG_NAMES = ("prefer", "avoid")
# `$` is only safe at the end of the text or before `|` / `)`: anything else can be a shell parameter ($$, $?, $1, $name)
_UNSAFE_DOLLAR = re.compile(r"\$(?![|)]|\Z)")
_REG_EXTENDED = 1  # <regex.h>: what earlyoom compiles with (REG_EXTENDED | REG_NOSUB)
_REG_NOSUB = 8
_REGEX_T_SIZE = 256  # regex_t is 64 bytes in glibc; room to spare
_ERRBUF_SIZE = 256
_HEADER = (
    "# Managed by ubuntu-tweaks (modules/earlyoom, [modules.earlyoom] in the profile): edit the profile, not {where}.\n"
    "# Read by earlyoom.service as its EnvironmentFile; `earlyoom -h` lists the flags.\n"
)


@functools.cache
def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None)
    if not hasattr(libc, "regcomp"):
        raise ModuleError("libc has no regcomp: cannot validate the earlyoom regular expressions")
    libc.regcomp.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    libc.regcomp.restype = ctypes.c_int
    libc.regerror.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    libc.regerror.restype = ctypes.c_size_t
    libc.regfree.argtypes = [ctypes.c_char_p]
    libc.regfree.restype = None
    return libc


def posix_ere_error(pattern: str) -> str | None:
    """libc ``regcomp`` (REG_EXTENDED | REG_NOSUB, the flags earlyoom uses) on the pattern: its message, or None."""
    libc = _libc()
    compiled = ctypes.create_string_buffer(_REGEX_T_SIZE)
    code = libc.regcomp(compiled, pattern.encode("utf-8"), _REG_EXTENDED | _REG_NOSUB)
    if code == 0:
        libc.regfree(compiled)
        return None
    message = ctypes.create_string_buffer(_ERRBUF_SIZE)
    libc.regerror(code, compiled, message, _ERRBUF_SIZE)
    return message.value.decode("utf-8", errors="replace")


def _unsafe(value: str) -> str | None:
    """Why the text cannot sit inside the double-quoted, sh-compatible ``EARLYOOM_ARGS="..."`` line, or None."""
    for char in value:
        if not 0x20 <= ord(char) <= 0x7E:
            return f"character {char!r} is not printable ASCII (no newline, tab or control character)"
    for char, why in (("'", "single quote"), ('"', "double quote"), ("\\", "backslash"), ("`", "backtick")):
        if char in value:
            return f"a {why} cannot be written safely into the defaults file"
    if _UNSAFE_DOLLAR.search(value):
        return "`$` is only allowed at the end of the expression or before `|` or `)` (the shell that reads the defaults file would expand it)"
    return None


def _percent(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModuleError(f"[modules.earlyoom] {name} must be a number, got {value!r}")
    if not 0 < value <= 100:
        raise ModuleError(f"[modules.earlyoom] {name} must be above 0 and at most 100 (percent), got {value!r}")
    return value


def _regex(name: str, value: object) -> str | None:
    if not isinstance(value, str):
        raise ModuleError(f"[modules.earlyoom] {name} must be a string (a POSIX extended regular expression)")
    if not value:
        return None  # an empty string switches the flag off
    problem = _unsafe(value)
    if problem:
        raise ModuleError(f"[modules.earlyoom] {name}: {problem}")
    error = posix_ere_error(value)
    if error:
        raise ModuleError(f"[modules.earlyoom] {name} is not a valid POSIX extended regular expression: {error}")
    return value


def _extra_word(word: str) -> str:
    problem = (
        _unsafe(word) or ("an empty word" if not word else None) or ("a word with a space" if " " in word else None)
    )
    if problem:
        raise ModuleError(f"[modules.earlyoom] extra_args {word!r}: {problem}")
    long_name = word[2:].partition("=")[0] if word.startswith("--") else ""
    if _MANAGED_SHORT.fullmatch(word) or (
        long_name and any(name.startswith(long_name) for name in _MANAGED_LONG_NAMES)
    ):
        raise ModuleError(
            f"[modules.earlyoom] extra_args {word!r} repeats a flag the typed options own "
            "(mem_percent, swap_percent, report_interval, prefer, avoid, notify)"
        )
    return word


@dataclass(frozen=True)
class EarlyoomOptions:
    mem_percent: float = 10
    swap_percent: float = 10
    report_interval: int = 3600
    prefer: str | None = None
    avoid: str | None = DEFAULT_AVOID
    notify: bool = False
    extra_args: tuple[str, ...] = ()


def parse_options(raw: Mapping[str, object]) -> EarlyoomOptions:
    unknown = sorted(set(raw) - set(OPTION_NAMES))
    if unknown:
        raise ModuleError(
            f"[modules.earlyoom] unknown option(s): {', '.join(unknown)} (known: {', '.join(OPTION_NAMES)})"
        )
    defaults = EarlyoomOptions()
    interval = raw.get("report_interval", defaults.report_interval)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 0:
        raise ModuleError(
            f"[modules.earlyoom] report_interval must be a whole number of seconds, 0 or more, got {interval!r}"
        )
    notify = raw.get("notify", defaults.notify)
    if not isinstance(notify, bool):
        raise ModuleError(f"[modules.earlyoom] notify must be true or false, got {notify!r}")
    extra = raw.get("extra_args", [])
    if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
        raise ModuleError("[modules.earlyoom] extra_args must be a list of strings, one per command-line word")
    return EarlyoomOptions(
        mem_percent=_percent("mem_percent", raw.get("mem_percent", defaults.mem_percent)),
        swap_percent=_percent("swap_percent", raw.get("swap_percent", defaults.swap_percent)),
        report_interval=interval,
        prefer=_regex("prefer", raw["prefer"]) if "prefer" in raw else defaults.prefer,
        avoid=_regex("avoid", raw["avoid"]) if "avoid" in raw else defaults.avoid,
        notify=notify,
        extra_args=tuple(_extra_word(word) for word in extra),
    )


def _number(value: float) -> str:
    """A whole number without a point (``4``), a fraction as Python prints it, round-trip exact (``2.1234567``)."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def render_args(options: EarlyoomOptions, *, user_level: bool = False) -> str:
    """The ``EARLYOOM_ARGS`` value. A user-level earlyoom cannot signal root's processes, so it never picks one."""
    words = [
        f"-m {_number(options.mem_percent)}",
        f"-s {_number(options.swap_percent)}",
        f"-r {options.report_interval}",
    ]
    if options.prefer:
        words.append(f"--prefer '{options.prefer}'")
    if options.avoid:
        words.append(f"--avoid '{options.avoid}'")
    if options.notify:
        words.append("-n")
    if user_level:
        words.append("--ignore-root-user")
    words.extend(options.extra_args)
    return " ".join(words)


def render_defaults(options: EarlyoomOptions, *, user_level: bool = False) -> str:
    where = "~/.config/earlyoom/earlyoom.env" if user_level else "/etc/default/earlyoom"
    return _HEADER.format(where=where) + f'EARLYOOM_ARGS="{render_args(options, user_level=user_level)}"\n'


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _diff(label: str, before: str | None, after: str) -> list[str]:
    old = (before or "").splitlines()
    lines = difflib.unified_diff(old, after.splitlines(), f"{label} (installed)", f"{label} (new)", lineterm="", n=0)
    return [f"  {line}" for line in lines]


class _Systemd:
    """The systemctl questions both installs ask, of the system or the user manager."""

    def __init__(self, process: Process, user_manager: bool) -> None:
        self._process = process
        self._prefix = ["systemctl", "--user"] if user_manager else ["systemctl"]

    def _is(self, verb: str, wanted: str) -> bool:
        result = self._process.query([*self._prefix, verb, SERVICE])
        return result.returncode == 0 and result.stdout.splitlines()[:1] == [wanted]

    def is_enabled(self) -> bool:
        return self._is("is-enabled", "enabled")

    def is_active(self) -> bool:
        return self._is("is-active", "active")

    def run(self, *argv: str) -> None:
        self._process.execute([*self._prefix, *argv])


def _bring_up(
    systemd: _Systemd, settle: float, *, changed: bool, restore: Callable[[], None] | None, what: str
) -> None:
    """Enable the service, start it, restart it when its options changed, and prove it stays up.

    earlyoom exits at once on a flag it rejects, and a last line of defence must never end up stopped by the module
    that configures it: with ``restore`` the previous options are put back and the service restarted before the
    failure is reported."""
    if not systemd.is_enabled():
        systemd.run("enable", SERVICE)
    was_active = systemd.is_active()
    if not was_active:
        systemd.run("start", SERVICE)
    elif changed:
        systemd.run("restart", SERVICE)
    else:
        return
    time.sleep(settle)  # a rejected flag makes earlyoom exit at once; look again after it had the chance
    if systemd.is_active():
        return
    if changed and restore is not None:
        restore()
        # a flag earlyoom rejects makes it crash-loop into systemd's start limit, which refuses a plain restart
        with contextlib.suppress(CommandError):  # the service is down already: the error below says why
            systemd.run("reset-failed", SERVICE)
            systemd.run("restart", SERVICE)
        outcome = "the previous options were put back" + ("" if systemd.is_active() else ", but it is still down")
        raise ModuleError(f"{what} did not stay active with the new options (see its journal); {outcome}")
    raise ModuleError(f"{what} does not stay active even with its options in place: see its journal")


class UserEarlyoom(Module):
    """The rootless copy: ``apt-get download earlyoom`` needs no root; the binary runs from ``~/.local/opt/earlyoom``."""

    name = "earlyoom-user"

    def __init__(
        self,
        process: Process,
        home: Path,
        options: EarlyoomOptions,
        *,
        settle: float = 1.0,
        system_binary: Path = SYSTEM_BINARY,
        owner: str = "",
    ) -> None:
        self._process = process
        self._home = home
        self._owner = owner  # the desktop user's name: how root reaches that user's systemd manager
        self._options = options
        self._settle = settle
        self._system_binary = system_binary
        self._systemd = _Systemd(process, True)

    @property
    def binary(self) -> Path:
        return self._home / USER_PREFIX / "usr/bin/earlyoom"

    def present(self) -> bool:
        return os.access(self.binary, os.X_OK)

    def running_beside_system(self) -> bool:
        """A user copy that still runs while the package's service owns earlyoom: two daemons would both act."""
        return (self._home / USER_UNIT).exists() and self._systemd.is_active()

    def retire(self) -> None:
        """Stop and disable the user copy, from root: once the system service exists the copy must not run beside it.
        Best effort: the user's manager may not be running (no session), and then there is nothing to stop."""
        if not (self._home / USER_UNIT).exists():
            return  # no query here: root asking `systemctl --user` would reach root's own manager, not the user's
        if not self._owner:
            raise ModuleError("the rootless earlyoom copy runs beside the system service and the user is unknown")
        with contextlib.suppress(CommandError):
            self._process.execute(
                ["systemctl", "--user", "--machine", f"{self._owner}@.host", "disable", "--now", SERVICE]
            )

    def _env_text(self) -> str:
        return render_defaults(self._options, user_level=True)

    def _unit_text(self) -> str:
        return (
            "# Managed by ubuntu-tweaks (modules/earlyoom, earlyoom-user): the rootless copy of earlyoom.\n"
            "[Unit]\n"
            "Description=earlyoom, user-level copy (acts only on this user's processes)\n"
            "Documentation=man:earlyoom(1)\n"
            "# the system package wins as soon as it is installed\n"
            f"ConditionPathExists=!{SYSTEM_BINARY}\n"
            "\n"
            "[Service]\n"
            f"EnvironmentFile=%h/{USER_ENV}\n"
            f"ExecStart=%h/{USER_PREFIX}/usr/bin/earlyoom $EARLYOOM_ARGS\n"
            "Restart=always\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def findings(self) -> list[Finding]:
        return [
            Finding(self.present(), f"~/{USER_PREFIX}/usr/bin/earlyoom is installed (rootless copy)"),
            Finding(_read(self._home / USER_ENV) == self._env_text(), f"~/{USER_ENV} has the rendered options"),
            Finding(_read(self._home / USER_UNIT) == self._unit_text(), f"~/{USER_UNIT} is the managed unit"),
            Finding(self._systemd.is_enabled(), "earlyoom.service (user) is enabled"),
            Finding(self._systemd.is_active(), "earlyoom.service (user) is active"),
        ]

    def plan(self) -> list[str]:
        lines: list[str] = []
        if not self.present():
            lines.append(f"apt-get download earlyoom (no root), extract it into ~/{USER_PREFIX}")
        for relative, text in ((USER_ENV, self._env_text()), (USER_UNIT, self._unit_text())):
            before = _read(self._home / relative)
            if before != text:
                lines.append(f"{'write' if before is not None else 'create'} ~/{relative}")
                lines.extend(_diff(f"~/{relative}", before, text))
        enabled, active = self._systemd.is_enabled(), self._systemd.is_active()
        if not enabled:
            lines.append("systemctl --user enable earlyoom.service")
        if not active:
            lines.append("systemctl --user start earlyoom.service")
        return lines or ["nothing to change"]

    def _extract(self) -> None:
        prefix = self._home / USER_PREFIX
        prefix.parent.mkdir(parents=True, exist_ok=True)
        # staged next to the final place (same filesystem), so the last step is one rename
        with tempfile.TemporaryDirectory(prefix=".earlyoom-", dir=prefix.parent) as scratch:
            apt_get(self._process, "download", PACKAGE, cwd=Path(scratch))
            debs = sorted(Path(scratch).glob(f"{PACKAGE}_*.deb"))
            if not debs:
                raise ModuleError("apt-get download earlyoom produced no .deb file")
            staging = Path(scratch) / "root"
            self._process.execute(["dpkg-deb", "-x", str(debs[-1]), str(staging)])
            if not (staging / "usr/bin/earlyoom").is_file():
                raise ModuleError("the downloaded package has no usr/bin/earlyoom")
            if prefix.exists():
                shutil.rmtree(prefix)  # no working binary there (this runs only while it is missing)
            os.replace(staging, prefix)

    def apply(self) -> None:
        if self._system_binary.exists():
            raise ModuleError(
                "the earlyoom package is installed, so its system service owns earlyoom and this user copy would not "
                "start (ConditionPathExists): the root install is the one to repair (run apply with sudo available)"
            )
        try:
            if not self.present():
                self._extract()
            files = {self._home / USER_ENV: self._env_text(), self._home / USER_UNIT: self._unit_text()}
            previous = {path: _read(path) for path in files}
            changed = [path for path, text in files.items() if previous[path] != text]
            for path in changed:
                write_file(path, files[path])
            if self._home / USER_UNIT in changed:
                self._systemd.run("daemon-reload")

            def restore() -> None:
                for path in changed:
                    if previous[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_file(path, previous[path])
                with contextlib.suppress(CommandError):
                    self._systemd.run("daemon-reload")

            _bring_up(
                self._systemd, self._settle, changed=bool(changed), restore=restore, what="earlyoom.service (user)"
            )
        except CommandError as error:
            raise ModuleError(str(error)) from error


class SystemEarlyoom(Module):
    """apt package, ``/etc/default/earlyoom`` rendered from the options, the service enabled and running."""

    name = "earlyoom"

    def __init__(
        self,
        process: Process,
        root: Path,
        options: EarlyoomOptions,
        *,
        is_root: bool,
        user: UserEarlyoom | None = None,
        settle: float = 1.0,
    ) -> None:
        self._process = process
        self._root = root
        self._options = options
        self._is_root = is_root
        self._user = user
        self._settle = settle
        self._systemd = _Systemd(process, False)

    @property
    def defaults_path(self) -> Path:
        return self._root / DEFAULTS_FILE

    def _installed(self) -> bool:
        result = self._process.query(["dpkg-query", "-W", "-f=${Status}", PACKAGE])
        return result.returncode == 0 and "install ok installed" in result.stdout

    def _rootless_copy(self) -> UserEarlyoom | None:
        """The user copy, when it is all the machine has: then it is what ``check`` and ``status`` judge."""
        if self._user is not None and self._user.present() and not self._installed():
            return self._user
        return None

    def findings(self) -> list[Finding]:
        if (copy := self._rootless_copy()) is not None:
            return copy.findings()
        found = [
            Finding(self._installed(), "the earlyoom package is installed"),
            Finding(
                _read(self.defaults_path) == render_defaults(self._options),
                "/etc/default/earlyoom has the rendered options",
            ),
            Finding(self._systemd.is_enabled(), "earlyoom.service is enabled"),
            Finding(self._systemd.is_active(), "earlyoom.service is active"),
        ]
        if self._user is not None and self._user.running_beside_system():
            found.append(
                Finding(
                    False, "no rootless copy of earlyoom runs beside the service", "run apply with root to retire it"
                )
            )
        return found

    def plan(self) -> list[str]:
        text = render_defaults(self._options)
        before = _read(self.defaults_path)
        installed = self._installed()
        lines: list[str] = []
        if not installed:
            lines.append("apt-get install earlyoom (keeps this file over the package's)")
        if before != text:
            lines.append(f"{'write' if before is not None else 'create'} /etc/default/earlyoom")
            lines.extend(_diff("/etc/default/earlyoom", before, text))
        if not installed:
            # the package enables and starts the service with its stock file: ours replaces it, so one restart
            lines.append("systemctl restart earlyoom.service (the package starts it with its stock options)")
        else:
            enabled, active = self._systemd.is_enabled(), self._systemd.is_active()
            if not enabled:
                lines.append("systemctl enable earlyoom.service")
            if not active:
                lines.append("systemctl start earlyoom.service")
            elif before != text:
                lines.append("systemctl restart earlyoom.service (the options changed)")
        if self._user is not None and self._user.running_beside_system():
            lines.append(
                "systemctl --user disable --now earlyoom.service (retire the rootless copy the service replaces)"
            )
        return lines or ["nothing to change"]

    def apply(self) -> None:
        if not self._is_root:
            raise ModuleError("must run as root (ubuntu-tweak runs it through sudo)")
        try:
            if not self._installed():
                # --force-confold: a later package upgrade must not stop to ask about the file this module owns
                apt_get(self._process, "install", "-y", "-o", "Dpkg::Options::=--force-confold", PACKAGE)
            previous = _read(self.defaults_path)
            changed = previous != render_defaults(self._options)
            if changed:
                write_file(self.defaults_path, render_defaults(self._options))

            def restore() -> None:
                if previous is None:
                    self.defaults_path.unlink(missing_ok=True)
                else:
                    write_file(self.defaults_path, previous)

            _bring_up(self._systemd, self._settle, changed=changed, restore=restore, what="earlyoom.service")
            if self._user is not None:
                self._user.retire()  # two earlyoom daemons would both act on one low-memory event
        except CommandError as error:
            raise ModuleError(str(error)) from error


class StagedEarlyoom(Module):
    """``EARLYOOM_ROOT=DIR``: only ``DIR/etc/default/earlyoom``; no package, no systemd, no root."""

    name = "earlyoom"

    def __init__(self, root: Path, options: EarlyoomOptions) -> None:
        self._path = root / DEFAULTS_FILE
        self._text = render_defaults(options)

    def findings(self) -> list[Finding]:
        return [Finding(_read(self._path) == self._text, "/etc/default/earlyoom has the rendered options")]

    def plan(self) -> list[str]:
        before = _read(self._path)
        if before == self._text:
            return ["nothing to change"]
        return [
            f"{'write' if before is not None else 'create'} /etc/default/earlyoom",
            *_diff("/etc/default/earlyoom", before, self._text),
        ]

    def apply(self) -> None:
        if _read(self._path) != self._text:
            write_file(self._path, self._text)


def run(name: str, build: Callable[[Mapping[str, str]], Module], argv: Sequence[str]) -> int:
    """The entry point of both scripts: build the module from the environment, dispatch ``argv``."""
    try:
        module = build(os.environ)
    except ModuleError as error:
        sys.stderr.write(f"{name}: {error}\n")
        return 1
    return main(module, argv, out=sys.stdout, err=sys.stderr)


def options_from(env: Mapping[str, str]) -> EarlyoomOptions:
    return parse_options(module_config(env))

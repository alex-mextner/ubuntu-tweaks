"""Scaffolding for system modules whose installer and checker are one Python script.

Accessed via: ``modules/ssh-server/ssh-server``, ``modules/passwords-app/passwords-app``,
``modules/passwords-app-polkit/passwords-app-polkit``, ``modules/firefox/firefox``,
``modules/firefox-policy/firefox-policy``, ``modules/firefox-autologin/firefox-autologin`` and
``modules/firefox-passkey-qr/firefox-passkey-qr``. Their manifests name ``<script> install`` and
``<script> check`` (spec, *Source kinds*, ``system``); ``status`` is the human view of the same findings.

Assumptions: a script never reads the profile; per-module options come from the engine in
``UBUNTU_TWEAK_MODULE_CONFIG`` (JSON of the merged ``[modules.<name>]`` table, unset means no options).
The one place a secret enters a script is ``keyring_secret`` (the desktop user's Secret Service, at run
time, in memory only). Root scripts run through sudo/pkexec, so the desktop user is found through
``SUDO_USER`` / ``PKEXEC_UID``, never through ``$HOME``. ``check`` and ``status`` only read; an
``apply`` that cannot finish without a human reports it by raising ``NeedsManual``.

Public API:

- ``EXIT_OK`` 0, ``EXIT_FAILED`` 1, ``EXIT_USAGE`` 2, ``EXIT_DRIFT`` 3 (the checker protocol),
  ``EXIT_NEEDS_MANUAL`` 10 (the install could not finish without a human step).
- ``module_config(env) -> dict``: the options table; ``ModuleError`` (exit 1) when it is not a JSON object.
- ``Account(name, home, uid)`` and ``invoking_account(env, *, lookup_name, lookup_uid, euid) -> Account``.
- ``Finding(ok, subject, detail="", manual=False)``: one verified fact; ``manual`` marks an unmet fact
  that no installer run can fix (a missing signing key).
- ``Module`` (ABC): ``findings()`` (read-only) and ``apply()``.
- ``NeedsManual(message)``, ``ModuleError(message)``.
- ``write_file(path, data, mode)``: atomic write (``str`` or ``bytes``) whose final mode is set before the rename.
- ``state_dir(home, env) -> Path``: ``<XDG_STATE_HOME or ~/.local/state>/ubuntu-tweaks``. The service modules
  pass an empty ``env``: their state must be found by a root script too, which never sees the user's variables.
- ``environment(**overrides)``: context manager that sets variables for the child processes started inside
  it and restores the previous values on exit (also on failure).
- ``read_owned_file(path, uid, limit)``: what a root script may read from the desktop user's tree: a regular
  file (no symlink at the end), owned by ``uid``, at most ``limit`` bytes; anything else is ``ModuleError``.
- ``keyring_secret(process, purpose) -> str | None``: ``secret-tool lookup application ubuntu-tweaks
  purpose <purpose>``; None when absent, empty or ``secret-tool`` is missing. Never printed or logged.
- ``main(module, argv, *, out, err) -> int``: the ``check`` / ``status`` / ``plan`` / ``install`` dispatcher;
  ``Module.plan()`` (default: nothing) is the read-only preview of what ``install`` will do.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ubuntu_tweaks.errors import UbuntuTweakError
from ubuntu_tweaks.process import Process

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DRIFT = 3
EXIT_NEEDS_MANUAL = 10

CONFIG_ENV = "UBUNTU_TWEAK_MODULE_CONFIG"
USAGE = "usage: {name} check|status|plan|install"
KEYRING_APPLICATION = "ubuntu-tweaks"


class ModuleError(Exception):
    """The installer cannot do its job (exit 1); the message is shown as is."""


class NeedsManual(Exception):
    """The install is blocked on a human step (exit 10); the message names it."""


@dataclass(frozen=True)
class Account:
    name: str
    home: Path
    uid: int


@dataclass(frozen=True)
class Finding:
    ok: bool
    subject: str
    detail: str = ""
    manual: bool = False

    def line(self) -> str:
        mark = "ok     " if self.ok else ("manual " if self.manual else "missing")
        return f"{mark} {self.subject}" + (f": {self.detail}" if self.detail else "")


class Module(ABC):
    name: str

    @abstractmethod
    def findings(self) -> list[Finding]:
        """Every fact the module declares, verified read-only."""

    @abstractmethod
    def apply(self) -> None:
        """Make ``findings()`` all ``ok``; raise ``NeedsManual`` for a step only a human can do."""

    def plan(self) -> list[str]:
        """What ``install`` is going to do, one line each; read-only. Default: nothing to add to the action line."""
        return []


def module_config(env: Mapping[str, str]) -> dict[str, object]:
    text = env.get(CONFIG_ENV, "")
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ModuleError(f"{CONFIG_ENV} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ModuleError(f"{CONFIG_ENV} must be a JSON object, got {type(value).__name__}")
    return value


def invoking_account(
    env: Mapping[str, str],
    *,
    lookup_name: Callable[[str], pwd.struct_passwd] = pwd.getpwnam,
    lookup_uid: Callable[[int], pwd.struct_passwd] = pwd.getpwuid,
    euid: int | None = None,
) -> Account:
    """The desktop user a root script acts for: ``SUDO_USER``, else ``PKEXEC_UID``, else the caller itself."""
    current = os.geteuid() if euid is None else euid
    try:
        if current == 0 and env.get("SUDO_USER") and env["SUDO_USER"] != "root":
            entry = lookup_name(env["SUDO_USER"])
        elif current == 0 and env.get("PKEXEC_UID", "").isdigit() and int(env["PKEXEC_UID"]) != 0:
            entry = lookup_uid(int(env["PKEXEC_UID"]))
        else:
            entry = lookup_uid(current)
    except KeyError as error:
        raise ModuleError(f"cannot find the invoking user: {error}") from error
    return Account(entry.pw_name, Path(entry.pw_dir), entry.pw_uid)


def write_file(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    """Atomic write whose final mode is set before the rename, so no reader ever sees the temporary mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def state_dir(home: Path, env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_STATE_HOME")
    return (Path(configured) if configured else home / ".local/state") / "ubuntu-tweaks"


@contextmanager
def environment(**overrides: str) -> Iterator[None]:
    """Set variables for the child processes started inside the block; restore the old values afterwards."""
    previous = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def read_owned_file(path: Path, uid: int, limit: int) -> bytes:
    """Bytes of a regular file the desktop user owns, read without following a final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise ModuleError(f"cannot read {path}: {error.strerror}") from error
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > limit:
        os.close(descriptor)
        raise ModuleError(f"refusing {path}: not a regular file owned by uid {uid} of at most {limit} bytes")
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read(limit + 1)


def keyring_secret(process: Process, purpose: str) -> str | None:
    """A secret of the desktop user's keyring, looked up at run time and kept in memory only."""
    result = process.query(
        ["secret-tool", "lookup", "application", KEYRING_APPLICATION, "purpose", purpose], timeout=20
    )
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def main(module: Module, argv: Sequence[str], *, out: TextIO, err: TextIO) -> int:
    script = module.name
    if len(argv) != 1 or argv[0] not in ("check", "status", "plan", "install"):
        err.write(USAGE.format(name=script) + "\n")
        return EXIT_USAGE
    command = argv[0]
    try:
        if command == "install":
            module.apply()
            return EXIT_OK
        if command == "plan":
            for line in module.plan():
                out.write(line + "\n")
            return EXIT_OK
        findings = module.findings()
    except NeedsManual as blocked:
        out.write(f"needs manual: {blocked}\n")
        return EXIT_NEEDS_MANUAL
    except (ModuleError, UbuntuTweakError, OSError) as error:
        err.write(f"{script}: {error}\n")
        return EXIT_FAILED
    unmet = [finding for finding in findings if not finding.ok]
    for finding in findings if command == "status" else unmet:
        out.write(finding.line() + "\n")
    return EXIT_DRIFT if unmet else EXIT_OK

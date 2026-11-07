"""Shared code of the ``session-priority`` module: keep the desktop responsive when the machine is overloaded.

Accessed via: ``modules/session-priority/session-priority`` (check / status / plan / install, as the desktop user).
Spec: docs/specs/ubuntu-tweak-cli.md, *Session priority*.

Two user-level systemd drop-ins under ``~/.config/systemd/user`` (no root; undo: delete the files, ``systemctl --user
daemon-reload``):

* ``org.gnome.Shell@.service.d``: ``MemorySwapMax=0``. The compositor's memory is never pushed to swap again, so the
  pointer and the frame clock do not wait for the swap file (the pages already swapped out come back on first use).
* ``session.slice.d``: ``CPUWeight``. The shell lives in ``session.slice``; terminal tools and agents live in sibling
  slices (``omp.slice``, ``app.slice``) with the default weight 100, so under a CPU storm the shell wins the contention.

GNOME Shell on Wayland cannot be restarted without ending the session, and does not need to be: the user manager's
``daemon-reload`` re-applies cgroup settings to running units. ``check`` reads the kernel's own files back
(``memory.swap.max``, ``cpu.weight`` under ``/sys/fs/cgroup``), not only what systemd says it loaded (observed on
the live machine after the reload: ``memory.swap.max`` 0 for the shell's unit, ``cpu.weight`` 300 for ``session.slice``).
A setting another drop-in or a runtime ``set-property`` overrides stays ``manual``: ``install`` stops with
``needs manual`` instead of looping.
The indexer needs no drop-in: ``localsearch-3`` already runs itself at nice 19 and the idle I/O class (measured).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ubuntu_tweaks.module_script import (
    Finding,
    Module,
    ModuleError,
    NeedsManual,
    read_text_or_none,
    unified_diff_lines,
    write_file,
)
from ubuntu_tweaks.process import CommandError, CommandResult, Process

CGROUP_ROOT = Path("/sys/fs/cgroup")
DROPIN_NAME = "50-ubuntu-tweaks-session-priority.conf"
USER_UNITS = ".config/systemd/user"
SHELL_TEMPLATE = "org.gnome.Shell@.service"
SHELL_INSTANCES = "org.gnome.Shell@*.service"
SESSION_SLICE = "session.slice"

RELOAD_SETTLE_POLLS = 5
RELOAD_SETTLE_SECONDS = 0.4
CPU_WEIGHT_SESSION = 300  # default 100; omp.slice / app.slice siblings keep 100


@dataclass(frozen=True)
class Dropin:
    unit: str  # the unit name the drop-in directory belongs to (a template for the shell)
    section: str
    key: str  # the property; ``systemctl show`` prints its value as written
    value: str
    cgroup_file: str  # the kernel file under the unit's control group that carries the value

    def text(self) -> str:
        return (
            "# Managed by ubuntu-tweak (modules/session-priority); delete this file to undo.\n"
            f"[{self.section}]\n{self.key}={self.value}\n"
        )


class Mismatch(NamedTuple):
    unit: str
    what: str
    wanted: str
    actual: str

    @property
    def detail(self) -> str:
        return f"{self.what} is {self.actual}, want {self.wanted}"

    def __str__(self) -> str:
        return f"{self.unit} {self.detail}"


DROPINS = (
    Dropin(SHELL_TEMPLATE, "Service", "MemorySwapMax", "0", "memory.swap.max"),
    Dropin(SESSION_SLICE, "Slice", "CPUWeight", str(CPU_WEIGHT_SESSION), "cpu.weight"),
)


def parse_show(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


class SessionPriority(Module):
    name = "session-priority"

    def __init__(self, home: Path, process: Process, cgroup_root: Path = CGROUP_ROOT) -> None:
        self._home = home
        self._process = process
        self._cgroup_root = cgroup_root

    def path(self, dropin: Dropin) -> Path:
        return self._home / USER_UNITS / f"{dropin.unit}.d" / DROPIN_NAME

    def _shown(self, path: Path) -> str:
        return "~/" + str(path.relative_to(self._home))

    # ---- the running units ----

    def _systemctl(self, *arguments: str) -> CommandResult:
        return self._process.query(["systemctl", "--user", *arguments])

    def _active(self, unit: str) -> bool:
        result = self._systemctl("is-active", unit)
        state = result.stdout.strip()
        if not state:  # is-active always names the state; silence means the user manager is unreachable
            raise ModuleError(f"systemctl --user is-active {unit} failed: {result.stderr.strip() or result.returncode}")
        return state == "active"

    def _concrete_units(self, dropin: Dropin) -> list[str]:
        """The running units a drop-in governs: every active shell instance for the template, else the unit if active."""
        if dropin.unit == SHELL_TEMPLATE:
            listed = self._systemctl("list-units", SHELL_INSTANCES, "--state=active", "--plain", "--no-legend")
            if not listed.ok:
                raise ModuleError(f"systemctl --user list-units failed: {listed.stderr.strip() or listed.returncode}")
            return [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]
        return [dropin.unit] if self._active(dropin.unit) else []

    def _live_mismatches(self, dropin: Dropin, unit: str) -> list[Mismatch]:
        """Where the running ``unit`` differs from the drop-in: systemd's loaded value, then the kernel's own file."""
        result = self._systemctl("show", unit, f"--property={dropin.key}", "--property=ControlGroup")
        if not result.ok:
            raise ModuleError(f"systemctl --user show {unit} failed: {result.stderr.strip() or result.returncode}")
        actual = parse_show(result.stdout)
        wrong: list[Mismatch] = []
        if actual.get(dropin.key) != dropin.value:
            wrong.append(Mismatch(unit, dropin.key, dropin.value, actual.get(dropin.key, "?")))
        control_group = actual.get("ControlGroup", "")
        kernel = (
            read_text_or_none(self._cgroup_root / control_group.lstrip("/") / dropin.cgroup_file)
            if control_group
            else None
        )
        seen = kernel.strip() if kernel is not None else "missing"
        if seen != dropin.value:
            wrong.append(Mismatch(unit, dropin.cgroup_file, dropin.value, seen))
        return wrong

    def _all_mismatches(self) -> list[Mismatch]:
        return [
            mismatch
            for dropin in DROPINS
            for unit in self._concrete_units(dropin)
            for mismatch in self._live_mismatches(dropin, unit)
        ]

    def _settled_mismatches(self) -> list[Mismatch]:
        """The mismatches after a reload, giving the manager a moment to write the new values into the cgroup files."""
        for _ in range(RELOAD_SETTLE_POLLS):
            mismatches = self._all_mismatches()
            if not mismatches:
                return []
            time.sleep(RELOAD_SETTLE_SECONDS)
        return self._all_mismatches()

    # ---- Module protocol ----

    def findings(self) -> list[Finding]:
        findings: list[Finding] = []
        for dropin in DROPINS:
            path = self.path(dropin)
            findings.append(Finding(read_text_or_none(path) == dropin.text(), self._shown(path)))
            try:
                units = self._concrete_units(dropin)
                if not units and dropin.unit == SHELL_TEMPLATE:
                    detail = "no org.gnome.Shell@ instance is active: the drop-in applies at its next start"
                    findings.append(Finding(True, "running GNOME Shell", detail))
                for unit in units:
                    wrong = self._live_mismatches(dropin, unit)
                    detail = ", ".join(m.detail for m in wrong)
                    findings.append(Finding(not wrong, f"running {unit}", detail))
            except ModuleError as error:
                # no user manager to ask: drift (exit 3, the engine's plan goes on), never a pass
                findings.append(Finding(False, f"running {dropin.unit}", str(error)))
        # `manual` is never set here: only a reload tells a fixable drift from an override, and install reports that
        return findings

    def plan(self) -> list[str]:
        lines: list[str] = []
        for dropin in DROPINS:
            path = self.path(dropin)
            old = read_text_or_none(path)
            if old != dropin.text():
                lines.extend(unified_diff_lines(self._shown(path), old, dropin.text()))
        try:
            mismatches = self._all_mismatches()
        except ModuleError as error:
            return [*lines, "    systemctl --user daemon-reload", f"    (live state unreadable: {error})"]
        if lines or mismatches:
            lines.append(
                "    systemctl --user daemon-reload (re-applies the settings to the running units; nothing restarts)"
            )
        lines.extend(f"    live {m.unit}: {m.what} {m.actual} -> {m.wanted}" for m in mismatches)
        return lines

    def apply(self) -> None:
        written = False
        for dropin in DROPINS:
            path = self.path(dropin)
            if read_text_or_none(path) != dropin.text():
                write_file(path, dropin.text(), 0o644)
                written = True
        try:
            stuck = self._all_mismatches()
            if written or stuck:
                self._process.execute(["systemctl", "--user", "daemon-reload"])
                stuck = self._settled_mismatches()
        except (ModuleError, CommandError) as error:
            raise NeedsManual(f"{error}: run `systemctl --user daemon-reload` in the desktop session") from error
        if stuck:
            raise NeedsManual(f"{'; '.join(self._cause(m, stuck) for m in stuck)}: `systemctl --user cat <unit>`")

    @staticmethod
    def _cause(mismatch: Mismatch, stuck: list[Mismatch]) -> str:
        """One sentence per stuck value: systemd's own value wrong = an override; only the kernel wrong = not systemd's doing."""
        kernel_files = {d.cgroup_file for d in DROPINS}
        systemd_wrong = any(m.unit == mismatch.unit and m.what not in kernel_files for m in stuck)
        if mismatch.what not in kernel_files:
            return f"another drop-in or runtime property overrides ours ({mismatch})"
        if systemd_wrong:
            return f"the kernel follows that override ({mismatch})"
        if mismatch.actual == "missing":
            return f"the kernel file is missing, so the cgroup controller is not delegated to the unit ({mismatch})"
        return f"the kernel value differs from what systemd loaded, written behind its back ({mismatch})"

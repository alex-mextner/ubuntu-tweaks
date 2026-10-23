"""Packages planner: install what ``packages.toml`` (shared + overlay union) names and is missing.

Accessed via: planner discovery (``build``). One ``package`` action per manager that has names;
its description lists only the names that are missing right now.

Assumptions:

- Never removes anything: a package that is installed but no longer listed is left alone.
- ``apt`` and ``snap`` need root (``needs_root = True``): one ``apt-get install -y`` (or ``snap
  install``) batch through sudo/pkexec. ``flatpak`` runs as the user (polkit asks when a system
  installation needs it) and installs from one remote: ``flatpak_remote`` of ``packages.toml``
  (default ``flathub``); the remote must already be configured.
- Installed means: ``dpkg-query`` status ``ii``; a ``snap list`` row; a ``flatpak list`` row
  (apps and runtimes). A missing tool (no ``snap`` on the machine) means "not installed", and the
  install then fails with that tool's own error.
- Package lists are refreshed only when ``packages.toml`` sets ``apt_update = true`` (default
  off): then ``apt-get update`` runs through sudo/pkexec right before the apt batch, and only
  when some apt package is missing.

- ``uv``, ``npm``, ``bun``, ``cargo`` and ``pipx`` (``tool_packages``) are user-level too: one ``package`` action per
  manager lists the missing entries; each is installed on its own, so one failure does not hide the others (the
  failures are raised together once every entry was tried; any transient one makes the action retry, and a retry
  installs only what is still missing). A manager whose binary is missing is *blocked* (``blocker``) with the
  name of what installs it; managers are never installed here. ``uv`` falls back to ``pipx`` and ``npm`` to ``bun``:
  an entry the primary could not install is installed by the fallback, and a name installed by either counts.

Public API: ``build(ctx, backend, *, process=None, home=None)``, ``installed_apt``, ``installed_snap``,
``installed_flatpak`` (each returns the set of installed names among the ones asked about),
``ToolPackageAction``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import NamedTuple

from ubuntu_tweaks import tool_packages, ui
from ubuntu_tweaks.actions import Action, ActionFailed, Permanent, Phase, Transient, is_transient
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError, UbuntuTweakError
from ubuntu_tweaks.process import CommandError, Process
from ubuntu_tweaks.settings import SettingsBackend

type Lister = Callable[[Process, Sequence[str]], set[str]]


def installed_apt(process: Process, names: Sequence[str]) -> set[str]:
    result = process.query(["dpkg-query", "-W", "-f=${binary:Package}\\t${db:Status-Abbrev}\\n", *names])
    found: set[str] = set()
    for line in result.stdout.splitlines():
        name, _, status = line.partition("\t")
        if status.startswith("ii"):
            found.add(name)
            found.add(name.partition(":")[0])
    return found & set(names)


def installed_snap(process: Process, names: Sequence[str]) -> set[str]:
    result = process.query(["snap", "list"])
    rows = result.stdout.splitlines()[1:] if result.ok else []
    return {row.split()[0] for row in rows if row.split()} & set(names)


def installed_flatpak(process: Process, names: Sequence[str]) -> set[str]:
    result = process.query(["flatpak", "list", "--columns=application"])
    rows = result.stdout.splitlines() if result.ok else []
    return {row.strip() for row in rows if row.strip()} & set(names)


class PackageAction(Action):
    phase = Phase.PACKAGES
    kind = "package"
    scope = None

    def __init__(
        self,
        manager: str,
        names: Collection[str],
        process: Process,
        lister: Lister,
        install_argv: Callable[[Sequence[str]], list[str]],
        *,
        needs_root: bool,
        before: Sequence[str] | None = None,
    ) -> None:
        self._manager = manager
        self._names = tuple(names)
        self._process = process
        self._lister = lister
        self._install_argv = install_argv
        self.needs_root = needs_root
        self._before = before

    def _missing(self) -> list[str]:
        present = self._lister(self._process, self._names)
        return [name for name in self._names if name not in present]

    def check(self) -> bool:
        return not self._missing()

    def _update_command(self) -> Sequence[str] | None:
        """What runs right before the install (``apt-get update``), decided when it is needed."""
        return self._before

    def describe(self) -> str:
        text = f"{self._manager}: install {', '.join(self._missing())}"
        before = self._update_command()
        return text + (f" (after {' '.join(before)})" if before else "")

    def run(self) -> None:
        missing = self._missing()
        if not missing:
            return
        before = self._update_command()
        if before:
            self._process.execute(self._process.elevate(list(before)) if self.needs_root else list(before))
        argv = self._install_argv(missing)
        self._process.execute(self._process.elevate(argv) if self.needs_root else argv)


class ManagerMissing(Action):
    """Stands for a package manager that is not installed: what a tool action is ``blocker``."""

    phase = Phase.PACKAGES
    kind = "package-manager"

    def __init__(self, manager: str) -> None:
        self._manager = manager

    def describe(self) -> str:
        return f"{self._manager} is not installed (installed by {tool_packages.INSTALLED_BY[self._manager]})"

    def check(self) -> bool:
        return False

    def run(self) -> None:
        raise ActionFailed(self.describe())


type Entry = tuple[str, str]  # (name, spec)


class EntryFailure(NamedTuple):
    spec: str
    error: UbuntuTweakError
    retryable: bool


def _retryable(error: UbuntuTweakError, argv: Sequence[str], spec: str) -> bool:
    """Whether the error names a network or lock cause. The command line and the spec are left out of the text: they quote the
    package, and a package called ``pytest-timeout`` is not a timeout."""
    if isinstance(error, Transient):
        return True
    return is_transient(
        CommandError(str(error).replace(" ".join(argv), "").replace(spec, ""), getattr(error, "returncode", None))
    )


class _Installer:
    """Installs entries one command each with one manager and remembers the ones that failed for good, so a retry
    of the others (the action is ``Transient`` while any failure looks retryable) does not repeat them."""

    def __init__(self, process: Process, manager: str, home: Path) -> None:
        self._process = process
        self.manager = manager
        self._home = home
        self._gave_up: dict[str, EntryFailure] = {}

    def install(self, specs: Sequence[str]) -> None:
        """Try every spec not given up on; raise one error for all that are failing: ``Transient`` when any looks
        retryable, ``Permanent`` otherwise."""
        failing: dict[str, EntryFailure] = {}
        for spec in specs:
            if spec in self._gave_up:
                failing[spec] = self._gave_up[spec]
                continue
            argv = tool_packages.install_argv(self.manager, spec, self._home)
            try:
                self._process.execute(argv)
            except UbuntuTweakError as error:
                failure = EntryFailure(spec, error, _retryable(error, argv, spec))
                failing[spec] = failure
                if not failure.retryable:
                    self._gave_up[spec] = failure
        if not failing:
            return
        message = "; ".join(f"{failure.spec}: {failure.error}" for failure in failing.values())
        if any(failure.retryable for failure in failing.values()):
            raise Transient(message)
        raise Permanent(message)


class ToolPackageAction(Action):
    """One manager's entries; ``uv`` falls back to ``pipx`` and ``npm`` to ``bun`` (``tool_packages.FALLBACK``)."""

    phase = Phase.PACKAGES
    kind = "package"
    scope = None

    def __init__(self, manager: str, entries: Sequence[Entry], process: Process, home: Path) -> None:
        self._manager = manager
        self.key = f"tool-package:{manager}"  # describe() changes while the action runs
        self._fallback = tool_packages.FALLBACK.get(manager)
        self._entries = tuple(entries)
        self._process = process
        self._home = home
        self._ran: list[str] | None = None  # what run() went for: describe() stays readable after the install
        self._primary = _Installer(process, manager, home)
        self._via = _Installer(process, self._fallback, home) if self._fallback else None

    def _missing(self) -> list[Entry]:
        present = tool_packages.installed_names(self._process, self._manager, self._home)
        if self._fallback is not None:
            present |= tool_packages.installed_names(self._process, self._fallback, self._home)
        return [(name, spec) for name, spec in self._entries if name not in present]

    def check(self) -> bool:
        return not self._missing()

    def blocker(self) -> Action | None:
        if not tool_packages.is_missing(self._process, self._manager):
            return None
        if self._fallback is not None and not tool_packages.is_missing(self._process, self._fallback):
            return None
        return ManagerMissing(self._manager)

    def describe(self) -> str:
        specs = self.missing_specs() if self._ran is None else self._ran
        return f"{self._manager}: install {', '.join(specs)}"

    def run(self) -> None:
        specs = self.missing_specs()
        if not specs:
            return
        if self._ran is None:
            self._ran = specs  # the first attempt's list: a retry has less to do, the line keeps what was asked
        if self._via is not None and tool_packages.is_missing(self._process, self._manager):
            # nothing to try with the primary: the engine goes on to the fallback and reports it as one
            raise ActionFailed(ManagerMissing(self._manager).describe())
        self._primary.install(specs)

    def alternatives(self) -> Sequence[Action]:
        if self._via is None or tool_packages.is_missing(self._process, self._via.manager):
            return []
        return [_ViaFallback(self, self._via)]

    def why(self) -> str:
        blocker = self.blocker()
        return blocker.describe() if blocker else ui.kind_why("package-user")

    def missing_specs(self) -> list[str]:
        return [spec for _, spec in self._missing()]


class _ViaFallback(Action):
    """The same entries installed by the fallback manager (the engine tries it when the primary failed)."""

    phase = Phase.PACKAGES
    kind = "package"
    scope = None

    def __init__(self, primary: ToolPackageAction, installer: _Installer) -> None:
        self._primary = primary
        self._installer = installer
        self._specs: list[str] | None = None  # what it installed: the primary's missing list is empty afterwards

    def describe(self) -> str:
        specs = self._primary.missing_specs() if self._specs is None else self._specs
        return f"{self._installer.manager}: install {', '.join(specs)}"

    def check(self) -> bool:
        return self._primary.check()

    def run(self) -> None:
        specs = self._primary.missing_specs()
        if self._specs is None:
            self._specs = specs
        self._installer.install(specs)


def build(
    ctx: Context, backend: SettingsBackend, *, process: Process | None = None, home: Path | None = None
) -> Sequence[Action]:
    runner = process or Process()
    packages = ctx.profile.load_packages()
    for manager, names in (
        ("apt", packages.apt),
        ("snap", packages.snap),
        ("snap_classic", packages.snap_classic),
        ("flatpak", packages.flatpak),
        ("brew", packages.brew),  # installed by the brew module's post, validated here for every machine
    ):
        for name in names:
            if name.startswith("-") or not name.strip():
                raise ConfigError(f"packages.toml {manager}: {name!r} is not a package name")
    actions: list[Action] = []
    if packages.apt:
        actions.append(
            PackageAction(
                "apt",
                packages.apt,
                runner,
                installed_apt,
                lambda names: ["apt-get", "install", "-y", *names],
                needs_root=True,
                before=["apt-get", "update"] if packages.apt_update else None,
            )
        )
    for label, names, flags in (("snap", packages.snap, ()), ("snap --classic", packages.snap_classic, ("--classic",))):
        if names:
            actions.append(
                PackageAction(
                    label,
                    names,
                    runner,
                    installed_snap,
                    lambda missing, flags=flags: ["snap", "install", *flags, *missing],
                    needs_root=True,
                )
            )
    if packages.flatpak:
        actions.append(
            PackageAction(
                "flatpak",
                packages.flatpak,
                runner,
                installed_flatpak,
                lambda names: ["flatpak", "install", "-y", "--noninteractive", packages.flatpak_remote, *names],
                needs_root=False,
            )
        )
    for manager in tool_packages.TOOL_MANAGERS:
        specs = getattr(packages, manager)
        if specs:
            entries = [(tool_packages.parse_spec(manager, spec), spec) for spec in specs]
            actions.append(ToolPackageAction(manager, entries, runner, home or Path.home()))
    return actions

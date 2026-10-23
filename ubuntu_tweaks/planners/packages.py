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

Public API: ``build(ctx, backend, *, process=None)``, ``installed_apt``, ``installed_snap``,
``installed_flatpak`` (each returns the set of installed names among the ones asked about).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.process import Process
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


def build(ctx: Context, backend: SettingsBackend, *, process: Process | None = None) -> Sequence[Action]:
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
    return actions

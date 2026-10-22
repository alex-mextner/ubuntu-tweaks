"""``ubuntu-tweak doctor [--yes]``: check the tools the CLI and the modules need.

Accessed via: ``bin/ubuntu-tweak doctor``; ``commands/init.py`` reuses ``requirements`` /
``missing`` / ``install``.

Checks ``python3-gi`` (importable ``gi``), ``dconf-cli`` (``dconf``), ``git``, ``glib-compile-schemas``
(package ``libglib2.0-bin``) and every enabled module's ``[deps] apt`` (``dpkg-query``). A requirement
that an enabled module installs itself (``[install] apt``: ``git`` by ``base-tools`` / ``git-gh``, a
module's dependency) is reported ``planned``, not ``MISSING``: ``init`` / ``apply`` install it, so
``doctor`` does not fail on tools the pipeline is about to install. Read-only unless ``--yes``, which
installs the missing apt packages with ``sudo apt-get install -y`` (without sudo when already root).
Exit 0 when nothing is missing, 3 when something is (still missing after ``--yes`` counts too). Does
not import PyGObject: it must work without it.

Public API: ``Requirement(label, apt, needed_by, present, planned_by=None)``, ``requirements(ctx)``,
``planned_installs(ctx)``, ``missing(requirements)``, ``install(missing)``, ``register(subparsers)``,
``run(ctx, args)``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ubuntu_tweaks.commands._support import has_gi
from ubuntu_tweaks.actions import Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ExitCode
from ubuntu_tweaks.manifest import discover_modules, module_defaults
from ubuntu_tweaks.process import elevate
from ubuntu_tweaks.process import run as run_command


@dataclass(frozen=True)
class Requirement:
    label: str
    apt: str
    needed_by: str
    present: Callable[[], bool]
    planned_by: str | None = None


def apt_installed(package: str) -> bool:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package], capture_output=True, text=True, check=False, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "install ok installed" in result.stdout


def _binary(name: str) -> Callable[[], bool]:
    return lambda: shutil.which(name) is not None


def planned_installs(ctx: Context) -> dict[str, str]:
    """apt package -> the enabled module whose ``[install] apt`` installs it (the first, by order)."""
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    planned: dict[str, str] = {}
    for manifest in sorted(manifests, key=lambda item: (item.order, item.name)):
        # only `phase = "base"` installs run before the submodule init that needs git
        if manifest.phase is Phase.BASE and ctx.selects(manifest.name) and config.module_enabled(manifest.name):
            for package in manifest.install.apt:
                planned.setdefault(package, manifest.name)
    return planned


def requirements(ctx: Context, *, honor_planned: bool = True) -> list[Requirement]:
    """What the CLI and the modules need. ``python3-gi``, ``dconf-cli`` and ``libglib2.0-bin`` are needed to
    build the plan at all, so they stay hard; ``git`` and the modules' ``[deps] apt`` that an enabled module
    installs itself (``[install] apt``) are ``planned``, not missing (``honor_planned=False``, used by
    ``init --no-sudo`` whose root actions are skipped, keeps them hard)."""
    planned = planned_installs(ctx) if honor_planned else {}
    found = [
        Requirement("python3-gi (PyGObject)", "python3-gi", "the CLI", has_gi),
        Requirement("dconf", "dconf-cli", "settings", _binary("dconf")),
        Requirement("git", "git", "submodules, update", _binary("git"), planned.get("git")),
        Requirement("glib-compile-schemas", "libglib2.0-bin", "extension schemas", _binary("glib-compile-schemas")),
    ]
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    for manifest in manifests:
        if not ctx.selects(manifest.name) or not config.module_enabled(manifest.name):
            continue
        for package in manifest.deps.apt:
            found.append(
                Requirement(
                    package,
                    package,
                    f"module {manifest.name}",
                    lambda package=package: apt_installed(package),
                    planned.get(package),
                )
            )
    return found


def missing(needed: Sequence[Requirement]) -> list[Requirement]:
    """What is absent and that no enabled module will install: ``init`` / ``apply`` install the planned ones."""
    return [item for item in needed if item.planned_by is None and not item.present()]


def install(absent: Sequence[Requirement]) -> None:
    packages = sorted({item.apt for item in absent})
    if not packages:
        return
    run_command(elevate(["apt-get", "install", "-y", *packages]))


def report(needed: Sequence[Requirement]) -> list[Requirement]:
    absent = missing(needed)
    absent_labels = {item.label for item in absent}
    for item in needed:
        if item.label in absent_labels:
            state = "MISSING"
        elif item.planned_by is not None and not item.present():
            state = "planned"
        else:
            state = "ok     "
        note = f" -> `init`/`apply` installs it (module {item.planned_by})" if state == "planned" else ""
        print(f"  {state} {item.label} [apt: {item.apt}] ({item.needed_by}){note}")
    return absent


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("doctor", help="check required packages and binaries (--yes installs them)")
    parser.add_argument("--yes", action="store_true", help="install the missing apt packages with sudo")
    parser.set_defaults(handler=run)


def run(ctx: Context, args: argparse.Namespace) -> int:
    needed = requirements(ctx)
    absent = report(needed)
    if not absent:
        print("All requirements are present.")
        return int(ExitCode.OK)
    packages = " ".join(sorted({item.apt for item in absent}))
    if not args.yes:
        print(f"{len(absent)} missing. Install with: ubuntu-tweak doctor --yes   (sudo apt-get install -y {packages})")
        return int(ExitCode.DRIFT)
    install(absent)
    print("After install:")
    absent = report(needed)
    return int(ExitCode.DRIFT if absent else ExitCode.OK)

"""``ubuntu-tweak doctor [--yes]``: check the tools the CLI and the modules need.

Accessed via: ``bin/ubuntu-tweak doctor``; ``commands/init.py`` reuses ``requirements`` /
``missing`` / ``install``.

Checks ``python3-gi`` (importable ``gi``), ``dconf-cli`` (``dconf``), ``git``, ``glib-compile-schemas``
(package ``libglib2.0-bin``) and every enabled module's ``[deps] apt`` (``dpkg-query``). Read-only
unless ``--yes``, which installs the missing apt packages with ``sudo apt-get install -y``
(without sudo when already root). Exit 0 when everything is present, 3 when something is missing
(still missing after ``--yes`` counts too). Does not import PyGObject: it must work without it.

Public API: ``Requirement``, ``requirements(ctx)``, ``missing(requirements)``,
``install(missing)``, ``register(subparsers)``, ``run(ctx, args)``.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ExitCode
from ubuntu_tweaks.manifest import discover_modules, module_defaults
from ubuntu_tweaks.proc import run as run_command


@dataclass(frozen=True)
class Requirement:
    label: str
    apt: str
    needed_by: str
    present: Callable[[], bool]


def _has_gi() -> bool:
    importlib.invalidate_caches()
    return importlib.util.find_spec("gi") is not None


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


def requirements(ctx: Context) -> list[Requirement]:
    found = [
        Requirement("python3-gi (PyGObject)", "python3-gi", "the CLI", _has_gi),
        Requirement("dconf", "dconf-cli", "settings", _binary("dconf")),
        Requirement("git", "git", "submodules, update", _binary("git")),
        Requirement("glib-compile-schemas", "libglib2.0-bin", "extension schemas", _binary("glib-compile-schemas")),
    ]
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    for manifest in manifests:
        if not ctx.selects(manifest.name) or not config.module_enabled(manifest.name):
            continue
        for package in manifest.deps.apt:
            found.append(
                Requirement(package, package, f"module {manifest.name}", lambda package=package: apt_installed(package))
            )
    return found


def missing(needed: Sequence[Requirement]) -> list[Requirement]:
    return [item for item in needed if not item.present()]


def install(absent: Sequence[Requirement]) -> None:
    packages = sorted({item.apt for item in absent})
    if not packages:
        return
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    run_command([*prefix, "apt-get", "install", "-y", *packages])


def report(needed: Sequence[Requirement]) -> list[Requirement]:
    absent = missing(needed)
    absent_labels = {item.label for item in absent}
    for item in needed:
        state = "MISSING" if item.label in absent_labels else "ok     "
        print(f"  {state} {item.label} [apt: {item.apt}] ({item.needed_by})")
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

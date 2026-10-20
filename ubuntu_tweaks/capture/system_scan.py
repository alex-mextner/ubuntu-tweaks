"""Section 3 of `capture`: the ``packages.toml`` and ``system.toml`` proposals.

Accessed via: ``plan.build_plan``. Read-only: ``apt-mark showmanual``, ``apt-cache depends``,
``snap list``, ``flatpak list``, ``timedatectl show``, ``localectl status`` and two files
(``/etc/default/keyboard``, the installer baseline) read through an injectable ``Runner`` so tests
need no system tools. A missing tool is an empty answer, never an error.

Packages: apt = manually installed packages minus the installer baseline
(``/var/log/installer/initial-status.gz`` when present, else the direct dependencies of the
Ubuntu metapackages) minus the ``[packages] ignore`` globs (kernels, language packs, runtime
libraries; ``keep`` globs win). snap = installed snaps minus base/plumbing snaps and the seeded
ones. flatpak = installed applications. Marks: ``+`` not yet in the profile's ``packages.toml``,
``=`` already there.

System: timezone (a machine-overlay value), locale and console keyboard (shared unless the overlay
already sets the key), marked ``+`` / ``~`` / ``=`` against the layer the value is written to.

Public API: ``Runner``, ``default_runner``, ``scan_packages``, ``scan_system``, ``PackageItem``,
``SystemItem``.
"""

from __future__ import annotations

import gzip
import re
import subprocess
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ubuntu_tweaks.capture.rules import Mark, PackageRules
from ubuntu_tweaks.profile import Packages

type Runner = Callable[[Sequence[str]], str | None]

INSTALLER_BASELINE = Path("/var/log/installer/initial-status.gz")
SNAP_SEED = Path("/var/lib/snapd/seed/seed.yaml")
KEYBOARD_FILE = Path("/etc/default/keyboard")
_DEPENDS = re.compile(r"^[ |]*(?:Pre)?Depends: ([^<\s]\S*)$")


@dataclass(frozen=True)
class PackageItem:
    manager: Literal["apt", "snap", "flatpak"]
    name: str
    mark: Mark


@dataclass(frozen=True)
class SystemItem:
    key: str  # "timezone", "locale", "keyboard.layout", ...
    value: str
    machine: bool
    mark: Mark
    previous: str | None


def default_runner(argv: Sequence[str]) -> str | None:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _lines(output: str | None) -> list[str]:
    return [line.strip() for line in (output or "").splitlines() if line.strip()]


def _installer_baseline(path: Path) -> set[str] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return {match.group(1) for line in handle if (match := re.match(r"Package: (\S+)", line))}
    except (OSError, EOFError, zlib.error):
        return None


def _metapackage_baseline(run: Runner, metapackages: Sequence[str]) -> set[str]:
    names: set[str] = set(metapackages)
    for meta in metapackages:
        output = run(
            [
                "apt-cache", "depends", "--no-recommends", "--no-suggests", "--no-conflicts",
                "--no-breaks", "--no-replaces", "--no-enhances", meta,
            ]
        )  # fmt: skip
        names.update(match.group(1) for line in (output or "").splitlines() if (match := _DEPENDS.match(line)))
    return names


def scan_packages(
    run: Runner,
    rules: PackageRules,
    existing: Packages,
    *,
    installer_baseline: Path = INSTALLER_BASELINE,
    snap_seed: Path = SNAP_SEED,
) -> tuple[list[PackageItem], str]:
    """Package proposals plus a one-line note on which apt baseline was used."""
    baseline = _installer_baseline(installer_baseline)
    note = f"apt baseline: {installer_baseline}"
    if baseline is None:
        baseline = _metapackage_baseline(run, rules.metapackages)
        note = "apt baseline: dependencies of " + ", ".join(rules.metapackages) + " (no installer baseline file)"
    items: list[PackageItem] = []
    manual = sorted(_lines(run(["apt-mark", "showmanual"])))
    priorities = (
        dict(
            line.split("\t", 1)
            for line in (run(["dpkg-query", "-W", "-f=${Package}\t${Priority}\n", *manual]) or "").splitlines()
            if "\t" in line
        )
        if manual
        else {}
    )
    for name in manual:
        if priorities.get(name) in ("required", "important"):
            continue
        if name in baseline or name in rules.metapackages:
            continue
        if rules.ignore.matches(name) and not rules.keep.matches(name):
            continue
        items.append(PackageItem("apt", name, "=" if name in existing.apt else "+"))
    seeded: set[str] = set()
    try:
        seeded = set(re.findall(r"^\s*-?\s*name:\s*(\S+)", snap_seed.read_text(encoding="utf-8"), re.MULTILINE))
    except (OSError, ValueError):
        pass
    for line in sorted(_lines(run(["snap", "list"]))[1:]):
        columns = line.split()
        name, notes = columns[0], columns[-1]
        if "base" in notes or "snapd" in notes or rules.ignore_snaps.matches(name) or name in seeded:
            continue
        items.append(PackageItem("snap", name, "=" if name in existing.snap else "+"))
    for name in sorted(_lines(run(["flatpak", "list", "--app", "--columns=application"]))):
        items.append(PackageItem("flatpak", name, "=" if name in existing.flatpak else "+"))
    return items, note


def _mark(current: str, recorded: str | None) -> tuple[Mark, str | None]:
    if recorded is None:
        return "+", None
    return ("=", None) if recorded == current else ("~", recorded)


def _keyboard_values(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {
        m.group(1): m.group(2) for m in re.finditer(r'^(XKB[A-Z]+)=["\']?([^"\'\n]*)["\']?\s*$', text, re.MULTILINE)
    }
    names = {"XKBMODEL": "model", "XKBLAYOUT": "layout", "XKBVARIANT": "variant", "XKBOPTIONS": "options"}
    return {names[key]: value for key, value in found.items() if key in names and value.strip(", ")}


def _layer_value(table: Mapping[str, object], key: str) -> str | None:
    """``timezone`` or ``keyboard.layout`` from a raw ``system.toml`` table."""
    node: object = table
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) else None


def scan_system(
    run: Runner,
    shared: Mapping[str, object],
    overlay: Mapping[str, object],
    keyboard_file: Path = KEYBOARD_FILE,
) -> list[SystemItem]:
    """``shared`` and ``overlay`` are the raw ``system.toml`` tables of the two layers. An item goes
    to the overlay when it is the timezone or the overlay already sets it (a shared write would
    never take effect: the overlay wins); it is compared with the layer it will be written to."""
    wanted: list[tuple[str, str]] = []
    timezone = (run(["timedatectl", "show", "-p", "Timezone", "--value"]) or "").strip()
    if timezone:
        wanted.append(("timezone", timezone))
    locale_match = re.search(r"System Locale:\s*LANG=(\S+)", run(["localectl", "status"]) or "")
    if locale_match:
        wanted.append(("locale", locale_match.group(1)))
    wanted += [(f"keyboard.{name}", value) for name, value in _keyboard_values(keyboard_file).items()]
    items: list[SystemItem] = []
    for key, value in wanted:
        machine = key == "timezone" or _layer_value(overlay, key) is not None
        items.append(SystemItem(key, value, machine, *_mark(value, _layer_value(overlay if machine else shared, key))))
    return items

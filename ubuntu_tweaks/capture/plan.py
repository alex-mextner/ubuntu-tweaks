"""The capture plan: everything `capture` found, computed once and then either rendered or committed.

Accessed via: ``commands/capture.py`` (preview and ``--commit`` share this one object, so what
the user saw is what is written), ``report.py`` and ``commit.py``.

``build_plan`` is read-only. ``--only`` takes panel ids plus the section names ``files``,
``packages`` and ``system``; with ``--only`` set, a section not named is not computed.

Public API: ``CapturePlan``, ``build_plan(...)``, ``SECTION_NAMES``, ``refusals(plan)`` (the secret
findings that make ``--commit`` exit 5, as ``(where, kind, class, line)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.capture.files_scan import FilesResult, scan_files
from ubuntu_tweaks.capture.rules import PANEL_SELECTORS, Rules
from ubuntu_tweaks.capture.settings_scan import SettingsResult, Target, scan_settings
from ubuntu_tweaks.capture.system_scan import (
    INSTALLER_BASELINE,
    KEYBOARD_FILE,
    SNAP_SEED,
    PackageItem,
    Runner,
    SystemItem,
    scan_packages,
    scan_system,
)
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.profile import Profile, read_toml
from ubuntu_tweaks.setkey import SchemaIndex
from ubuntu_tweaks.settings import KeyFileData, SettingsBackend, read_keyfile

SECTION_NAMES = ("files", "packages", "system")


@dataclass(frozen=True)
class CapturePlan:
    profile: Profile
    only: frozenset[str]
    settings: SettingsResult | None
    files: FilesResult | None
    packages: list[PackageItem] | None
    packages_note: str
    system: list[SystemItem] | None


def validate_only(only: frozenset[str]) -> None:
    unknown = sorted(only - set(PANEL_SELECTORS) - set(SECTION_NAMES))
    if unknown:
        raise ConfigError(
            f"unknown panel(s) for --only: {', '.join(unknown)}; known: {', '.join(PANEL_SELECTORS + SECTION_NAMES)}"
        )


def build_plan(
    profile: Profile,
    rules: Rules,
    backend: SettingsBackend,
    *,
    home: Path,
    only: frozenset[str] = frozenset(),
    run: Runner,
    installer_baseline: Path = INSTALLER_BASELINE,
    snap_seed: Path = SNAP_SEED,
    keyboard_file: Path = KEYBOARD_FILE,
) -> CapturePlan:
    validate_only(only)
    panels = frozenset(only & set(PANEL_SELECTORS))
    cache: dict[tuple[Target, str], KeyFileData] = {}

    def existing(target: Target, panel: str) -> KeyFileData:
        if (target, panel) not in cache:
            directory = profile.settings_dir if target == "shared" else profile.overlay_settings_dir
            path = directory / f"{panel}.ini"
            cache[(target, panel)] = read_keyfile(path) if path.is_file() else {}
        return cache[(target, panel)]

    wants_settings = not only or bool(panels)
    settings = (
        scan_settings(
            backend, SchemaIndex(backend), rules, existing, home / ".config/glib-2.0/settings/keyfile", panels
        )
        if wants_settings
        else None
    )
    files = scan_files(home, profile, rules) if not only or "files" in only else None
    packages, note = None, ""
    if not only or "packages" in only:
        packages, note = scan_packages(
            run, rules.packages, profile.load_packages(), installer_baseline=installer_baseline, snap_seed=snap_seed
        )
    system = None
    if not only or "system" in only:
        profile.load_system()  # validates both layers (ConfigError) before the raw tables are compared
        system = scan_system(run, read_toml(profile.system_toml), read_toml(profile.overlay_system_toml), keyboard_file)
    return CapturePlan(profile, only, settings, files, packages, note, system)


def refusals(plan: CapturePlan) -> list[tuple[str, str, str, int]]:
    found: list[tuple[str, str, str, int]] = []
    if plan.files is not None:
        for adopted in plan.files.adopted:
            found += [(f"~/{adopted.rel}", f.kind, f.cls, f.line) for f in adopted.findings]
    return found

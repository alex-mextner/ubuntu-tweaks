"""Settings bundles: purpose-grouped dconf keyfiles that are merged at load (Home Assistant's
``packages:``, called *bundle* here because ``packages.toml`` already means apt/snap/flatpak).

Accessed via: ``planners/settings.py`` (what ``apply`` / ``status`` enforce), ``capture`` (where a
captured key goes and whether the profile already holds it), the ``bundles``, ``bundle-move`` and
``note`` commands. Pure: no GLib, no writes; text parsing is ``keyfile_doc.py``.

Model (spec, *Bundles*):

- A bundle is any ``<profile>/settings/**/*.ini`` (origin ``shared``) or
  ``<profile>/machines/<machine>/settings/**/*.ini`` (origin ``overlay``). Its name is the path
  under ``settings/`` without ``.ini`` (``dev-keyboard``, ``laptop/power``). Its sections may be any
  dconf directory; panel names are only what ``capture`` names files by default.
- Active bundles are merged key by key. An overlay key beats a shared key (a *shadow*, reported). The
  same key twice among the active shared bundles, or twice among the active overlay bundles, is a
  *conflict* when the values differ (``ConfigError``, exit 2, naming both files and lines; values
  compare as text) and a *duplicate* warning when they are identical. Nothing is picked silently.
- An optional ``[ubuntu-tweak]`` section is the bundle's header, not dconf: ``description``,
  ``machines`` (TOML list: the bundle applies only on those machine names) and ``requires-module``
  (the bundle applies only while that module is enabled). Unknown header keys, wrong types and an
  unknown module are ``ConfigError``. The header never reaches a plan.
- A comment block directly above a key is its reason (``keyfile_doc``).

Public API: ``HEADER_SECTION``, ``Bundle``, ``BundleKey``, ``Located``, ``Clash``, ``Shadow``,
``BundleSet`` (``bundles``, ``active()``, ``conflicts``, ``duplicates``, ``shadows``,
``winners``, ``locate()``, ``require_valid()``, ``duplicate_warnings()``), ``load_bundles(profile,
module_enabled=None)``, ``module_gate(modules_dir, profile)``, ``validate_bundle_name(name)``,
``read_bundle(file, source, machine)``, ``key_path(section, key)``.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.keyfile_doc import KeyfileDoc, comment_text
from ubuntu_tweaks.manifest import discover_modules, module_defaults
from ubuntu_tweaks.profile import BundleFile, Origin, Profile

HEADER_SECTION = "ubuntu-tweak"
HEADER_KEYS = ("description", "machines", "requires-module")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")

type ModuleGate = Callable[[str], bool | None]


def validate_bundle_name(name: str) -> str:
    if not _NAME.fullmatch(name) or ".." in name.split("/"):
        raise ConfigError(
            f"invalid bundle name {name!r}: use letters, digits, '.', '_', '-', with '/' for subdirectories"
        )
    return name


def key_path(section: str, key: str) -> str:
    """The full dconf key path of a bundle key: section ``org/gnome/x`` + ``k`` -> ``/org/gnome/x/k``."""
    return f"/{key}" if section == "/" else f"/{section}/{key}"


@dataclass(frozen=True)
class BundleKey:
    section: str  # normalized: ``org/gnome/x``
    key: str
    value: str
    reason: str  # the comment block above the key, joined into one line; "" when there is none
    line: int  # 1-based

    @property
    def full(self) -> str:
        return key_path(self.section, self.key)


@dataclass(frozen=True)
class Bundle:
    name: str
    origin: Origin
    path: Path
    source: str  # profile-relative, e.g. ``settings/dev-keyboard.ini``
    description: str
    machines: tuple[str, ...] | None
    requires_module: str | None
    keys: tuple[BundleKey, ...]
    inactive: str | None = None  # why it does not apply on this machine, None when it does

    @property
    def active(self) -> bool:
        return self.inactive is None

    def where(self, key: BundleKey) -> str:
        return f"{self.source}:{key.line}"


@dataclass(frozen=True)
class Located:
    bundle: Bundle
    key: BundleKey

    @property
    def where(self) -> str:
        return self.bundle.where(self.key)


@dataclass(frozen=True)
class Clash:
    """The same key in two bundles of one origin."""

    first: Located
    second: Located

    @property
    def same_value(self) -> bool:
        return self.first.key.value == self.second.key.value


@dataclass(frozen=True)
class Shadow:
    """A shared key an overlay bundle overrides."""

    shared: Located
    overlay: Located

    @property
    def same_value(self) -> bool:
        return self.shared.key.value == self.overlay.key.value


def _toml_value(raw: str) -> object | None:
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return None


def _parse_header(doc: KeyfileDoc, source: str) -> tuple[str, tuple[str, ...] | None, str | None]:
    entries = {e.key: e for e in doc.entries() if e.section == HEADER_SECTION}
    unknown = sorted(set(entries) - set(HEADER_KEYS))
    if unknown:
        raise ConfigError(
            f"{source} [{HEADER_SECTION}]: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(HEADER_KEYS)}"
        )
    description = ""
    if "description" in entries:
        raw = entries["description"].value
        parsed = _toml_value(raw)
        if parsed is not None and not isinstance(parsed, str):
            raise ConfigError(f'{source}:{entries["description"].number} description must be text, e.g. "My bundle"')
        description = parsed if isinstance(parsed, str) else raw
    machines: tuple[str, ...] | None = None
    if "machines" in entries:
        parsed = _toml_value(entries["machines"].value)
        if not isinstance(parsed, list) or not parsed or not all(isinstance(x, str) and x for x in parsed):
            raise ConfigError(
                f"{source}:{entries['machines'].number} machines must be a non-empty list of names, "
                f'e.g. machines = ["home"]'
            )
        machines = tuple(parsed)
    required: str | None = None
    if "requires-module" in entries:
        parsed = _toml_value(entries["requires-module"].value)
        if not isinstance(parsed, str) or not parsed:
            raise ConfigError(f"{source}:{entries['requires-module'].number} requires-module must be a quoted name")
        required = parsed
    return description, machines, required


def read_bundle(file: BundleFile, source: str, machine: str) -> Bundle:
    try:
        text = file.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{source}: cannot read: {getattr(error, 'strerror', None) or error}") from error
    doc = KeyfileDoc.parse(text, source)
    description, machines, required = _parse_header(doc, source)
    keys = tuple(
        BundleKey(e.section, e.key, e.value, comment_text(doc.reason(e)), e.number)
        for e in doc.entries()
        if e.section != HEADER_SECTION
    )
    inactive = None
    if machines is not None and machine not in machines:
        inactive = f"machines = {list(machines)} does not include {machine}"
    return Bundle(file.name, file.origin, file.path, source, description, machines, required, keys, inactive)


@dataclass(frozen=True)
class BundleSet:
    bundles: tuple[Bundle, ...]
    conflicts: tuple[Clash, ...]
    duplicates: tuple[Clash, ...]
    shadows: tuple[Shadow, ...]

    def active(self) -> tuple[Bundle, ...]:
        return tuple(bundle for bundle in self.bundles if bundle.active)

    def find(self, name: str, origin: Origin) -> Bundle | None:
        return next((b for b in self.bundles if b.name == name and b.origin == origin), None)

    @cached_property
    def inactive_shared(self) -> dict[tuple[str, str], Located]:
        """Keys that shared bundles *not applying on this machine* hold: another machine reads them."""
        found: dict[tuple[str, str], Located] = {}
        for bundle in self.bundles:
            if bundle.origin == "shared" and not bundle.active:
                for key in bundle.keys:
                    found.setdefault((key.section, key.key), Located(bundle, key))
        return found

    @cached_property
    def winners(self) -> dict[tuple[str, str], Located]:
        """The holder that applies for each ``(section, key)``: the first overlay one, else the first shared one."""
        winners: dict[tuple[str, str], Located] = {}
        overlaid: set[tuple[str, str]] = set()
        for bundle in self.active():  # shared first, so an overlay holder replaces it
            for key in bundle.keys:
                slot = (key.section, key.key)
                if bundle.origin == "shared":
                    winners.setdefault(slot, Located(bundle, key))
                elif slot not in overlaid:
                    winners[slot] = Located(bundle, key)
                    overlaid.add(slot)
        return winners

    def locate(self, section: str, key: str) -> Located | None:
        return self.winners.get((section, key))

    def require_valid(self) -> None:
        if self.conflicts:
            lines = [
                f"conflicting values for {c.first.key.full}: "
                f"{c.first.where} = {c.first.key.value} vs {c.second.where} = {c.second.key.value}"
                for c in self.conflicts
            ]
            raise ConfigError(
                "the same key is set to different values in two bundles of one layer; keep one "
                "(`ubuntu-tweak bundle-move` groups keys):\n  " + "\n  ".join(lines)
            )

    def duplicate_warnings(self) -> list[str]:
        return [
            f"duplicate key {c.first.key.full}: {c.first.where} and {c.second.where} hold the same value"
            for c in self.duplicates
        ]


def load_bundles(profile: Profile, module_enabled: ModuleGate | None = None) -> BundleSet:
    """Read every bundle, decide which ones apply here and find the clashes among those.

    ``module_enabled(name)`` says whether module ``name`` is enabled (``None``: no such module); without
    it ``requires-module`` is not evaluated."""
    bundles: list[Bundle] = []
    for file in profile.bundle_files():
        source = file.path.relative_to(profile.root).as_posix()
        bundle = read_bundle(file, source, profile.machine)
        if bundle.requires_module is not None and module_enabled is not None:
            state = module_enabled(bundle.requires_module)
            if state is None:
                raise ConfigError(f"{source}: requires-module {bundle.requires_module!r}: no such module")
            if bundle.active and not state:
                bundle = replace(bundle, inactive=f"requires-module {bundle.requires_module} is disabled")
        bundles.append(bundle)
    active = [b for b in bundles if b.active]
    conflicts: list[Clash] = []
    duplicates: list[Clash] = []
    seen: dict[tuple[Origin, str, str], Located] = {}
    shared_by_key: dict[tuple[str, str], list[Located]] = {}
    for bundle in active:
        for key in bundle.keys:
            found = Located(bundle, key)
            slot = (bundle.origin, key.section, key.key)
            if slot in seen:
                clash = Clash(seen[slot], found)
                (duplicates if clash.same_value else conflicts).append(clash)
            else:
                seen[slot] = found
            if bundle.origin == "shared":
                shared_by_key.setdefault((key.section, key.key), []).append(found)
    shadows = [
        Shadow(shared, Located(bundle, key))
        for bundle in active
        if bundle.origin == "overlay"
        for key in bundle.keys
        for shared in shared_by_key.get((key.section, key.key), ())
    ]
    return BundleSet(tuple(bundles), tuple(conflicts), tuple(duplicates), tuple(shadows))


def module_gate(modules_dir: Path, profile: Profile) -> ModuleGate:
    """``module_enabled`` for ``load_bundles``: the merged module config (defaults < profile < overlay)."""
    manifests = discover_modules(modules_dir)
    config = profile.load_config(module_defaults(manifests))
    known = {manifest.name for manifest in manifests}

    def gate(name: str) -> bool | None:
        return config.module_enabled(name) if name in known else None

    return gate

"""The profile: where personal Ubuntu settings live, and how layers merge.

Accessed via: ``Context.profile`` in every command (built by ``cli.main`` from ``--profile``
and ``--machine``); ``commands/machine.py`` prints the origins computed here.

Assumptions: pure reads. Nothing here writes, creates directories or runs commands, so every
function is testable with a temporary directory and an injected ``env`` / ``hostname_file``.
A missing default profile is "no profile" (module defaults only); an explicitly requested
profile that does not exist is a ``ConfigError``.

Public API:

- ``DEFAULT_PROFILE_RELPATH`` (``.files/ubuntu``, relative to ``$HOME``), ``PROFILE_ENV``
  (``UBUNTU_TWEAK_PROFILE``), ``HOSTNAME_FILE`` (``/etc/hostname``).
- ``detect_machine(hostname_file=HOSTNAME_FILE) -> str``: stripped file content, else
  ``socket.gethostname()``.
- ``Profile.resolve(profile=None, machine=None, *, env=None, hostname_file=HOSTNAME_FILE,
  home=None) -> Profile``: location is ``profile`` > ``$UBUNTU_TWEAK_PROFILE`` >
  ``<home>/.files/ubuntu``; machine is ``machine`` > ``detect_machine()`` and must be one path
  component.
- ``Profile`` (frozen): ``root``, ``machine``, ``machine_source`` (``"flag"`` | ``"hostname"``);
  paths ``overlay_dir``, ``profile_toml``, ``machine_toml``, ``settings_dir``,
  ``overlay_settings_dir``, ``files_dir``, ``overlay_files_dir``, ``packages_toml``,
  ``overlay_packages_toml``, ``system_toml``, ``overlay_system_toml``; ``exists``,
  ``has_overlay``;
  ``settings_panels() -> tuple[PanelFiles, ...]``; ``files() -> tuple[ProfileFile, ...]`` (leaf
  files, sorted; an overlay entry replaces every shared entry equal to, inside or above it, so
  no two entries ever target nested paths); ``load_config(module_defaults=None) -> Config``
  (every ``[modules.<name>]`` must be a table in its own file, ``ConfigError`` otherwise);
  ``load_packages() -> Packages``; ``load_system() -> SystemConfig`` (each layer validated on its
  own, then unioned; duplicates collapse).
- ``Config`` (frozen): ``data`` (the merged tree; treat as read-only), ``origins`` (leaf path ->
  ``Layer``); ``table(*path)``, ``get(*path, default=None)``, ``origin(*path)``,
  ``module_enabled(name)``.
- ``merge_layers(layers, *, union_lists=False) -> Config``: ``layers`` is ordered
  ``[(Layer, table), ...]``, last wins; tables merge, scalars replace, lists replace (or union
  with ``union_lists=True``, shared order first).
- ``Layer`` = ``"module"`` | ``"shared"`` | ``"overlay"``; ``Origin`` = ``"shared"`` | ``"overlay"``.
- ``PanelFiles(panel, shared, overlay)`` and ``ProfileFile(rel, source, origin, shadows)``
  (``shadows``: tuple of the shared files an overlay entry replaces).
- ``Packages(apt, snap, flatpak, flatpak_remote, apt_update)``, ``Keyboard(model, layout, variant, options)``,
  ``SystemConfig(timezone, locale, keyboard)``: typed views of ``packages.toml`` and
  ``system.toml`` after the shared + overlay union-merge.
- ``TomlValue`` / ``TomlTable``: the value types of parsed TOML.
"""

from __future__ import annotations

import copy
import os
import socket
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath
from typing import Literal

from ubuntu_tweaks.errors import ConfigError

type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable
type TomlTable = dict[str, TomlValue]
type Layer = Literal["module", "shared", "overlay"]
type Origin = Literal["shared", "overlay"]
type MachineSource = Literal["flag", "hostname"]

PROFILE_ENV = "UBUNTU_TWEAK_PROFILE"
HOSTNAME_FILE = Path("/etc/hostname")
DEFAULT_PROFILE_RELPATH = Path(".files/ubuntu")


def detect_machine(hostname_file: Path = HOSTNAME_FILE) -> str:
    try:
        name = hostname_file.read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    return name or socket.gethostname()


def _validate_machine_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ConfigError(f"invalid machine name {name!r}: it must be a single path component")
    return name


def read_toml(path: Path) -> TomlTable:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from error
    except OSError as error:
        raise ConfigError(f"{path}: cannot read: {error.strerror or error}") from error


@dataclass(frozen=True)
class Config:
    """A merged TOML tree plus, for every leaf, the layer that set it last."""

    data: TomlTable = field(default_factory=dict)
    origins: Mapping[tuple[str, ...], Layer] = field(default_factory=dict)

    def get(self, *path: str, default: TomlValue | None = None) -> TomlValue | None:
        node: TomlValue = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def table(self, *path: str) -> TomlTable:
        node = self.get(*path)
        return node if isinstance(node, dict) else {}

    def origin(self, *path: str) -> Layer | None:
        return self.origins.get(path)

    def module_enabled(self, name: str) -> bool:
        for node_path in (("modules",), ("modules", name)):
            node = self.get(*node_path)
            if node is not None and not isinstance(node, dict):
                raise ConfigError(f"{'.'.join(node_path)} must be a table, got {node!r}")
        value = self.get("modules", name, "enabled", default=True)
        if not isinstance(value, bool):
            raise ConfigError(f"modules.{name}.enabled must be true or false, got {value!r}")
        return value


def _drop_origins(origins: dict[tuple[str, ...], Layer], prefix: tuple[str, ...]) -> None:
    for path in [p for p in origins if p[: len(prefix)] == prefix]:
        del origins[path]


def _merge_into(
    target: TomlTable,
    origins: dict[tuple[str, ...], Layer],
    source: Mapping[str, TomlValue],
    layer: Layer,
    prefix: tuple[str, ...],
    union_lists: bool,
) -> None:
    for key, value in source.items():
        path = (*prefix, key)
        existing = target.get(key)
        if isinstance(value, dict):
            if not isinstance(existing, dict):
                _drop_origins(origins, path)
                existing = target[key] = {}
            _merge_into(existing, origins, value, layer, path, union_lists)
        elif union_lists and isinstance(value, list) and isinstance(existing, list):
            added: list[TomlValue] = []
            for item in value:
                if item not in existing and item not in added:
                    added.append(item)
            existing.extend(copy.deepcopy(added))
            if added:
                origins[path] = layer
        else:
            _drop_origins(origins, path)
            target[key] = copy.deepcopy(value)
            origins[path] = layer


def merge_layers(layers: Sequence[tuple[Layer, Mapping[str, TomlValue]]], *, union_lists: bool = False) -> Config:
    data: TomlTable = {}
    origins: dict[tuple[str, ...], Layer] = {}
    for layer, table in layers:
        _merge_into(data, origins, table, layer, (), union_lists)
    return Config(data, origins)


@dataclass(frozen=True)
class PanelFiles:
    """The shared and/or overlay ``settings/<panel>.ini`` of one Settings panel."""

    panel: str
    shared: Path | None
    overlay: Path | None


@dataclass(frozen=True)
class ProfileFile:
    """A leaf under ``files/`` (shared or overlay) and the ``$HOME``-relative path it maps to.

    ``shadows`` lists the shared files an overlay entry replaces: the one at the same path, or any
    inside or above it (empty for shared entries).
    """

    rel: PurePosixPath
    source: Path
    origin: Origin
    shadows: tuple[Path, ...]


@dataclass(frozen=True)
class Packages:
    apt: tuple[str, ...] = ()
    snap: tuple[str, ...] = ()
    flatpak: tuple[str, ...] = ()
    flatpak_remote: str = "flathub"
    apt_update: bool = False


@dataclass(frozen=True)
class Keyboard:
    model: str | None = None
    layout: str | None = None
    variant: str | None = None
    options: str | None = None


@dataclass(frozen=True)
class SystemConfig:
    timezone: str | None = None
    locale: str | None = None
    keyboard: Keyboard = field(default_factory=Keyboard)


def _reject_unknown(table: Mapping[str, TomlValue], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")


def _optional_str(table: Mapping[str, TomlValue], key: str, where: str) -> str | None:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string, got {value!r}")
    return value


def _str_tuple(table: Mapping[str, TomlValue], key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{where}: {key} must be a list of non-empty strings, got {value!r}")
    return tuple(dict.fromkeys(str(item) for item in value))


def _parse_packages(table: TomlTable, where: str) -> Packages:
    _reject_unknown(table, ("apt", "snap", "flatpak", "flatpak_remote", "apt_update"), where)
    remote = _optional_str(table, "flatpak_remote", where)
    if remote is not None and (not remote or remote.startswith("-") or any(char.isspace() for char in remote)):
        raise ConfigError(f"{where}: flatpak_remote must be a remote name, got {remote!r}")
    apt_update = table.get("apt_update", False)
    if not isinstance(apt_update, bool):
        raise ConfigError(f"{where}: apt_update must be true or false, got {apt_update!r}")
    return Packages(
        apt=_str_tuple(table, "apt", where),
        snap=_str_tuple(table, "snap", where),
        flatpak=_str_tuple(table, "flatpak", where),
        flatpak_remote=remote or "flathub",
        apt_update=apt_update,
    )


def _parse_system(table: TomlTable, where: str) -> SystemConfig:
    _reject_unknown(table, ("timezone", "locale", "keyboard"), where)
    keyboard_table = table.get("keyboard", {})
    if not isinstance(keyboard_table, dict):
        raise ConfigError(f"{where}: keyboard must be a table, got {keyboard_table!r}")
    keyboard_where = f"{where} [keyboard]"
    _reject_unknown(keyboard_table, ("model", "layout", "variant", "options"), keyboard_where)
    keyboard = Keyboard(
        model=_optional_str(keyboard_table, "model", keyboard_where),
        layout=_optional_str(keyboard_table, "layout", keyboard_where),
        variant=_optional_str(keyboard_table, "variant", keyboard_where),
        options=_optional_str(keyboard_table, "options", keyboard_where),
    )
    return SystemConfig(
        timezone=_optional_str(table, "timezone", where),
        locale=_optional_str(table, "locale", where),
        keyboard=keyboard,
    )


def _check_modules_shape(table: TomlTable, where: str) -> None:
    """``[modules.<name>]`` must be tables: a scalar there would replace a lower layer's whole table."""
    modules = table.get("modules", {})
    if not isinstance(modules, dict):
        raise ConfigError(f"{where}: modules must be a table, got {modules!r}")
    for name, entry in modules.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: modules.{name} must be a table, got {entry!r}")


def _overlaps(first: PurePosixPath, second: PurePosixPath) -> bool:
    """True when one path equals or contains the other, so linking both would collide."""
    shorter = min(len(first.parts), len(second.parts))
    return first.parts[:shorter] == second.parts[:shorter]


def _leaves(root: Path) -> dict[PurePosixPath, Path]:
    """Every non-directory under ``root`` (a symlink to a directory counts as a leaf)."""
    found: dict[PurePosixPath, Path] = {}
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        real_dirs: list[str] = []
        for name in dirnames:
            if (directory / name).is_symlink():
                filenames.append(name)
            else:
                real_dirs.append(name)
        dirnames[:] = real_dirs
        for name in filenames:
            path = directory / name
            found[PurePosixPath(path.relative_to(root).as_posix())] = path
    return found


@dataclass(frozen=True)
class Profile:
    root: Path
    machine: str
    machine_source: MachineSource = "hostname"

    @classmethod
    def resolve(
        cls,
        profile: str | os.PathLike[str] | None = None,
        machine: str | None = None,
        *,
        env: Mapping[str, str] | None = None,
        hostname_file: Path = HOSTNAME_FILE,
        home: Path | None = None,
    ) -> Profile:
        environment = os.environ if env is None else env
        if profile is not None and not str(profile).strip():
            raise ConfigError("--profile needs a directory, got an empty value")
        requested = profile if profile is not None else environment.get(PROFILE_ENV) or None
        if requested is not None:
            root = Path(requested).expanduser().absolute()
            if not root.is_dir():
                raise ConfigError(f"profile directory {root} does not exist")
        else:
            base = home if home is not None else Path.home()
            root = base / DEFAULT_PROFILE_RELPATH
        if machine is not None:
            return cls(root, _validate_machine_name(machine), "flag")
        return cls(root, _validate_machine_name(detect_machine(hostname_file)), "hostname")

    @property
    def overlay_dir(self) -> Path:
        return self.root / "machines" / self.machine

    @property
    def profile_toml(self) -> Path:
        return self.root / "profile.toml"

    @property
    def machine_toml(self) -> Path:
        return self.overlay_dir / "machine.toml"

    @property
    def settings_dir(self) -> Path:
        return self.root / "settings"

    @property
    def overlay_settings_dir(self) -> Path:
        return self.overlay_dir / "settings"

    @property
    def files_dir(self) -> Path:
        return self.root / "files"

    @property
    def overlay_files_dir(self) -> Path:
        return self.overlay_dir / "files"

    @property
    def packages_toml(self) -> Path:
        return self.root / "packages.toml"

    @property
    def overlay_packages_toml(self) -> Path:
        return self.overlay_dir / "packages.toml"

    @property
    def system_toml(self) -> Path:
        return self.root / "system.toml"

    @property
    def overlay_system_toml(self) -> Path:
        return self.overlay_dir / "system.toml"

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    @property
    def has_overlay(self) -> bool:
        return self.overlay_dir.is_dir()

    def settings_panels(self) -> tuple[PanelFiles, ...]:
        def panels(directory: Path) -> dict[str, Path]:
            if not directory.is_dir():
                return {}
            return {path.stem: path for path in directory.glob("*.ini") if path.is_file()}

        shared = panels(self.settings_dir)
        overlay = panels(self.overlay_settings_dir)
        return tuple(
            PanelFiles(name, shared.get(name), overlay.get(name)) for name in sorted(shared.keys() | overlay.keys())
        )

    def files(self) -> tuple[ProfileFile, ...]:
        shared = _leaves(self.files_dir)
        overlay = _leaves(self.overlay_files_dir)
        result = [
            ProfileFile(rel, path, "shared", ())
            for rel, path in shared.items()
            if not any(_overlaps(rel, other) for other in overlay)
        ]
        result += [
            ProfileFile(
                rel,
                path,
                "overlay",
                tuple(source for shared_rel, source in shared.items() if _overlaps(shared_rel, rel)),
            )
            for rel, path in overlay.items()
        ]
        return tuple(sorted(result, key=lambda entry: entry.rel))

    def load_config(self, module_defaults: Mapping[str, TomlValue] | None = None) -> Config:
        shared = read_toml(self.profile_toml)
        overlay = read_toml(self.machine_toml)
        _check_modules_shape(shared, str(self.profile_toml))
        _check_modules_shape(overlay, str(self.machine_toml))
        return merge_layers([("module", module_defaults or {}), ("shared", shared), ("overlay", overlay)])

    def _load_union[T](self, shared: Path, overlay: Path, parse: Callable[[TomlTable, str], T]) -> T:
        """Parse each layer alone (so one layer cannot mask a typo in the other), then the union."""
        shared_table, overlay_table = read_toml(shared), read_toml(overlay)
        parse(shared_table, str(shared))
        parse(overlay_table, str(overlay))
        merged = merge_layers([("shared", shared_table), ("overlay", overlay_table)], union_lists=True)
        return parse(merged.data, str(shared))

    def load_packages(self) -> Packages:
        return self._load_union(self.packages_toml, self.overlay_packages_toml, _parse_packages)

    def load_system(self) -> SystemConfig:
        return self._load_union(self.system_toml, self.overlay_system_toml, _parse_system)

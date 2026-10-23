"""Parse and validate ``modules/<name>/module.toml`` into frozen dataclasses.

Accessed via: every command that lists or applies modules (``list``, ``apply``, ``status``);
the engine calls ``discover_modules(ctx.modules_dir)``.

Assumptions: validation is strict (unknown keys, wrong types and kinds that lack the fields
they need are all ``ConfigError``, exit 2) so a typo in a manifest fails loudly instead of
silently dropping a setting. Nothing here touches the system.

Public API:

- ``SourceKind``: ``SUBMODULE``, ``LOCAL``, ``BUILTIN``, ``SYSTEM`` (string values
  ``"submodule"``, ``"local"``, ``"builtin"``, ``"system"``). ``ego`` is reserved and rejected
  with ``not supported yet (#20)``.
- ``SettingsMode``: ``ENFORCE`` (default) or ``SEED``.
- ``Source(kind, path, install, check, root, preview, repo)``: ``path`` is set for submodule/local (relative to
  the repo root); ``install``, ``check`` and ``preview`` (optional read-only plan command) are argv
  tuples, set for ``system`` only, whose ``argv[0]`` is a path inside the module directory: a runner
  must execute ``manifest.directory / argv[0]`` with the rest as arguments, never a ``$PATH`` lookup;
  ``root`` (default True) says whether the installer needs sudo; ``repo`` (optional, system only) is the
  default git URL of the tool the module fetches: the planner hands it to the installer as the option
  ``repo`` unless ``[modules.<name>] repo`` in the profile sets one.
- ``ExtensionSpec(conflicts, retire, enable)``, ``SettingsFile(file, schema, mode)``,
  ``Seed(key, provider)``, ``Migration(from_uuid, from_path)``, ``Deps(apt)``.
- ``Install(apt, provides)``, ``Step(name, args)`` and ``Post(name, run, check, needs_root, after,
  description, unless_linked)``: the init-pipeline tables ``[install]``, ``[[step]]`` and ``[[post]]`` (spec, *Init
  pipeline*); ``PACKAGE_PLACEHOLDER`` matches the ``{packages.<manager>}`` argv placeholder.
- ``Manifest(name, directory, description, enabled, source, extension, settings, seeds,
  migrations, deps, order, phase, install, steps, post, manual)``; ``enabled`` is the module default that
  ``profile.toml`` / ``machine.toml`` can override; ``order`` (0-999, default ``DEFAULT_ORDER``) and
  ``phase`` (``base`` / ``packages`` / ``system`` as a ``Phase``, or ``None``) place the module's
  install-time actions in the plan; ``manual`` is the optional hint printed in the closing ``Manual
  steps`` block when the module's installer exits 10 (how the user finishes it).
- ``parse_manifest(name, text, directory, origin="engine") -> Manifest``.
- ``Manifest.origin`` is ``"engine"`` (``modules/<name>/``) or ``"profile"`` (``<profile>/modules/<name>/``, same
  manifest and kinds; a ``submodule`` is refused, a ``local`` path is relative to the module directory).
- ``load_manifest(directory, origin="engine") -> Manifest`` (module name = directory name).
- ``discover_modules(modules_dir, profile=None) -> tuple[Manifest, ...]``: sorted by name; directories
  without ``module.toml`` are not modules yet and are skipped; with a ``profile`` that exists, its own modules
  are added and one with the name of an engine module replaces it.
- ``module_defaults(manifests) -> TomlTable``: ``{"modules": {name: {"enabled": bool}}}``, the
  lowest layer for ``Profile.load_config``.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from ubuntu_tweaks.coverage import Layer, Rule, parse_rules
from ubuntu_tweaks.dconf_path import is_dconf_dir_path
from ubuntu_tweaks.actions import DEFAULT_ORDER, Phase
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.profile import Profile, TomlTable, TomlValue

MANIFEST_FILE = "module.toml"
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_EGO_MESSAGE = "source kind 'ego' is not supported yet (#20)"
MAX_ORDER = 999
PACKAGE_PLACEHOLDER = re.compile(r"\{packages\.(apt|snap|flatpak|brew)\}")
type Origin = Literal["engine", "profile"]
MODULE_PHASES = {"base": Phase.BASE, "packages": Phase.PACKAGES, "system": Phase.SYSTEM}
POST_AFTER_INSTALL = "install"


class SourceKind(StrEnum):
    SUBMODULE = "submodule"
    LOCAL = "local"
    BUILTIN = "builtin"
    SYSTEM = "system"


class SettingsMode(StrEnum):
    ENFORCE = "enforce"
    SEED = "seed"


@dataclass(frozen=True)
class Source:
    kind: SourceKind
    path: str | None = None
    install: tuple[str, ...] = ()
    check: tuple[str, ...] = ()
    root: bool = True
    preview: tuple[str, ...] = ()
    repo: str | None = None


@dataclass(frozen=True)
class ExtensionSpec:
    conflicts: tuple[str, ...] = ()
    retire: tuple[str, ...] = ()
    enable: tuple[
        str, ...
    ] = ()  # builtin only: uuids of extensions the system provides (apt), enabled by the toggle planner


@dataclass(frozen=True)
class SettingsFile:
    file: str
    schema: str | None = None
    mode: SettingsMode = SettingsMode.ENFORCE


@dataclass(frozen=True)
class Seed:
    key: str
    provider: str


@dataclass(frozen=True)
class Migration:
    from_uuid: str | None = None
    from_path: str | None = None


@dataclass(frozen=True)
class Deps:
    apt: tuple[str, ...] = ()


@dataclass(frozen=True)
class Install:
    """``[install]``: distro packages the module is for. ``provides`` maps a package to a command that
    counts as the package being there (brew's ``gh`` satisfies ``gh``)."""

    apt: tuple[str, ...] = ()
    provides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Post:
    """``[[post]]``: a follow-up command run after the module's install, with its own check."""

    name: str
    run: tuple[str, ...]
    check: tuple[str, ...]
    needs_root: bool = False
    after: str | None = None
    description: str = ""
    unless_linked: tuple[str, ...] = ()  # $HOME-relative files: the post is not planned when the profile links one


@dataclass(frozen=True)
class Fallback:
    """``[[fallback]]``: an alternative installer of a ``system`` module, tried when the primary fails for good or
    needs root that is not available (spec, *Reconcile and failover*)."""

    install: tuple[str, ...]
    root: bool
    description: str = ""


@dataclass(frozen=True)
class Step:
    """``[[step]]``: a named built-in step (``ubuntu_tweaks/steps.py``) with its own arguments."""

    name: str
    args: Mapping[str, TomlValue]


@dataclass(frozen=True)
class Manifest:
    name: str
    directory: Path
    description: str
    enabled: bool
    source: Source
    extension: ExtensionSpec | None = None
    settings: tuple[SettingsFile, ...] = ()
    seeds: tuple[Seed, ...] = ()
    migrations: tuple[Migration, ...] = ()
    deps: Deps = Deps()
    order: int = DEFAULT_ORDER
    phase: Phase | None = None
    install: Install = Install()
    steps: tuple[Step, ...] = ()
    post: tuple[Post, ...] = ()
    manual: str = ""  # how the user completes the step the installer reports as manual (exit 10)
    requires: tuple[str, ...] = ()  # modules whose failure blocks this one (failover dependencies)
    fallbacks: tuple[Fallback, ...] = ()  # system modules: alternative installers
    coverage: tuple[Rule, ...] = ()  # [[coverage]] rules this module contributes (spec, *Coverage*)
    origin: Origin = "engine"  # "profile": the module lives in the profile's modules/ directory


class _Reader:
    """Typed access to one TOML table; every error names the manifest and the table."""

    def __init__(self, table: Mapping[str, TomlValue], where: str) -> None:
        self.table = table
        self.where = where

    def fail(self, message: str) -> ConfigError:
        return ConfigError(f"{self.where}: {message}")

    def only(self, *allowed: str) -> None:
        unknown = sorted(set(self.table) - set(allowed))
        if unknown:
            raise self.fail(f"unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")

    def string(self, key: str) -> str | None:
        value = self.table.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise self.fail(f"'{key}' must be a non-empty string, got {value!r}")
        return value

    def required_string(self, key: str) -> str:
        value = self.string(key)
        if value is None:
            raise self.fail(f"missing required key '{key}'")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self.table.get(key, default)
        if not isinstance(value, bool):
            raise self.fail(f"'{key}' must be true or false, got {value!r}")
        return value

    def integer(self, key: str, default: int, low: int, high: int) -> int:
        value = self.table.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise self.fail(f"'{key}' must be a whole number from {low} to {high}, got {value!r}")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise self.fail(f"'{key}' must be a list of non-empty strings, got {value!r}")
        return tuple(str(item) for item in value)

    def sub(self, key: str) -> _Reader | None:
        value = self.table.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise self.fail(f"'{key}' must be a table, got {value!r}")
        return _Reader(value, f"{self.where} [{key}]")

    def array(self, key: str) -> list[_Reader]:
        value = self.table.get(key, [])
        if not isinstance(value, list):
            raise self.fail(f"'{key}' must be an array of tables ([[{key}]])")
        readers: list[_Reader] = []
        for index, item in enumerate(value, 1):
            if not isinstance(item, dict):
                raise self.fail(f"'{key}' must be an array of tables ([[{key}]])")
            readers.append(_Reader(item, f"{self.where} [[{key}]] #{index}"))
        return readers


def _relative_path(reader: _Reader, value: str, key: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise reader.fail(f"'{key}' must be a relative path inside the module, without '..', got {value!r}")


def _command(reader: _Reader, key: str) -> tuple[str, ...]:
    text = reader.required_string(key)
    try:
        argv = tuple(shlex.split(text))
    except ValueError as error:
        raise reader.fail(f"'{key}' is not a valid command line: {error}") from error
    if not argv or not argv[0]:
        raise reader.fail(f"'{key}' must name a program inside the module, got {text!r}")
    _relative_path(reader, argv[0], key)
    return argv


def _profile_source(source: Source, directory: Path, reader: _Reader) -> Source:
    """A module of the profile cannot be a git submodule of the engine; a ``local`` extension is found next
    to the module, so its path is made absolute (the engine resolves relative ones against its own checkout)."""
    if source.kind is SourceKind.SUBMODULE:
        raise reader.fail("a profile module cannot be a git submodule of the engine; use kind local or system")
    if source.kind is SourceKind.LOCAL and source.path is not None:
        return replace(source, path=str(directory / source.path))
    return source


def _parse_source(reader: _Reader) -> Source:
    kind_text = reader.required_string("kind")
    if kind_text == "ego":
        raise reader.fail(_EGO_MESSAGE)
    try:
        kind = SourceKind(kind_text)
    except ValueError:
        valid = ", ".join(kind.value for kind in SourceKind)
        raise reader.fail(f"unknown source kind {kind_text!r}; valid kinds: {valid}") from None
    if kind in (SourceKind.SUBMODULE, SourceKind.LOCAL):
        reader.only("kind", "path")
        path = reader.required_string("path")
        _relative_path(reader, path, "path")
        return Source(kind, path=path)
    if kind is SourceKind.SYSTEM:
        reader.only("kind", "install", "check", "root", "preview", "repo")
        return Source(
            kind,
            install=_command(reader, "install"),
            check=_command(reader, "check"),
            root=reader.boolean("root", True),
            preview=_command(reader, "preview") if "preview" in reader.table else (),
            repo=reader.string("repo"),
        )
    reader.only("kind")
    return Source(kind)


def _parse_extension(reader: _Reader, kind: SourceKind) -> ExtensionSpec:
    reader.only("enable", "conflicts", "retire")
    spec = ExtensionSpec(
        enable=reader.strings("enable"), conflicts=reader.strings("conflicts"), retire=reader.strings("retire")
    )
    if spec.enable and kind in (SourceKind.SUBMODULE, SourceKind.LOCAL):
        raise reader.fail("'enable' is for builtin modules; a submodule or local module's uuid is its metadata.json's")
    for uuid in (*spec.enable, *spec.conflicts, *spec.retire):
        if "/" in uuid or uuid in {".", ".."}:
            raise reader.fail(f"extension uuid {uuid!r} must not contain '/'")
    return spec


def _parse_settings(reader: _Reader, kind: SourceKind) -> SettingsFile:
    reader.only("file", "schema", "mode")
    file = reader.required_string("file")
    _relative_path(reader, file, "file")
    schema = reader.string("schema")
    if schema is None and kind is SourceKind.BUILTIN:
        raise reader.fail("a builtin module has no extension metadata, so 'schema' is required")
    mode_text = reader.string("mode") or SettingsMode.ENFORCE.value
    try:
        mode = SettingsMode(mode_text)
    except ValueError:
        valid = ", ".join(mode.value for mode in SettingsMode)
        raise reader.fail(f"unknown mode {mode_text!r}; valid modes: {valid}") from None
    return SettingsFile(file, schema, mode)


def _parse_seed(reader: _Reader) -> Seed:
    reader.only("key", "provider")
    return Seed(reader.required_string("key"), reader.required_string("provider"))


def _parse_migration(reader: _Reader) -> Migration:
    reader.only("from_uuid", "from_path")
    from_uuid = reader.string("from_uuid")
    from_path = reader.string("from_path")
    if from_uuid is None and from_path is None:
        raise reader.fail("needs 'from_uuid' or 'from_path'")
    if from_path is not None and not is_dconf_dir_path(from_path):
        raise reader.fail(f"'from_path' must be a dconf path like /org/gnome/x/, got {from_path!r}")
    return Migration(from_uuid, from_path)


def _parse_phase(reader: _Reader) -> Phase | None:
    text = reader.string("phase")
    if text is None:
        return None
    try:
        return MODULE_PHASES[text]
    except KeyError:
        raise reader.fail(f"unknown phase {text!r}; valid phases: {', '.join(MODULE_PHASES)}") from None


def _parse_install(reader: _Reader) -> Install:
    reader.only("apt", "provides")
    apt = reader.strings("apt")
    for name in apt:
        if name.startswith("-") or any(char.isspace() for char in name):
            raise reader.fail(f"{name!r} is not a package name (no leading '-', no whitespace)")
    provides_reader = reader.sub("provides")
    provides: list[tuple[str, str]] = []
    if provides_reader is not None:
        for package in provides_reader.table:
            if package not in apt:
                raise provides_reader.fail(f"{package!r} is not listed in apt")
            provides.append((package, provides_reader.required_string(package)))
    return Install(apt, tuple(provides))


def _parse_step(reader: _Reader) -> Step:
    name = reader.required_string("name")
    return Step(name, {key: value for key, value in reader.table.items() if key != "name"})


def _argv(reader: _Reader, key: str) -> tuple[str, ...]:
    value = reader.table.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise reader.fail(f"'{key}' must be a non-empty argv list of strings, got {value!r}")
    argv = tuple(str(item) for item in value)
    if "/" in argv[0] and not argv[0].startswith("/"):
        raise reader.fail(f"'{key}' must start with a program on PATH or an absolute path, got {argv[0]!r}")
    if PACKAGE_PLACEHOLDER.fullmatch(argv[0]):
        raise reader.fail(f"'{key}': a {{packages.<manager>}} placeholder cannot be the program")
    for item in argv:
        if "{packages" in item.lower() and not PACKAGE_PLACEHOLDER.fullmatch(item):
            raise reader.fail(
                f"'{key}': {item!r}: a {{packages.apt|snap|flatpak|brew}} placeholder must be a whole argument"
            )
    return argv


def _parse_fallbacks(readers: list[_Reader], source: Source, where: _Reader) -> tuple[Fallback, ...]:
    if readers and source.kind is not SourceKind.SYSTEM:
        raise where.fail("[[fallback]] is only for system modules (an alternative to [source] install)")
    fallbacks: list[Fallback] = []
    for reader in readers:
        reader.only("install", "root", "description")
        fallbacks.append(
            Fallback(
                _command(reader, "install"),
                reader.boolean("root", source.root),
                reader.string("description") or "",
            )
        )
    return tuple(fallbacks)


def _parse_posts(readers: list[_Reader]) -> tuple[Post, ...]:
    posts: list[Post] = []
    for reader in readers:
        reader.only("name", "run", "check", "needs_root", "after", "description", "unless_linked")
        name = reader.required_string("name")
        if not _NAME.fullmatch(name) or name == POST_AFTER_INSTALL:
            raise reader.fail(f"invalid post name {name!r}; use lowercase letters, digits, '.', '_' and '-'")
        earlier = [post.name for post in posts]
        if name in earlier:
            raise reader.fail(f"duplicate post name {name!r}")
        after = reader.string("after")
        if after is not None and after != POST_AFTER_INSTALL and after not in earlier:
            raise reader.fail(f"'after' must be '{POST_AFTER_INSTALL}' or an earlier [[post]] name, got {after!r}")
        posts.append(
            Post(
                name,
                _argv(reader, "run"),
                _argv(reader, "check"),
                reader.boolean("needs_root", False),
                after,
                reader.string("description") or "",
                reader.strings("unless_linked"),
            )
        )
    return tuple(posts)


def parse_manifest(name: str, text: str, directory: Path, origin: Origin = "engine") -> Manifest:
    where = str(directory / MANIFEST_FILE)
    if not _NAME.fullmatch(name):
        raise ConfigError(f"{where}: invalid module name {name!r}; use lowercase letters, digits, '.', '_' and '-'")
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{where}: invalid TOML: {error}") from error

    root = _Reader(table, where)
    root.only(
        "description",
        "enabled",
        "order",
        "phase",
        "source",
        "extension",
        "settings",
        "seeds",
        "migrations",
        "deps",
        "install",
        "step",
        "post",
        "manual",
        "requires",
        "fallback",
        "coverage",
    )
    source_reader = root.sub("source")
    if source_reader is None:
        raise root.fail("missing required table [source]")
    source = _parse_source(source_reader)
    if origin == "profile":
        source = _profile_source(source, directory, root)

    extension_reader = root.sub("extension")
    has_code = source.kind in (SourceKind.SUBMODULE, SourceKind.LOCAL)
    if extension_reader is not None and not has_code and source.kind is not SourceKind.BUILTIN:
        raise root.fail(f"[extension] is only for submodule, local and builtin modules, not {source.kind.value}")
    settings = tuple(_parse_settings(item, source.kind) for item in root.array("settings"))
    seeds = tuple(_parse_seed(item) for item in root.array("seeds"))
    migrations = tuple(_parse_migration(item) for item in root.array("migrations"))
    if source.kind is SourceKind.SYSTEM and (settings or seeds or migrations):
        raise root.fail("a system module takes no [[settings]], [[seeds]] or [[migrations]]")

    deps_reader = root.sub("deps")
    deps = Deps()
    if deps_reader is not None:
        deps_reader.only("apt")
        deps = Deps(apt=deps_reader.strings("apt"))

    install_reader = root.sub("install")
    steps = tuple(_parse_step(item) for item in root.array("step"))
    if source.kind is SourceKind.SYSTEM and (install_reader is not None or steps):
        raise root.fail("a system module has its own installer: it takes no [install] or [[step]] (use [[post]])")

    return Manifest(
        name=name,
        directory=directory,
        description=root.string("description") or "",
        enabled=root.boolean("enabled", True),
        source=source,
        extension=_parse_extension(extension_reader, source.kind) if extension_reader is not None else None,
        settings=settings,
        seeds=seeds,
        migrations=migrations,
        deps=deps,
        order=root.integer("order", DEFAULT_ORDER, 0, MAX_ORDER),
        phase=_parse_phase(root),
        install=_parse_install(install_reader) if install_reader is not None else Install(),
        steps=steps,
        post=_parse_posts(root.array("post")),
        manual=root.string("manual") or "",
        requires=root.strings("requires"),
        fallbacks=_parse_fallbacks(root.array("fallback"), source, root),
        coverage=tuple(parse_rules([item.table for item in root.array("coverage")], Layer.MODULE, where, name)),
        origin=origin,
    )


def load_manifest(directory: Path, origin: Origin = "engine") -> Manifest:
    path = directory / MANIFEST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{path}: cannot read: {error.strerror or error}") from error
    return parse_manifest(directory.name, text, directory, origin)


def _load_dir(modules_dir: Path, origin: Origin) -> tuple[Manifest, ...]:
    if not modules_dir.is_dir():
        return ()
    return tuple(
        load_manifest(directory, origin)
        for directory in sorted(modules_dir.iterdir())
        if (directory / MANIFEST_FILE).is_file()
    )


def discover_modules(modules_dir: Path, profile: Profile | None = None) -> tuple[Manifest, ...]:
    """The engine's modules plus the profile's own (``<profile>/modules/<name>/``, spec *Profile modules*);
    a profile module with the name of an engine module replaces it."""
    found = {manifest.name: manifest for manifest in _load_dir(modules_dir, "engine")}
    if profile is not None and profile.exists:
        found.update({manifest.name: manifest for manifest in _load_dir(profile.modules_dir, "profile")})
    return tuple(found[name] for name in sorted(found))


def module_defaults(manifests: Sequence[Manifest]) -> TomlTable:
    return {"modules": {manifest.name: {"enabled": manifest.enabled} for manifest in manifests}}

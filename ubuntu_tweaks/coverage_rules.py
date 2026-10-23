"""Loading the four layers of coverage rules (spec, *Coverage*).

Accessed via: ``commands/coverage.py`` and ``commands/status.py`` (one ``load_rules(ctx)`` per scan).

Layers, lowest first: the engine's ``coverage_defaults.toml``, the ``[[coverage]]`` tables of every enabled module
(``Manifest.coverage``, parsed with the manifest), ``<profile root>/coverage.toml`` then
``<profile root>/coverage.d/*.toml`` (by name), and the same two for the machine overlay ``machines/<host>/``, so
several authors can add rules in separate files. ``Rules.classify`` lets the highest layer win; inside a layer the
first rule that matches wins, in load order. A missing file is an empty layer; a malformed one is ``ConfigError``
(exit 2) naming the file.

Assumptions: pure reads of TOML files, no machine access.

Public API: ``DEFAULTS_TOML`` (path of the engine rules), ``COVERAGE_TOML`` (the file name of the profile and machine
layers), ``rule_entries(path)`` (the raw ``[[coverage]]`` tables of one file, for ``profile_check``) and
``load_rules(ctx) -> Rules``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Layer, Rule, Rules, parse_rules
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import module_defaults
from ubuntu_tweaks.profile import TomlValue, read_toml

DEFAULTS_TOML = Path(__file__).with_name("coverage_defaults.toml")
COVERAGE_TOML = "coverage.toml"
COVERAGE_D = "coverage.d"


def rule_entries(path: Path) -> list[Mapping[str, TomlValue]]:
    """The ``[[coverage]]`` tables of ``path`` (nothing when the file is absent)."""
    table = read_toml(path)
    unknown = sorted(set(table) - {"coverage"})
    if unknown:
        raise ConfigError(f"{path}: unknown top-level key(s) {', '.join(unknown)}; only [[coverage]] is read here")
    value = table.get("coverage", [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(f"{path}: 'coverage' must be an array of [[coverage]] tables")
    return [item for item in value if isinstance(item, dict)]


def _file_rules(path: Path, layer: Layer, owner: str) -> list[Rule]:
    return parse_rules(rule_entries(path), layer, str(path), owner)


def drop_ins(directory: Path) -> list[Path]:
    """``coverage.d/*.toml`` regular files of ``directory``, sorted by name (empty when the directory is absent)."""
    return sorted(path for path in (directory / COVERAGE_D).glob("*.toml") if path.is_file())


def profile_layer_files(ctx: Context) -> list[tuple[Layer, str, Path]]:
    """The profile and machine rule files in load order: ``coverage.toml`` first, then ``coverage.d/*.toml`` by name,
    the profile's before the machine overlay's. The label names the file in messages and ``owner``."""
    found: list[tuple[Layer, str, Path]] = []
    profile = ctx.profile
    for layer, directory, prefix in (
        (Layer.PROFILE, profile.root, ""),
        (Layer.MACHINE, profile.overlay_dir, f"machines/{profile.machine}/"),
    ):
        found.append((layer, prefix + COVERAGE_TOML, directory / COVERAGE_TOML))
        found += [(layer, f"{prefix}{COVERAGE_D}/{path.name}", path) for path in drop_ins(directory)]
    return found


def load_rules(ctx: Context) -> Rules:
    profile = ctx.profile
    manifests = ctx.manifests()
    config = profile.load_config(module_defaults(manifests))
    rules = _file_rules(DEFAULTS_TOML, Layer.ENGINE, "")
    for manifest in manifests:
        if config.module_enabled(manifest.name):
            rules += manifest.coverage
    for layer, label, path in profile_layer_files(ctx):
        rules += _file_rules(path, layer, label)
    return Rules(tuple(rules))

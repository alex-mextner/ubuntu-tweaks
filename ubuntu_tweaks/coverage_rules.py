"""Loading the four layers of coverage rules (spec, *Coverage*).

Accessed via: ``commands/coverage.py`` and ``commands/status.py`` (one ``load_rules(ctx)`` per scan).

Layers, lowest first: the engine's ``coverage_defaults.toml``, the ``[[coverage]]`` tables of every enabled module
(``Manifest.coverage``, parsed with the manifest), ``<profile root>/coverage.toml`` and the machine overlay's
``machines/<host>/coverage.toml``. ``Rules.classify`` lets the highest layer win; inside a layer the first rule that
matches wins. A missing file is an empty layer; a malformed one is ``ConfigError`` (exit 2) naming the file.

Assumptions: pure reads of TOML files, no machine access.

Public API: ``DEFAULTS_TOML`` (path of the engine rules) and ``load_rules(ctx) -> Rules``.
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


def _entries(path: Path) -> list[Mapping[str, TomlValue]]:
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
    return parse_rules(_entries(path), layer, str(path), owner)


def load_rules(ctx: Context) -> Rules:
    profile = ctx.profile
    manifests = ctx.manifests()
    config = profile.load_config(module_defaults(manifests))
    rules = _file_rules(DEFAULTS_TOML, Layer.ENGINE, "")
    for manifest in manifests:
        if config.module_enabled(manifest.name):
            rules += manifest.coverage
    rules += _file_rules(profile.root / COVERAGE_TOML, Layer.PROFILE, COVERAGE_TOML)
    rules += _file_rules(
        profile.overlay_dir / COVERAGE_TOML, Layer.MACHINE, f"machines/{profile.machine}/{COVERAGE_TOML}"
    )
    return Rules(tuple(rules))

"""``ubuntu-tweak machine``: which machine this is and where each setting comes from. Read-only.

Accessed via: ``bin/ubuntu-tweak machine [--machine NAME] [--only a,b]``.

Prints the machine name and how it was detected, whether the profile and the machine overlay
exist, and for each module setting, panel file and linked file whether it comes from the
shared profile or the machine overlay. Public API: ``register(subparsers)``,
``run(ctx, args) -> int`` and ``render(ctx) -> str`` (the report, for tests).
"""

from __future__ import annotations

import argparse
import json

from ubuntu_tweaks import ui
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.manifest import discover_modules, module_defaults
from ubuntu_tweaks.profile import Config, Layer, Profile, TomlValue

_LAYER_LABEL: dict[Layer, str] = {"module": "module default", "shared": "shared", "overlay": "overlay"}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "machine",
        help="show the detected machine and whether each setting comes from the shared profile or the overlay",
    )
    parser.set_defaults(handler=run)


def _header(profile: Profile) -> list[str]:
    source = "--machine" if profile.machine_source == "flag" else "/etc/hostname"
    lines = [f"machine:  {profile.machine} (from {source})"]
    lines.append(f"profile:  {profile.root} ({'present' if profile.exists else 'missing: module defaults only'})")
    if profile.has_overlay:
        lines.append(f"overlay:  {profile.overlay_dir} (present)")
    else:
        lines.append(f"overlay:  {profile.overlay_dir} (absent: shared profile only)")
    return lines


def _module_lines(ctx: Context, config: Config) -> list[str]:
    manifests = {manifest.name: manifest for manifest in discover_modules(ctx.modules_dir)}
    configured = {path[1] for path in config.origins if len(path) > 2 and path[0] == "modules"}
    ctx.require_modules(manifests.keys())
    lines = ["", "modules:"]
    for name in sorted(manifests.keys() | configured):
        if not ctx.selects(name):
            continue
        note = "" if name in manifests else " (no such module)"
        state = "enabled" if config.module_enabled(name) else "disabled"
        lines.append(f"  {name}: {state} ({_LAYER_LABEL[config.origin('modules', name, 'enabled') or 'module']}){note}")
        for path, layer in sorted(config.origins.items()):
            if path[:2] == ("modules", name) and path != ("modules", name, "enabled"):
                lines.append(f"    {'.'.join(path[2:])} = {_format(config.get(*path))} ({_LAYER_LABEL[layer]})")
    return lines


def _format(value: TomlValue | None) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _panel_lines(profile: Profile) -> list[str]:
    lines = ["", "settings bundles:"]
    origins: dict[str, set[str]] = {}
    for bundle in profile.bundle_files():
        origins.setdefault(bundle.name, set()).add(bundle.origin)
    for name in sorted(origins):
        if origins[name] == {"shared", "overlay"}:
            origin = "overlay over shared (key-level)"
        else:
            origin = "overlay" if "overlay" in origins[name] else "shared"
        lines.append(f"  {name}: {origin}")
    if not origins:
        lines.append("  (none)")
    return lines


def _file_lines(profile: Profile) -> list[str]:
    lines = ["", "linked files:"]
    files = profile.files()
    for entry in files:
        origin = "overlay (shadows shared)" if entry.shadows else entry.origin
        lines.append(f"  ~/{entry.rel}: {origin}")
    if not files:
        lines.append("  (none)")
    return lines


def _toml_origin(shared: bool, overlay: bool) -> str:
    if shared and overlay:
        return "shared + overlay (lists unioned)"
    return "overlay" if overlay else "shared" if shared else "none"


def _root_lines(profile: Profile) -> list[str]:
    return [
        "",
        f"packages.toml: {_toml_origin(profile.packages_toml.is_file(), profile.overlay_packages_toml.is_file())}",
        f"system.toml:   {_toml_origin(profile.system_toml.is_file(), profile.overlay_system_toml.is_file())}",
    ]


_EXPLAIN = {
    "modules:": "Each module, whether it is on for this machine, and which layer decided: "
    "module default < shared profile < machine overlay.",
    "settings bundles:": "Groups of GNOME settings kept in the profile; for this machine an overlay key beats a shared one.",
    "linked files:": "Profile files that are symlinked into your home directory.",
}


def render(ctx: Context) -> str:
    profile = ctx.profile
    config = profile.load_config(module_defaults(discover_modules(ctx.modules_dir)))
    lines = _header(profile)
    lines += _module_lines(ctx, config)
    lines += _panel_lines(profile)
    lines += _file_lines(profile)
    lines += _root_lines(profile)
    if ui.rich():
        explained: list[str] = []
        for line in lines:
            explained.append(line)
            if line in _EXPLAIN:
                explained += ui.wrap(_EXPLAIN[line], indent=2)
        return ui.decorate("\n".join(explained), "machine")
    return "\n".join(lines)


def run(ctx: Context, args: argparse.Namespace) -> int:
    print(render(ctx))
    return 0

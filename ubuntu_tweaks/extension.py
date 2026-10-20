"""GNOME Shell extension modules: where the code is, and what its ``metadata.json`` says.

Accessed via: the ``submodule``, ``schemas``, ``extension_links`` and ``toggle`` planners.

Assumptions: nothing here writes. The uuid and the schema directory are read from the
extension's own ``metadata.json`` every time they are asked for, never hardcoded and never
cached, so a module keeps working across a rename of the extension (hyperdock#3) and an
action created before a submodule was initialized sees the files once it is.

Public API:

- ``ExtensionMetadata(uuid, settings_schema)``.
- ``ExtensionModule(manifest, source_dir)``: ``name``, ``schemas_dir`` (``<source>/schemas``),
  ``metadata() -> ExtensionMetadata | None`` (``None`` while ``metadata.json`` is absent, i.e. the
  submodule is not initialized), ``require_metadata()`` (raises ``ConfigError`` instead).
- ``extension_modules(ctx, *, enabled_only=True) -> list[ExtensionModule]``: the ``submodule``
  and ``local`` modules, ordered by name; ``enabled_only`` applies the merged config (module
  default < profile.toml < machine.toml). An *enabled* ``local`` module without ``metadata.json``
  is ``ConfigError`` (exit 2): its code is in this repo, so a missing file is a typo; a disabled
  one is not looked at.
- ``user_extensions_dir(home=None, env=None) -> Path``: where the Shell looks for the user's
  extensions (``$XDG_DATA_HOME`` else ``<home>/.local/share``, then ``gnome-shell/extensions``);
  the same rule as ``settings.default_extension_roots``, kept here so this module needs no
  PyGObject.
- ``tilde(path) -> str``: ``path`` with the home directory shortened to ``~``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import Manifest, SourceKind, discover_modules, module_defaults

METADATA_FILE = "metadata.json"
RELOGIN_HINT = "Log out and back in to load new or changed GNOME extension code (Shell on Wayland reads it at login)."


@dataclass(frozen=True)
class ExtensionMetadata:
    uuid: str
    settings_schema: str | None


@dataclass(frozen=True)
class ExtensionModule:
    manifest: Manifest
    source_dir: Path

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def schemas_dir(self) -> Path:
        return self.source_dir / "schemas"

    def metadata(self) -> ExtensionMetadata | None:
        path = self.source_dir / METADATA_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ConfigError(f"{path}: cannot read: {error.strerror or error}") from error
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ConfigError(f"{path}: invalid JSON: {error}") from error
        uuid = data.get("uuid") if isinstance(data, dict) else None
        if not isinstance(uuid, str) or not uuid or "/" in uuid or uuid in {".", ".."}:
            raise ConfigError(f"{path}: 'uuid' must be a non-empty string without '/', got {uuid!r}")
        schema = data.get("settings-schema")
        if schema is not None and not isinstance(schema, str):
            raise ConfigError(f"{path}: 'settings-schema' must be a string, got {schema!r}")
        return ExtensionMetadata(uuid, schema)

    def require_metadata(self) -> ExtensionMetadata:
        found = self.metadata()
        if found is None:
            raise ConfigError(
                f"module {self.name}: {self.source_dir / METADATA_FILE} not found"
                + (" (initialize the submodule first)" if self.manifest.source.kind is SourceKind.SUBMODULE else "")
            )
        return found


def extension_modules(ctx: Context, *, enabled_only: bool = True) -> list[ExtensionModule]:
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    found: list[ExtensionModule] = []
    for manifest in manifests:
        if manifest.source.kind not in (SourceKind.SUBMODULE, SourceKind.LOCAL):
            continue
        enabled = config.module_enabled(manifest.name)
        if enabled_only and not enabled:
            continue
        assert manifest.source.path is not None  # the manifest parser requires it for these kinds
        module = ExtensionModule(manifest, ctx.repo_root / manifest.source.path)
        if enabled and manifest.source.kind is SourceKind.LOCAL:
            module.require_metadata()
        found.append(module)
    return found


def user_extensions_dir(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    data_home = environment.get("XDG_DATA_HOME") or (home or Path.home()) / ".local/share"
    return Path(data_home) / "gnome-shell/extensions"


def tilde(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    return "~" + text[len(home) :] if text.startswith(home + os.sep) else text

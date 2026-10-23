"""extensions adapter: the GNOME Shell extensions that are switched on (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``.

Assumptions:

- Items are ``extension:<uuid>`` for every uuid in the effective ``org.gnome.shell enabled-extensions`` (what the Shell
  loads, the Ubuntu defaults included: the engine rules exclude those). ``detail`` says whether the code sits in the
  user's ``~/.local/share/gnome-shell/extensions`` or comes from the system. A machine without the GNOME Shell schema
  has no extensions: nothing is reported.
- ``owned`` is every extension uuid an engine or profile module ships (``metadata.json`` of the module's source, read
  without requiring it: a submodule that is not checked out yet has no uuid to claim).

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.extension import ExtensionModule
from ubuntu_tweaks.manifest import SourceKind

NAME = "extensions"
_SCHEMA = "org.gnome.shell"
_KEY = "enabled-extensions"


def discover(ctx: Context) -> Iterable[Item]:
    backend = system().settings(ctx)
    if not backend.has_key(_SCHEMA, _KEY):
        return
    uuids = backend.effective(_SCHEMA, None, _KEY).unpack()
    user_directory = system().home / ".local/share/gnome-shell/extensions"
    for uuid in sorted(set(uuids)):
        where = "user-installed" if (user_directory / uuid).exists() else "system"
        yield Item(NAME, f"extension:{uuid}", "extension", f"enabled, {where}")


def owned(ctx: Context) -> Mapping[str, str]:
    found: dict[str, str] = {}
    for manifest in ctx.manifests():
        if manifest.source.kind not in (SourceKind.SUBMODULE, SourceKind.LOCAL) or manifest.source.path is None:
            continue
        try:
            metadata = ExtensionModule(manifest, ctx.repo_root / manifest.source.path).metadata()
        except ConfigError:  # a broken metadata.json is the extension planners' error, not a coverage finding
            continue
        if metadata is not None:
            found.setdefault(f"extension:{metadata.uuid}", f"module {manifest.name}")
    return found

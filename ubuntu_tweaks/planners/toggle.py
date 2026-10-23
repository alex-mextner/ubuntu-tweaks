"""Planner: enable/disable shell extensions as single-item edits of the user's dconf lists.

Accessed via: planner discovery (``planners/__init__.py``).

Assumptions:

- ``org.gnome.shell`` ``enabled-extensions`` / ``disabled-extensions`` are edited through the
  *user value* only (``backend.user_value``); the effective value is never written back, because
  on Ubuntu it comes from the session's override and writing it would freeze that override. An
  unset user value counts as an empty list, so the first single-item write creates it.
- A ``builtin`` module whose apt package ships an extension names it in ``[extension] enable``: the
  uuids are enabled like a code module's (no ``metadata.json`` to read, so no lazy lookup).
- For an enabled module: ``enable`` (the module's uuid, from ``metadata.json``, resolved lazily)
  is in the user ``enabled-extensions`` and not in ``disabled-extensions``; every ``conflicts`` uuid
  is not in ``enabled-extensions`` and is in ``disabled-extensions`` (the Shell lets
  ``disabled-extensions`` win over a system default); every ``retire`` uuid is dropped from
  ``enabled-extensions`` only. ``enable`` has kind ``enable``, the other two ``disable``.
- ``extras()`` lists the user-enabled extensions that no module declares (as its uuid, a
  conflict or a retire).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

from ubuntu_tweaks.actions import Action, ManualStepRequired, Phase  # noqa: E402
from ubuntu_tweaks.context import Context  # noqa: E402
from ubuntu_tweaks.extension import ExtensionModule, extension_modules  # noqa: E402
from ubuntu_tweaks.manifest import SourceKind, module_defaults  # noqa: E402
from ubuntu_tweaks.settings import SettingsBackend  # noqa: E402

SCHEMA = "org.gnome.shell"
ENABLED = "enabled-extensions"
DISABLED = "disabled-extensions"


def user_list(backend: SettingsBackend, key: str) -> list[str]:
    value = backend.user_value(SCHEMA, None, key)
    return [] if value is None else list(value.unpack())


def _edit(backend: SettingsBackend, key: str, *, add: str | None = None, remove: str | None = None) -> None:
    """Add or remove one item of the user value; write only when something changes."""
    current = user_list(backend, key)
    wanted = [item for item in current if item != remove]
    if add is not None and add not in wanted:
        wanted.append(add)
    if wanted != current:
        backend.write(SCHEMA, None, key, GLib.Variant.new_strv(wanted))


class _ExtensionListAction(Action):
    phase = Phase.TOGGLE

    def __init__(self, backend: SettingsBackend, module: str, uuid: Callable[[], str | None], subject: str) -> None:
        self.backend = backend
        self.scope = module
        self._uuid = uuid
        self._subject = subject

    def resolved(self) -> str:
        uuid = self._uuid()
        if uuid is None:
            raise ManualStepRequired(f"module {self.scope}: extension uuid unknown, its sources are missing")
        return uuid

    def _label(self) -> str:
        return self._uuid() or f"{self._subject} (uuid known once the submodule is initialized)"


class EnableExtension(_ExtensionListAction):
    kind = "enable"

    def describe(self) -> str:
        return f"enable {self._label()}"

    def check(self) -> bool:
        uuid = self._uuid()
        return (
            uuid is not None
            and uuid in user_list(self.backend, ENABLED)
            and uuid not in user_list(self.backend, DISABLED)
        )

    def run(self) -> None:
        uuid = self.resolved()
        _edit(self.backend, ENABLED, add=uuid)
        _edit(self.backend, DISABLED, remove=uuid)


class DisableConflict(_ExtensionListAction):
    kind = "disable"

    def describe(self) -> str:
        return f"disable {self._label()} (conflicts with module {self.scope})"

    def check(self) -> bool:
        uuid = self._uuid()
        if uuid is None:
            return False
        return uuid not in user_list(self.backend, ENABLED) and uuid in user_list(self.backend, DISABLED)

    def run(self) -> None:
        uuid = self.resolved()
        _edit(self.backend, ENABLED, remove=uuid)
        _edit(self.backend, DISABLED, add=uuid)


class RetireExtension(_ExtensionListAction):
    kind = "disable"

    def describe(self) -> str:
        return f"drop {self._label()} from enabled-extensions (retired by module {self.scope})"

    def check(self) -> bool:
        uuid = self._uuid()
        return uuid is not None and uuid not in user_list(self.backend, ENABLED)

    def run(self) -> None:
        _edit(self.backend, ENABLED, remove=self.resolved())


def _fixed(uuid: str) -> Callable[[], str]:
    return lambda: uuid


def _lazy_uuid(module: ExtensionModule) -> Callable[[], str | None]:
    def resolve() -> str | None:
        metadata = module.metadata()
        return None if metadata is None else metadata.uuid

    return resolve


def build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]:
    actions: list[Action] = []
    for module in extension_modules(ctx):
        actions.append(EnableExtension(backend, module.name, _lazy_uuid(module), module.name))
        spec = module.manifest.extension
        if spec is None:
            continue
        actions += [DisableConflict(backend, module.name, _fixed(uuid), uuid) for uuid in spec.conflicts]
        actions += [RetireExtension(backend, module.name, _fixed(uuid), uuid) for uuid in spec.retire]
    actions += _system_extension_actions(ctx, backend)
    return actions


def _system_extension_actions(ctx: Context, backend: SettingsBackend) -> list[Action]:
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    actions: list[Action] = []
    for manifest in manifests:
        spec = manifest.extension
        if spec is None or manifest.source.kind is not SourceKind.BUILTIN or not config.module_enabled(manifest.name):
            continue
        actions += [EnableExtension(backend, manifest.name, _fixed(uuid), uuid) for uuid in spec.enable]
        actions += [DisableConflict(backend, manifest.name, _fixed(uuid), uuid) for uuid in spec.conflicts]
        actions += [RetireExtension(backend, manifest.name, _fixed(uuid), uuid) for uuid in spec.retire]
    return actions


def extras(ctx: Context, backend: SettingsBackend) -> Sequence[str]:
    declared: set[str] = set()
    for module in extension_modules(ctx, enabled_only=False):
        metadata = module.metadata()
        if metadata is not None:
            declared.add(metadata.uuid)
    for manifest in ctx.manifests():
        if manifest.extension is not None:
            declared.update(manifest.extension.enable, manifest.extension.conflicts, manifest.extension.retire)
    return [
        f"extension {uuid} is enabled but no module declares it"
        for uuid in user_list(backend, ENABLED)
        if uuid not in declared
    ]

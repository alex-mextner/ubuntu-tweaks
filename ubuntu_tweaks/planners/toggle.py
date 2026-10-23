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

import os
from collections.abc import Callable, Sequence

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

from ubuntu_tweaks.actions import Action, ManualStepRequired, Phase  # noqa: E402
from ubuntu_tweaks.context import Context  # noqa: E402
from ubuntu_tweaks.extension import ExtensionModule, extension_modules  # noqa: E402
from ubuntu_tweaks.manifest import SourceKind, module_defaults  # noqa: E402
from ubuntu_tweaks.settings import SettingsBackend  # noqa: E402
from ubuntu_tweaks.process import Process  # noqa: E402

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
    """Turn a conflicting extension off, but only once the replacement can actually load: disabling the Ubuntu dock
    while hyperdock is only linked (GNOME on Wayland reads new extension code at login) would leave no dock at all."""

    kind = "disable"

    def __init__(
        self,
        backend: SettingsBackend,
        module: str,
        uuid: Callable[[], str | None],
        subject: str,
        replacement_loaded: Callable[[], bool] = lambda: True,
    ) -> None:
        super().__init__(backend, module, uuid, subject)
        self._replacement_loaded = replacement_loaded

    def describe(self) -> str:
        waiting = (
            "" if self._replacement_loaded() else f"; waits until the shell has loaded {self.scope} (log out and in)"
        )
        return f"disable {self._label()} (conflicts with module {self.scope}{waiting})"

    def check(self) -> bool:
        uuid = self._uuid()
        if uuid is None:
            return False
        return uuid not in user_list(self.backend, ENABLED) and uuid in user_list(self.backend, DISABLED)

    def run(self) -> None:
        uuid = self.resolved()
        if not self._replacement_loaded():
            raise ManualStepRequired(
                f"{uuid} stays enabled until the shell has loaded {self.scope}: log out and back in, then run "
                "`ubuntu-tweak apply --commit` again (the autoheal module does that at login)"
            )
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


ASSUME_LOADED_ENV = "UBUNTU_TWEAK_SHELL_LOADED"
LIST_TIMEOUT_SECONDS = 15


def _loaded_check(process: Process, replacement: Callable[[], str | None]) -> Callable[[], bool]:
    """True when the running shell knows the replacement extension (``gnome-extensions list``), or when that cannot
    be asked (no ``gnome-extensions``, no session bus) or ``UBUNTU_TWEAK_SHELL_LOADED=1`` says to assume it; the
    answer is read once per plan."""
    cache: list[bool] = []

    def loaded() -> bool:
        if not cache:
            uuid = replacement()
            if os.environ.get(ASSUME_LOADED_ENV) == "1":
                cache.append(True)
            elif uuid is None:
                cache.append(False)
            else:
                result = process.query(["gnome-extensions", "list"], timeout=LIST_TIMEOUT_SECONDS)
                cache.append(uuid in result.stdout.split() if result.ok else True)
        return cache[0]

    return loaded


def _fixed(uuid: str) -> Callable[[], str]:
    return lambda: uuid


def _lazy_uuid(module: ExtensionModule) -> Callable[[], str | None]:
    def resolve() -> str | None:
        metadata = module.metadata()
        return None if metadata is None else metadata.uuid

    return resolve


def build(ctx: Context, backend: SettingsBackend, *, process: Process | None = None) -> Sequence[Action]:
    actions: list[Action] = []
    runner = process or Process()
    for module in extension_modules(ctx):
        replacement = _lazy_uuid(module)
        actions.append(EnableExtension(backend, module.name, replacement, module.name))
        spec = module.manifest.extension
        if spec is None:
            continue
        loaded = _loaded_check(runner, replacement)
        actions += [DisableConflict(backend, module.name, _fixed(uuid), uuid, loaded) for uuid in spec.conflicts]
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

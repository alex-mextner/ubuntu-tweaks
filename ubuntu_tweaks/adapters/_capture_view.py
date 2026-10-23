"""What the adapters that classify like ``capture`` share: its rule tables and the settings schema index.

Accessed via: ``adapters/dconf.py`` and ``adapters/files.py`` (``classify``); names starting with ``_`` are skipped by
adapter discovery.

Assumptions: ``classify`` is called once per item of a scan, so loading ``panels.toml`` / ``defaults.toml`` and indexing
every installed schema each time would dominate the scan. ``capture_view`` keeps the view of the last
``(context, System)`` pair it was asked about and rebuilds it when either changes; one scan has one of each. The view
reads only: the rules from the engine tables and the profile's ``[capture]`` table, the schemas through the backend.

Public API: ``CaptureView`` (``rules``, ``backend``, ``index``), ``capture_view(ctx) -> CaptureView`` and
``forget_view()`` (``adapters.scan`` calls it first, so a scan never sees a previous one's rules).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from ubuntu_tweaks.adapters._system import System, system
from ubuntu_tweaks.capture.rules import Rules, load_rules
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.setkey import SchemaIndex
from ubuntu_tweaks.settings import SettingsBackend


@dataclass
class CaptureView:
    rules: Rules
    machine: System
    ctx: Context

    @functools.cached_property
    def backend(self) -> SettingsBackend:
        return self.machine.settings(self.ctx)

    @functools.cached_property
    def index(self) -> SchemaIndex:
        return SchemaIndex(self.backend)


_last: tuple[Context, System, CaptureView] | None = None


def forget_view() -> None:
    global _last
    _last = None


def capture_view(ctx: Context) -> CaptureView:
    global _last
    machine = system()
    if _last is not None and _last[0] is ctx and _last[1] is machine:
        return _last[2]
    table = ctx.profile.load_config().get("capture")
    view = CaptureView(load_rules(table if isinstance(table, dict) else None), machine, ctx)
    _last = (ctx, machine, view)
    return view

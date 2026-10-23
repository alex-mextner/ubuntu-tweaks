"""Drop-in module discovery: the one helper behind ``commands/`` and ``planners/``.

Accessed via: ``cli.discover_commands`` and ``planners.discover_planners``.

Contract: every module of ``package`` whose name does not start with ``_`` is imported and must
expose a callable ``entry``; one that does not is ``ConfigError`` (exit 2), so a typo in a drop-in
file fails loudly instead of silently dropping the command or planner.

Public API: ``discover(package, *, kind, entry, signature) -> list[ModuleType]`` sorted by module
name (``kind`` and ``signature`` only word the error: ``planner module X has no build(ctx, backend)``).
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from ubuntu_tweaks.errors import ConfigError


def discover(package: ModuleType, *, kind: str, entry: str, signature: str) -> list[ModuleType]:
    found: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        if not callable(getattr(module, entry, None)):
            raise ConfigError(f"{kind} module {module.__name__} has no {signature}")
        found.append(module)
    return found

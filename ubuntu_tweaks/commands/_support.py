"""Helpers shared by the command modules (skipped by command discovery: the name starts with ``_``).

Public API: ``has_gi() -> bool`` (PyGObject is importable) and ``require_gi()``, which raises
``CommandError`` naming ``ubuntu-tweak doctor --yes`` instead of letting a command that needs
PyGObject die with a ``ModuleNotFoundError`` traceback on a machine that lacks ``python3-gi``.
"""

from __future__ import annotations

import importlib
import importlib.util

from ubuntu_tweaks.proc import CommandError


def has_gi() -> bool:
    importlib.invalidate_caches()
    spec = importlib.util.find_spec("gi")
    return spec is not None and spec.origin is not None  # a stray gi/ directory is a namespace package


def require_gi() -> None:
    if not has_gi():
        raise CommandError("python3-gi (PyGObject) is not installed: run `ubuntu-tweak doctor --yes` first")

"""Helpers shared by the command modules (skipped by command discovery: the name starts with ``_``).

Public API: ``has_gi() -> bool`` (PyGObject is importable) and ``require_gi()``, which raises
``CommandError`` naming ``ubuntu-tweak doctor --yes`` instead of letting a command that needs
PyGObject die with a ``ModuleNotFoundError`` traceback on a machine that lacks ``python3-gi``;
``rerun_command(args, subcommand) -> str`` (the command line to tell the user to run from a normal terminal:
the subcommand plus the ``--profile`` / ``--machine`` / ``--only`` / ``--dotfiles-repo`` the user gave);
``root_session(args, subcommand, *, skip=None) -> RootSession`` (the one object that asks for root once,
failing with that command line and the ``skip`` alternative; a seam for tests); ``confirm(question) -> bool``
(asks on a terminal, ``False`` without one); ``say(kind, text)`` (one status sentence on stdout).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import shlex
import sys

from ubuntu_tweaks import ui
from ubuntu_tweaks.elevation import RootSession
from ubuntu_tweaks.process import CommandError

GLOBAL_FLAGS = ("profile", "machine", "only", "dotfiles_repo")


def has_gi() -> bool:
    importlib.invalidate_caches()
    spec = importlib.util.find_spec("gi")
    return spec is not None and spec.origin is not None  # a stray gi/ directory is a namespace package


def require_gi() -> None:
    if not has_gi():
        raise CommandError("python3-gi (PyGObject) is not installed: run `ubuntu-tweak doctor --yes` first")


def rerun_command(args: argparse.Namespace, subcommand: str) -> str:
    given = (
        f" --{name.replace('_', '-')} {shlex.quote(str(getattr(args, name)))}"
        for name in GLOBAL_FLAGS
        if getattr(args, name, None)
    )
    return f"ubuntu-tweak {subcommand}{''.join(given)}"


def root_session(args: argparse.Namespace, subcommand: str, *, skip: str | None = None) -> RootSession:
    return RootSession(
        command=rerun_command(args, subcommand), skip=rerun_command(args, skip) if skip is not None else None
    )


def say(kind: str, text: str) -> None:
    print(ui.say(kind, text))


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:  # Ctrl-D: the same as no
        return False
    return answer.strip().lower() in {"y", "yes"}

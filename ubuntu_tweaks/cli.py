"""Argument parsing and command dispatch for ``bin/ubuntu-tweak``.

Accessed via: ``bin/ubuntu-tweak`` calls ``main()``.

Commands self-register: every ``ubuntu_tweaks/commands/<name>.py`` (names starting with ``_``
excepted) exposes ``register(subparsers)``, which adds its sub-parser and sets
``handler=<run>`` through ``set_defaults``. This file never names a command. Command modules
import heavy dependencies (``gi``) lazily inside their handler so ``--help`` and cheap commands
stay fast.

Global flags ``--profile DIR``, ``--machine NAME``, ``--only a,b``, ``--dotfiles-repo OWNER/NAME`` and
``--refresh`` are accepted before or after the command; they are added to every discovered sub-parser
here, so a command must not define options of those names.

Public API:

- ``main(argv=None) -> int``: parse, build the ``Context``, run the handler and return its exit
  code. ``UbuntuTweakError`` becomes its ``exit_code`` with the message on stderr; a missing
  ``gi`` module becomes a one-line "sudo apt install python3-gi" message with exit 1; any other
  exception prints a traceback and returns ``ExitCode.INTERNAL``.
- ``build_parser() -> argparse.ArgumentParser`` and ``discover_commands() -> list[ModuleType]``.
"""

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from ubuntu_tweaks import commands, identity
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.discovery import discover
from ubuntu_tweaks.errors import ExitCode, UbuntuTweakError
from ubuntu_tweaks.identity import REPO_ENV
from ubuntu_tweaks.profile import PROFILE_ENV, Profile


def discover_commands() -> list[ModuleType]:
    return discover(commands, kind="command", entry="register", signature="register(subparsers)")


def _add_global_flags(parser: argparse.ArgumentParser, *, default: object) -> None:
    parser.add_argument(
        "--profile",
        metavar="DIR",
        default=default,
        help=f"profile directory (default: ${PROFILE_ENV}, else the ubuntu profile of your dotfiles repository)",
    )
    parser.add_argument(
        "--machine",
        metavar="NAME",
        default=default,
        help="machine name for the overlay (default: contents of /etc/hostname)",
    )
    parser.add_argument(
        "--only",
        metavar="NAMES",
        default=default,
        help="comma-separated modules (panels for capture) to limit the command to",
    )
    parser.add_argument(
        "--dotfiles-repo",
        metavar="OWNER/NAME",
        default=default,
        help=f"your dotfiles repository (default: ${REPO_ENV}, else found through gh: .files, dotfiles, .dotfiles, dot-files)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        default=default,
        help="ask gh for the dotfiles repository again instead of using the cached answer",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ubuntu-tweak",
        description="Install, configure and update Ubuntu tweaks and the personal settings profile.",
    )
    _add_global_flags(parser, default=None)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for module in discover_commands():
        module.register(subparsers)
    for subparser in {id(choice): choice for choice in subparsers.choices.values()}.values():  # aliases share one
        _add_global_flags(subparser, default=argparse.SUPPRESS)
    return parser


def _only(value: str | None) -> frozenset[str]:
    return frozenset(name.strip() for name in (value or "").split(",") if name.strip())


def _resolve_profile(args: argparse.Namespace) -> Profile:
    flag = args.dotfiles_repo or None
    if flag:
        os.environ[REPO_ENV] = flag  # the dotfiles module (a child process) sees the same repository
    refresh = bool(args.refresh)
    return Profile.resolve(
        args.profile,
        args.machine,
        locate=lambda: identity.locate(home=Path.home(), env=os.environ, refresh=refresh, repo_override=flag),
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        context = Context(
            repo_root=REPO_ROOT,
            profile=_resolve_profile(args),
            only=_only(args.only),
        )
        result = args.handler(context, args)
    except UbuntuTweakError as error:
        print(f"ubuntu-tweak: {error}", file=sys.stderr)
        return int(error.exit_code)
    except ModuleNotFoundError as error:
        if error.name != "gi":
            traceback.print_exc()
            return int(ExitCode.INTERNAL)
        print(
            f"ubuntu-tweak: PyGObject (Python module `gi`) is not available in {sys.executable}; "
            "install it with: sudo apt install python3-gi",
            file=sys.stderr,
        )
        return int(ExitCode.INTERNAL)
    except Exception:
        traceback.print_exc()
        return int(ExitCode.INTERNAL)
    return int(result or ExitCode.OK)

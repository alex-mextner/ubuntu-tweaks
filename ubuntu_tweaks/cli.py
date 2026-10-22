"""Argument parsing and command dispatch for ``bin/ubuntu-tweak``.

Accessed via: ``bin/ubuntu-tweak`` calls ``main()``.

Commands self-register: every ``ubuntu_tweaks/commands/<name>.py`` (names starting with ``_``
excepted) exposes ``register(subparsers)``, which adds its sub-parser and sets
``handler=<run>`` through ``set_defaults``. This file never names a command. Command modules
import heavy dependencies (``gi``) lazily inside their handler so ``--help`` and cheap commands
stay fast.

Global flags ``--profile DIR``, ``--machine NAME`` and ``--only a,b`` are accepted before or
after the command; they are added to every discovered sub-parser here, so a command must not
define options of those names.

Public API:

- ``main(argv=None) -> int``: parse, build the ``Context``, run the handler and return its exit
  code. ``UbuntuTweakError`` becomes its ``exit_code`` with the message on stderr; a missing
  ``gi`` module becomes a one-line "sudo apt install python3-gi" message with exit 1; any other
  exception prints a traceback and returns ``ExitCode.INTERNAL``.
- ``build_parser() -> argparse.ArgumentParser`` and ``discover_commands() -> list[ModuleType]``.
"""

import argparse
import sys
import traceback
from collections.abc import Sequence
from types import ModuleType

from ubuntu_tweaks import commands, ui
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.discovery import discover
from ubuntu_tweaks.errors import ExitCode, UbuntuTweakError
from ubuntu_tweaks.profile import PROFILE_ENV, Profile


def discover_commands() -> list[ModuleType]:
    return discover(commands, kind="command", entry="register", signature="register(subparsers)")


def _add_global_flags(parser: argparse.ArgumentParser, *, default: object) -> None:
    parser.add_argument(
        "--profile",
        metavar="DIR",
        default=default,
        help=f"profile directory (default: ${PROFILE_ENV}, else ~/.files/ubuntu)",
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
        "--color",
        choices=("auto", "always", "never"),
        default=default,
        help="color and the explained layout: auto = on a terminal unless NO_COLOR/TERM=dumb (default)",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        default=default,
        help="plain, byte-stable text without colors or explanations (alias of --color never + compact layout)",
    )
    parser.add_argument(
        "--lang",
        choices=("en", "ru"),
        default=default,
        help="language of the explanations (default: $UBUNTU_TWEAK_LANG, else en)",
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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        ui.configure(color=args.color or "auto", plain=bool(args.plain), lang=args.lang)
        context = Context(
            repo_root=REPO_ROOT,
            profile=Profile.resolve(args.profile, args.machine),
            only=_only(args.only),
        )
        result = args.handler(context, args)
    except UbuntuTweakError as error:
        print(ui.format_error(str(error), int(error.exit_code), stream=sys.stderr), file=sys.stderr)
        return int(error.exit_code)
    except ModuleNotFoundError as error:
        if error.name != "gi":
            traceback.print_exc()
            return int(ExitCode.INTERNAL)
        message = (
            f"PyGObject (Python module `gi`) is not available in {sys.executable}; "
            "install it with: sudo apt install python3-gi"
        )
        print(
            ui.format_error(message, int(ExitCode.INTERNAL), fix="sudo apt install python3-gi", stream=sys.stderr),
            file=sys.stderr,
        )
        return int(ExitCode.INTERNAL)
    except Exception:
        traceback.print_exc()
        return int(ExitCode.INTERNAL)
    return int(result or ExitCode.OK)

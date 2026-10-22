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

- ``main(argv=None) -> int``: ``_run`` inside the session log (``sessionlog.run_logged``: every run of every
  command is logged, usage errors and ``--help`` included): parse, build the ``Context``, run the handler and
  return its exit code. ``UbuntuTweakError`` becomes its ``exit_code`` with the message on stderr; a missing
  ``gi`` module becomes a one-line "sudo apt install python3-gi" message with exit 1; any other
  exception prints a traceback and returns ``ExitCode.INTERNAL``. ``--dry-run`` (a global flag) turns the
  command into its preview, ends the run with ``dry run: nothing was changed`` on stderr and is a usage error
  (exit 2) together with ``--commit`` / ``--yes``.
- ``build_parser() -> argparse.ArgumentParser`` and ``discover_commands() -> list[ModuleType]``.
"""

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from ubuntu_tweaks import commands, identity, sessionlog, ui
from ubuntu_tweaks.checkout import stable_checkout, warning_line
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.discovery import discover
from ubuntu_tweaks.errors import ConfigError, ExitCode, UbuntuTweakError
from ubuntu_tweaks.identity import REPO_ENV
from ubuntu_tweaks.profile import PROFILE_ENV, Profile

DRY_RUN_NOTE = "dry run: nothing was changed"


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
        "--dry-run",
        action="store_true",
        default=default,
        help="preview only, exactly like the run without --commit: change nothing, ask for no sudo (excludes --commit)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        default=default,
        help="ask gh for the dotfiles repository again instead of using the cached answer",
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


def _resolve_profile(args: argparse.Namespace) -> Profile:
    flag = args.dotfiles_repo or None
    if flag:
        os.environ[REPO_ENV] = flag  # the dotfiles module (a child process) sees the same repository
    refresh = bool(args.refresh)
    return Profile.resolve(
        args.profile,
        args.machine,
        locate=lambda: identity.locate(
            home=Path.home(), env=os.environ, refresh=refresh, repo_override=flag, persist=not args.dry_run
        ),
    )


def _refuse_dry_run_commit(args: argparse.Namespace) -> None:
    """``--dry-run`` is the explicit preview, so it cannot be combined with the word that commits (``--commit``,
    ``doctor``'s ``--yes``): that is a usage error, not a silent pick of one of them."""
    if not args.dry_run:
        return
    for word in ("commit", "yes"):
        if getattr(args, word, False):
            raise ConfigError(f"--dry-run and --{word} exclude each other: a dry run never changes anything")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    return sessionlog.run_logged(arguments, lambda: _run(arguments))


def _run(argv: Sequence[str]) -> int:
    try:
        args = build_parser().parse_args(argv)
        sessionlog.set_command(args.command)
        ui.configure(color=args.color or "auto", plain=bool(args.plain), lang=args.lang)
        _refuse_dry_run_commit(args)
        checkout = stable_checkout(REPO_ROOT, os.environ)
        if not checkout.is_stable:
            print(warning_line(checkout.root, checkout.running), file=sys.stderr)
        if getattr(args, "plans_links", False):
            checkout.require_usable(args.command)
        context = Context(
            repo_root=checkout.root if checkout.usable else checkout.running,
            profile=_resolve_profile(args),
            only=_only(args.only),
            running_root=checkout.running,
        )
        sessionlog.note("profile", str(context.profile.root))
        sessionlog.note("machine", context.profile.machine)
        result = args.handler(context, args)
    except UbuntuTweakError as error:
        print(ui.format_error(str(error), int(error.exit_code), fix=error.fix, stream=sys.stderr), file=sys.stderr)
        return int(error.exit_code)
    except ModuleNotFoundError as error:
        if error.name != "gi":
            sessionlog.note("exception", type(error).__name__)
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
    except Exception as error:
        sessionlog.note("exception", type(error).__name__)
        traceback.print_exc()
        return int(ExitCode.INTERNAL)
    if args.dry_run:
        print(DRY_RUN_NOTE, file=sys.stderr)
    return int(result or ExitCode.OK)

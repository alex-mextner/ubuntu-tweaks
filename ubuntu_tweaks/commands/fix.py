"""``ubuntu-tweak fix [--yes]``: repair everything that is not right, in one command.

Accessed via: ``bin/ubuntu-tweak fix``. Spec: *fix*, *Elevation*.

Steps, with one ``elevation.RootSession`` for the whole run (root is asked for once, before the first root
action; exit 6 with the command to run when it cannot be obtained):

1. ``doctor``: the report; missing apt packages that no module installs itself are installed with sudo
   (after a question on a terminal, or at once with ``--yes``).
2. ``apply --commit`` of the whole plan (``--only`` narrows it): the plan is printed, one question "Apply N
   action(s) (M need root)?" is asked on a terminal (``--yes`` skips it), then it runs. Without a terminal and
   without ``--yes`` nothing changes (exit 3): a mutating command never acts without consent.
3. ``status``, printed as the command does, then a closing line; each item that is left is listed with its
   reason (the manual step's message).

Exit 0 only when ``status`` is clean afterwards (so a second run reports ``0 changed`` and exits 0), 3 when
drift is left or nothing was confirmed, 6 when root was needed and not obtainable.

Public API: ``register(subparsers)``, ``run(ctx, args)``.
"""

from __future__ import annotations

import argparse
import sys

from ubuntu_tweaks import ui
from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.commands import doctor, status
from ubuntu_tweaks.commands._support import confirm, require_gi, root_session, say
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.elevation import RootSession
from ubuntu_tweaks.errors import ExitCode


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "fix",
        help="repair everything: install missing packages, apply the whole plan, check status (asks for sudo once)",
    )
    parser.add_argument("--yes", action="store_true", help="do not ask before installing and applying")
    parser.set_defaults(handler=run)


def _declined(what: str, *, installed: bool = False) -> int:
    """Say that ``what`` was not confirmed (and what that left undone); the exit code is 3 (drift remains)."""
    if sys.stdin.isatty():
        how = "Answer y at the question, or re-run with --yes."
    else:
        how = "There is no terminal to ask on: re-run with --yes to do it without asking."
    done = "The missing packages were installed, but nothing else was changed" if installed else "Nothing was changed"
    say("error", f"{done} ({what} not confirmed). {how}")
    return int(ExitCode.DRIFT)


def _confirm_plan(plan: Plan) -> bool:
    roots = sum(action.needs_root for action in plan.actions)
    return confirm(f"Apply {len(plan)} action(s) ({roots} need root)?")


def _install_missing(ctx: Context, args: argparse.Namespace, root: RootSession) -> tuple[int | None, bool]:
    """Step 1: ``(exit code or None when nothing blocks the plan, whether packages were installed)``."""
    needed = doctor.requirements(ctx)
    absent = doctor.report(needed)
    if not absent:
        return None, False
    packages = " ".join(sorted({item.apt for item in absent}))
    if not (args.yes or confirm(f"Install {packages} with sudo apt-get?")):
        return _declined(f"installing {packages}"), False
    root.require(1, f"apt-get install {packages}")
    doctor.install(absent)
    absent = doctor.missing(needed)
    if absent:
        say("error", "Still missing after install: " + ", ".join(item.label for item in absent))
        return int(ExitCode.DRIFT), True
    return None, True


def run(ctx: Context, args: argparse.Namespace) -> int:
    with root_session(args, "fix", skip="apply --commit --no-sudo") as root:
        return _fix(ctx, args, root)


def _fix(ctx: Context, args: argparse.Namespace, root: RootSession) -> int:
    if ui.rich():
        print(ui.heading("ubuntu-tweak fix") + " " + ui.style("(repair everything that is not right)", "dim"))
    blocked, installed = _install_missing(ctx, args, root)
    if blocked is not None:
        return blocked
    require_gi()
    from ubuntu_tweaks import reconcile

    outcome = reconcile.apply_detailed(
        ctx,
        commit=True,
        no_sudo=False,
        title="fix",
        root=root,
        confirm=None if args.yes else _confirm_plan,
    )
    if outcome.declined:
        return _declined("the plan", installed=installed)
    print()
    code = status.run(ctx, argparse.Namespace(explain=False, json=False))
    if code == int(ExitCode.OK):
        say("ok", "fix: everything matches the profile and modules; nothing is left.")
        return code
    left = [f"{number}. [{action.kind}] {message}" for number, (action, message) in enumerate(outcome.manual, 1)]
    say("manual", f"fix: not everything could be repaired (exit {code}); what still needs you:")
    print("\n".join(f"  {line}" for line in left or ["see Missing above"]))
    return code

"""``ubuntu-tweak apply [--commit] [--no-sudo]``: preview or run the plan.

Accessed via: ``bin/ubuntu-tweak apply``.

Without ``--commit`` it prints the plan and changes nothing (``--dry-run`` is the explicit spelling); with
``--commit`` it repairs the machine in one run: root is obtained once up front (``elevation``, exit 6 when it
cannot be), the missing apt packages ``doctor`` would report are installed without a question
(``doctor.ensure_requirements``), then the plan runs and prints ``N changed`` (a second run prints
``0 changed``). ``--no-sudo`` skips root actions and says so. The work is in ``ubuntu_tweaks.reconcile``,
imported lazily because it needs PyGObject.
"""

from __future__ import annotations

import argparse

from ubuntu_tweaks.commands import doctor
from ubuntu_tweaks.commands._support import require_gi, root_session
from ubuntu_tweaks.context import Context


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "apply",
        help="show the plan that makes the system match modules + profile (--commit installs missing packages and runs it)",
    )
    parser.add_argument("--commit", action="store_true", help="execute the plan (default: preview only)")
    parser.add_argument("--no-sudo", action="store_true", help="skip actions that need root and say so")
    parser.set_defaults(handler=run)


def run(ctx: Context, args: argparse.Namespace) -> int:
    with root_session(args, "apply --commit", skip="apply --commit --no-sudo") as root:
        blocked = doctor.ensure_requirements(ctx, args, root, command="apply")
        if blocked is not None:
            return blocked
        require_gi()
        from ubuntu_tweaks import reconcile

        return reconcile.apply(ctx, commit=args.commit, no_sudo=args.no_sudo, root=root)

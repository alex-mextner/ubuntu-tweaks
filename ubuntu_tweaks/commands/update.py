"""``ubuntu-tweak update [--commit] [--no-sudo]``: pull the umbrella, move submodules, apply.

Accessed via: ``bin/ubuntu-tweak update``.

With ``--commit``: ``git pull --ff-only`` of the umbrella checkout, ``git submodule update --init
--recursive`` (the only command that moves a submodule that is checked out at another commit),
then ``apply --commit`` in a **fresh interpreter** (the pull may have changed this very package),
carrying over ``--machine``, ``--profile`` and ``--only``; the re-login hint is printed when the pull
or the submodule update changed anything. Without ``--commit`` it prints the two git commands and
the ``apply`` preview of the *current* checkout (what the plan will be after the pull is not
knowable before it). The release channel and schedule are ticket #19, not handled here.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import RELOGIN_HINT
from ubuntu_tweaks.process import run as run_command

PULL = ("git", "pull", "--ff-only")
SUBMODULES = ("git", "submodule", "update", "--init", "--recursive")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "update", help="git pull --ff-only, update submodules, then apply (--commit runs it)"
    )
    parser.add_argument("--commit", action="store_true", help="pull and apply (default: preview only)")
    parser.add_argument("--no-sudo", action="store_true", help="skip actions that need root and say so")
    parser.set_defaults(handler=run)


def _code_state(ctx: Context) -> str:
    """HEAD plus the commit of every submodule: changes whenever the code does."""
    repo = ["git", "-C", str(ctx.repo_root)]
    head = run_command([*repo, "rev-parse", "HEAD"], capture=True)
    return head + run_command([*repo, "submodule", "status"], capture=True)


def _apply_command(ctx: Context, *, no_sudo: bool) -> list[str]:
    command = [sys.executable, str(ctx.repo_root / "bin" / "ubuntu-tweak"), "apply", "--commit"]
    command += ["--machine", ctx.profile.machine]
    if ctx.profile.exists:
        command += ["--profile", str(ctx.profile.root)]
    if ctx.only:
        command += ["--only", ",".join(sorted(ctx.only))]
    if no_sudo:
        command.append("--no-sudo")
    return command


def run(ctx: Context, args: argparse.Namespace) -> int:
    if not args.commit:
        from ubuntu_tweaks.commands._support import require_gi

        require_gi()
        from ubuntu_tweaks import reconcile

        print(f"Would run in {ctx.repo_root}:\n  {' '.join(PULL)}\n  {' '.join(SUBMODULES)}")
        print("Plan for the current checkout (computed before the pull):")
        return reconcile.apply(ctx, commit=False, no_sudo=args.no_sudo)

    before = _code_state(ctx)
    run_command(PULL, cwd=ctx.repo_root)
    run_command(SUBMODULES, cwd=ctx.repo_root)
    code = subprocess.run(_apply_command(ctx, no_sudo=args.no_sudo), check=False).returncode
    if _code_state(ctx) != before:
        print(RELOGIN_HINT)
    return code

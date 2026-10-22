"""``ubuntu-tweak update [--commit] [--no-sudo]``: pull the umbrella, move submodules, apply.

Accessed via: ``bin/ubuntu-tweak update``.

With ``--commit``: ``git pull --ff-only`` of the umbrella checkout, ``git submodule update --init
--recursive`` (the only command that moves a submodule that is checked out at another commit),
then ``apply --commit`` in a **fresh interpreter** (the pull may have changed this very package),
carrying over ``--machine``, ``--profile`` and ``--only``; the re-login hint is printed when the pull
or the submodule update changed anything. The child ``apply --commit`` obtains root itself, once, before its
first root action (``elevation``); its exit code, 6 included, is ``update``'s. Without ``--commit`` it prints
the two git commands it would run and the ``apply`` preview of the *current* checkout (what the plan will be after the pull is not
knowable before it). A tree that is not a git checkout (a tarball or a copied directory: no ``.git``)
has nothing to pull: the git steps are skipped with a message and ``apply`` still runs, so ``update``
never needs ``git`` there. The release channel and schedule are ticket #19, not handled here.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from ubuntu_tweaks import ui
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


def _is_git_checkout(ctx: Context) -> bool:
    return (ctx.repo_root / ".git").exists()  # a directory, or a file in a worktree or submodule


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
    command += ui.cli_flags()
    return command


def _note(text: str, *, kind: str = "info") -> None:
    print(ui.say(kind, text))


def run(ctx: Context, args: argparse.Namespace) -> int:
    if not args.commit:
        from ubuntu_tweaks.commands._support import require_gi

        require_gi()
        from ubuntu_tweaks import reconcile

        if _is_git_checkout(ctx):
            if ui.rich():
                print(ui.heading("ubuntu-tweak update") + " " + ui.style("(preview)", "dim"))
                print(f"  {ui.style('would run in', 'dim')} {ctx.repo_root}:")
                print(f"    {ui.command(' '.join(PULL))}")
                print(f"    {ui.command(' '.join(SUBMODULES))}")
                _note("Plan for the current checkout (computed before the pull; the pull may change it):")
            else:
                print(f"Would run in {ctx.repo_root}:\n  {' '.join(PULL)}\n  {' '.join(SUBMODULES)}")
                print("Plan for the current checkout (computed before the pull):")
        else:
            _note(f"{ctx.repo_root} is not a git checkout: nothing to pull, no submodules to update. Plan:")
        return reconcile.apply(
            ctx, commit=False, no_sudo=args.no_sudo, title="", preview_footer="update.preview_footer"
        )

    if not _is_git_checkout(ctx):
        _note(f"{ctx.repo_root} is not a git checkout: skipping the pull and the submodule update.", kind="manual")
        return subprocess.run(_apply_command(ctx, no_sudo=args.no_sudo), check=False).returncode
    before = _code_state(ctx)
    if ui.rich():
        _note(f"Step 1/2: {' '.join(PULL)}; {' '.join(SUBMODULES)}")
    run_command(PULL, cwd=ctx.repo_root)
    run_command(SUBMODULES, cwd=ctx.repo_root)
    if ui.rich():
        _note("Step 2/2: apply --commit with the updated code")
    code = subprocess.run(_apply_command(ctx, no_sudo=args.no_sudo), check=False).returncode
    if _code_state(ctx) != before:
        _note(RELOGIN_HINT, kind="manual")
    return code

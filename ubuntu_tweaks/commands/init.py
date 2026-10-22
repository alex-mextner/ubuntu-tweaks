"""``ubuntu-tweak init [--commit] [--yes] [--no-sudo]``: set up a fresh system.

Accessed via: ``bin/ubuntu-tweak init`` (and the ``install.sh`` wrapper, which passes ``--commit``).

Steps: ``doctor`` -> the ``apply`` plan extended with the symlink ``~/.local/bin/ubuntu-tweak``
-> the CLI (submodule init is a ``submodule`` action of the plan, ``git submodule update --init
--recursive``). Without ``--commit`` everything is only printed (invariant 1). With ``--commit``
missing apt packages are installed first, after a confirmation on a terminal or at once with
``--yes``; without either, ``init`` stops and names the command (``--no-sudo`` skips the install
and goes on). A missing *default* profile is not an error: the header says which dotfiles repository gh
found, none, or ``needs manual: ... gh auth login`` (``reconcile.header_lines``) and the plan covers the modules only. The CLI link has its own kinds
(``cli-link``, ``cli-link-replace``): it is not extension code, so it triggers neither the re-login
hint nor the staged settings pass; ``--only`` limits ``init`` to the named modules, so it drops the
CLI link. Re-running is safe (every action is idempotent). With ``--commit`` (and no ``--no-sudo``) root is obtained
once, through one ``elevation.RootSession`` shared by the apt install and the plan (exit 6 when it cannot be).

Pipeline order (spec, *Init pipeline*): there is no list of steps in this file. The ``apply`` plan is
ordered by ``(phase, order)`` and every pipeline step is a module whose ``module.toml`` declares its
``order`` (base tools + zsh 10-11, clipboard 20, git/gh 30, brew 40, ssh 50, passwords-app 60, Firefox 70,
dock 80, optional modules 100+) and ``phase``; a tool a module installs itself is reported ``planned`` by
``doctor``, not ``MISSING``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ubuntu_tweaks import ui
from ubuntu_tweaks.commands import doctor
from ubuntu_tweaks.commands._support import confirm, root_session, say
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.elevation import RootSession
from ubuntu_tweaks.errors import ExitCode
from ubuntu_tweaks.symlink import SymlinkAction

CLI_LINK = Path(".local/bin/ubuntu-tweak")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("init", help="fresh system: doctor, submodules, CLI link, apply (--commit runs it)")
    parser.add_argument("--commit", action="store_true", help="install packages and apply (default: preview only)")
    parser.add_argument("--yes", action="store_true", help="install missing apt packages without asking")
    parser.add_argument("--no-sudo", action="store_true", help="skip actions that need root and say so")
    parser.set_defaults(handler=run)


def _step(number: int, key: str) -> None:
    if ui.rich():
        print(ui.heading(ui.t("init.step", n=number, total=2, text=ui.t(key))))
    else:
        print("doctor:" if number == 1 else "apply:")


def run(ctx: Context, args: argparse.Namespace) -> int:
    with root_session(args, "init --commit", skip="init --commit --no-sudo") as root:
        return _run(ctx, args, root)


def _run(ctx: Context, args: argparse.Namespace, root: RootSession) -> int:
    if ui.rich():
        print(ui.heading("ubuntu-tweak init") + " " + ui.style("(set up this machine)", "dim"))
    _step(1, "init.step.doctor")
    needed = doctor.requirements(ctx, honor_planned=not args.no_sudo)  # no sudo: the module's apt batch cannot run
    absent = doctor.report(needed)
    if absent:
        packages = " ".join(sorted({item.apt for item in absent}))
        if not args.commit:
            say("info", f"init --commit would install: sudo apt-get install -y {packages}")
        elif args.no_sudo:
            say("manual", f"Not installing {packages} (--no-sudo). Later: sudo apt-get install -y {packages}")
        elif args.yes or confirm(f"Install {packages} with sudo apt-get?"):
            root.require(1, f"apt-get install {packages}")
            doctor.install(absent)
            absent = doctor.missing(needed)
            if absent:
                say("error", "Still missing after install: " + ", ".join(item.label for item in absent))
                return int(ExitCode.DRIFT)
        else:
            say("error", f"Missing packages: {packages}. Re-run with --yes, or run: ubuntu-tweak doctor --yes")
            return int(ExitCode.DRIFT)
    if doctor.missing([item for item in needed if item.apt == "python3-gi"]):
        say("error", "apply preview skipped: python3-gi is missing (install it first).")
        return int(ExitCode.DRIFT)

    from ubuntu_tweaks import reconcile

    cli_link = SymlinkAction(Path.home() / CLI_LINK, ctx.repo_root / "bin" / "ubuntu-tweak", kind_prefix="cli-")
    if ui.rich():
        print()
    _step(2, "init.step.apply")
    return reconcile.apply(
        ctx,
        commit=args.commit,
        no_sudo=args.no_sudo,
        extra_actions=[cli_link],
        title="",
        preview_footer="init.preview_footer",
        root=root,
    )

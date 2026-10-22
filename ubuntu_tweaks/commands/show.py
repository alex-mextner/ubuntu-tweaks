"""``ubuntu-tweak show``: a readable Russian overview of all settings of a machine. Read-only.

Accessed via: ``bin/ubuntu-tweak show [--changed | --all] [--panel ID] [--format text|md|html]
[--output FILE] [--ssh [USER@]HOST [--ssh-option KEY=VALUE ...]]``.

Groups the settings by GNOME Settings panel (the capture rules' ``panels.toml``), each key with a Russian
explanation, its current value in a readable form, its default and a status (изменено вами / по умолчанию /
задано файлом-переопределением), then what lives outside dconf (files, time/locale/keyboard, packages,
modules). ``--changed`` (default) lists only what was changed or overridden, ``--all`` every key of every
fixed-path schema. Secret, identity and hardware keys are shown as ``скрыто``; their values are never read
into the output. ``--format html`` writes one self-contained file (``--output``, default
``/tmp/ubuntu-tweak-show-<machine>.html``); text and Markdown go to stdout unless ``--output`` is given.

``--ssh`` shows another machine without installing anything on it: only read-only probes run there
(``dconf dump /``, ``cat``, ``timedatectl show``, ...), the schemas, descriptions and rules are this
machine's, and keys with no schema here are listed as raw. The only file written anywhere is ``--output``.

Public API: ``register(subparsers)``, ``run(ctx, args) -> int`` and ``build(ctx, args, ...) -> Report``.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ubuntu_tweaks import ui
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.process import CommandError

from ubuntu_tweaks.show.model import Report

if TYPE_CHECKING:
    from ubuntu_tweaks.show.system import ModuleState

FORMATS = ("text", "md", "html")
KEYFILE_PROBE = ["sh", "-c", 'cat "$HOME/.config/glib-2.0/settings/keyfile"']


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "show",
        help="readable Russian overview of all settings of this (or, with --ssh, another) machine; read-only",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--changed", action="store_true", help="only keys you changed, plus overrides (default)")
    scope.add_argument("--all", action="store_true", help="every key of every fixed-path schema, defaults included")
    parser.add_argument("--panel", metavar="ID", help="only one Settings panel (a panel id, or `other`)")
    parser.add_argument("--format", choices=FORMATS, default="text", help="text (default), md or html")
    parser.add_argument(
        "--output", metavar="FILE", help="write the report here (html default: /tmp/ubuntu-tweak-show-<machine>.html)"
    )
    parser.add_argument("--ssh", metavar="[USER@]HOST", help="read the settings of another machine over ssh, read-only")
    parser.add_argument(
        "--ssh-option", action="append", default=[], metavar="KEY=VALUE", help="an ssh -o option; repeatable"
    )
    parser.set_defaults(handler=run)


def _modules(ctx: Context) -> list[ModuleState]:
    from ubuntu_tweaks.manifest import module_defaults
    from ubuntu_tweaks.show.system import ModuleState as State

    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    return [
        State(
            manifest.name,
            manifest.source.kind.value,
            config.module_enabled(manifest.name),
            manifest.source.path or f"modules/{manifest.name}",
        )
        for manifest in manifests
    ]


def build(ctx: Context, args: argparse.Namespace) -> Report:
    from ubuntu_tweaks.capture.rules import PANEL_SELECTORS, load_rules
    from ubuntu_tweaks.capture.system_scan import default_runner
    from ubuntu_tweaks.settings import GioBackend, parse_keyfile
    from ubuntu_tweaks.show.catalog import GioCatalog
    from ubuntu_tweaks.show.collect import PANEL_OTHER, collect_panels
    from ubuntu_tweaks.show.descriptions import load_descriptions
    from ubuntu_tweaks.show.ssh import ssh_runner, target_host
    from ubuntu_tweaks.show.system import collect_extras

    if args.panel is not None and args.panel not in (*PANEL_SELECTORS, PANEL_OTHER):
        raise ConfigError(f"unknown panel {args.panel!r}; known: {', '.join((*PANEL_SELECTORS, PANEL_OTHER))}")
    remote = args.ssh is not None
    run = ssh_runner(args.ssh, args.ssh_option) if remote else default_runner
    where = f"по ssh {args.ssh}" if remote else "локально"
    dump = run(["dconf", "dump", "/"])
    if dump is None:
        raise CommandError(f"`dconf dump /` failed {where}: is dconf-cli installed and the machine reachable?")
    override_text = run(KEYFILE_PROBE)
    try:
        override = parse_keyfile(override_text) if override_text else {}
    except ConfigError:
        override = {}
    backend = GioBackend.with_extensions(Path.home())
    panels, untouched, counts = collect_panels(
        user=parse_keyfile(dump),
        override=override,
        backend=backend,
        catalog=GioCatalog(backend.schema_dirs),
        rules=load_rules(),
        texts=load_descriptions(),
        show_all=args.all,
        only_panel=args.panel,
    )
    label = ctx.profile.machine if not remote or ctx.profile.machine_source == "flag" else target_host(args.ssh)
    return Report(
        label,
        where if not remote else f"{where}; схемы и описания — с машины, где запущена команда",
        "all" if args.all else "changed",
        panels,
        collect_extras(run, None if remote else _modules(ctx)),
        counts,
        untouched,
    )


def run(ctx: Context, args: argparse.Namespace) -> int:
    from ubuntu_tweaks.commands._support import require_gi
    from ubuntu_tweaks.show.html_page import render_html
    from ubuntu_tweaks.show.render import render_markdown, render_text

    require_gi()
    report = build(ctx, args)
    text = {"text": render_text, "md": render_markdown, "html": render_html}[args.format](report)
    safe_label = re.sub(r"[^A-Za-z0-9._-]", "-", report.label)  # the label becomes a file name under /tmp
    output = args.output or (f"/tmp/ubuntu-tweak-show-{safe_label}.html" if args.format == "html" else None)
    if output is None:
        print(ui.decorate(text, "show") if args.format == "text" else text, end="")
        return 0
    target = Path(output).expanduser()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW  # never follow a symlink planted at a /tmp path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as error:
        raise CommandError(f"cannot write the report to {target}: {error.strerror or error}") from error
    print(ui.say("ok", f"written {target} ({report.counts['keys']} keys in {len(report.panels)} panels)"))
    return 0

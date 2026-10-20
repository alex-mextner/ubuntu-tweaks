"""``ubuntu-tweak capture``: inventory of the live system as the exact diff the profile would receive.

Accessed via: ``cli.py`` discovers this module and calls ``register``.

Preview is the default and is read-only. ``--commit`` writes the same plan into the profile
working tree (settings panel files, allow-listed files with backup + symlink, packages.toml,
system.toml) and never runs ``git commit``. ``--report FILE.md`` writes the report as markdown
(full values; the terminal clips long cells); ``-v`` also lists entries already equal to the
profile and every cache-state candidate; ``--only`` (global flag) takes panel ids and the section
names ``files``, ``packages``, ``system``. Exit 0; 2 invalid config; 5 refused (secret finding or
unsafe path, nothing written).

Heavy imports (``gi`` through the settings backend) happen inside the functions so ``--help``
stays fast. ``execute`` is the body with every system input injected (tests); ``run`` wires the
real ones.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError

if TYPE_CHECKING:
    from ubuntu_tweaks.capture.schemas import SchemaCatalog
    from ubuntu_tweaks.capture.system_scan import Runner
    from ubuntu_tweaks.settings import SettingsBackend


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "capture",
        help="preview (or with --commit write) what the live system would add to the profile",
        description=(
            "Read-only inventory of the live system, printed as the exact diff the profile would receive. "
            "--commit writes it into the profile working tree (no git commit)."
        ),
    )
    parser.add_argument("--commit", action="store_true", help="write the plan into the profile (default: preview only)")
    parser.add_argument("--report", metavar="FILE.md", help="also write the report as markdown")
    parser.add_argument("-v", "--verbose", action="store_true", help="also list entries equal to the profile")
    parser.set_defaults(handler=run)


def _capture_table(ctx: Context) -> dict[str, object]:
    table = ctx.profile.load_config().get("capture")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError("profile [capture] must be a table")
    return dict(table)


def execute(
    ctx: Context,
    args: argparse.Namespace,
    backend: SettingsBackend,
    catalog: SchemaCatalog,
    *,
    home: Path,
    now: datetime,
    run: Runner,
    **sources: Path,
) -> int:
    """``sources`` may carry ``installer_baseline``, ``snap_seed`` and ``keyboard_file``."""
    from ubuntu_tweaks.capture.commit import commit
    from ubuntu_tweaks.capture.plan import build_plan
    from ubuntu_tweaks.capture.report import build_document, render_markdown, render_text
    from ubuntu_tweaks.capture.rules import load_rules

    rules = load_rules(_capture_table(ctx))
    plan = build_plan(ctx.profile, rules, backend, catalog, home=home, only=ctx.only, run=run, **sources)
    document = build_document(plan, machine=ctx.profile.machine, commit=args.commit, verbose=args.verbose)
    print(render_text(document), end="")
    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render_markdown(document), encoding="utf-8")
        print(f"report written to {report}")
    if args.commit:
        for line in commit(plan, email_exempt_keys=rules.email_exempt_keys, home=home, now=now):
            print(line)
        print(f"nothing was committed to git: review `git diff` in {ctx.profile.root}")
    return 0


def run(ctx: Context, args: argparse.Namespace) -> int:
    from ubuntu_tweaks.capture.schemas import gio_catalog
    from ubuntu_tweaks.capture.system_scan import default_runner
    from ubuntu_tweaks.settings import GioBackend

    home = Path.home()
    backend = GioBackend.with_extensions(home)
    return execute(
        ctx, args, backend, gio_catalog(backend.schema_dirs), home=home, now=datetime.now(UTC), run=default_runner
    )

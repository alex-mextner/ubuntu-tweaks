"""The ``error-intercept`` command.

Accessed via: ``modules/error-intercept/error-intercept`` (also linked as ``~/.local/bin/error-intercept``) and the
user units the installer writes: ``ingest`` is the ``ExecStart`` of ``update-notifier-crash.service`` and of
``error-intercept-scan.service``, ``ingest-spool`` of ``error-intercept-spool.service``, ``drain`` of ``error-intercept-drain.service``, ``journal`` of
``error-intercept-journal.service``.

    ingest [--crash-dir DIR] [--dry-run]   publish and archive the apport reports (exit 0, no window, ever);
                                            while ``DISABLED`` exists: exec the stock update-notifier-crash
    ingest-spool [--dry-run]               publish the shell extension's spool files
    drain                                  ``agent-sub drain`` (what error-intercept-drain.timer runs every 5 minutes)
    journal [OPTIONS]                      follow the journal (``error_intercept.journal_source.main(publish, argv=OPTIONS)``)
    list [--json] [--limit N]              the audited events, newest last
    replay ID                              deliver an audited event again
    status [--json]                        stop switch, waiting reports, retry queue, archive
    enable | disable                       the stop switch (the ``DISABLED`` file)
    archive list [--json]                  the reports and spool files moved out of /var/crash

Exit codes: 0 done, 1 not found (``replay``) or an unexpected failure, 2 usage / journal source not installed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from error_intercept import apport_source, spool
from error_intercept.archive import list_archive
from error_intercept.audit import Audit
from error_intercept.paths import ORIGINAL_NOTIFIER, Paths, ensure_private_dir
from error_intercept.publisher import LOCAL_PREFIX, AgentSubPublisher, DurablePublisher, PublishError, locate_agent_sub

Execv = Callable[[str, list[str]], Any]


class Context:
    """Everything a command needs, built once; tests build it with fakes."""

    def __init__(
        self,
        paths: Paths,
        env: Mapping[str, str],
        out: TextIO,
        err: TextIO,
        runner: Callable[..., Any] = subprocess.run,
        execv: Execv = os.execv,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        uid: int | None = None,
        quiet_s: float = apport_source.QUIET_S,
    ) -> None:
        self.paths = paths
        self.env = env
        self.out = out
        self.err = err
        self.execv = execv
        self.clock = clock
        self.sleep = sleep
        self.uid = uid
        self.quiet_s = quiet_s
        self.audit = Audit(paths.audit, clock)
        self.agent_sub = AgentSubPublisher(paths, env, runner)
        self.publisher = DurablePublisher(self.agent_sub, paths, self.audit, clock)

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")

    def warn(self, line: str) -> None:
        self.err.write(f"error-intercept: {line}\n")


def _drain_quietly(ctx: Context) -> None:
    """``agent-sub drain`` after the work: pending events are only retried there. A failure is not the ingester's."""
    try:
        ctx.agent_sub.drain()
    except PublishError as error:
        ctx.warn(f"agent-sub drain failed: {error}")


def _drain(ctx: Context, args: argparse.Namespace) -> int:
    try:
        for line in ctx.agent_sub.drain().splitlines():
            ctx.say(line)
    except PublishError as error:
        ctx.warn(f"agent-sub drain failed: {error}")
        return 1
    return 0


def _ingest(ctx: Context, args: argparse.Namespace) -> int:
    if ctx.paths.disabled.exists():
        if args.dry_run:
            ctx.say(f"DISABLED ({ctx.paths.tilde(ctx.paths.disabled)}): a real run would exec {ORIGINAL_NOTIFIER}")
            return 0
        ctx.execv(ORIGINAL_NOTIFIER, [ORIGINAL_NOTIFIER])
        return 0  # only reached when execv is replaced (tests)
    if args.crash_dir:
        ctx.paths = Paths(ctx.paths.home, ctx.paths.state, ctx.paths.config, Path(args.crash_dir))
    if not args.dry_run:
        ensure_private_dir(ctx.paths.state)
        for name in ctx.publisher.retry_pending():
            ctx.say(f"retried pending event -> {name}")
    ingester = apport_source.Ingester(
        ctx.paths,
        ctx.publisher,
        ctx.audit,
        clock=ctx.clock,
        sleep=ctx.sleep,
        uid=ctx.uid,
        dry_run=args.dry_run,
        quiet_s=ctx.quiet_s,
    )
    outcome = ingester.run()
    for line in outcome.lines:
        ctx.say(line)
    ctx.say(
        f"{'would handle' if args.dry_run else 'handled'}: {outcome.published} published, "
        f"{outcome.archived} archived, {outcome.metadata_only} metadata-only"
    )
    if not args.dry_run:
        _drain_quietly(ctx)
    return 0


def _ingest_spool(ctx: Context, args: argparse.Namespace) -> int:
    if not args.dry_run:
        ensure_private_dir(ctx.paths.state)
        ctx.publisher.retry_pending()
    for line in spool.ingest_spool(ctx.paths, ctx.publisher, ctx.audit, args.dry_run):
        ctx.say(line)
    if not args.dry_run:
        _drain_quietly(ctx)
    return 0


class _SwitchedPublisher:
    """The journal source publishes nothing while the stop switch exists."""

    def __init__(self, inner: DurablePublisher, paths: Paths) -> None:
        self._inner = inner
        self._paths = paths

    def __call__(self, event: dict[str, Any]) -> str:
        return "disabled" if self._paths.disabled.exists() else self._inner(event)


def _journal(ctx: Context, args: argparse.Namespace) -> int:
    try:
        from error_intercept import journal_source
    except ModuleNotFoundError as error:
        if error.name != "error_intercept.journal_source":
            raise
        ctx.warn("the journal source is not installed in this checkout (error_intercept/journal_source.py)")
        return 2
    result = journal_source.main(_SwitchedPublisher(ctx.publisher, ctx.paths), argv=args.journal_args)
    return result if isinstance(result, int) else 0


def _stamp(ts: object) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if isinstance(ts, (int, float)) else "?"


def _list(ctx: Context, args: argparse.Namespace) -> int:
    records = [r for r in ctx.audit.records() if r.get("event_id")][-args.limit :]
    if args.json:
        ctx.say(json.dumps(records, ensure_ascii=False, indent=1))
        return 0
    for record in records:
        event = record.get("event") if isinstance(record.get("event"), dict) else {}
        ctx.say(
            f"{_stamp(record.get('ts'))}  {record['event_id']}  {record.get('decision', '?'):<15} "
            f"{event.get('severity', '?'):<8} {event.get('key', record.get('report', ''))}"
        )
    if not records:
        ctx.say("no events yet")
    return 0


def _replay(ctx: Context, args: argparse.Namespace) -> int:
    record = ctx.audit.find(args.id)
    if record is None:
        ctx.warn(f"no audited event {args.id!r} (see `error-intercept list`)")
        return 1
    event_id = str(record["event_id"])
    event = record.get("event")
    try:
        if not event_id.startswith(LOCAL_PREFIX):
            new_id = ctx.agent_sub.replay(event_id)
        elif isinstance(event, dict):
            new_id = ctx.agent_sub(event)
        else:
            ctx.warn(f"{event_id} has no stored event to publish")
            return 1
    except PublishError as error:
        ctx.warn(f"replay failed: {error}")
        return 1
    ctx.audit.append(decision="replayed", event_id=new_id, was=event_id, status="published")
    ctx.say(f"replayed {event_id} -> {new_id}")
    return 0


def _count(directory: Path, pattern: str) -> int:
    try:
        return sum(1 for _ in directory.glob(pattern))
    except OSError:
        return 0


def _status_facts(ctx: Context) -> dict[str, Any]:
    paths = ctx.paths
    waiting = apport_source.Ingester(paths, ctx.publisher, ctx.audit, clock=ctx.clock, uid=ctx.uid, dry_run=True).run()
    archived = list_archive(paths.archive)
    records = [r for r in ctx.audit.records() if r.get("event_id")]
    return {
        "enabled": not paths.disabled.exists(),
        "agent_sub": locate_agent_sub(paths, ctx.env),
        "crash_dir": str(paths.crash_dir),
        "crash_reports_waiting": sum(1 for line in waiting.lines if not line.startswith(" ")),
        "pending_retry": _count(paths.pending, f"{LOCAL_PREFIX}*.json"),
        "spool_waiting": _count(paths.spool, "*.json"),
        "archived": len(archived),
        "archive_bytes": sum(path.stat().st_size for path in archived),
        "events": len(records),
        "last_event": records[-1] if records else None,
    }


def _status(ctx: Context, args: argparse.Namespace) -> int:
    facts = _status_facts(ctx)
    if args.json:
        ctx.say(json.dumps(facts, ensure_ascii=False, indent=1, default=str))
        return 0
    last = facts["last_event"]
    ctx.say(f"interception: {'on' if facts['enabled'] else 'OFF (DISABLED switch: the stock apport dialog is back)'}")
    ctx.say(f"agent-sub: {facts['agent_sub'] or 'NOT FOUND (events wait in the retry queue)'}")
    ctx.say(f"crash reports waiting to be ingested in {facts['crash_dir']}: {facts['crash_reports_waiting']}")
    ctx.say(f"events waiting for agent-sub (retry queue): {facts['pending_retry']}")
    ctx.say(f"shell-extension spool files waiting: {facts['spool_waiting']}")
    ctx.say(
        f"archive: {facts['archived']} file(s), {facts['archive_bytes'] // 1024} KiB at {ctx.paths.tilde(ctx.paths.archive)}"
    )
    ctx.say(
        f"events audited: {facts['events']}"
        + (f"; last {last['event_id']} at {_stamp(last.get('ts'))}" if last else "")
    )
    return 0


def _switch(ctx: Context, args: argparse.Namespace) -> int:
    if args.command == "disable":
        ensure_private_dir(ctx.paths.state)
        ctx.paths.disabled.write_text(f"{int(ctx.clock())}\n", encoding="utf-8")
        ctx.audit.append(decision="disabled")
        ctx.say(
            "interception OFF: the stock apport dialog is back, the extension stops hiding windows, the journal source is silent"
        )
    else:
        ctx.paths.disabled.unlink(missing_ok=True)
        ctx.audit.append(decision="enabled")
        ctx.say("interception ON")
    return 0


def _archive_list(ctx: Context, args: argparse.Namespace) -> int:
    rows = [
        {"file": ctx.paths.tilde(path), "bytes": path.stat().st_size, "modified": _stamp(path.stat().st_mtime)}
        for path in list_archive(ctx.paths.archive)
    ]
    if args.json:
        ctx.say(json.dumps(rows, indent=1))
        return 0
    for row in rows:
        ctx.say(f"{row['modified']}  {row['bytes']:>11}  {row['file']}")
    if not rows:
        ctx.say("archive is empty")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="error-intercept", description=__doc__.split("\n\n")[0])
    commands = top.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="publish and archive the apport crash reports")
    ingest.add_argument("--crash-dir", help="scan this directory instead of /var/crash")
    ingest.add_argument("--dry-run", action="store_true", help="show what would happen; touch nothing")
    ingest.set_defaults(run=_ingest)
    commands.add_parser(
        "drain", help="agent-sub drain: retry events agent-sub still holds (the 5-minute timer)"
    ).set_defaults(run=_drain)
    ingest_spool = commands.add_parser("ingest-spool", help="publish the shell extension's spool files")
    ingest_spool.add_argument("--dry-run", action="store_true")
    ingest_spool.set_defaults(run=_ingest_spool)
    journal = commands.add_parser("journal", help="follow the journal and publish matching lines")
    journal.add_argument(
        "journal_args", nargs=argparse.REMAINDER, help="options of the journal source (--dry-run, --once ...)"
    )
    journal.set_defaults(run=_journal)
    listing = commands.add_parser("list", help="audited events")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--limit", type=int, default=30)
    listing.set_defaults(run=_list)
    replay = commands.add_parser("replay", help="deliver an audited event again")
    replay.add_argument("id")
    replay.set_defaults(run=_replay)
    status = commands.add_parser("status", help="stop switch, queues, archive")
    status.add_argument("--json", action="store_true")
    status.set_defaults(run=_status)
    commands.add_parser("enable", help="stop switch off").set_defaults(run=_switch)
    commands.add_parser(
        "disable", help="stop switch on: stock apport dialog back, nothing hidden or published"
    ).set_defaults(run=_switch)
    archive = commands.add_parser("archive", help="the archived reports")
    archive_commands = archive.add_subparsers(dest="archive_command", required=True)
    archive_list = archive_commands.add_parser("list")
    archive_list.add_argument("--json", action="store_true")
    archive_list.set_defaults(run=_archive_list)
    return top


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    **seams: Any,
) -> int:
    """Run one command. ``seams`` replace the process-level collaborators (runner, execv, clock, sleep, uid, quiet_s)."""
    environment = dict(os.environ if env is None else env)
    where = home or Path(environment.get("HOME") or os.path.expanduser("~"))
    top = parser()
    args, extra = top.parse_known_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "journal":
        args.journal_args = extra  # the journal source has its own options (--dry-run, --once, ...)
    elif extra:
        top.error(f"unrecognized arguments: {' '.join(extra)}")
    ctx = Context(Paths.from_env(environment, where), environment, out or sys.stdout, err or sys.stderr, **seams)
    try:
        return args.run(ctx, args)
    except Exception:  # noqa: BLE001 - the process boundary: a bug is logged and exits 1, never a window
        ctx.err.write(traceback.format_exc())
        return 1

"""Scan the apport crash directory: publish what happened, archive the report so apport can never show it.

Accessed via: ``cli ingest`` (what ``update-notifier-crash.service`` and ``error-intercept-scan.service`` run).

For every ``*.crash`` in the directory:

* **owned by the user and readable** (the common case: ``-rw-r----- user whoopsie``, deletable thanks to the
  sticky bit): parse, sanitize, classify, publish, then gzip into the archive and remove the original with its
  ``.upload`` stamps. The archive step runs after a successful publish or after the failure was recorded (the
  publisher keeps a failed event in its pending queue), so the report never stays where apport lists it.
  Backlog: an unseen report (apport's test ``atime <= mtime``) younger than 24 h gets an event; a seen or older one
  is archived with an audit line and no event.
  Optional rule ``nested_test_shell.exempt`` (``rules.py``, default off): a nested test shell crash is then archived
  with the audit decision ``exempt-nested`` and no event.
* **anyone else's** (root-owned system crashes: unreadable and not removable by the user): a metadata-only event
  from the file name and mtime, once per report (``seen.json``), and the file stays.

Nothing is uploaded. A report younger than ``quiet_s`` seconds may still be written by apport: the ingester waits
(bounded) until its mtime stops moving.

Public API: ``Ingester(...).run() -> Outcome``; ``build_event(...)``, ``metadata_event(...)``;
``BACKLOG_S``; ``is_unseen(stat)``; ``apport_can_list(path, uid)`` (apport-checkreports' test, for the tests).
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from error_intercept.archive import (
    ClaimDirError,
    claim,
    free_archive_path,
    recover_claims,
    release,
    remove_upload_stamps,
    store,
)
from error_intercept.audit import Audit
from error_intercept.classify import Classification, classify
from error_intercept.crash_report import ReportError, parse_report
from error_intercept.paths import Paths, ensure_private_dir
from error_intercept.publisher import LOCAL_PREFIX, TOPIC, Publish
from error_intercept.rules import Rules, load_rules
from error_intercept.sanitize import SafeReport, clean_line, clip, mask, printable, sanitize_report

BACKLOG_S = 24 * 3600
TEXT_LIMIT = 1800
DATA_LIMIT = 6000
QUIET_S = 3.0
QUIET_MAX_WAIT_S = 30.0
Event = dict[str, Any]


@dataclass
class Outcome:
    lines: list[str] = field(default_factory=list)
    published: int = 0
    archived: int = 0
    metadata_only: int = 0
    exempt: int = 0

    def note(self, line: str) -> None:
        self.lines.append(line)


def is_unseen(info: os.stat_result) -> bool:
    """apport's ``seen_report`` is ``atime > mtime``."""
    return not info.st_atime > info.st_mtime


def apport_can_list(path: Path, uid: int) -> bool:
    """What ``apport-checkreports`` requires of a report to offer it: readable and writable by the user, unseen."""
    try:
        info = path.lstat()
    except OSError:
        return False
    readable = os.access(path, os.R_OK | os.W_OK) and (info.st_uid == uid or bool(info.st_mode & stat.S_IRWXO))
    return readable and is_unseen(info) and info.st_size > 0


def _signal_text(safe: SafeReport, cls: Classification) -> str:
    reason = safe.fields.get("SegvReason", "")
    return f"{cls.signal}" + (f" ({reason})" if reason else "")


def _summary(safe: SafeReport, cls: Classification, archived: str) -> str:
    fields = safe.fields
    title = fields.get("Title") or f"{cls.source} {cls.kind} ({cls.signal})"
    lines = [title + (" [nested test shell]" if cls.nested else "")]
    lines.append(f"Signal: {_signal_text(safe, cls)}; top frame: {cls.function}")
    if fields.get("Package"):
        lines.append(f"Package: {fields['Package']}")
    if fields.get("ProcCmdline"):
        lines.append(f"Command: {clean_line(fields['ProcCmdline'], 200)}")
    if fields.get("UnreportableReason"):
        lines.append(f"Apport says unreportable: {clean_line(fields['UnreportableReason'], 200)}")
    lines += [f"Fact: {fact}" for fact in safe.facts]
    lines.append(f"Report kept gzipped at {archived}")
    return clip("\n".join(lines), TEXT_LIMIT)


def _data(safe: SafeReport, cls: Classification, name: str, archived: str) -> dict[str, Any]:
    fields = safe.fields
    data: dict[str, Any] = {
        "report": name,
        "executable": fields.get("ExecutablePath", ""),
        "signal": cls.signal,
        "segv_reason": fields.get("SegvReason", ""),
        "segv_analysis": fields.get("SegvAnalysis", ""),
        "stacktrace_top": fields.get("StacktraceTop", ""),
        "cmdline": fields.get("ProcCmdline", ""),
        "package": fields.get("Package", ""),
        "architecture": fields.get("PackageArchitecture", ""),
        "date": fields.get("Date", ""),
        "unreportable_reason": fields.get("UnreportableReason", ""),
        "tags": list(cls.tags),
        "nested_test_shell": cls.nested,
        "facts": safe.facts,
        "archive": archived,
        "journal": safe.journal,
    }
    if len(json.dumps(data, ensure_ascii=False)) > DATA_LIMIT:
        data["journal"] = clip(safe.journal, 500)
        data["segv_analysis"] = clip(data["segv_analysis"], 200)
    return {key: value for key, value in data.items() if value not in ("", [], None) or key == "nested_test_shell"}


def build_event(name: str, safe: SafeReport, cls: Classification, archived: str) -> Event:
    return {
        "topic": TOPIC,
        "kind": cls.kind,
        "severity": cls.severity,
        "source": cls.source,
        "key": cls.key,
        "text": _summary(safe, cls, archived),
        "data": _data(safe, cls, name, archived),
        "frames": [],
    }


REASONS = {
    "root-owned": "but only root can read it; no details are available. The apport window for it is suppressed.",
    "claim-failed": "but it could not be claimed, so it was left in place and not read.",
    "unparsable": "but it could not be read as an apport report. The report is archived and the window suppressed.",
}


def metadata_event(name: str, mtime: float, uid: int, home: Path, reason: str = "root-owned") -> Event:
    """The event for a report nobody can read in full: all that is known is the file name, its owner and its mtime."""
    stem = name[: -len(".crash")]
    program, _, owner = stem.rpartition(".")
    program = program or stem
    source = program.rsplit("_", 1)[-1] or "unknown"
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    text = f"A crash report for {source} exists ({name}, {when}) {REASONS[reason]}"
    return {
        "topic": TOPIC,
        "kind": "crash",
        "severity": "warning",
        "source": source,
        "key": f"{source}|unknown|{reason}",
        "text": mask(text, home),
        "data": {
            "report": name,
            "metadata_only": True,
            "reason": reason,
            "report_owner": owner,
            "report_mtime": when,
            "uid": uid,
        },
        "frames": [],
    }


def wait_until_written(path: Path, quiet_s: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
    """Wait (at most ``QUIET_MAX_WAIT_S`` one-second naps) until nothing has written to ``path`` for ``quiet_s``."""
    for _ in range(int(QUIET_MAX_WAIT_S)):
        try:
            age = clock() - path.lstat().st_mtime
        except OSError:
            return
        if age >= quiet_s:
            return
        sleep(1.0)


class Ingester:
    def __init__(
        self,
        paths: Paths,
        publish: Publish,
        audit: Audit,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        uid: int | None = None,
        dry_run: bool = False,
        quiet_s: float = QUIET_S,
        rules: Rules | None = None,
    ) -> None:
        self._paths = paths
        self._publish = publish
        self._audit = audit
        self._clock = clock
        self._sleep = sleep
        self._uid = os.geteuid() if uid is None else uid
        self._dry_run = dry_run
        self._quiet_s = quiet_s
        self._rules = load_rules(paths) if rules is None else rules
        self._seen = self._load_seen()

    def _load_seen(self) -> dict[str, int]:
        try:
            value = json.loads(self._paths.seen.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): int(v) for k, v in value.items()} if isinstance(value, dict) else {}

    def _save_seen(self, present: set[str]) -> None:
        kept = {name: mtime for name, mtime in self._seen.items() if name in present}
        if kept == self._load_seen():
            return
        target = self._paths.seen
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(kept), encoding="utf-8")
        os.replace(temporary, target)

    def run(self) -> Outcome:
        outcome = Outcome()
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(self._paths.crash_dir)
                if entry.name.endswith(".crash") and not entry.name.startswith(".")
            )
        except OSError as error:
            outcome.note(f"crash directory {self._paths.crash_dir} is not readable: {error}")
            return outcome
        if self._dry_run:
            self._scan(names, outcome)
            return outcome
        ensure_private_dir(self._paths.state)
        with open(self._paths.lock, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for restored in recover_claims(self._paths.crash_dir, self._uid):
                self._audit.append(decision="claim-recovered", report=printable(restored.name))
                names.append(restored.name)
            names = sorted(set(names))  # a recovered report may be back under a name the scan already holds
            self._scan(names, outcome)
            self._save_seen(set(names))
        return outcome

    def _scan(self, names: list[str], outcome: Outcome) -> None:
        for name in names:
            path = self._paths.crash_dir / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            try:
                if info.st_uid == self._uid and os.access(path, os.R_OK):
                    self._own(path, info, outcome)
                else:
                    self._foreign(path, info, outcome)
            except FileNotFoundError:
                continue  # the user (or apport) removed it while we were looking: nothing left to ingest

    # -- reports the user owns ---------------------------------------------------------------------

    def _own(self, path: Path, info: os.stat_result, outcome: Outcome) -> None:
        """Claim the report FIRST, then read, publish and archive that very inode: apport may write a new report under
        the same name at any moment, and a replacement must never be archived unpublished."""
        if self._dry_run:
            self._inspect(path, path, info, outcome)
            return
        try:
            claimed = claim(path, self._uid)
        except FileNotFoundError:
            raise
        except OSError as error:  # a planted claim directory must not hide the crash: report it by metadata
            cause = (
                "its claim directory is not private to this user"
                if isinstance(error, ClaimDirError)
                else clean_line(str(error), 120)
            )
            self._foreign(path, info, outcome, "claim-failed", f"cannot be claimed: {cause}")
            return
        try:
            wait_until_written(claimed, self._quiet_s, self._clock, self._sleep)
            claimed_info = claimed.lstat()
            if not stat.S_ISREG(claimed_info.st_mode) or claimed_info.st_uid != self._uid:
                return  # what was claimed is not what the scan saw: the finally block puts it back
            self._inspect(claimed, path, claimed_info, outcome)
        finally:
            if os.path.lexists(claimed):  # nothing removed it: it goes back, apport lists it again
                release(claimed, path)

    def _inspect(self, source: Path, path: Path, info: os.stat_result, outcome: Outcome) -> None:
        """``source`` is the file to read (the claimed inode), ``path`` its name in the crash directory."""
        old = self._clock() - info.st_mtime > BACKLOG_S
        if old or not is_unseen(info):
            why = "older than 24 h" if old else "already seen in apport"
            outcome.note(f"{printable(path.name)}: backlog ({why}): archive without an event")
            self._backlog(source, path, info, why, outcome)
            return
        try:
            report = parse_report(source)
        except (ReportError, OSError) as error:
            self._unparsable(source, path, info, error, outcome)
            return
        safe = sanitize_report(report, self._paths.home)
        cls = classify(report.fields)  # the parsed fields: what the sanitizer clips must not change the routing
        planned = free_archive_path(self._paths.archive, path, info.st_mtime)
        archived = printable(self._paths.tilde(planned))
        event = build_event(printable(path.name), safe, cls, archived)
        outcome.note(f"{printable(path.name)}: {cls.severity} {cls.kind} {cls.key}")
        if cls.nested and self._rules.nested_exempt:
            self._exempt(source, path, info, event, cls, outcome, planned)
            return
        if self._dry_run:
            outcome.note("  would publish: " + event["text"].replace("\n", "\n    "))
            outcome.note(f"  would archive to {archived} and remove {path}")
            return
        event_id = self._publish(event)
        status = "pending-local" if event_id.startswith(LOCAL_PREFIX) else "published"
        self._audit.append(
            decision="published",
            event_id=event_id,
            report=printable(path.name),
            status=status,
            severity=cls.severity,
            key=cls.key,
            event=event,
        )
        outcome.published += 1
        self._archive(source, path, info, outcome, planned)

    def _exempt(
        self,
        source: Path,
        path: Path,
        info: os.stat_result,
        event: Event,
        cls: Classification,
        outcome: Outcome,
        planned: Path,
    ) -> None:
        """The owner turned the nested-test-shell exemption on: audit log only, no event; the report is still archived."""
        outcome.note("  exempt (nested_test_shell.exempt): audit log only, not published")
        if self._dry_run:
            outcome.note(f"  would archive to {event['data']['archive']} and remove {path}")
            return
        self._audit.append(
            decision="exempt-nested",
            report=printable(path.name),
            severity=cls.severity,
            key=cls.key,
            event=event,
        )
        outcome.exempt += 1
        self._archive(source, path, info, outcome, planned)

    def _backlog(self, source: Path, path: Path, info: os.stat_result, why: str, outcome: Outcome) -> None:
        if self._dry_run:
            return
        self._audit.append(decision="backlog-archived", report=printable(path.name), reason=why)
        self._archive(source, path, info, outcome)

    def _unparsable(self, source: Path, path: Path, info: os.stat_result, error: Exception, outcome: Outcome) -> None:
        outcome.note(f"{printable(path.name)}: cannot parse ({error}): metadata-only event, report archived")
        if self._dry_run:
            return
        event = metadata_event(printable(path.name), info.st_mtime, info.st_uid, self._paths.home, "unparsable")
        event["text"] += f" Parsing failed: {clean_line(str(error), 120)}."
        event_id = self._publish(event)
        self._audit.append(
            decision="parse-failed", event_id=event_id, report=printable(path.name), error=str(error), event=event
        )
        outcome.published += 1
        self._archive(source, path, info, outcome)

    def _archive(
        self, source: Path, path: Path, info: os.stat_result, outcome: Outcome, target: Path | None = None
    ) -> None:
        """Gzip the claimed ``source`` into the archive and drop the stamps; on failure it stays claimed and ``_own`` puts it back."""
        # named after the report, not after its claim (a claim can have taken an ``.rN`` name)
        planned = target or free_archive_path(self._paths.archive, path, info.st_mtime)
        try:
            final = store(source, self._paths.archive, info.st_mtime, planned)
        except OSError as error:
            self._audit.append(decision="archive-failed", report=printable(path.name), error=str(error))
            outcome.note(f"{printable(path.name)}: archive failed ({error}); it stays in the crash directory")
            return
        # the report is archived: a stamp that stays is not an archive failure
        stamps, stuck = remove_upload_stamps(path)
        for stamp, error in stuck:
            self._audit.append(
                decision="stamp-removal-failed",
                report=printable(path.name),
                stamp=printable(stamp.name),
                error=str(error),
            )
            outcome.note(f"{printable(path.name)}: archived, but {printable(stamp.name)} stayed ({error})")
        self._audit.append(
            decision="archived",
            report=printable(path.name),
            archive=printable(self._paths.tilde(final)),
            stamps=[printable(s.name) for s in stamps],
        )
        outcome.archived += 1

    # -- reports that are not ours: left in place ---------------------------------------------------

    def _foreign(
        self,
        path: Path,
        info: os.stat_result,
        outcome: Outcome,
        reason: str = "root-owned",
        why: str = "root-owned or unreadable",
    ) -> None:
        name = path.name
        if self._seen.get(name) == info.st_mtime_ns:
            return
        old = self._clock() - info.st_mtime > BACKLOG_S
        outcome.note(
            f"{printable(name)}: {why}: {'no event (older than 24 h)' if old else 'metadata-only event'}, left in place"
        )
        if self._dry_run:
            return
        if old:
            self._audit.append(
                decision="backlog-left-in-place", report=printable(name), reason=f"{why}, older than 24 h"
            )
        else:
            event = metadata_event(printable(name), info.st_mtime, info.st_uid, self._paths.home, reason)
            if reason == "claim-failed":
                event["text"] += f" Detail: {why}."
            event_id = self._publish(event)
            status = "pending-local" if event_id.startswith(LOCAL_PREFIX) else "published"
            self._audit.append(
                decision="metadata-only" if reason == "root-owned" else reason,
                event_id=event_id,
                report=printable(name),
                status=status,
                event=event,
                why=why,
            )
            outcome.metadata_only += 1
        self._seen[name] = info.st_mtime_ns

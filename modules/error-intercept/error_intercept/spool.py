"""Publish the events the shell extension dropped into the spool directory, then archive each file.

Accessed via: ``cli ingest-spool`` (run by ``error-intercept-spool.service`` when ``error-intercept-spool.path``
sees a new file).

The extension never forks anything: it writes one JSON file per hidden or observed window,
``{"v": 1, "ts": epoch, "source": "shell-extension", "kind": "window", "severity": "warning", "key": "<rule>|<wm_class>",
"rule": "...", "action": "intercept|observe", "wm_class": "...", "app_id": "...", "title": "...", "pid": N}``,
atomically (a ``.tmp`` or leading-dot name is a file still being written and is skipped). A file that is not valid
JSON, is too big or is not an object is archived too, with an audit line saying why: it must not be retried for ever.
While the stop switch ``DISABLED`` exists nothing is published and the files wait. One consumer at a time
(``spool.lock``): the login scan and the path-triggered service can overlap.

Public API: ``ingest_spool(paths, publish, audit, dry_run=False) -> list[str]`` (one line per file),
``event_from_spool(record, home) -> dict``, ``MAX_FILE_BYTES``.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any

from error_intercept.archive import archive_file
from error_intercept.audit import Audit
from error_intercept.paths import Paths
from error_intercept.publisher import LOCAL_PREFIX, TOPIC, Publish
from error_intercept.sanitize import clean_line, clip, printable

MAX_FILE_BYTES = 16_384
SEVERITIES = ("info", "warning", "critical")
ACTIONS = {"intercept": "hidden", "observe": "left visible"}


class SpoolError(Exception):
    """The spool file is not a usable record; the message is the audit reason."""


def _text(value: object, limit: int) -> str:
    return clean_line(str(value), limit) if value not in (None, "") else ""


def event_from_spool(record: dict[str, Any], home: Path) -> dict[str, Any]:
    wm_class = _text(record.get("wm_class"), 80)
    app_id = _text(record.get("app_id"), 80)
    rule = _text(record.get("rule"), 80) or "unknown-rule"
    action = record.get("action") if record.get("action") in ACTIONS else "observe"
    severity = record.get("severity") if record.get("severity") in SEVERITIES else "warning"
    source = wm_class or app_id or "shell-extension"
    title = _text(record.get("title"), 200)
    verb = ACTIONS[str(action)]
    text = f"An error window was {verb} (rule {rule}): {source}" + (f" - {title}" if title else "")
    key = _text(record.get("key"), 160) or f"{rule}|{source}"
    data = {
        "rule": rule,
        "action": action,
        "wm_class": wm_class,
        "app_id": app_id,
        "title": title,
        "pid": record.get("pid") if isinstance(record.get("pid"), int) else None,
        "spooled_at": record.get("ts") if isinstance(record.get("ts"), (int, float)) else None,
    }
    return {
        "topic": TOPIC,
        "kind": _text(record.get("kind"), 40) or "window",
        "severity": severity,
        "source": source,
        "key": key,
        "text": clip(text, 1000),
        "data": {name: value for name, value in data.items() if value not in ("", None)},
        "frames": [],
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SpoolError(f"unreadable: {error}") from error
    if len(raw) > MAX_FILE_BYTES:
        raise SpoolError(f"larger than {MAX_FILE_BYTES} bytes")
    try:
        record = json.loads(raw)
    except ValueError as error:
        raise SpoolError("not JSON") from error
    if not isinstance(record, dict):
        raise SpoolError("not a JSON object")
    return record


def _archive(path: Path, destination: Path) -> None:
    archive_file(path, destination, path.stat().st_mtime, use_claim=False)


def _waiting(spool: Path) -> list[Path]:
    try:
        entries = sorted(spool.iterdir())
    except OSError:
        return []
    return [
        p for p in entries if p.suffix == ".json" and not p.name.startswith(".") and p.is_file() and not p.is_symlink()
    ]


def ingest_spool(paths: Paths, publish: Publish, audit: Audit, dry_run: bool = False) -> list[str]:
    if paths.disabled.exists():
        return ["DISABLED: the spool is left as it is"]
    if dry_run:
        return _handle_all(paths, publish, audit, dry_run=True)
    paths.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    # the login scan service and the path-triggered service may run together: one consumer at a time
    with open(paths.spool_lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _handle_all(paths, publish, audit, dry_run=False)


def _handle_all(paths: Paths, publish: Publish, audit: Audit, *, dry_run: bool) -> list[str]:
    lines: list[str] = []
    for path in _waiting(paths.spool):
        try:
            lines += _handle(path, paths, publish, audit, dry_run=dry_run)
        except FileNotFoundError:
            continue  # another consumer (or the extension's cleanup) took it first
    return lines


def _handle(path: Path, paths: Paths, publish: Publish, audit: Audit, *, dry_run: bool) -> list[str]:
    shown = printable(path.name)
    try:
        record = _load(path)
    except SpoolError as error:
        if not dry_run:
            audit.append(decision="spool-malformed", report=shown, reason=str(error))
            _archive(path, paths.spool_archive)
        return [f"{shown}: {error}: archived without an event"]
    event = event_from_spool(record, paths.home)
    lines = [f"{shown}: {event['severity']} {event['kind']} {event['key']}"]
    if dry_run:
        return [*lines, "  would publish: " + event["text"]]
    event_id = publish(event)
    status = "pending-local" if event_id.startswith(LOCAL_PREFIX) else "published"
    audit.append(decision="spool-published", event_id=event_id, report=shown, status=status, event=event)
    _archive(path, paths.spool_archive)
    return lines

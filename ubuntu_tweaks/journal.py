"""The run journal: what ``apply --commit`` planned, did and could not do (spec, *Reconcile and failover*).

Accessed via: ``reconcile._commit`` (writes it) and ``commands/status.py`` (prints the last run).

Assumptions: one file, ``<state>/ubuntu-tweaks/run-journal.json``, replaced atomically after every action so a
crash, Ctrl-C or power loss leaves a readable record. The journal is information, not a replay log: convergence
needs no replay because every action is re-observed (``check()``) by the next run. It holds action kinds and
descriptions, never secrets (descriptions are what the plan already prints).

Public API:

- ``journal_path(environ=None, home=None) -> Path``.
- ``RunJournal(path, command)``: ``begin(actions)``, ``add(actions)`` (a rebuilt plan), ``record(action, status,
  detail="")`` with status ``pending`` / ``done`` / ``failed`` / ``blocked`` / ``deferred`` / ``manual``,
  ``finish(exit_code)``.
- ``previous(path) -> Run | None``: the journal on disk; ``Run.finished`` False means that run was interrupted.
- ``Run`` (``id``, ``command``, ``started``, ``finished``, ``exit_code``, ``entries``) and ``Entry`` (``key``,
  ``kind``, ``text``, ``status``, ``detail``); ``Run.counts() -> dict[str, int]``,
  ``Run.problems() -> list[Entry]`` (failed, blocked, deferred).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ubuntu_tweaks.actions import Action
from ubuntu_tweaks.atomic_write import atomic_write
from ubuntu_tweaks.capture.secrets import mask_secrets

MAX_TEXT = 500  # one journal line is a pointer to the session log, not the log

PENDING = "pending"
PROBLEM_STATUSES = ("failed", "blocked", "deferred")


def _safe(text: str) -> str:
    """What is stored: secrets masked the way the session log masks them, and clipped (error text can carry a
    child tool's stderr)."""
    masked = mask_secrets(text)
    return masked if len(masked) <= MAX_TEXT else masked[: MAX_TEXT - 1] + "…"


def journal_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("XDG_STATE_HOME")
    state = Path(configured) if configured else (home if home is not None else Path.home()) / ".local/state"
    return state / "ubuntu-tweaks" / "run-journal.json"


def action_key(action: Action) -> str:
    return f"{action.kind}|{action.scope or ''}|{action.key or action.describe()}"


@dataclass
class Entry:
    key: str
    kind: str
    text: str
    status: str = PENDING
    detail: str = ""


@dataclass
class Run:
    id: str
    command: str
    started: float
    finished: bool = False
    exit_code: int | None = None
    entries: list[Entry] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts

    def problems(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.status in PROBLEM_STATUSES]


def previous(path: Path) -> Run | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = [Entry(**entry) for entry in document["entries"]]
        return Run(
            document["id"],
            document["command"],
            document["started"],
            document["finished"],
            document["exit_code"],
            entries,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


class RunJournal:
    def __init__(self, path: Path, command: str, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._run = Run(f"{int(clock())}-{os.getpid()}", command, clock())
        self._index: dict[str, Entry] = {}

    @property
    def run(self) -> Run:
        return self._run

    def begin(self, actions: Iterable[Action]) -> None:
        self.add(actions)

    def add(self, actions: Iterable[Action]) -> None:
        """Entries for actions the journal does not hold yet (a plan rebuilt mid-run adds some)."""
        for action in actions:
            key = action_key(action)
            if key not in self._index:
                entry = Entry(_safe(key), action.kind, _safe(action.describe()))
                self._index[key] = entry
                self._run.entries.append(entry)
        self._write()

    def record(self, action: Action, status: str, detail: str = "") -> None:
        key = action_key(action)
        entry = self._index.get(key)
        if entry is None:
            entry = Entry(_safe(key), action.kind, _safe(action.describe()))
            self._index[key] = entry
            self._run.entries.append(entry)
        entry.status = status
        entry.detail = _safe(detail)
        self._write()

    def finish(self, exit_code: int) -> None:
        self._run.finished = True
        self._run.exit_code = exit_code
        self._write()

    def _write(self) -> None:
        document = {
            "id": self._run.id,
            "command": self._run.command,
            "started": self._run.started,
            "finished": self._run.finished,
            "exit_code": self._run.exit_code,
            "entries": [vars(entry) for entry in self._run.entries],
        }
        try:
            atomic_write(self._path, json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8"))
        except OSError:
            pass  # the journal is a record, never a reason to stop a run

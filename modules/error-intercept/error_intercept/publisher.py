"""Publish an event to the ``errors`` topic through ``agent-sub``, and never lose one that could not be published.

Accessed via: ``cli`` builds ``DurablePublisher`` and hands it to ``apport_source``, ``spool`` and the journal
source (``error_intercept.journal_source.main(publish)``); the tests inject a runner or a fake callable.

The publisher seam is ``publish(event: dict) -> str`` with the keys ``topic``, ``kind``, ``severity``, ``source``,
``key``, ``text``, ``data`` and ``frames``; it returns the event id. ``AgentSubPublisher`` runs
``agent-sub publish ... --json`` (the runner is injectable) and raises ``PublishError`` when that fails.
``drain()`` runs ``agent-sub drain`` (agent-sub only retries pending events there, so the ingesters call it after
their work and ``error-intercept-drain.timer`` every 5 minutes). ``DurablePublisher`` wraps it: a failed publish is written to the audit log AND kept as a JSON file in the
``pending`` directory, the returned id then starts with ``pending-``, and ``retry_pending`` publishes those files
again (the next ingester run does that first).

``agent-sub`` is looked up on PATH, then in ``~/.local/bin``, then in the ``agent_tools_dir`` of the config file
(``$XDG_CONFIG_HOME/error-intercept/config.json``), then in ``~/xp/agent-tools`` and ``~/dev/agent-tools`` (its
``lib/agenttools_subscriptions/agent-sub`` launcher). The child gets a PATH that also holds the usual user tool
directories, because a systemd user unit may start with a shorter PATH than the login shell (omp, tmux and tg-ctl
must be found by agent-sub when it starts an agent).

Public API: ``PublishError``, ``AgentSubPublisher``, ``DurablePublisher``, ``locate_agent_sub``, ``tool_path``,
``LOCAL_PREFIX`` (``pending-``), ``Publish`` (the callable type).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from error_intercept.audit import Audit
from error_intercept.durable import write_durably
from error_intercept.paths import Paths

Event = dict[str, Any]
Publish = Callable[[Event], str]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
LOCAL_PREFIX = "pending-"
TOPIC = "errors"
USAGE_EXIT = 2  # agent-sub: usage error or a value it refuses
REQUIRED_KEYS = ("kind", "severity", "source", "key", "text")
MAX_ARGUMENT_BYTES = 120_000  # Linux refuses one argument over 128 KiB (E2BIG): such an event can never be published
PUBLISH_TIMEOUT_S = 240  # publishing waits (bounded) for a freshly started agent to come up
FALLBACK_TOOL_DIRS = (".local/bin", ".bun/bin", ".cargo/bin", "go/bin", "n/bin")
LAUNCHER = Path("lib/agenttools_subscriptions/agent-sub")
CHECKOUT_GUESSES = ("xp/agent-tools", "dev/agent-tools")


class PublishError(Exception):
    """``agent-sub publish`` did not store the event; the message says why.

    ``permanent`` marks a refusal that retrying cannot fix (agent-sub exit 2: bad topic, severity or data; an event
    without the required keys; a NUL byte or an argument over the kernel's size limit): such an event is set aside (``.rejected``) instead of blocking the retry queue.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def load_config(paths: Paths) -> dict[str, Any]:
    try:
        value = json.loads(paths.config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def locate_agent_sub(
    paths: Paths,
    env: Mapping[str, str],
    which: Callable[..., str | None] = shutil.which,
    is_executable: Callable[[Path], bool] = lambda path: path.is_file() and os.access(path, os.X_OK),
) -> str | None:
    found = which("agent-sub", path=tool_path(env, paths.home))
    if found:
        return found
    candidates: list[Path] = []
    configured = load_config(paths).get("agent_tools_dir")
    if isinstance(configured, str) and configured:
        candidates.append(Path(configured).expanduser() / LAUNCHER)
    candidates += [paths.home / guess / LAUNCHER for guess in CHECKOUT_GUESSES]
    return next((str(path) for path in candidates if is_executable(path)), None)


def tool_path(env: Mapping[str, str], home: Path) -> str:
    """PATH for the child: the caller's, then the user tool directories that are not in it yet."""
    current = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    extra = [str(home / name) for name in FALLBACK_TOOL_DIRS] + ["/home/linuxbrew/.linuxbrew/bin"]
    return os.pathsep.join(current + [path for path in extra if path not in current])


class AgentSubPublisher:
    def __init__(
        self,
        paths: Paths,
        env: Mapping[str, str],
        runner: Runner = subprocess.run,
        locate: Callable[[], str | None] | None = None,
    ) -> None:
        self._env = {**env, "PATH": tool_path(env, paths.home)}
        self._runner = runner
        self._locate = locate or (lambda: locate_agent_sub(paths, env))

    @staticmethod
    def _refuse_unsendable(arguments: tuple[str, ...]) -> None:
        """A NUL byte or an argument over the kernel's limit can never be passed to a child: retrying it for ever would
        block the queue behind it, so it is a permanent refusal (kept under ``.rejected``) before anything is started."""
        for argument in arguments:
            if "\0" in argument:
                raise PublishError("an argument holds a NUL byte, which cannot be passed to agent-sub", permanent=True)
            try:
                size = len(os.fsencode(argument))
            except UnicodeEncodeError as error:
                raise PublishError(f"an argument is not encodable: {error}", permanent=True) from error
            if size > MAX_ARGUMENT_BYTES:
                raise PublishError(
                    f"an argument is {size} bytes, over the {MAX_ARGUMENT_BYTES} bytes one argument may hold",
                    permanent=True,
                )

    def _invoke(self, *arguments: str) -> "subprocess.CompletedProcess[str]":
        launcher = self._locate()
        if launcher is None:
            raise PublishError("agent-sub not found (install agent-tools or set agent_tools_dir in the config)")
        self._refuse_unsendable(arguments)
        try:
            done = self._runner(
                [launcher, *arguments], capture_output=True, text=True, timeout=PUBLISH_TIMEOUT_S, env=self._env
            )
        except UnicodeDecodeError as error:  # the child already ran and printed bytes that are not UTF-8: unknown outcome
            raise PublishError(f"agent-sub output could not be decoded: {error}") from error
        except ValueError as error:  # the child could not be built from these arguments: retrying cannot help
            raise PublishError(f"agent-sub could not run: {error}", permanent=True) from error
        except OSError as error:
            raise PublishError(f"agent-sub could not run: {error}") from error  # E2BIG too: one argument is bounded above, so it is the environment
        except subprocess.SubprocessError as error:
            raise PublishError(f"agent-sub could not run: {error}") from error
        if done.returncode != 0:
            last = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["no output"]
            raise PublishError(
                f"agent-sub exited {done.returncode}: {last[0]}", permanent=done.returncode == USAGE_EXIT
            )
        return done

    def _publish_reply(self, *arguments: str) -> dict[str, Any]:
        done = self._invoke(*arguments, "--json")
        try:
            reply = json.loads(done.stdout)
        except ValueError as error:
            raise PublishError(f"agent-sub printed no JSON: {done.stdout.strip()[:200]!r}") from error
        if not isinstance(reply, dict) or not isinstance(reply.get("id"), str):
            raise PublishError(f"agent-sub reply has no event id: {done.stdout.strip()[:200]!r}")
        return reply

    def __call__(self, event: Event) -> str:
        missing = [key for key in REQUIRED_KEYS if key not in event]
        if missing:
            raise PublishError(f"event lacks {', '.join(missing)}", permanent=True)
        arguments = [
            "publish",
            f"--topic={event.get('topic', TOPIC)}",
            f"--kind={event['kind']}",
            f"--severity={event['severity']}",
            f"--source={event['source']}",
            f"--key={event['key']}",
            f"--text={event['text']}",
            f"--data={json.dumps(event.get('data') or {}, ensure_ascii=False)}",
        ]
        arguments += [f"--frame={frame}" for frame in event.get("frames") or []]
        return str(self._publish_reply(*arguments)["id"])

    def replay(self, event_id: str) -> str:
        """``agent-sub replay ID`` (plain text, no ``--json`` there): the event keeps its id."""
        self._invoke("replay", event_id)
        return event_id

    def drain(self) -> str:
        """``agent-sub drain``: retry what agent-sub itself holds (rate-limited, unacked, waiting for an agent)."""
        return self._invoke("drain").stdout.strip()


class DurablePublisher:
    def __init__(
        self,
        inner: Publish,
        paths: Paths,
        audit: Audit,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = inner
        self._paths = paths
        self._audit = audit
        self._clock = clock

    def __call__(self, event: Event) -> str:
        try:
            return self._inner(event)
        except PublishError as error:
            return self._keep(event, str(error), error.permanent)

    def _keep(self, event: Event, reason: str, permanent: bool = False) -> str:
        stamp = self._clock()
        digest = hashlib.sha1(json.dumps(event, sort_keys=True, default=str).encode() + str(stamp).encode())
        local_id = f"{LOCAL_PREFIX}{int(stamp)}-{digest.hexdigest()[:8]}"
        self._paths.pending.mkdir(mode=0o700, parents=True, exist_ok=True)
        kept = json.dumps({"id": local_id, "error": reason, "event": event}).encode("ascii")
        # on disk before anybody archives the report; a refused event is kept under a name no retry looks at
        write_durably(self._paths.pending / f"{local_id}{'.rejected' if permanent else '.json'}", kept)
        self._audit.append(
            decision="publish-rejected" if permanent else "publish-failed",
            event_id=local_id,
            status="rejected" if permanent else "pending-local",
            error=reason,
            event=event,
        )
        return local_id

    def pending_files(self) -> list[Path]:
        try:
            return sorted(self._paths.pending.glob(f"{LOCAL_PREFIX}*.json"))
        except OSError:
            return []

    def retry_pending(self) -> list[str]:
        """Publish the kept events again, oldest first; stops at the first failure (agent-sub is still down).

        Serialized by a lock file: ``ingest`` and ``ingest-spool`` may run at the same moment and must not both
        publish (or delete) the same file.
        """
        files = self.pending_files()
        if not files:
            return []
        with open(self._paths.pending / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._retry_each(self.pending_files())

    def _retry_each(self, files: list[Path]) -> list[str]:
        published: list[str] = []
        for path in files:
            try:
                kept = json.loads(path.read_text(encoding="utf-8"))
                event = kept["event"]
                new_id = self._inner(event)
            except PublishError as error:
                if not error.permanent:
                    break
                self._audit.append(decision="retry-rejected", report=path.name, error=str(error))
                path.rename(path.with_suffix(".rejected"))
                continue
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._audit.append(decision="pending-unreadable", report=path.name, error=str(error))
                path.rename(path.with_suffix(".bad"))
                continue
            self._audit.append(decision="retried", event_id=new_id, was=kept.get("id"), status="published", event=event)
            path.unlink(missing_ok=True)
            published.append(new_id)
        return published

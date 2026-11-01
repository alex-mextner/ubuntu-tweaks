"""Where error-intercept keeps its files (spec: docs/specs/error-intercept.md, *Files*).

Accessed via: every module of the package takes a ``Paths``; ``cli.main`` builds it from the environment.

Public API:

- ``Paths.from_env(env, home)``: state ``$XDG_STATE_HOME/error-intercept`` (``~/.local/state`` by default),
  config ``$XDG_CONFIG_HOME/error-intercept``, crash directory ``/var/crash``.
- the derived files: ``archive``, ``spool``, ``pending``, ``audit``, ``disabled`` (the stop switch),
  ``seen`` (root-owned reports already reported), ``lock``, ``config_file`` and ``spool_archive``.
- ``ORIGINAL_NOTIFIER``: the stock apport dialog launcher the stop switch hands control back to.
- ``ensure_private_dir(path)``: create a directory readable by the owner only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ORIGINAL_NOTIFIER = "/usr/lib/update-notifier/update-notifier-crash"
DEFAULT_CRASH_DIR = "/var/crash"
APP = "error-intercept"


@dataclass(frozen=True)
class Paths:
    home: Path
    state: Path
    config: Path
    crash_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str], home: Path) -> Paths:
        state_home = Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME") else home / ".local/state"
        config_home = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
        crash = Path(env.get("ERROR_INTERCEPT_CRASH_DIR") or DEFAULT_CRASH_DIR)
        return cls(home=home, state=state_home / APP, config=config_home / APP, crash_dir=crash)

    @property
    def archive(self) -> Path:
        return self.state / "archive"

    @property
    def spool_archive(self) -> Path:
        return self.archive / "spool"

    @property
    def spool(self) -> Path:
        return self.state / "spool"

    @property
    def pending(self) -> Path:
        return self.state / "pending"

    @property
    def audit(self) -> Path:
        return self.state / "audit.jsonl"

    @property
    def disabled(self) -> Path:
        return self.state / "DISABLED"

    @property
    def seen(self) -> Path:
        return self.state / "seen.json"

    @property
    def lock(self) -> Path:
        return self.state / "ingest.lock"

    @property
    def config_file(self) -> Path:
        return self.config / "config.json"

    def tilde(self, path: Path | str) -> str:
        """``path`` with the home directory written as ``~`` (what is shown to people and sent to agents)."""
        text = str(path)
        home = str(self.home)
        return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)

"""Base of the small modules that only own a few text files (and directories) and run a command afterwards.

Accessed via: ``modules/inotify-limits/inotify-limits``, ``modules/tmp-scratch/tmp-scratch`` and
``modules/tmpdir/tmpdir``.

``ManagedFiles`` is a ``Module`` whose ``findings()``, ``plan()`` and ``apply()`` all come from one list of
``ManagedFile`` / ``ManagedDirectory`` declarations, so what ``plan`` promises is what ``install`` writes. A
file that exists and was not written by the module (its first line is not ``MARKER``-prefixed) is copied to
``<root>/var/backups/ubuntu-tweaks/<module>/<UTC stamp>/`` before it is replaced. A subclass adds facts
(``extra_findings``), a human step that stops everything (``blocked``), what to say in the plan (``extra_plan``)
and the command that makes the new files live (``converge``).
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ubuntu_tweaks.module_script import (
    Finding,
    Module,
    ModuleError,
    NeedsManual,
    read_text_or_none,
    unified_diff_lines,
    write_file,
)

MARKER = "# Managed by ubuntu-tweaks"
BACKUP_DIR = "var/backups/ubuntu-tweaks"


@dataclass(frozen=True)
class ManagedFile:
    path: Path
    text: str
    mode: int = 0o644


@dataclass(frozen=True)
class ManagedDirectory:
    path: Path
    mode: int = 0o700


class ManagedFiles(Module):
    """Subclasses set ``name`` and call ``super().__init__`` with the already root-prefixed paths."""

    requires_root = True

    def __init__(
        self,
        root: Path,
        files: Sequence[ManagedFile],
        directories: Sequence[ManagedDirectory] = (),
        *,
        is_root: bool,
        clock: datetime | None = None,
    ) -> None:
        self._root = root
        self._files = tuple(files)
        self._directories = tuple(directories)
        self._is_root = is_root
        self._stamp = (clock or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")

    # -- hooks -------------------------------------------------------------------------------------------------

    def extra_findings(self) -> list[Finding]:
        return []

    def blocked(self) -> str | None:
        """A human step that must happen first: nothing is written while it stands."""
        return None

    def extra_plan(self, changed: bool) -> list[str]:
        return []

    def converge(self, changed: bool) -> None:
        """Runs after the files are written (``changed``: at least one was); also when nothing was written, so a
        live state that drifted from clean files is repaired."""

    def needs_converge(self) -> bool:
        """True when the live state differs from the files even though the files are in place."""
        return False

    # -- facts -------------------------------------------------------------------------------------------------

    def shown(self, path: Path) -> str:
        try:
            return "/" + str(path.relative_to(self._root))
        except ValueError:
            return str(path)

    def _stale_files(self) -> list[tuple[ManagedFile, str | None]]:
        return [(item, old) for item in self._files if (old := read_text_or_none(item.path)) != item.text]

    def _missing_directories(self) -> list[ManagedDirectory]:
        return [
            item
            for item in self._directories
            if not item.path.is_dir() or item.path.stat().st_mode & 0o777 != item.mode
        ]

    def findings(self) -> list[Finding]:
        facts: list[Finding] = []
        for item in self._files:
            old = read_text_or_none(item.path)
            detail = "" if old == item.text else "missing" if old is None else "differs from the declared content"
            facts.append(Finding(old == item.text, self.shown(item.path), detail))
        stale_dirs = {item.path for item in self._missing_directories()}
        facts += [
            Finding(item.path not in stale_dirs, f"{self.shown(item.path)} is a directory ({item.mode:04o})")
            for item in self._directories
        ]
        return facts + self.extra_findings()

    # -- plan --------------------------------------------------------------------------------------------------

    def plan(self) -> list[str]:
        problem = self.blocked()
        if problem:
            return [f"needs manual: {problem}"]
        lines: list[str] = []
        stale = self._stale_files()
        for item, old in stale:
            if old is not None and not old.startswith(MARKER):
                lines.append(f"  back up {self.shown(item.path)} to /{BACKUP_DIR}/{self.name}/<UTC timestamp>/")
            lines.append(f"  write {self.shown(item.path)}")
            lines += unified_diff_lines(self.shown(item.path), old, item.text)
        lines += [f"  create {self.shown(item.path)} ({item.mode:04o})" for item in self._missing_directories()]
        lines += self.extra_plan(bool(stale))
        if not lines and self.needs_converge():
            lines += self.extra_plan(False)
        return lines or ["nothing to change"]

    # -- install -----------------------------------------------------------------------------------------------

    def _backup(self, path: Path) -> None:
        target = self._root / BACKUP_DIR / self.name / self._stamp
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / path.name)

    def apply(self) -> None:
        if self.requires_root and not self._is_root:
            raise ModuleError("must run as root (ubuntu-tweak runs it through sudo)")
        problem = self.blocked()
        if problem:
            raise NeedsManual(problem)
        stale = self._stale_files()
        for item, old in stale:
            if old is not None and old.strip() and not old.startswith(MARKER):
                self._backup(item.path)
            write_file(item.path, item.text, item.mode)
            sys.stdout.write(f"wrote {self.shown(item.path)}\n")
        for directory in self._missing_directories():
            directory.path.mkdir(parents=True, exist_ok=True)
            directory.path.chmod(directory.mode)
            sys.stdout.write(f"created {self.shown(directory.path)}\n")
        if stale or self.needs_converge():
            self.converge(bool(stale))
        else:
            sys.stdout.write("nothing to change\n")

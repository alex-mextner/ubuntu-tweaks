"""Section 2 of `capture`: which files would be adopted into ``files/``, and a classified listing of
everything else in ``~/.config`` and the ``$HOME`` dotfiles.

Accessed via: ``plan.build_plan``. Read-only. File *contents* are read only to scan and classify
them (adopted files, and small unknown text files for the listing). Candidate contents are never
kept; an adopted file's bytes are kept in ``AdoptedFile.content`` so ``--commit`` writes exactly
what was scanned. Nothing is printed except classes, reasons, sizes, counts and key *names*.

Adoption is an explicit allow-list (``Rules.files.include``, globs relative to ``$HOME``); nothing
outside it is ever adopted. The listing (``Candidate``) is advice: the user promotes an entry by
adding it to ``include_files`` in the profile.

Classes: ``portable-config`` (small text, no credential pattern), ``secret-bearing`` (credential
name, credential store directory, or a secret pattern inside), ``cache-state`` (binary, large,
log/db/lock/history, cache directories), ``mixed`` (credential-looking key names without a secret
value, or a large application data directory).

Public API: ``scan_files(home, profile, rules) -> FilesResult``; ``scan_entry(home, path, rules) -> FilesResult``
(the same classified walk of one entry, which the coverage ``files`` adapter uses); ``classify_file(name, size, data,
rules)``; the dataclasses ``AdoptedFile``, ``SkippedFile``, ``Candidate``, ``FilesResult``.
"""

from __future__ import annotations

import difflib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from ubuntu_tweaks.capture.rules import FileRules, Rules
from ubuntu_tweaks.capture.secrets import Finding, key_names, scan_text
from ubuntu_tweaks.errors import RefusedError
from ubuntu_tweaks.profile import USER_UNIT_DIR, Profile

type FileStatus = Literal["new", "identical", "differs", "linked"]
type FileClass = Literal["portable-config", "secret-bearing", "cache-state", "mixed"]

_GLOB_CHARS = re.compile(r"[*?]")


@dataclass(frozen=True)
class AdoptedFile:
    rel: str
    source: Path
    dest: Path
    machine: bool
    size: int
    status: FileStatus
    lines_added: int = 0
    lines_removed: int = 0
    findings: tuple[Finding, ...] = ()
    content: bytes = field(default=b"", repr=False)  # exactly what was scanned; commit writes these bytes


@dataclass(frozen=True)
class SkippedFile:
    rel: str
    reason: str


@dataclass(frozen=True)
class Candidate:
    rel: str
    kind: Literal["file", "dir"]
    cls: FileClass
    reason: str
    size: int | None
    entries: int | None


@dataclass
class FilesResult:
    adopted: list[AdoptedFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    symlinks_ignored: int = 0


def classify_file(name: str, size: int, data: bytes | None, rules: FileRules) -> tuple[FileClass, str]:
    """Class and reason of one file from its name, size and (for small files) content."""
    if rules.secret_names.matches(name):
        return "secret-bearing", "name matches a credential pattern"
    if rules.cache_names.matches(name):
        return "cache-state", "log, database, lock, history or backup by name"
    if size == 0:
        return "cache-state", "empty marker file"
    if size > rules.max_text_bytes or data is None:
        return "cache-state", f"larger than {rules.max_text_bytes // 1024} KiB: state or data, not hand-written config"
    if b"\0" in data:
        return "cache-state", "binary file"
    text = data.decode("utf-8", errors="replace")
    findings = scan_text(text, exempt_email_keys=rules.email_exempt)
    if findings:
        kinds = ", ".join(sorted({finding.kind for finding in findings}))
        return "secret-bearing", f"content matches: {kinds}"
    names = key_names(text, rules.secret_key_names)
    if names:
        listed = ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        return "mixed", f"key names suggest credentials: {listed}"
    suffix = PurePosixPath(name).suffix
    if suffix in rules.portable_extensions:
        return "portable-config", "small text config, no credential pattern or key name"
    return "portable-config", "small text file, no credential pattern or key name"


def _checked_patterns(patterns: tuple[str, ...]) -> None:
    for pattern in patterns:
        parts = PurePosixPath(pattern).parts
        if pattern.startswith("/") or ".." in parts or pattern.startswith("~"):
            raise RefusedError(f"refused: include_files entry {pattern!r} is absolute or leaves $HOME")


def _expand(home: Path, patterns: tuple[str, ...]) -> list[str]:
    found: dict[str, None] = {}
    for pattern in patterns:
        if _GLOB_CHARS.search(pattern):
            for path in sorted(home.glob(pattern)):
                if not path.is_dir() or path.is_symlink():
                    found[path.relative_to(home).as_posix()] = None
        elif os.path.lexists(home / pattern):
            found[pattern] = None
    return list(found)


def _line_delta(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _is_unit_file(rel: str) -> bool:
    """A systemd user unit or drop-in: unlike other allow-listed files, one secret in it skips just that file."""
    return rel.startswith(f"{USER_UNIT_DIR.as_posix()}/")


def _adopt(home: Path, profile: Profile, rules: FileRules, rel: str, result: FilesResult) -> None:
    source = home / rel
    machine = rules.machine.matches(rel)
    dest = (profile.overlay_files_dir if machine else profile.files_dir) / rel
    if source.is_symlink():
        if os.path.realpath(source) != os.path.realpath(dest):
            result.skipped.append(SkippedFile(rel, f"symlink to {os.readlink(source)}: not adopted"))
            return
        if not dest.is_file():
            result.skipped.append(SkippedFile(rel, "symlink into the profile, but the profile copy is gone"))
            return
        try:
            if dest.stat().st_size > rules.max_file_bytes:
                result.skipped.append(SkippedFile(rel, "profile copy larger than the scan limit: not adopted"))
                return
            raw = dest.read_bytes()
        except OSError as error:
            result.skipped.append(SkippedFile(rel, f"profile copy unreadable: {error.strerror or error}"))
            return
        findings = scan_text(raw.decode("utf-8", errors="replace"), exempt_email_keys=rules.email_exempt)
        result.adopted.append(
            AdoptedFile(
                rel=rel, source=source, dest=dest, machine=machine, size=len(raw), status="linked", findings=findings
            )
        )
        return
    if not source.is_file():
        result.skipped.append(SkippedFile(rel, "not a regular file"))
        return
    size = source.stat().st_size
    if size > rules.max_file_bytes:
        result.skipped.append(SkippedFile(rel, f"larger than {rules.max_file_bytes // 1024} KiB"))
        return
    try:
        raw = source.read_bytes()
        text = raw.decode("utf-8")
    except OSError as error:
        result.skipped.append(SkippedFile(rel, f"unreadable: {error.strerror or error}"))
        return
    except UnicodeDecodeError:
        result.skipped.append(SkippedFile(rel, "not UTF-8 text"))
        return
    findings = scan_text(text, exempt_email_keys=rules.email_exempt)
    if findings and _is_unit_file(rel):
        # an `Environment=` line with a token: the unit stays where it is, the rest of the capture goes on
        kinds = ", ".join(sorted({f.kind for f in findings}))
        result.skipped.append(
            SkippedFile(rel, f"secret finding ({kinds}): not adopted; keep the secret in the keychain")
        )
        return
    status: FileStatus = "new"
    added = removed = 0
    if dest.is_file():
        old = dest.read_text(encoding="utf-8", errors="replace")
        status = "identical" if old == text else "differs"
        if status == "differs":
            added, removed = _line_delta(old, text)
    result.adopted.append(
        AdoptedFile(
            rel=rel,
            source=source,
            dest=dest,
            machine=machine,
            size=size,
            status=status,
            lines_added=added,
            lines_removed=removed,
            findings=findings,
            content=raw,
        )
    )


def _count_entries(path: Path, cap: int) -> int:
    """Files under ``path`` (not following symlinked directories), stopping at ``cap``."""
    count = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        count += 1
                        if count >= cap:
                            return count
        except OSError:
            continue
    return count


def _list_dir(home: Path, path: Path, depth: int, rules: FileRules, skip: frozenset[str], result: FilesResult) -> None:
    rel = path.relative_to(home).as_posix()
    name = path.name
    if rules.secret_dirs.matches(name):
        result.candidates.append(
            Candidate(
                rel,
                "dir",
                "secret-bearing",
                "credential store or browser profile by name",
                None,
                _count_entries(path, 10_000),
            )
        )
        return
    if rules.mixed_dirs.matches(name):
        result.candidates.append(
            Candidate(
                rel,
                "dir",
                "mixed",
                "state and data of many apps, including credential stores (keyrings): not descended",
                None,
                _count_entries(path, 10_000),
            )
        )
        return
    if rules.cache_dirs.matches(name):
        result.candidates.append(
            Candidate(
                rel, "dir", "cache-state", "cache, runtime or tool state by name", None, _count_entries(path, 10_000)
            )
        )
        return
    count = _count_entries(path, rules.large_dir_entries)
    if count >= rules.large_dir_entries or depth > rules.max_depth:
        why = f"application data directory ({count}+ files): state, caches and config interleaved"
        if count < rules.large_dir_entries:
            why = f"deeper than {rules.max_depth} levels"
        result.candidates.append(Candidate(rel, "dir", "mixed", why, None, count))
        return
    try:
        children = sorted(path.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_symlink():
            result.symlinks_ignored += 1
        elif child.is_dir():
            _list_dir(home, child, depth + 1, rules, skip, result)
        elif child.relative_to(home).as_posix() not in skip:
            _list_file(home, child, rules, result)


def _list_file(home: Path, path: Path, rules: FileRules, result: FilesResult) -> None:
    rel = path.relative_to(home).as_posix()
    try:
        info = path.stat()
        size = info.st_size
        if not stat.S_ISREG(info.st_mode):  # a socket, pipe or device: never opened (a pipe would block)
            result.candidates.append(Candidate(rel, "file", "cache-state", "socket, pipe or device", size, None))
            return
        data = (
            path.read_bytes()
            if 0 < size <= rules.max_text_bytes and not rules.secret_names.matches(path.name)
            else None
        )
    except OSError:
        result.candidates.append(Candidate(rel, "file", "cache-state", "unreadable", None, None))
        return
    cls, reason = classify_file(path.name, size, data, rules)
    result.candidates.append(Candidate(rel, "file", cls, reason, size, None))


def scan_entry(home: Path, path: Path, rules: FileRules) -> FilesResult:
    """The classified candidates of ONE entry (a file, or a directory walked like ``scan_files`` walks it): what the
    coverage ``files`` adapter asks about a top-level item. A secret-named file is never opened, a non-regular file
    is never opened, and nothing is written."""
    result = FilesResult()
    if path.is_dir():
        _list_dir(home, path, 1, rules, frozenset(), result)
    else:
        _list_file(home, path, rules, result)
    return result


def scan_files(home: Path, profile: Profile, rules: Rules) -> FilesResult:
    file_rules = rules.files
    _checked_patterns(file_rules.include)
    result = FilesResult()
    adopted_rels = _expand(home, file_rules.include)
    for rel in adopted_rels:
        _adopt(home, profile, file_rules, rel, result)
    skip = frozenset(adopted_rels)
    roots: list[Path] = []
    config = home / ".config"
    if config.is_dir():
        roots += sorted(config.iterdir())
    if home.is_dir():
        profile_root = profile.root.resolve()
        roots += sorted(
            p
            for p in home.iterdir()
            if p.name.startswith(".")
            and p.name != ".config"
            and p.resolve() != profile_root
            and p.resolve() not in profile_root.parents  # the checkout holding the profile (~/.files)
        )
    for path in roots:
        rel = path.relative_to(home).as_posix()
        if path.is_symlink():
            result.symlinks_ignored += 1
        elif path.is_dir():
            _list_dir(home, path, 1, file_rules, skip, result)
        elif rel not in skip:
            _list_file(home, path, file_rules, result)
    return result

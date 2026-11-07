"""Move a report out of the crash directory into the private archive, gzipped.

Accessed via: ``apport_source`` (``.crash`` reports), ``spool`` (event files) and ``cli archive list``.

Moving the file out of ``/var/crash`` is what makes apport unable to show it and ``whoopsie`` unable to upload it
(the ``.upload`` / ``.uploaded`` stamps next to it are removed too). The order protects the report:

1. **claim** it BEFORE anything reads it: rename it into the user's private directory ``.ei-claims-<uid>`` of its own
   directory (same file system; ``/var/crash`` is shared and sticky, so there is one directory per user, created 0700
   and refused when it is a symlink, someone else's or open to others). Apport may write a NEW report under the old
   name meanwhile; the claimed inode is the one that is parsed, published, archived and removed, so a replacement
   report is never archived unpublished;
2. gzip the claimed file to a short temporary name in the archive, ``fsync`` it, link it to its final name (never
   replacing an earlier archive: a second report with the same name and second gets ``.1``, ``.2``, ...) and
   ``fsync`` the archive directory (the crash directory may be on another file system: a power cut must not keep
   the deletion and lose the archive entry);
3. only then unlink the claimed file.

Any failure before step 3 puts the report back (``release``); when apport already created a new report under the name,
the old one is put back under ``<stem>.r<N>.crash`` (the name is cut so that it stays within 255 bytes), so BOTH reports
are ingested. A process that died between the steps leaves its claim in the claim directory; ``recover_claims``
restores such files at the next scan the same way. A second claim of a name already in the claim directory takes
the next free name there.

Public API:

- ``claim(source, uid) -> Path``, ``release(claimed, source) -> Path``, ``claim_dir(directory, uid)`` (validated;
  ``ClaimDirError`` when it is not the user's private directory), ``CLAIM_PREFIX``;
- ``store(file, archive_dir, mtime, target=None) -> Path``: gzip into the archive and remove ``file`` (no claim: for a
  private directory such as the spool, or a file already claimed);
  ``free_archive_path(archive_dir, source, mtime)`` (the name the next archive will use: bounded to 240 bytes);
- ``recover_claims(directory, uid) -> list[Path]``: the restored reports;
- ``remove_upload_stamps(report) -> (removed, failed)``: delete ``<report>.upload`` and ``.uploaded`` beside a ``.crash``;
- ``list_archive(archive_dir) -> list[Path]``, newest first.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import stat
import time
from pathlib import Path

from error_intercept.crash_report import open_noatime
from error_intercept.durable import fsync_dir, short_temporary

STAMP_SUFFIXES = (".upload", ".uploaded")
_SERIAL_NAME = re.compile(r"\A[^.].*\.r\d+\.crash\Z", re.S)  # what ``_free_name`` gives a report beside a newer one
COMPRESS_LEVEL = 3  # a crash report is mostly an already-compressed core dump: speed over ratio
CLAIM_PREFIX = ".ei-claims-"  # + the uid: one claim directory per user
ARCHIVE_NAME_BYTES = 240  # an archive file name stays under the file system limit, leaving room for a serial
FS_NAME_BYTES = 255  # the file system limit of one name
KEPT_STEM_BYTES = 180


def archive_name(source: Path, mtime: float, serial: int = 0) -> str:
    tail = f".{time.strftime('%Y%m%dT%H%M%S', time.gmtime(mtime))}{f'.{serial}' if serial else ''}.gz"
    raw = os.fsencode(source.name)
    if len(raw) + len(tail) <= ARCHIVE_NAME_BYTES:
        return source.name + tail
    digest = hashlib.sha1(raw).hexdigest()[:10]
    return f"{os.fsdecode(raw[:KEPT_STEM_BYTES])}~{digest}{tail}"


def free_archive_path(archive_dir: Path, source: Path, mtime: float) -> Path:
    serial = 0
    while os.path.lexists(archive_dir / (name := archive_name(source, mtime, serial))):
        serial += 1
    return archive_dir / name


class ClaimDirError(OSError):
    """The claim directory is not the private directory of the user that it must be (a symlink, another owner, open to others)."""


def claim_dir(directory: Path, uid: int, *, create: bool = True) -> Path:
    """The claim directory of ``uid`` inside the crash directory, created 0700 and checked on every use.

    One directory per user: ``/var/crash`` is shared and sticky, so another user may create (or pre-create) a name
    there. A directory that is a symlink, belongs to someone else or is open to group/others is refused, never used.
    """
    claims = directory / f"{CLAIM_PREFIX}{uid}"
    if create:
        try:
            os.mkdir(claims, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(claims)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ClaimDirError(f"{claims} is not a private directory of uid {uid}: refusing to use it")
    return claims


def claim(source: Path, uid: int) -> Path:
    """Move a report into the claim directory (a free name there); from now on this inode is the report."""
    claims = claim_dir(source.parent, uid)
    claimed = _free_name(claims, source.name)
    os.rename(source, claimed)
    return claimed


def _free_name(directory: Path, name: str) -> Path:
    """``name`` when free, else ``<stem>.r<N>.crash`` (bounded): a report is never overwritten by its older generation."""
    path = directory / name
    stem = name[: -len(".crash")] if name.endswith(".crash") else name
    serial = 0
    while os.path.lexists(path):
        serial += 1
        tail = f".r{serial}.crash"
        kept = os.fsencode(stem)[: FS_NAME_BYTES - len(tail)]
        path = directory / (os.fsdecode(kept) + tail)
    return path


def _other_link(claimed: Path, source: Path) -> Path | None:
    """The entry beside ``source`` that is the same file as ``claimed`` and has a name ``release`` could have given it."""
    info = os.lstat(claimed)
    with os.scandir(source.parent) as entries:
        for entry in entries:
            if entry.name != source.name and not _SERIAL_NAME.search(entry.name):
                continue
            if entry.inode() == info.st_ino and entry.stat(follow_symlinks=False).st_dev == info.st_dev:
                return Path(entry.path)
    return None


def release(claimed: Path, source: Path) -> Path:
    """Undo ``claim``; returns where the report is now (``source``, or an ``.rN`` name when a new report took it)."""
    if os.lstat(claimed).st_nlink > 1:  # a release that died between link and unlink: the report is already back
        back = _other_link(claimed, source)
        if back is not None:
            claimed.unlink()
            return back
    # link, never rename: rename replaces a report apport created under the free name a moment ago
    while True:
        target = _free_name(source.parent, source.name)
        try:
            os.link(claimed, target)
            break
        except FileExistsError:
            continue  # the name was taken after the check: the next pass finds the next free serial
    claimed.unlink()
    return target


def _compress(claimed: Path, partial: Path) -> None:
    with open_noatime(claimed) as reading:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=COMPRESS_LEVEL) as writing:
                shutil.copyfileobj(reading, writing, 1 << 20)
            raw.flush()
            os.fsync(raw.fileno())


def _publish_archive(partial: Path, target: Path) -> Path:
    """Give the finished temporary file its name without ever replacing an existing archive."""
    serial = 0
    final = target
    while True:
        try:
            os.link(partial, final)
            break
        except FileExistsError:
            serial += 1
            final = target.with_name(target.name[: -len(".gz")] + f".{serial}.gz")
    partial.unlink()
    return final


def store(file: Path, archive_dir: Path, mtime: float, target: Path | None = None) -> Path:
    """Gzip ``file`` into the archive, make that durable, and only then remove ``file``. A failure leaves ``file`` as it was."""
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = target or free_archive_path(archive_dir, file, mtime)
    partial = short_temporary(target)
    try:
        _compress(file, partial)
        final = _publish_archive(partial, target)
        fsync_dir(archive_dir)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    file.unlink()
    return final


def recover_claims(directory: Path, uid: int) -> list[Path]:
    """Restore the reports of ``uid`` that a crashed run had claimed (under an ``.rN`` name when the name is taken)."""
    try:
        claims = claim_dir(directory, uid, create=False)
        entries = list(os.scandir(claims))
    except OSError:
        return []
    restored: list[Path] = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or entry.stat(follow_symlinks=False).st_uid != uid:
            continue
        restored.append(release(Path(entry.path), directory / entry.name))
    return restored


def remove_upload_stamps(report: Path) -> tuple[list[Path], list[tuple[Path, OSError]]]:
    """Delete the stamps beside ``report``; returns (removed, failed): whoopsie owns ``.uploaded`` in a sticky directory."""
    removed: list[Path] = []
    failed: list[tuple[Path, OSError]] = []
    stem = report.with_suffix("")
    for suffix in STAMP_SUFFIXES:
        stamp = stem.with_name(stem.name + suffix)
        try:
            stamp.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            failed.append((stamp, error))
            continue
        removed.append(stamp)
    return removed, failed


def list_archive(archive_dir: Path) -> list[Path]:
    try:
        files = [path for path in archive_dir.rglob("*.gz") if path.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)

"""Move a report out of the crash directory into the private archive, gzipped.

Accessed via: ``apport_source`` (``.crash`` reports), ``spool`` (event files) and ``cli archive list``.

Moving the file out of ``/var/crash`` is what makes apport unable to show it and ``whoopsie`` unable to upload it
(the ``.upload`` / ``.uploaded`` stamps next to it are removed too). The order protects the report:

1. **claim** it: rename it into the private subdirectory ``.ei-claims`` of its own directory (same file system, same
   name, so no name-length limit is touched; the sticky bit lets the owner create it). Apport may write a NEW
   report under the old name while we compress; only the claimed inode is ever deleted;
2. gzip the claimed file to a short temporary name in the archive, ``fsync`` it, link it to its final name (never
   replacing an earlier archive: a second report with the same name and second gets ``.1``, ``.2``, ...) and
   ``fsync`` the archive directory (the crash directory may be on another file system: a power cut must not keep
   the deletion and lose the archive entry);
3. only then unlink the claimed file.

Any failure before step 3 puts the report back (``release``); when apport already created a new report under the name,
the old one is put back under ``<stem>.r<N>.crash``, so BOTH reports are ingested. A process that died between the
steps leaves its claim in ``.ei-claims``; ``recover_claims`` restores such files at the next scan the same way.

Public API:

- ``archive_file(source, archive_dir, mtime, target=None, *, use_claim=True) -> Path`` and ``free_archive_path(archive_dir, source, mtime)``
  (the name the next ``archive_file`` will use: bounded to 240 bytes whatever the report's name);
- ``recover_claims(directory, uid) -> list[Path]``: the restored reports;
- ``remove_upload_stamps(report) -> list[Path]``: delete ``<report>.upload`` and ``.uploaded`` beside a ``.crash``;
- ``list_archive(archive_dir) -> list[Path]``, newest first; ``CLAIM_DIR``.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import time
from pathlib import Path

from error_intercept.crash_report import open_noatime
from error_intercept.durable import fsync_dir, short_temporary

STAMP_SUFFIXES = (".upload", ".uploaded")
COMPRESS_LEVEL = 3  # a crash report is mostly an already-compressed core dump: speed over ratio
CLAIM_DIR = ".ei-claims"
NAME_LIMIT = 240  # bytes of an archive file name (the file system allows 255)
KEPT_STEM_BYTES = 180


def archive_name(source: Path, mtime: float, serial: int = 0) -> str:
    tail = f".{time.strftime('%Y%m%dT%H%M%S', time.gmtime(mtime))}{f'.{serial}' if serial else ''}.gz"
    raw = os.fsencode(source.name)
    if len(raw) + len(tail) <= NAME_LIMIT:
        return source.name + tail
    digest = hashlib.sha1(raw).hexdigest()[:10]
    return f"{os.fsdecode(raw[:KEPT_STEM_BYTES])}~{digest}{tail}"


def free_archive_path(archive_dir: Path, source: Path, mtime: float) -> Path:
    serial = 0
    while os.path.lexists(archive_dir / (name := archive_name(source, mtime, serial))):
        serial += 1
    return archive_dir / name


def _claim_dir(directory: Path) -> Path:
    claims = directory / CLAIM_DIR
    claims.mkdir(mode=0o700, exist_ok=True)
    return claims


def claim(source: Path) -> Path:
    claimed = _claim_dir(source.parent) / source.name
    if os.path.lexists(claimed):
        raise FileExistsError(f"{claimed} is the claim of an earlier run: recover_claims puts it back first")
    os.rename(source, claimed)
    return claimed


def _restore_name(directory: Path, name: str) -> Path:
    """``name`` when free, else ``<stem>.r<N>.crash``: a report is never overwritten by its older generation."""
    path = directory / name
    stem = name[: -len(".crash")] if name.endswith(".crash") else name
    serial = 0
    while os.path.lexists(path):
        serial += 1
        path = directory / f"{stem}.r{serial}.crash"
    return path


def release(claimed: Path, source: Path) -> Path:
    """Undo ``claim``; returns where the report is now (``source``, or an ``.rN`` name when a new report took it)."""
    target = _restore_name(source.parent, source.name)
    os.rename(claimed, target)
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


def archive_file(
    source: Path, archive_dir: Path, mtime: float, target: Path | None = None, *, use_claim: bool = True
) -> Path:
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = target or free_archive_path(archive_dir, source, mtime)
    partial = short_temporary(target)
    claimed = claim(source) if use_claim else source  # a private directory (the spool) has no apport to race with
    try:
        _compress(claimed, partial)
        final = _publish_archive(partial, target)
        fsync_dir(archive_dir)
    except BaseException:
        partial.unlink(missing_ok=True)
        if use_claim:
            release(claimed, source)
        raise
    claimed.unlink()
    return final


def recover_claims(directory: Path, uid: int) -> list[Path]:
    """Restore the reports of ``uid`` that a crashed run had claimed (under an ``.rN`` name when the name is taken)."""
    claims = directory / CLAIM_DIR
    try:
        entries = list(os.scandir(claims))
    except OSError:
        return []
    restored: list[Path] = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or entry.stat(follow_symlinks=False).st_uid != uid:
            continue
        restored.append(release(Path(entry.path), directory / entry.name))
    return restored


def remove_upload_stamps(report: Path) -> list[Path]:
    removed: list[Path] = []
    stem = report.with_suffix("")
    for suffix in STAMP_SUFFIXES:
        stamp = stem.with_name(stem.name + suffix)
        try:
            stamp.unlink()
        except FileNotFoundError:
            continue
        removed.append(stamp)
    return removed


def list_archive(archive_dir: Path) -> list[Path]:
    try:
        files = [path for path in archive_dir.rglob("*.gz") if path.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)

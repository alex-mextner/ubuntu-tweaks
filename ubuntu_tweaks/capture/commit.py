"""`capture --commit`: write the plan into the profile working tree. Never runs git.

Accessed via: ``commands/capture.py`` only, after the preview logic has built the plan.

Order and guarantees:

1. **Everything is checked before anything is written.** The serialized ``settings/*.ini``,
   ``packages.toml`` and ``system.toml`` texts and every adopted file are scanned for secrets; one
   finding raises ``RefusedError`` (exit 5) listing where and what kind, never the value. A path that
   would leave ``$HOME`` or the profile, a destination that is a symlink or a directory, and an
   adopted file that changed since the preview (its bytes differ from the scanned ones) are refused
   too, so what is written is exactly what was scanned and shown.
2. Settings: ``settings/<panel>.ini`` / ``machines/<machine>/settings/<panel>.ini`` get the captured
   keys merged into the existing file (keys the live system no longer has stay); written atomically.
3. Files: the scanned bytes are written into ``files/`` (or the machine overlay), the original is
   backed up to ``~/.local/state/ubuntu-tweaks/backup/<UTC ts>/<abs path>``, then replaced
   atomically by a symlink to the profile copy. Each step is atomic per file; a failure stops at
   that file, files adopted before it stay adopted and a rerun finishes the rest.
4. ``packages.toml`` / ``system.toml`` are updated for ``+``/``~`` items (the file is regenerated
   from its parsed content: comments are not preserved, ticket #29).

``_backup`` is a local copy of what slice B's ``backup.py`` provides; integration dedupes it.

Public API: ``commit(plan, *, email_exempt_keys, home, now) -> list[str]`` (human-readable lines
for what was written).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ubuntu_tweaks.capture.plan import CapturePlan, refusals
from ubuntu_tweaks.capture.secrets import scan_text
from ubuntu_tweaks.capture.settings_scan import KeyEntry
from ubuntu_tweaks.capture.toml_io import dump_toml, read_toml
from ubuntu_tweaks.errors import RefusedError
from ubuntu_tweaks.settings import KeyFileData, dump_keyfile, path_to_section, read_keyfile


def _backup(path: Path, *, home: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    target = home / ".local/state/ubuntu-tweaks/backup" / stamp / path.absolute().as_posix().lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def _atomic_write(path: Path, data: bytes, mode_from: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        if mode_from is not None:
            shutil.copymode(mode_from, temp_name)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _inside(path: Path, root: Path) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def _merged_ini(existing: KeyFileData, entries: list[KeyEntry]) -> KeyFileData:
    merged = {section: dict(keys) for section, keys in existing.items()}
    for entry in entries:
        merged.setdefault(path_to_section(entry.directory), {})[entry.key] = entry.value
    return merged


def _planned_writes(plan: CapturePlan) -> list[tuple[Path, str]]:
    profile = plan.profile
    writes: list[tuple[Path, str]] = []

    if plan.settings is not None:
        groups: dict[tuple[str, str], list[KeyEntry]] = {}
        for entry in plan.settings.entries:
            if entry.mark != "=":
                groups.setdefault((entry.target, entry.panel), []).append(entry)
        for (target, panel), entries in sorted(groups.items()):
            directory = profile.settings_dir if target == "shared" else profile.overlay_settings_dir
            path = directory / f"{panel}.ini"
            existing = read_keyfile(path) if path.is_file() else {}
            writes.append((path, dump_keyfile(_merged_ini(existing, entries))))

    if plan.packages is not None:
        new = [item for item in plan.packages if item.mark == "+"]
        if new:
            data = read_toml(profile.packages_toml)
            for manager in ("apt", "snap", "flatpak"):
                names = [item.name for item in new if item.manager == manager]
                if names:
                    current = data.get(manager, [])
                    data[manager] = sorted({*(current if isinstance(current, list) else []), *names})
            writes.append((profile.packages_toml, dump_toml(data)))

    if plan.system is not None:
        for machine_level, path in ((False, profile.system_toml), (True, profile.overlay_system_toml)):
            items = [i for i in plan.system if i.machine == machine_level and i.mark != "="]
            if not items:
                continue
            data = read_toml(path)
            for item in items:
                if "." in item.key:
                    table_name, name = item.key.split(".", 1)
                    section = data.setdefault(table_name, {})
                    if isinstance(section, dict):
                        section[name] = item.value
                else:
                    data[item.key] = item.value
            writes.append((path, dump_toml(data)))
    return writes


def commit(plan: CapturePlan, *, email_exempt_keys: Sequence[str], home: Path, now: datetime) -> list[str]:
    profile = plan.profile
    writes = _planned_writes(plan)

    findings = [
        (str(path), f.kind, f.cls, f.line)
        for path, text in writes
        for f in scan_text(text, exempt_email_keys=email_exempt_keys)
    ]
    findings += refusals(plan)
    if findings:
        detail = "\n".join(f"  {where}: {kind} ({cls}) line {line}" for where, kind, cls, line in findings)
        raise RefusedError(f"refused: secret finding; nothing was written\n{detail}")

    adopted = [a for a in (plan.files.adopted if plan.files else []) if a.status != "linked"]
    for a in adopted:
        if not _inside(a.source.parent, home):
            raise RefusedError(f"refused: unsafe path {a.source}: its directory resolves outside {home}")
        if not _inside(a.dest.parent, profile.root) or a.dest.is_symlink() or a.dest.is_dir():
            raise RefusedError(
                f"refused: unsafe destination {a.dest}: outside {profile.root}, a symlink or a directory"
            )
        if a.source.is_symlink() or not a.source.is_file() or a.source.read_bytes() != a.content:
            raise RefusedError(f"refused: {a.source} changed since the preview; run capture again")
        if a.status == "identical" and (not a.dest.is_file() or a.dest.read_bytes() != a.content):
            raise RefusedError(f"refused: {a.dest} changed since the preview; run capture again")
    for path, _text in writes:
        if not _inside(path.parent, profile.root) or path.is_symlink() or path.is_dir():
            raise RefusedError(f"refused: unsafe destination {path}: outside {profile.root}, a symlink or a directory")

    done: list[str] = []
    for path, text in writes:
        _atomic_write(path, text.encode("utf-8"), mode_from=path if path.exists() else None)
        done.append(f"wrote {path}")
    for a in adopted:
        if a.status != "identical":
            _atomic_write(a.dest, a.content, mode_from=a.source)
        backup = _backup(a.source, home=home, now=now)
        link = a.source.with_name(f".{a.source.name}.ubuntu-tweak-link")
        link.unlink(missing_ok=True)
        os.symlink(os.path.abspath(a.dest), link)
        os.replace(link, a.source)
        done.append(f"adopted {a.source} -> {a.dest} (backup {backup})")
    return done

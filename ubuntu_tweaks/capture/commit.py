"""`capture --commit`: write the plan into the profile working tree. Never runs git.

Accessed via: ``commands/capture.py`` only, after the preview logic has built the plan.

Order and guarantees:

1. **Everything is checked before anything is written.** The serialized ``settings/*.ini``,
   ``packages.toml`` and ``system.toml`` texts and every adopted file are scanned for secrets; one
   finding raises ``RefusedError`` (exit 5) listing where and what kind, never the value. A path that
   would leave ``$HOME`` or the profile, a destination that is a symlink or a directory, and an
   adopted file that changed since the preview (its bytes differ from the scanned ones) are refused
   too, so what is written is exactly what was scanned and shown.
2. Settings: each captured key is written into the bundle chosen for it (``settings/<bundle>.ini`` /
   ``machines/<machine>/settings/<bundle>.ini``): a changed value is rewritten in place, a new key is
   appended to its section, and every comment, the key order and the layout of the file stay as they
   are; written atomically.
3. Files: the scanned bytes are written into ``files/`` (or the machine overlay), the original is
   backed up to ``~/.local/state/ubuntu-tweaks/backup/<UTC ts>/<abs path>``, then replaced
   atomically by a symlink to the profile copy. Each step is atomic per file; a failure stops at
   that file, files adopted before it stay adopted and a rerun finishes the rest.
4. ``packages.toml`` / ``system.toml`` are edited in place (``toml_edit``): captured names are
   appended to their array, captured values replace the old ones, comments and order stay; the
   edited text is re-parsed and compared with the intended data before anything is written.

Public API: ``commit(plan, *, email_exempt_keys, home, now, state_home=None) -> list[str]``
(human-readable lines for what was written; ``state_home`` defaults to ``<home>/.local/state``).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from ubuntu_tweaks.atomic_write import atomic_write
from ubuntu_tweaks.backup import backup as backup_file
from ubuntu_tweaks.capture.plan import CapturePlan, refusals
from ubuntu_tweaks.capture.secrets import Finding, scan_text
from ubuntu_tweaks.capture.settings_scan import KeyEntry
from ubuntu_tweaks.capture.system_scan import SystemItem
from ubuntu_tweaks.errors import RefusedError, UbuntuTweakError
from ubuntu_tweaks.keyfile_doc import KeyfileDoc
from ubuntu_tweaks.profile import TomlTable, read_toml
from ubuntu_tweaks.settings import path_to_section
from ubuntu_tweaks.toml_edit import TomlDoc


def _inside(path: Path, root: Path) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def _bundle_text(path: Path, entries: list[KeyEntry]) -> str:
    """The bundle with the captured keys applied in place: a changed value is rewritten where it stands
    (comments, order and layout untouched), a new key is appended to its section."""
    doc = KeyfileDoc.parse(path.read_text(encoding="utf-8") if path.is_file() else "", str(path))
    for entry in sorted(entries, key=lambda e: (e.directory, e.key)):
        section = path_to_section(entry.directory)
        if doc.find(section, entry.key) is None:
            doc.add(section, entry.key, entry.value)
        else:
            doc.set_value(section, entry.key, entry.value)
    return doc.text()


def _toml_text(path: Path, edit: Callable[[TomlDoc], None], expected: TomlTable) -> str:
    """``path`` edited in place by ``edit``; the result is re-parsed and must equal ``expected``, so a
    scanner mistake fails here instead of in the profile."""
    doc = TomlDoc.parse(path.read_text(encoding="utf-8") if path.is_file() else "", str(path))
    edit(doc)
    try:
        parsed = tomllib.loads(doc.text())
    except tomllib.TOMLDecodeError as error:
        raise UbuntuTweakError(f"internal error: the edited {path} is not valid TOML ({error})") from error
    if parsed != expected:
        raise UbuntuTweakError(f"internal error: the edit of {path} does not give the intended content")
    return doc.text()


def _planned_writes(plan: CapturePlan) -> list[tuple[Path, str]]:
    profile = plan.profile
    writes: list[tuple[Path, str]] = []

    if plan.settings is not None:
        groups: dict[tuple[str, str], list[KeyEntry]] = {}
        for entry in plan.settings.entries:
            if entry.mark != "=":
                groups.setdefault((entry.target, entry.bundle), []).append(entry)
        for (target, bundle), entries in sorted(groups.items()):
            directory = profile.settings_dir if target == "shared" else profile.overlay_settings_dir
            path = directory / f"{bundle}.ini"
            writes.append((path, _bundle_text(path, entries)))

    if plan.packages is not None:
        new = [item for item in plan.packages if item.mark == "+"]
        if new:
            data = read_toml(profile.packages_toml)
            added: dict[str, list[str]] = {}
            for manager in ("apt", "snap", "flatpak"):
                names = [item.name for item in new if item.manager == manager]
                if names:
                    added[manager] = names
                    current = data.get(manager, [])
                    data[manager] = [*(current if isinstance(current, list) else []), *names]

            def add_packages(doc: TomlDoc) -> None:
                for manager, names in added.items():
                    doc.append_to_array((), manager, names)

            writes.append((profile.packages_toml, _toml_text(profile.packages_toml, add_packages, data)))

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

            def set_values(doc: TomlDoc, items: list[SystemItem] = items) -> None:
                for item in items:
                    table, _, name = item.key.rpartition(".")
                    doc.set_string(tuple(table.split(".")) if table else (), name, item.value)

            writes.append((path, _toml_text(path, set_values, data)))
    return writes


def _new_findings(path: Path, text: str, email_exempt_keys: Sequence[str]) -> list[Finding]:
    """Secret findings in ``text``, except those on a full-line comment the file already had: a reason the
    user wrote (a commit sha, a name) is theirs and was reviewed in git; values and new text are scanned."""
    try:
        old = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        old = []
    known = {line for line in old if line.lstrip().startswith("#")}
    lines = text.splitlines()
    return [
        finding
        for finding in scan_text(text, exempt_email_keys=email_exempt_keys)
        if not (finding.line <= len(lines) and lines[finding.line - 1] in known)
    ]


def commit(
    plan: CapturePlan,
    *,
    email_exempt_keys: Sequence[str],
    home: Path,
    now: datetime,
    state_home: Path | None = None,
) -> list[str]:
    profile = plan.profile
    writes = _planned_writes(plan)

    findings = [
        (str(path), f.kind, f.cls, f.line)
        for path, text in writes
        for f in _new_findings(path, text, email_exempt_keys)
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
        atomic_write(path, text.encode("utf-8"), mode_from=path if path.exists() else None)
        done.append(f"wrote {path}")
    for a in adopted:
        if a.status != "identical":
            atomic_write(a.dest, a.content, mode_from=a.source)
        backup = backup_file(
            a.source, now=now, state_home=state_home if state_home is not None else home / ".local/state"
        )
        link = a.source.with_name(f".{a.source.name}.ubuntu-tweak-link")
        link.unlink(missing_ok=True)
        os.symlink(os.path.abspath(a.dest), link)
        os.replace(link, a.source)
        done.append(f"adopted {a.source} -> {a.dest} (backup {backup})")
    return done

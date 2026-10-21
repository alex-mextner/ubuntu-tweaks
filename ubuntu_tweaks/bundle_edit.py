"""Edits of the profile's own files that the user would otherwise do by hand: ``bundle-move`` (group keys
into a bundle) and ``note`` (set the reason of a key or package). Pure planning; the commands print
the plan and, with ``--commit``, write it.

Accessed via: ``commands/bundle_move.py`` and ``commands/note.py``.

Assumptions:

- A plan is computed completely before anything is written, from the same loader ``apply`` uses
  (``bundles.load_bundles``), so conflicts and malformed files stop it (``ConfigError``) first.
- ``bundle-move`` moves a key within its origin: a shared key goes to ``settings/<bundle>.ini``, an
  overlay key to ``machines/<machine>/settings/<bundle>.ini`` (created when missing). A move must not
  change which machines see the key: it is refused when the source and an existing destination differ in
  ``machines`` / ``requires-module``, or when keys from differently filtered bundles would share a new
  destination; a new destination inherits the source's filters. Only bundles that apply on this machine
  take part. The key's comment block moves with it; a section that empties takes its own comment along
  (nothing is lost, also when the destination already holds the key); a key the destination already
  holds with the same value is merged (the source copy goes).
- ``note`` addresses ``<bundle>:<dconf key path>``, ``packages:<apt|snap|flatpak>:<name>`` or
  ``system:<key>``; an empty text removes the reason. The key must exist; it is never created.

Public API: ``Edit(path, text)``, ``EditPlan(lines, writes)``, ``plan_move(profile, destination,
patterns, module_enabled)``, ``plan_note(profile, spec, text, origin)``, ``write_edits(plan, profile)``.
"""

from __future__ import annotations

import re
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.atomic_write import atomic_write
from ubuntu_tweaks.bundles import (
    HEADER_SECTION,
    Located,
    ModuleGate,
    key_path,
    load_bundles,
    validate_bundle_name,
)
from ubuntu_tweaks.capture.globs import compile_glob
from ubuntu_tweaks.errors import ConfigError, RefusedError
from ubuntu_tweaks.keyfile_doc import KeyfileDoc, comment_text, normalize_section
from ubuntu_tweaks.profile import Origin, Profile
from ubuntu_tweaks.toml_edit import TomlDoc

_PACKAGES_SPEC = re.compile(r"packages:(apt|snap|flatpak):(.+)")
_SYSTEM_SPEC = re.compile(r"system:([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?)")


@dataclass(frozen=True)
class Edit:
    path: Path
    text: str


@dataclass(frozen=True)
class EditPlan:
    lines: tuple[str, ...]
    writes: tuple[Edit, ...]


def _rel(profile: Profile, path: Path) -> str:
    return path.relative_to(profile.root).as_posix()


def _glob(pattern: str) -> re.Pattern[str]:
    return compile_glob(pattern if pattern.startswith("/") else f"/{pattern}")


def plan_move(
    profile: Profile, destination: str, patterns: Sequence[str], module_enabled: ModuleGate | None = None
) -> EditPlan:
    validate_bundle_name(destination)
    bundles = load_bundles(profile, module_enabled)
    bundles.require_valid()
    compiled = {pattern: _glob(pattern) for pattern in patterns}
    matched: set[str] = set()
    moves: list[Located] = []
    for bundle in bundles.active():
        for key in bundle.keys:
            hits = [p for p, regex in compiled.items() if regex.fullmatch(key.full)]
            if hits:
                matched.update(hits)
                moves.append(Located(bundle, key))
    missing = [p for p in patterns if p not in matched]
    if missing:
        raise ConfigError(f"no key in the active bundles matches: {', '.join(missing)}")

    originals: dict[Path, str] = {}
    docs: dict[Path, KeyfileDoc] = {}

    def doc_for(path: Path) -> KeyfileDoc:
        if path not in docs:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            originals[path] = text
            docs[path] = KeyfileDoc.parse(text, _rel(profile, path))
        return docs[path]

    lines: list[str] = []
    new_filters: dict[Origin, tuple[tuple[str, ...] | None, str | None]] = {}
    for held in moves:
        bundle, key = held.bundle, held.key
        target_path = profile.bundle_path(destination, bundle.origin)
        if target_path == bundle.path:
            continue
        wanted = (bundle.machines, bundle.requires_module)
        existing = bundles.find(destination, bundle.origin)
        if existing is not None:
            if not existing.active:
                raise ConfigError(
                    f"{existing.source} does not apply on this machine ({existing.inactive}); not moving into it"
                )
            if (existing.machines, existing.requires_module) != wanted:
                raise ConfigError(
                    f"{key.full}: {bundle.source} and {existing.source} differ in machines / requires-module, so the "
                    "move would change where the key applies; align the two headers first"
                )
        elif new_filters.setdefault(bundle.origin, wanted) != wanted:
            raise ConfigError(
                f"{key.full}: the keys come from bundles with different machines / requires-module headers and "
                f"cannot share the new bundle {destination}; move them one header group at a time"
            )
        target, source = doc_for(target_path), doc_for(bundle.path)
        if existing is None and not target.entries():
            for name, value in (("machines", wanted[0]), ("requires-module", wanted[1])):
                if value:
                    target.add(HEADER_SECTION, name, json.dumps(value))  # the new bundle applies where the old one did
        removed = source.remove(key.section, key.key)
        target_entry = target.find(key.section, key.key)
        where = profile.bundle_relpath(destination, bundle.origin)
        reason = f"  # {key.reason}" if key.reason else ""
        if target_entry is None:
            target.add(key.section, key.key, removed.value, removed.comment, removed.section_comment)
            lines.append(f"move {key.full}: {held.where} -> {where}{reason}")
        else:  # load_bundles already refused two different values for one key in one layer: same value here
            if removed.comment and not target.reason(target_entry):
                target.set_comment(key.section, key.key, comment_text(removed.comment))
            if removed.section_comment:
                target.append_section_comment(key.section, removed.section_comment)
            lines.append(f"merge {key.full}: {held.where} dropped, {where} already holds the same value{reason}")
    writes = tuple(Edit(path, doc.text()) for path, doc in docs.items() if doc.text() != originals[path])
    if not writes:
        lines.append(f"nothing to move: every match is already in {destination}")
    return EditPlan(tuple(lines), writes)


def write_edits(plan: EditPlan, profile: Profile) -> list[str]:
    """Write every edit atomically after refusing the unsafe ones (exit 5, nothing written): a symlink or
    directory at the destination, or a directory that resolves outside the profile."""
    root = os.path.realpath(profile.root)
    for edit in plan.writes:
        parent = os.path.realpath(edit.path.parent)
        if parent != root and not parent.startswith(root + os.sep):
            raise RefusedError(f"refused: {edit.path} resolves outside {profile.root}")
        if edit.path.is_symlink() or edit.path.is_dir():
            raise RefusedError(f"refused: {edit.path} is a symlink or a directory")
    done = []
    for edit in plan.writes:
        atomic_write(edit.path, edit.text.encode("utf-8"), mode_from=edit.path if edit.path.exists() else None)
        done.append(f"wrote {edit.path}")
    return done


def _split_key_path(spec: str) -> tuple[str, str]:
    path = spec.lstrip("/")
    section, _, key = path.rpartition("/")
    return normalize_section(section or "/"), key


def _note_bundle(profile: Profile, name: str, key_spec: str, text: str, origin: Origin | None) -> EditPlan:
    if "/" not in key_spec.lstrip("/"):
        raise ConfigError(
            f"{name}:{key_spec}: a bundle key is a dconf key path such as org/gnome/desktop/interface/gtk-theme"
        )
    section, key = _split_key_path(key_spec)
    bundles = load_bundles(profile)
    candidates = [b for b in bundles.bundles if b.name == name and (origin is None or b.origin == origin)]
    if not candidates:
        raise ConfigError(f"no bundle named {name}" + (f" in the {origin} layer" if origin else ""))
    holders = [b for b in candidates if any(k.section == section and k.key == key for k in b.keys)]
    if not holders:
        raise ConfigError(f"{name} does not hold {key_path(section, key)}")
    if len(holders) > 1:
        raise ConfigError(
            f"{name} holds {key_path(section, key)} in both layers: pick one with --origin shared|overlay"
        )
    bundle = holders[0]
    doc = KeyfileDoc.parse(bundle.path.read_text(encoding="utf-8"), bundle.source)
    entry = doc.find(section, key)
    assert entry is not None
    old = comment_text(doc.reason(entry))
    doc.set_comment(section, key, text)
    line = f"{bundle.source}:{entry.number} {key_path(section, key)}: reason {old!r} -> {text!r}"
    return EditPlan((line,), (Edit(bundle.path, doc.text()),))


def _note_toml(
    profile: Profile,
    table: tuple[str, ...],
    key: str,
    item: str | None,
    text: str,
    origin: Origin | None,
    label: str,
    filename: str,
) -> EditPlan:
    layers = [
        (o, path)
        for o, path in (
            ("shared", profile.packages_toml if filename == "packages.toml" else profile.system_toml),
            ("overlay", profile.overlay_packages_toml if filename == "packages.toml" else profile.overlay_system_toml),
        )
        if path.is_file() and (origin is None or o == origin)
    ]
    holders: list[tuple[Path, TomlDoc]] = []
    for _origin, path in layers:
        doc = TomlDoc.parse(path.read_text(encoding="utf-8"), _rel(profile, path))
        present = item in (doc.array(table, key) or ()) if item is not None else doc.has(table, key)
        if present:
            holders.append((path, doc))
    if not holders:
        raise ConfigError(f"{label} is not in {filename}")
    if len(holders) > 1:
        raise ConfigError(f"{label} is in both {filename} files: pick one with --origin shared|overlay")
    path, doc = holders[0]
    if item is not None:
        old = doc.item_reason(table, key, item)
        doc.set_item_comment(table, key, item, text)
    else:
        old = doc.key_reason(table, key)
        doc.set_key_comment(table, key, text)
    return EditPlan((f"{_rel(profile, path)} {label}: reason {old!r} -> {text!r}",), (Edit(path, doc.text()),))


def plan_note(profile: Profile, spec: str, text: str, origin: Origin | None = None) -> EditPlan:
    text = text.strip()
    one_line = " ".join(text.split())  # a TOML trailing comment must stay on its line
    if packages := _PACKAGES_SPEC.fullmatch(spec):
        manager, name = packages.groups()
        return _note_toml(profile, (), manager, name, one_line, origin, f"{manager} {name}", "packages.toml")
    if system := _SYSTEM_SPEC.fullmatch(spec):
        table, _, key = system.group(1).rpartition(".")
        return _note_toml(
            profile,
            tuple(table.split(".")) if table else (),
            key,
            None,
            one_line,
            origin,
            system.group(1),
            "system.toml",
        )
    name, separator, key_spec = spec.partition(":")
    if not separator or not name or not key_spec:
        raise ConfigError(
            f"{spec!r}: expected <bundle>:<dconf key path>, packages:<apt|snap|flatpak>:<name> or system:<key>"
        )
    return _note_bundle(profile, name, key_spec, text, origin)

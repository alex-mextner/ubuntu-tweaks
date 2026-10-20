"""Section 1 of `capture`: sort the user's dconf keys into panel files, skips and the unmapped bucket.

Accessed via: ``plan.build_plan``. Read-only: only ``SettingsBackend`` reads and the
``glib-2.0/settings/keyfile`` override file are touched.

Every key that has a user value is accounted for exactly once:

- value equal to the schema default -> ``at_default`` (counted, never listed);
- otherwise one ``KeyEntry`` (captured), one ``Skipped`` (with its class) or one ``Unmapped``.

Decision order per key: profile ``exclude`` -> built-in secret/identity/hardware excludes -> the
panel rule (``skip`` rules give their own class) -> state (unless an explicit ``machine`` rule
claims the key) -> no schema -> secret scan of the value -> no panel (``unmapped``) -> target
(machine when a machine rule, the panel's disposition or an absolute path in the value says so).

The keyfile override (``~/.config/glib-2.0/settings/keyfile``) can make the effective value differ
from dconf. It is never frozen into the profile; keys it changes are reported as ``Override``s
and captured entries it also touches carry the ``override`` flag.

Public API: ``scan_settings(...) -> SettingsResult`` and the result dataclasses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ubuntu_tweaks.capture.rules import Mark, Rules
from ubuntu_tweaks.capture.secrets import scan_text, scan_value
from ubuntu_tweaks.setkey import ResolvedKey, SchemaIndex
from ubuntu_tweaks.settings import (
    KeyFileData,
    SettingsBackend,
    SettingsError,
    Variant,
    format_value,
    parse_value,
    path_to_section,
    read_keyfile,
    section_to_path,
)

type Target = Literal["shared", "machine"]
type ExistingReader = Callable[[Target, str], KeyFileData]


@dataclass(frozen=True)
class KeyEntry:
    panel: str
    target: Target
    directory: str
    key: str
    value: str
    mark: Mark
    previous: str | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Skipped:
    cls: str
    full: str
    reason: str
    panel: str | None


@dataclass(frozen=True)
class Unmapped:
    full: str
    schema: str


@dataclass(frozen=True)
class Override:
    full: str
    panel: str | None
    override_value: str
    dconf_value: str
    active: bool
    masked: bool


@dataclass
class SettingsResult:
    entries: list[KeyEntry] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)
    at_default: int = 0
    user_keys: int = 0


def _strings(value: Variant) -> Iterator[str]:
    type_string = value.get_type_string()
    if type_string in ("s", "o", "g"):
        yield value.get_string()
    elif type_string == "v":
        yield from _strings(value.get_variant())
    elif value.is_container():
        for index in range(value.n_children()):
            yield from _strings(value.get_child_value(index))


# First path segments that mean "a file on this machine" (a dconf object path such as
# /org/gnome/.../custom0/ must not match).
_FILESYSTEM_ROOTS = frozenset(
    {
        "home",
        "mnt",
        "media",
        "opt",
        "tmp",
        "var",
        "srv",
        "root",
        "run",
    }
)


def has_machine_path(value: Variant) -> bool:
    """True when a string in ``value`` is an absolute filesystem path (or ``file:///`` URI) outside ``/usr/share``."""
    for text in _strings(value):
        path = text.removeprefix("file://") if text.startswith("file:///") else text
        if not path.startswith("/") or path.startswith("/usr/share/"):
            continue
        if path[1:].split("/", 1)[0] in _FILESYSTEM_ROOTS:
            return True
    return False


def _panel_of(rules: Rules, full: str) -> str | None:
    rule = rules.panel_rule(full)
    return rule.panel if rule is not None else None


def _skip(result: SettingsResult, cls: str, full: str, reason: str, panel: str | None) -> None:
    result.skipped.append(Skipped(cls, full, reason, panel))


def _diff(
    existing: ExistingReader,
    target: Target,
    panel: str,
    directory: str,
    key: str,
    value: Variant,
    resolved: ResolvedKey,
    backend: SettingsBackend,
) -> tuple[Mark, str | None]:
    text = existing(target, panel).get(path_to_section(directory), {}).get(key)
    if text is None:
        return "+", None
    try:
        same = parse_value(backend, resolved.schema, key, text).equal(value)
    except SettingsError:
        same = False
    return ("=", None) if same else ("~", text)


def _resolve(index: SchemaIndex, directory: str, key: str) -> ResolvedKey | None:
    resolved = index.resolve(directory, key)
    return resolved if isinstance(resolved, ResolvedKey) else None


def scan_settings(
    backend: SettingsBackend,
    index: SchemaIndex,
    rules: Rules,
    existing: ExistingReader,
    override_file: Path,
    only: frozenset[str] = frozenset(),
) -> SettingsResult:
    result = SettingsResult()
    for user_key in backend.list_user_keys():
        full, directory, key = user_key.full, user_key.path, user_key.key
        panel_rule = rules.panel_rule(full)
        panel = panel_rule.panel if panel_rule is not None else None
        if only and panel not in only:
            continue  # out of scope: not counted anywhere, so the totals stay an exact account
        result.user_keys += 1
        resolved = _resolve(index, directory, key)
        value: Variant | None = None
        if resolved is not None:
            value = backend.user_value(resolved.schema, resolved.path, key)
            if value is None or value.equal(backend.default(resolved.schema, resolved.path, key)):
                result.at_default += 1
                continue
        if rules.profile_exclude.matches(full):
            _skip(result, "excluded-by-rule", full, "profile [capture] exclude", panel)
            continue
        hard = rules.hard_exclude(full)
        if hard is not None:
            _skip(result, hard.cls, full, hard.reason, panel)
            continue
        if panel_rule is not None and panel_rule.to == "skip":
            _skip(result, panel_rule.cls or "excluded-by-rule", full, panel_rule.reason, panel)
            continue
        machine_reason = rules.machine_rule(full)
        state = None if machine_reason else rules.state_rule(full)
        if state is not None:
            _skip(result, "state", full, state.reason, panel)
            continue
        if resolved is None or value is None:
            _skip(result, "no-schema", full, "no installed schema describes this key", panel)
            continue
        text = format_value(value)
        findings = scan_value(text)
        if findings:
            kinds = ", ".join(sorted({finding.kind for finding in findings}))
            cls = "identity" if all(finding.cls == "identity" for finding in findings) else "secret"
            _skip(result, cls, full, f"value matches: {kinds} (masked)", panel)
            continue
        if panel is None or panel_rule is None:
            result.unmapped.append(Unmapped(full, resolved.schema))
            continue
        flags: list[str] = []
        target: Target = "machine" if machine_reason or panel_rule.to == "machine" else "shared"
        if has_machine_path(value):
            target = "machine"
            flags.append("path")
        if target == "shared" and key in existing("machine", panel).get(path_to_section(directory), {}):
            target = "machine"  # the overlay wins at apply: a shared write would never take effect
        mark, previous = _diff(existing, target, panel, directory, key, value, resolved, backend)
        result.entries.append(KeyEntry(panel, target, directory, key, text, mark, previous, tuple(flags)))
    _scan_overrides(result, backend, index, rules, override_file, only)
    return result


def _override_masked(rules: Rules, full: str, key: str, override_value: Variant, reference: Variant) -> bool:
    """Values of keys the capture rules never show are masked in the override table too."""
    if rules.profile_exclude.matches(full) or rules.hard_exclude(full) is not None:
        return True
    panel_rule = rules.panel_rule(full)
    if panel_rule is not None and panel_rule.to == "skip" and panel_rule.cls in ("secret", "identity", "hardware"):
        return True
    text = f"{key} = {format_value(override_value)}\n{key} = {format_value(reference)}"
    return bool(scan_text(text, exempt_email_keys=rules.email_exempt_keys))


def _scan_overrides(
    result: SettingsResult,
    backend: SettingsBackend,
    index: SchemaIndex,
    rules: Rules,
    override_file: Path,
    only: frozenset[str],
) -> None:
    if not override_file.is_file():
        return
    data: Mapping[str, Mapping[str, str]] = read_keyfile(override_file)
    flagged: set[str] = set()
    for section, items in sorted(data.items()):
        directory = section_to_path(section)
        for key, text in sorted(items.items()):
            resolved = _resolve(index, directory, key)
            if resolved is None:
                continue
            try:
                override_value = parse_value(backend, resolved.schema, key, text)
            except SettingsError:
                continue
            user = backend.user_value(resolved.schema, resolved.path, key)
            reference = user if user is not None else backend.default(resolved.schema, resolved.path, key)
            if override_value.equal(reference):
                continue
            full = directory + key
            panel = _panel_of(rules, full)
            if only and panel not in only:
                continue
            flagged.add(full)
            effective = backend.effective(resolved.schema, resolved.path, key)
            result.overrides.append(
                Override(
                    full,
                    panel,
                    format_value(override_value),
                    format_value(reference),
                    effective.equal(override_value),
                    _override_masked(rules, full, key, override_value, reference),
                )
            )
    result.entries[:] = [
        KeyEntry(e.panel, e.target, e.directory, e.key, e.value, e.mark, e.previous, (*e.flags, "override"))
        if e.directory + e.key in flagged
        else e
        for e in result.entries
    ]

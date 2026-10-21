"""Section 1 of `capture`: sort the user's dconf keys into bundles, skips and the unmapped bucket.

Accessed via: ``plan.build_plan``. Read-only: only ``SettingsBackend`` reads and the
``glib-2.0/settings/keyfile`` override file are touched.

Every key that has a user value is accounted for exactly once:

- value equal to the schema default -> ``at_default`` (counted, never listed);
- otherwise one ``KeyEntry`` (captured), one ``Skipped`` (with its class) or one ``Unmapped``.

Decision order per key: profile ``exclude`` -> built-in secret/identity/hardware excludes -> the
panel rule (``skip`` rules give their own class) -> state (unless an explicit ``machine`` rule or the
profile's ``include`` claims the key) -> auto (same exemptions) -> no schema -> secret scan of the value
-> no panel (``unmapped``) -> target (machine when a machine rule, the panel's disposition or an
absolute path in the value says so) -> destination bundle: the bundle that already holds the key (never
moved), else a ``[capture.bundles]`` match, else the panel's own bundle.

The keyfile override (``~/.config/glib-2.0/settings/keyfile``) can make the effective value differ
from dconf. It is never frozen into the profile; keys it changes are reported as ``Override``s
and captured entries it also touches carry the ``override`` flag.

Public API: ``scan_settings(...) -> SettingsResult`` and the result dataclasses.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from ubuntu_tweaks.bundles import BundleSet, Located
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.capture.rules import Mark, Rules
from ubuntu_tweaks.capture.secrets import scan_text, scan_value
from ubuntu_tweaks.setkey import ResolvedKey, SchemaIndex
from ubuntu_tweaks.settings import (
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
type Route = Literal["profile", "rule", "panel"]  # the key was already in a bundle / [capture.bundles] / panel default


@dataclass(frozen=True)
class KeyEntry:
    panel: str  # the Settings panel the capture rules assign the key to
    target: Target
    bundle: str  # the bundle it is written to: where the profile already holds it, a [capture.bundles] match, else the panel
    directory: str
    key: str
    value: str
    mark: Mark
    previous: str | None
    flags: tuple[str, ...]
    route: Route
    reason: str  # the comment above the key in the profile; "" for a new key


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


def _resolve(index: SchemaIndex, directory: str, key: str) -> ResolvedKey | None:
    resolved = index.resolve(directory, key)
    return resolved if isinstance(resolved, ResolvedKey) else None


def _classify_existing(
    held: Located,
    value: Variant,
    resolved: ResolvedKey,
    backend: SettingsBackend,
) -> tuple[Mark, str | None]:
    try:
        same = parse_value(backend, resolved.schema, resolved.key, held.key.value).equal(value)
    except SettingsError:
        same = False
    return ("=", None) if same else ("~", held.key.value)


def scan_settings(
    backend: SettingsBackend,
    index: SchemaIndex,
    rules: Rules,
    bundles: BundleSet,
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
        section = path_to_section(directory)
        held = bundles.locate(section, key)  # a key the user keeps in a bundle is a choice, never state or auto
        state = None if machine_reason or held else rules.state_rule(full)
        if state is not None:
            _skip(result, "state", full, state.reason, panel)
            continue
        auto = None if machine_reason or held else rules.auto_rule(full)
        if auto is not None:
            _skip(result, "auto", full, auto.reason, panel)
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
        flags: list[str] = ["path"] if has_machine_path(value) else []
        if held is not None:
            # (1) the profile already holds the key: it stays in its bundle, never moves
            target: Target = "machine" if held.bundle.origin == "overlay" else "shared"
            mark, previous = _classify_existing(held, value, resolved, backend)
            entry = KeyEntry(
                panel, target, held.bundle.name, directory, key, text, mark, previous, tuple(flags), "profile",
                held.key.reason,
            )  # fmt: skip
        else:
            routed = rules.bundle_route(full)  # (2) [capture.bundles], (3) the built-in panel file
            target = "machine" if machine_reason or panel_rule.to == "machine" or flags else "shared"
            if target == "shared" and (section, key) in bundles.inactive_shared:
                # another machine's bundle holds this key: a shared write would conflict there
                target = "machine"
                flags.append("other-machines")
            destination = bundles.find(routed or panel, "overlay" if target == "machine" else "shared")
            if destination is not None and not destination.active:
                raise ConfigError(
                    f"capture would write {full} into {destination.source}, which does not apply on this machine "
                    f"({destination.inactive}); route it elsewhere with [capture.bundles] or fix the bundle header"
                )
            entry = KeyEntry(
                panel, target, routed or panel, directory, key, text, "+", None, tuple(flags),
                "rule" if routed else "panel", "",
            )  # fmt: skip
        result.entries.append(entry)
        if held is not None:
            # identical duplicates in other bundles of the same layer must follow, or the profile would conflict
            for clash in bundles.duplicates:
                other = clash.second
                if (
                    clash.first.key.full == full
                    and other.bundle.origin == held.bundle.origin
                    and other.bundle.name != held.bundle.name
                    and not any(
                        e.bundle == other.bundle.name and e.target == entry.target and e.key == key
                        for e in result.entries
                    )
                ):
                    result.entries.append(replace(entry, bundle=other.bundle.name, reason=other.key.reason))
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
        replace(e, flags=(*e.flags, "override")) if e.directory + e.key in flagged else e for e in result.entries
    ]

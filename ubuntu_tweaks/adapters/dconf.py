"""dconf adapter: every key whose user value differs from the schema default (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``, ``capture``.

Assumptions:

- Items are ``dconf:/org/gnome/...`` (one per key); the keys come from ``dconf dump /`` through the settings backend
  and each is compared with the default of the schema that owns its directory (``setkey.SchemaIndex``, relocatable
  schemas included). A key at its default is not an item. A key no installed schema describes is an item (its value
  cannot be judged, so it is reported, with that in ``detail``).
- ``enabled-extensions`` / ``disabled-extensions`` belong to the ``extensions`` adapter and are skipped here.
- ``owned`` is what the profile declares: module presets and the active bundles (``planners.settings.declared_keys``);
  a key a bundle lists is tracked even while it sits at its default or its schema is missing.
- ``capture`` turns untracked keys into bundle text with only their user values, grouped by the profile's
  ``[capture.bundles]`` route or the GNOME Settings panel the capture rules send the key to (``panels.toml``); a key the
  capture rules refuse (secret, identity, hardware, a profile ``exclude``) or one with no installed schema is left out:
  ``capture`` would not write it either.
- Values never leave the backend except as GVariant text in ``detail`` (clipped) and in proposals.

- ``classify``: the keys ``capture`` would skip for a stated class are classified with capture's reason (``state``,
  ``auto``, ``excluded-by-rule``, a key no schema describes -> ``excluded``; ``secret``, ``identity``, ``hardware``
  and a value that matches a secret pattern -> ``secret``, the value never shown); a key capture would capture,
  into a bundle or the machine overlay, gets no opinion and stays ``untracked``. It runs only for keys no rule and no
  declared bundle claims, and uses the same rule tables as ``capture`` (``adapters/_capture_view.py``).

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``, ``classify(ctx, item, rules)``, ``capture(ctx, items)``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from ubuntu_tweaks.adapters._capture_view import capture_view
from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.capture.rules import load_rules as load_capture_rules
from ubuntu_tweaks.capture.secrets import scan_value
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.coverage import Coverage, Finding, Item, Proposal, Rules
from ubuntu_tweaks.planners.settings import declared_keys
from ubuntu_tweaks.setkey import MODULE_OWNED_KEYS, ResolvedKey, SchemaIndex, short
from ubuntu_tweaks.settings import SettingsError, dump_keyfile, format_value, path_to_section

NAME = "dconf"
_PREFIX = "dconf:"


def _full(item: Item) -> str:
    return item.id.removeprefix(_PREFIX)


def discover(ctx: Context) -> Iterable[Item]:
    backend = system().settings(ctx)
    try:
        keys = backend.list_user_keys()
    except SettingsError as error:
        yield Item(NAME, f"{_PREFIX}/", "database", f"cannot read the dconf database: {error}")
        return
    index = SchemaIndex(backend)
    for user_key in keys:
        full = user_key.full
        if full in MODULE_OWNED_KEYS:
            continue
        target = index.resolve(user_key.path, user_key.key)
        if not isinstance(target, ResolvedKey):
            yield Item(NAME, _PREFIX + full, "key", target.reason)
            continue
        value = backend.user_value(target.schema, target.path, target.key)
        default = backend.default(target.schema, target.path, target.key)
        if value is None or value.equal(default):
            continue
        yield Item(NAME, _PREFIX + full, "key", short(f"{format_value(value)} (default {format_value(default)})", 80))


def owned(ctx: Context) -> Mapping[str, str]:
    return {_PREFIX + full: owner for full, owner in declared_keys(ctx, system().settings(ctx)).items()}


_HARD_CLASSES = frozenset({"secret", "identity", "hardware"})  # never shown, never captured: reported as ``secret``


def _skipped(item: Item, cls: str, reason: str) -> Finding:
    """A key ``capture`` skips for ``cls``: hard classes are ``secret``, the rest excluded. The finding drops the item's
    detail (the value, clipped): the reason says all there is to say, and a value may be a secret or a location."""
    return Finding(replace(item, detail=""), Coverage.SECRET if cls in _HARD_CLASSES else Coverage.EXCLUDED, reason)


def classify(ctx: Context, item: Item, rules: Rules) -> Finding | None:
    """The opinion, or ``None`` when it cannot be formed: an unreadable key or an invalid ``[capture]`` table must not
    abort the whole scan (``capture`` itself reports that table)."""
    try:
        return _opinion(ctx, item)
    except (SettingsError, ConfigError):
        return None


def _opinion(ctx: Context, item: Item) -> Finding | None:
    """What ``capture`` would do with the key, in capture's own decision order: the profile's ``exclude``, the built-in
    secret / identity / hardware exclusions, a panel ``skip`` rule, then (unless a ``machine`` rule claims the key)
    ``state`` and ``auto``, no installed schema, and last a secret pattern in the value. A key capture would capture
    (into a bundle, or into the machine overlay) gets no opinion: it stays ``untracked``."""
    full = _full(item)
    if full == "/":
        return None  # the database could not be read: nothing to judge
    view = capture_view(ctx)
    capture_rules = view.rules
    if capture_rules.profile_exclude.matches(full):
        return _skipped(item, "excluded-by-rule", "profile [capture] exclude")
    hard = capture_rules.hard_exclude(full)
    if hard is not None:
        return _skipped(item, hard.cls, hard.reason)
    panel_rule = capture_rules.panel_rule(full)
    if panel_rule is not None and panel_rule.to == "skip":
        return _skipped(item, panel_rule.cls or "excluded-by-rule", panel_rule.reason)
    if capture_rules.machine_rule(full) is None:
        volatile = capture_rules.state_rule(full) or capture_rules.auto_rule(full)
        if volatile is not None:
            return _skipped(item, volatile.cls, volatile.reason)
    directory, _, name = full.rpartition("/")
    target = view.index.resolve(directory + "/", name)
    if not isinstance(target, ResolvedKey):
        return _skipped(item, "no-schema", "no installed schema describes this key")
    value = view.backend.user_value(target.schema, target.path, target.key)
    if value is None:
        return None
    findings = scan_value(format_value(value))
    if findings:
        kinds = ", ".join(sorted({finding.kind for finding in findings}))
        return _skipped(item, "secret", f"value matches: {kinds} (masked)")
    return None


def capture(ctx: Context, items: Sequence[Item]) -> Iterable[Proposal]:
    backend = system().settings(ctx)
    index = SchemaIndex(backend)
    table = ctx.profile.load_config().get("capture")
    rules = load_capture_rules(table if isinstance(table, dict) else None)
    sections: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    members: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        full = _full(item)
        directory, _, name = full.rpartition("/")
        target = index.resolve(directory + "/", name)
        if (
            not isinstance(target, ResolvedKey)
            or rules.profile_exclude.matches(full)
            or rules.hard_exclude(full) is not None
        ):
            continue
        panel_rule = rules.panel_rule(full)
        if panel_rule is not None and panel_rule.to == "skip":
            continue
        value = backend.user_value(target.schema, target.path, target.key)
        if value is None:
            continue
        panel = rules.bundle_route(full) or (panel_rule.panel if panel_rule is not None else "other")
        sections[panel][path_to_section(target.directory)][name] = format_value(value)
        members[panel].append(item)
    for panel in sorted(sections):
        yield Proposal(f"settings/{panel}.ini", dump_keyfile(sections[panel]), tuple(members[panel]))

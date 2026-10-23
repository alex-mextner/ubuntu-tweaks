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

Public API: ``NAME``, ``discover(ctx)``, ``owned(ctx)``, ``capture(ctx, items)``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.capture.rules import load_rules as load_capture_rules
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item, Proposal
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

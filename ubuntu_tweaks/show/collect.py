"""Turning a dconf dump plus the schemas into the panel sections of a ``show`` report. Read-only.

Accessed via: ``commands/show.py`` (``build``). Inputs are plain data, so the same code serves the
local machine and a remote one (``--ssh``): ``user`` is a ``dconf dump /`` keyfile, ``override`` the
parsed ``~/.config/glib-2.0/settings/keyfile`` (it overrides schema *defaults*), the schemas come from
the ``SettingsBackend`` / ``SchemaCatalog`` of the machine running the command.

Panel membership is the capture rules' (``capture.rules.Rules.panel_rule``, i.e. ``panels.toml``); keys
no rule claims land in the ``other`` section. Per key: effective value = user value, else override
value, else schema default; status ``changed`` (user value differs from the default, or the key has no
schema to compare with), ``override`` (no user value, the override file sets it) or ``default``.
``--all`` adds every key of every fixed-path schema as ``default``.

Masking: a key is ``hidden`` (value replaced by ``скрыто``, nothing of it kept in the report) when the
capture rules class it secret/identity/hardware (``Rules.hard_exclude`` or a ``skip`` panel rule of
those classes) or when the secret scanner finds a credential-looking value in it.

Instances of a relocatable schema (per-application notification settings, ...) with four or more
instances of the same key are folded into one row with one ``instance: value`` line each.

Public API: ``collect_panels(...)``, ``PANEL_OTHER``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from gi.repository import GLib

from ubuntu_tweaks.capture.rules import Rules
from ubuntu_tweaks.capture.secrets import scan_value
from ubuntu_tweaks.setkey import ResolvedKey, SchemaIndex
from ubuntu_tweaks.settings import (
    KeyFileData,
    SettingsBackend,
    SettingsError,
    Variant,
    format_value,
    parse_value,
    section_to_path,
)
from ubuntu_tweaks.show.catalog import KeyMeta, SchemaCatalog
from ubuntu_tweaks.show.descriptions import Descriptions, KeyText
from ubuntu_tweaks.show.model import HIDDEN, PanelSection, Row, Status
from ubuntu_tweaks.show.values import Rendered, humanize

PANEL_OTHER = "other"
_HARD_CLASSES = frozenset({"secret", "identity", "hardware"})
_GROUP_FROM = 4  # instances of one relocatable key before they fold into a single row


@dataclass(frozen=True)
class _Source:
    directory: str
    key: str
    user: str | None
    override: str | None

    @property
    def full(self) -> str:
        return self.directory + self.key


def _sources(user: KeyFileData, override: KeyFileData) -> dict[str, _Source]:
    found: dict[str, _Source] = {}
    for section, items in user.items():
        directory = section_to_path(section)
        for key, text in items.items():
            found[directory + key] = _Source(directory, key, text, None)
    for section, items in override.items():
        directory = section_to_path(section)
        for key, text in items.items():
            known = found.get(directory + key)
            found[directory + key] = _Source(directory, key, known.user if known else None, text)
    return found


def _parse(backend: SettingsBackend, resolved: ResolvedKey | None, key: str, text: str | None) -> Variant | None:
    if text is None:
        return None
    try:
        if resolved is not None:
            return parse_value(backend, resolved.schema, key, text)
        return GLib.Variant.parse(None, text)
    except (SettingsError, GLib.Error):
        return None


def _hidden(rules: Rules, full: str, texts: Iterable[str | None]) -> bool:
    if rules.hard_exclude(full) is not None:
        return True
    rule = rules.panel_rule(full)
    if rule is not None and rule.to == "skip" and rule.cls in _HARD_CLASSES:
        return True
    return any(scan_value(text) for text in texts if text)


def _explain(text: KeyText, meta: KeyMeta, has_schema: bool) -> tuple[str, bool]:
    if text.ru:
        return text.ru, True
    if meta.summary:
        return f"{meta.summary} (описание из схемы, без перевода)", False
    if not has_schema:
        return "Схемы этого ключа на машине нет — значение показано как есть.", False
    return "У ключа нет описания.", False


def _render(value: Variant | None, spec_text: KeyText) -> Rendered:
    if value is None:
        return Rendered(("—",))
    return humanize(value.unpack(), spec_text.spec)


def _row(
    source: _Source,
    index: SchemaIndex,
    backend: SettingsBackend,
    catalog: SchemaCatalog,
    rules: Rules,
    texts: Descriptions,
) -> Row:
    resolved = index.resolve(source.directory, source.key)
    resolved = resolved if isinstance(resolved, ResolvedKey) else None
    schema = resolved.schema if resolved else None
    meta = catalog.meta(schema, source.key) if schema else KeyMeta()
    user = _parse(backend, resolved, source.key, source.user)
    override = _parse(backend, resolved, source.key, source.override)
    # a value that does not fit this machine's schema (another version, over --ssh) must not vanish from the report
    user = user if user is not None or source.user is None else _parse(backend, None, source.key, source.user)
    override = (
        override
        if override is not None or source.override is None
        else _parse(backend, None, source.key, source.override)
    )
    default = override
    if default is None and resolved is not None:
        default = backend.default(resolved.schema, resolved.path, source.key)
    effective = user if user is not None else default
    status: Status
    if user is not None:
        status = "changed" if default is None or not user.equal(default) else "default"
    else:
        status = "override" if override is not None else "default"
    key_text = texts.lookup(schema, source.key, source.full)
    explanation, translated = _explain(key_text, meta, resolved is not None)
    path = source.full
    if _hidden(rules, source.full, [source.user, source.override, format_value(effective) if effective else None]):
        # the path of a hidden key can carry an identity too (a device id, a connection uuid): keep the key name only
        return Row(f"…/{source.key}", schema, source.key, status, explanation, translated, (HIDDEN,), (HIDDEN,), True)
    value = _render(effective, key_text)
    shown_default = _render(default, key_text) if default is not None else Rendered(("нет схемы — не с чем сравнить",))
    return Row(
        path, schema, source.key, status, explanation, translated, value.lines, shown_default.lines, False, value.swatch
    )


def _fold(rows: list[Row], index: SchemaIndex) -> list[Row]:
    """Fold four or more instances of one relocatable key into one row (``instance: value`` lines)."""
    groups: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        if row.schema is not None and index.fixed_path(row.schema) is None:
            groups.setdefault((row.schema, row.key), []).append(row)
    folded = {pair for pair, members in groups.items() if len(members) >= _GROUP_FROM}
    result: list[Row] = []
    done: set[tuple[str, str]] = set()
    for row in rows:
        pair = (row.schema or "", row.key)
        if pair not in folded:
            result.append(row)
        elif pair not in done:
            done.add(pair)
            members = groups[pair]
            lines = tuple(
                f"{m.path.rstrip('/').rsplit('/', 2)[-2]}: {' / '.join(m.value)}" if not m.hidden else HIDDEN
                for m in members
            )
            status: Status = "changed" if any(m.status == "changed" for m in members) else members[0].status
            result.append(
                Row(
                    f"{row.schema} × {len(members)}",
                    row.schema,
                    row.key,
                    status,
                    row.explanation,
                    row.translated,
                    lines,
                    next((m.default for m in members if not m.hidden), (HIDDEN,)),
                    all(m.hidden for m in members),
                )  # fmt: skip
            )
    return result


def _all_defaults(backend: SettingsBackend, catalog: SchemaCatalog, known: Mapping[str, _Source]) -> list[_Source]:
    extra: list[_Source] = []
    for info in backend.schemas():
        if info.path is None:
            continue
        for key in catalog.keys(info.id):
            if info.path + key not in known:
                extra.append(_Source(info.path, key, None, None))
    return extra


def collect_panels(
    *,
    user: KeyFileData,
    override: KeyFileData,
    backend: SettingsBackend,
    catalog: SchemaCatalog,
    rules: Rules,
    texts: Descriptions,
    show_all: bool = False,
    only_panel: str | None = None,
) -> tuple[tuple[PanelSection, ...], tuple[str, ...], dict[str, int]]:
    """Panel sections (only those with rows, in ``panels.toml`` order, ``other`` last), the titles of the
    panels left out because they have nothing to show, and the counters."""
    index = SchemaIndex(backend)
    sources = _sources(user, override)
    ordered = sorted(sources.values(), key=lambda source: source.full)
    if show_all:
        ordered = sorted([*ordered, *_all_defaults(backend, catalog, sources)], key=lambda source: source.full)
    by_panel: dict[str, list[Row]] = {}
    for source in ordered:
        rule = rules.panel_rule(source.full)
        panel = rule.panel if rule is not None else PANEL_OTHER
        if only_panel is not None and panel != only_panel:
            continue
        row = _row(source, index, backend, catalog, rules, texts)
        if row.status == "default" and not show_all:
            continue
        by_panel.setdefault(panel, []).append(row)
    sections: list[PanelSection] = []
    untouched: list[str] = []
    for panel_id in [*rules.panels, PANEL_OTHER]:
        text = texts.panel(panel_id)
        info = rules.panels.get(panel_id)
        title = text.title if text else (info.title if info else "Прочее")
        about = text.about if text else ""
        rows = _fold(by_panel.get(panel_id, []), index)
        if rows:
            sections.append(PanelSection(panel_id, title, about, tuple(rows)))
        elif only_panel is None:
            untouched.append(title)
    rows = [row for section in sections for row in section.rows]
    counts = {
        "changed": sum(row.status == "changed" for row in rows),
        "override": sum(row.status == "override" for row in rows),
        "default": sum(row.status == "default" for row in rows),
        "hidden": sum(row.hidden for row in rows),
        "no-schema": sum(row.schema is None for row in rows),
        "translated": sum(row.translated for row in rows),
        "keys": len(rows),
    }  # counted after folding, so the summary matches the rows on the page
    return tuple(sections), tuple(untouched), counts

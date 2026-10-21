"""The `capture` report: one document model, rendered as plain text (stdout) or markdown (``--report``).

Accessed via: ``commands/capture.py``.

Assumptions: values of captured dconf keys are shown (they are about to enter the profile);
values of skipped keys, secret findings and file contents are never shown. A finding appears as
its kind (``email``, ``pem-block``, ...), nothing of the matched text.

Section order follows the contract: Settings, Files, Packages and System, Skipped (with the
``unmapped`` bucket), Totals. ``verbose`` also lists entries already equal to the profile (``=``),
already-linked files and every cache-state candidate.

Public API: ``build_document(plan, *, machine, commit, verbose) -> Document``,
``render_text(document)``, ``render_markdown(document)``, ``totals_line(plan)``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ubuntu_tweaks.capture.files_scan import AdoptedFile
from ubuntu_tweaks.capture.plan import CapturePlan, refusals
from ubuntu_tweaks.capture.rules import SKIP_CLASSES
from ubuntu_tweaks.capture.settings_scan import KeyEntry
from ubuntu_tweaks.settings import path_to_section

_TEXT_CELL_WIDTH = 80
_CLASS_ORDER = ("portable-config", "mixed", "secret-bearing", "cache-state")


class Code(str):
    """A cell the markdown renderer puts in a code span."""


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class Lines:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


type Block = Heading | Lines | Table


@dataclass
class Document:
    blocks: list[Block] = field(default_factory=list)

    def heading(self, level: int, text: str) -> None:
        self.blocks.append(Heading(level, text))

    def lines(self, *lines: str) -> None:
        self.blocks.append(Lines(lines))

    def table(self, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        self.blocks.append(Table(headers, tuple(rows)))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _destination(target: str, bundle: str, machine: str) -> str:
    return f"settings/{bundle}.ini" if target == "shared" else f"machines/{machine}/settings/{bundle}.ini"


_FLAG_NOTE = {
    "path": "absolute path",
    "override": "keyfile override also set",
    "other-machines": "kept off the shared layer: a bundle for other machines sets it",
}
_ROUTE_NOTE = {"rule": "[capture.bundles]"}


def _settings_section(doc: Document, plan: CapturePlan, machine: str, verbose: bool) -> None:
    result = plan.settings
    assert result is not None
    doc.heading(2, "1. Settings")
    groups: dict[tuple[str, str], list[KeyEntry]] = {}
    for entry in result.entries:
        groups.setdefault((entry.target, entry.bundle), []).append(entry)
    if not groups:
        doc.lines("nothing to capture")
    for (target, bundle), entries in sorted(groups.items(), key=lambda item: (item[0][0] != "shared", item[0][1])):
        destination = _destination(target, bundle, machine)
        shown = [e for e in entries if verbose or e.mark != "="]
        hidden = len(entries) - len(shown)
        if not shown:
            doc.lines(f"{hidden} in {destination}: already in the profile (= hidden, -v shows them)")
            continue
        doc.heading(3, f"{destination} ({target})")
        rows = []
        for entry in sorted(shown, key=lambda e: (e.directory, e.key)):
            notes = [f"was {_cell_text(entry.previous)}" if entry.previous is not None else ""]
            notes.append(f"why: {_cell_text(entry.reason)}" if entry.reason else "")
            notes.append(
                _ROUTE_NOTE.get(entry.route, "")
                or ("stays in its bundle" if entry.route == "profile" and bundle != entry.panel else "")
            )
            notes += [_FLAG_NOTE[f] for f in entry.flags]
            rows.append(
                (
                    entry.mark,
                    Code(path_to_section(entry.directory)),
                    Code(entry.key),
                    Code(_cell_text(entry.value)),
                    entry.panel,
                    "; ".join(n for n in notes if n),
                )
            )
        doc.table(("", "dconf path", "key", "value", "panel", "note"), rows)
        if hidden:
            doc.lines(f"{hidden} already in the profile (= hidden, -v shows them)")
    if result.overrides:
        doc.heading(3, "keyfile overrides (not captured), per panel")
        doc.lines(
            "~/.config/glib-2.0/settings/keyfile sets these keys to a value that differs from dconf, so their "
            "effective value comes from the override. They are never frozen into the profile; the keyfile itself is "
            "captured as a file."
        )
        rows = []
        for override in sorted(result.overrides, key=lambda o: (o.panel or "~", o.full)):
            rows.append(
                (
                    override.panel or "-",
                    Code(override.full),
                    Code("[masked]" if override.masked else _cell_text(override.override_value)),
                    Code("[masked]" if override.masked else _cell_text(override.dconf_value)),
                    "effective here" if override.active else "not effective in this process",
                )
            )
        doc.table(("panel", "key", "keyfile value", "dconf value or default", "state"), rows)


def _file_status(adopted: AdoptedFile) -> str:
    if adopted.status == "differs":
        return f"differs (+{adopted.lines_added} -{adopted.lines_removed} lines)"
    return adopted.status


def _files_section(doc: Document, plan: CapturePlan, machine: str, verbose: bool) -> None:
    result = plan.files
    assert result is not None
    profile_root = plan.profile.root
    doc.heading(2, "2. Files")
    rows = []
    for adopted in result.adopted:
        if adopted.status == "linked" and not verbose:
            continue
        destination = (
            adopted.dest.relative_to(profile_root).as_posix()
            if adopted.dest.is_relative_to(profile_root)
            else str(adopted.dest)
        )
        action = "already linked" if adopted.status == "linked" else "backup+symlink"
        note = "REFUSES --commit: " + ", ".join(sorted({f.kind for f in adopted.findings})) if adopted.findings else ""
        rows.append(
            (Code(f"~/{adopted.rel}"), f"{adopted.size} B", Code(destination), action, _file_status(adopted), note)
        )
    if rows:
        doc.heading(3, "adopted (allow-list)")
        doc.table(("file", "size", "destination", "action", "vs profile", "note"), rows)
    else:
        doc.lines("no allow-listed file to adopt")
    if result.skipped:
        doc.heading(3, "allow-listed but not adopted")
        doc.table(("file", "reason"), [(Code(f"~/{s.rel}"), s.reason) for s in result.skipped])
    doc.heading(3, "candidates (not adopted; add to include_files in the profile's [capture] to promote)")
    counts = Counter(c.cls for c in result.candidates)
    doc.lines(
        ", ".join(f"{counts.get(cls, 0)} {cls}" for cls in _CLASS_ORDER)
        + (f"; {_plural(result.symlinks_ignored, 'symlink')} ignored" if result.symlinks_ignored else "")
    )
    for cls in _CLASS_ORDER:
        entries = sorted((c for c in result.candidates if c.cls == cls), key=lambda c: c.rel)
        if not entries:
            continue
        if cls == "cache-state" and not verbose:
            names = [c.rel + ("/" if c.kind == "dir" else "") for c in entries]
            doc.lines(f"cache-state ({len(entries)}; -v lists reasons): " + ", ".join(names))
            continue
        doc.heading(4, cls)
        doc.table(
            ("path", "size or files", "reason"),
            [
                (
                    Code(c.rel + ("/" if c.kind == "dir" else "")),
                    f"{c.size} B" if c.size is not None else f"{c.entries}+ files" if c.entries is not None else "",
                    c.reason,
                )
                for c in entries
            ],
        )


def _packages_section(doc: Document, plan: CapturePlan, verbose: bool) -> None:
    doc.heading(2, "3. Packages and System")
    if plan.packages is not None:
        doc.heading(3, "packages.toml proposal")
        doc.lines(plan.packages_note)
        for manager in ("apt", "snap", "flatpak"):
            items = [i for i in plan.packages if i.manager == manager]
            new = [i.name for i in items if i.mark == "+"]
            doc.lines(
                f"+ {manager} ({len(new)} new, {len(items) - len(new)} already in the profile): "
                + (", ".join(new) or "-")
            )
            if verbose and len(items) != len(new):
                doc.lines(f"= {manager}: " + ", ".join(i.name for i in items if i.mark == "="))
    if plan.system is not None:
        doc.heading(3, "system.toml proposal")
        if not plan.system:
            doc.lines("no system settings detected")
        else:
            doc.table(
                ("", "key", "value", "goes to", "note"),
                [
                    (
                        i.mark,
                        Code(i.key),
                        Code(i.value),
                        f"machines/{plan.profile.machine}/system.toml" if i.machine else "system.toml",
                        f"was {i.previous}" if i.previous else "",
                    )
                    for i in plan.system
                    if verbose or i.mark != "="
                ],
            )


def _skipped_section(doc: Document, plan: CapturePlan) -> None:
    result = plan.settings
    if result is None:
        return
    doc.heading(2, "4. Skipped")
    counts = Counter(s.cls for s in result.skipped)
    doc.lines(
        "classes: " + ", ".join(f"{cls} {counts.get(cls, 0)}" for cls in SKIP_CLASSES),
        f"unmapped (user value, no panel owns it; nothing is captured for these): {len(result.unmapped)}",
        f"at schema default (not listed): {result.at_default}",
    )
    for cls in SKIP_CLASSES:
        entries = sorted((s for s in result.skipped if s.cls == cls), key=lambda s: s.full)
        if entries:
            doc.heading(3, f"{cls} ({len(entries)})")
            if cls == "auto":
                doc.lines(
                    "keys the system or an app registered by itself (ids, ordering lists, bookkeeping), not "
                    'choices of yours; promote any of them with `[capture] include = ["<dconf path glob>"]` in '
                    "profile.toml"
                )
            doc.table(("key", "panel", "reason"), [(Code(s.full), s.panel or "-", s.reason) for s in entries])
    if result.unmapped:
        doc.heading(3, f"unmapped ({len(result.unmapped)})")
        doc.table(("key", "schema"), [(Code(u.full), u.schema) for u in sorted(result.unmapped, key=lambda u: u.full)])


def totals_line(plan: CapturePlan) -> str:
    parts: list[str] = []
    if plan.settings is not None:
        s = plan.settings
        bundles = {(e.target, e.bundle) for e in s.entries}
        machine = sum(1 for e in s.entries if e.target == "machine")
        marks = Counter(e.mark for e in s.entries)
        parts.append(
            f"settings: {len(s.entries)} keys in {_plural(len(bundles), 'bundle')} ({len(s.entries) - machine} shared, {machine} machine; "
            f"{marks['+']} new, {marks['~']} differ, {marks['=']} same), {len(s.unmapped)} unmapped, {len(s.skipped)} skipped, "
            f"{len(s.overrides)} keyfile overrides, {s.at_default} at default, {s.user_keys} user keys in scope"
        )
    if plan.files is not None:
        f = plan.files
        todo = sum(1 for a in f.adopted if a.status != "linked")
        parts.append(f"files: {todo} to adopt, {len(f.skipped)} not adopted, {len(f.candidates)} candidates")
    if plan.packages is not None:
        parts.append(f"packages: {sum(1 for i in plan.packages if i.mark == '+')} new")
    if plan.system is not None:
        parts.append(f"system: {sum(1 for i in plan.system if i.mark != '=')} to write")
    found = len(refusals(plan))
    parts.append(f"secret findings in adopted items: {found}")
    return "totals: " + "; ".join(parts)


def _summary_section(doc: Document, plan: CapturePlan, machine: str) -> None:
    """The block that opens the report: every count the user needs before reading a detail table."""
    doc.heading(2, "SUMMARY")
    result = plan.settings
    if result is not None:
        counts = Counter(s.cls for s in result.skipped)
        doc.lines(
            f"dconf user keys in scope: {result.user_keys} = {len(result.entries)} captured + "
            f"{len(result.skipped)} skipped + {len(result.unmapped)} unmapped + {result.at_default} at schema default "
            "(values equal to the default are never captured)"
        )
        destinations: dict[tuple[str, str], list[KeyEntry]] = {}
        for entry in result.entries:
            destinations.setdefault((entry.target, entry.bundle), []).append(entry)
        doc.heading(3, "captured, per destination bundle")
        if destinations:
            rows = []
            for (target, bundle), entries in sorted(destinations.items(), key=lambda i: (i[0][0] != "shared", i[0][1])):
                marks = Counter(e.mark for e in entries)
                rows.append(
                    (
                        Code(_destination(target, bundle, machine)),
                        str(len(entries)),
                        str(marks["+"]),
                        str(marks["~"]),
                        str(marks["="]),
                        ", ".join(sorted({e.panel for e in entries})),
                    )
                )
            doc.table(("destination", "keys", "new", "differ", "same", "panels"), rows)
        else:
            doc.lines("nothing to capture")
        doc.heading(3, "per Settings panel")
        panels = sorted(
            {e.panel for e in result.entries}
            | {s.panel for s in result.skipped if s.panel}
            | {o.panel for o in result.overrides if o.panel}
        )
        doc.table(
            ("panel", "captured", "skipped", "keyfile overrides"),
            [
                (
                    panel,
                    str(sum(1 for e in result.entries if e.panel == panel)),
                    str(sum(1 for s in result.skipped if s.panel == panel)),
                    str(sum(1 for o in result.overrides if o.panel == panel)),
                )
                for panel in panels
            ],
        )
        doc.heading(3, "not captured, per class")
        doc.table(
            ("class", "keys"),
            [
                *((cls, str(counts.get(cls, 0))) for cls in SKIP_CLASSES),
                ("unmapped", str(len(result.unmapped))),
                ("at schema default", str(result.at_default)),
            ],
        )
        if result.overrides:
            captured_too = sum(1 for e in result.entries if "override" in e.flags)
            doc.lines(
                f"keyfile overrides: {len(result.overrides)} keys have their effective value from "
                f"~/.config/glib-2.0/settings/keyfile (never written to the profile; {captured_too} of them are also "
                "captured from dconf and carry the `override` note)"
            )
    if plan.files is not None:
        f = plan.files
        todo = sum(1 for a in f.adopted if a.status != "linked")
        doc.lines(
            f"files: {todo} to adopt, {len(f.skipped)} allow-listed but not adopted, {len(f.candidates)} candidates "
            "(never adopted unless promoted)"
        )
    if plan.packages is not None:
        new = Counter(i.manager for i in plan.packages if i.mark == "+")
        doc.lines(
            f"packages: {sum(new.values())} new ({new['apt']} apt, {new['snap']} snap, {new['flatpak']} flatpak), "
            f"{sum(1 for i in plan.packages if i.mark != '+')} already in the profile"
        )
    if plan.system is not None:
        todo_keys = [i.key for i in plan.system if i.mark != "="]
        doc.lines(f"system: {len(todo_keys)} to write" + (f" ({', '.join(todo_keys)})" if todo_keys else ""))
    doc.lines(f"secret findings in adopted items: {len(refusals(plan))}")
    if result is not None:
        doc.lines(
            f"unmapped: {len(result.unmapped)}"
            + (
                " (" + ", ".join(sorted({u.schema for u in result.unmapped})) + "; schemas no panel claims)"
                if result.unmapped
                else ""
            )
        )


def build_document(
    plan: CapturePlan,
    *,
    machine: str,
    commit: bool,
    verbose: bool,
    dry_run: bool = False,
    summary_only: bool = False,
) -> Document:
    doc = Document()
    doc.heading(1, "ubuntu-tweak capture")
    overlay = "exists" if plan.profile.has_overlay else "does not exist yet"
    if commit:
        mode = "COMMIT"
    elif dry_run:
        mode = "dry-run (nothing is written; the same as the default preview)"
    else:
        mode = "preview (nothing is written; --commit writes exactly this)"
    doc.lines(
        f"mode: {mode}",
        f"profile: {plan.profile.root}",
        f"machine: {machine} (overlay {overlay})",
        "only: " + (", ".join(sorted(plan.only)) or "everything"),
    )
    _summary_section(doc, plan, machine)
    if not summary_only:
        if plan.settings is not None:
            _settings_section(doc, plan, machine, verbose)
        if plan.files is not None:
            _files_section(doc, plan, machine, verbose)
        if plan.packages is not None or plan.system is not None:
            _packages_section(doc, plan, verbose)
        _skipped_section(doc, plan)
    doc.heading(2, "5. Totals")
    doc.lines(totals_line(plan))
    found = refusals(plan)
    if found:
        doc.lines(
            "--commit would be refused (exit 5): secret finding",
            *(f"  {where}: {kind} ({cls}) line {line}" for where, kind, cls, line in found),
        )
    return doc


def _cell_text(cell: str) -> str:
    return " ".join(str(cell).split())


def render_text(doc: Document) -> str:
    out: list[str] = []
    for block in doc.blocks:
        if isinstance(block, Heading):
            out += ["", ("=" if block.level <= 2 else "-") + f" {block.text}"]
        elif isinstance(block, Lines):
            out += list(block.lines)
        else:
            cells = [
                tuple(
                    clipped
                    if len(clipped := _cell_text(c)) <= _TEXT_CELL_WIDTH
                    else clipped[: _TEXT_CELL_WIDTH - 1] + "…"
                    for c in row
                )
                for row in (block.headers, *block.rows)
            ]
            widths = [max(len(row[i]) for row in cells) for i in range(len(block.headers))]
            for index, row in enumerate(cells):
                if index == 0 and not any(block.headers):
                    continue
                out.append("  " + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(out).lstrip("\n") + "\n"


def _md(cell: str) -> str:
    text = _cell_text(cell).replace("|", "\\|")
    if isinstance(cell, Code) and text and "`" not in text:
        return f"`{text}`"
    return text.replace("<", "&lt;")


def render_markdown(doc: Document) -> str:
    out: list[str] = []
    for block in doc.blocks:
        if isinstance(block, Heading):
            out += ["", "#" * block.level + f" {block.text}", ""]
        elif isinstance(block, Lines):
            for line in block.lines:
                out += [line.replace("<", "&lt;") if line.strip() else line, ""]
        else:
            out.append("| " + " | ".join(block.headers) + " |")
            out.append("|" + "|".join("---" for _ in block.headers) + "|")
            out += ["| " + " | ".join(_md(c) for c in row) + " |" for row in block.rows]
            out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n") + "\n"

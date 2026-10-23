"""Plain-text and Markdown renderers of a ``show`` report. Pure: ``Report`` in, ``str`` out.

Accessed via: ``commands/show.py``. Public API: ``render_text(report)``, ``render_markdown(report)``,
``summary_line(report)``. The HTML renderer lives in ``show.html_page``.
"""

from __future__ import annotations

from ubuntu_tweaks.show.model import STATUS_LABELS, Entry, ExtraSection, PanelSection, Report, Row


def summary_line(report: Report) -> str:
    counts = report.counts
    return (
        f"параметров в списке: {counts['keys']} — изменено вами: {counts['changed']}, "
        f"задано файлом-переопределением: {counts['override']}, по умолчанию: {counts['default']}; "
        f"скрыто как секретные: {counts['hidden']}, без схемы на машине: {counts['no-schema']}"
    )


def _title(report: Report) -> str:
    mode = "все параметры" if report.mode == "all" else "только изменённое"
    return f"Настройки Ubuntu — {report.label} ({report.source}), режим: {mode}"


def _text_value(lines: tuple[str, ...], indent: str, label: str) -> list[str]:
    if len(lines) == 1:
        return [f"{indent}{label}: {lines[0]}"]
    return [f"{indent}{label}:", *(f"{indent}  • {line}" for line in lines)]


def _text_row(row: Row) -> list[str]:
    out = [f"  {row.title}  [{STATUS_LABELS[row.status]}]", f"    {row.path}"]
    out.append(f"    Что это: {row.explanation}")
    out += _text_value(row.value, "    ", "Сейчас")
    out += _text_value(row.default, "    ", "По умолчанию")
    return [*out, ""]


def _text_panel(panel: PanelSection) -> list[str]:
    head = f"═══ {panel.title} [{panel.id}] — {len(panel.rows)} ═══"
    out = [head] + ([panel.about] if panel.about else []) + [""]
    for row in panel.rows:
        out += _text_row(row)
    return out


def _text_extra(section: ExtraSection) -> list[str]:
    out = [f"═══ {section.title} ═══", section.about, ""]
    for entry in section.entries:
        out += _text_value(entry.lines, "  ", entry.label)
    return [*out, ""]


def render_text(report: Report) -> str:
    out = [_title(report), summary_line(report), ""]
    out += ["Содержание: " + "; ".join([*(p.title for p in report.panels), *(e.title for e in report.extras)]), ""]
    for panel in report.panels:
        out += _text_panel(panel)
    if report.untouched:
        out += ["Панели без изменённых параметров: " + ", ".join(report.untouched), ""]
    for extra in report.extras:
        out += _text_extra(extra)
    return "\n".join(out).rstrip() + "\n"


def _code(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def _md_value(label: str, lines: tuple[str, ...]) -> list[str]:
    if len(lines) == 1:
        return [f"- **{label}:** {_code(lines[0])}"]
    return [f"- **{label}:**", *(f"  - {_code(line)}" for line in lines)]


def _md_entry(entry: Entry) -> list[str]:
    return _md_value(entry.label, entry.lines)


def render_markdown(report: Report) -> str:
    out = [f"# {_title(report)}", "", summary_line(report), "", "## Содержание", ""]
    out += [f"- {panel.title} ({len(panel.rows)})" for panel in report.panels]
    out += [f"- {extra.title}" for extra in report.extras]
    out.append("")
    for panel in report.panels:
        out += [f"## {panel.title} (`{panel.id}`)", ""]
        out += [f"_{panel.about}_", ""] if panel.about else []
        for row in panel.rows:
            out += [f"### `{row.title}` — {STATUS_LABELS[row.status]}", "", f"`{row.path}`", ""]
            out += [f"- **Что это:** {row.explanation}"]
            out += _md_value("Сейчас", row.value) + _md_value("По умолчанию", row.default)
            out.append("")
    if report.untouched:
        out += ["Панели без изменённых параметров: " + ", ".join(report.untouched), ""]
    for extra in report.extras:
        out += [f"## {extra.title}", "", f"_{extra.about}_", ""]
        for entry in extra.entries:
            out += _md_entry(entry)
        out.append("")
    return "\n".join(out).rstrip() + "\n"

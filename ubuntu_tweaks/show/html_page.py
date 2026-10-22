"""The HTML renderer of ``ubuntu-tweak show``: one self-contained file, no JavaScript, no external requests.

Accessed via: ``commands/show.py --format html``. Inline CSS only, light and dark through
``prefers-color-scheme``, a table of contents of anchor links, one collapsible ``<details>`` per panel
and per extra section. Every piece of text passes ``html.escape``; the only unescaped inline style is a
``background`` colour that ``values.normalize_color`` has validated as ``#rrggbb``.

Public API: ``render_html(report) -> str``.
"""

from __future__ import annotations

from html import escape

from ubuntu_tweaks.show.model import STATUS_LABELS, Entry, ExtraSection, PanelSection, Report, Row
from ubuntu_tweaks.show.render import summary_line

_CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--fg:#1d1d1f;--mute:#6b6b70;--card:#fff;--line:#dcdce0;--accent:#2a6df4;
--changed:#0b7a3b;--override:#9a5b00;--default:#6b6b70;--hidden:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#e9e9ec;--mute:#9b9ba3;--card:#1f1f23;--line:#34343a;
--accent:#7aa2ff;--changed:#5fd68a;--override:#f0b252;--default:#9b9ba3;--hidden:#ff8a80}}
*{box-sizing:border-box}body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:70rem;margin:0 auto}h1{font-size:1.6rem;margin:0 0 .25rem}.sum{color:var(--mute);margin:0 0 1.5rem}
nav{background:var(--card);border:1px solid var(--line);border-radius:.6rem;padding:.8rem 1.2rem;margin-bottom:1.5rem}
nav ul{margin:.3rem 0 0;padding-left:1.2rem;columns:2 18rem}nav a{color:var(--accent);text-decoration:none}
nav a:hover{text-decoration:underline}
details{background:var(--card);border:1px solid var(--line);border-radius:.6rem;margin:0 0 1rem}
summary{cursor:pointer;padding:.75rem 1.1rem;font-weight:600;font-size:1.1rem}
summary small{font-weight:400;color:var(--mute);margin-left:.5rem}
.about{margin:0;padding:0 1.1rem .6rem;color:var(--mute)}
table{width:100%;border-collapse:collapse;font-size:.93rem}th,td{padding:.55rem .8rem;vertical-align:top;
text-align:left;border-top:1px solid var(--line)}th{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;
color:var(--mute)}
td.k code{font-weight:600}td.k small{display:block;color:var(--mute);word-break:break-all}
td ul{margin:0;padding-left:1.1rem}td ul.one{list-style:none;padding:0}
.badge{display:inline-block;padding:.1rem .55rem;border-radius:1rem;border:1px solid currentColor;font-size:.78rem;
white-space:nowrap}.changed{color:var(--changed)}.override{color:var(--override)}.default{color:var(--default)}
.hidden{color:var(--hidden);font-style:italic}
.sw{display:inline-block;width:.9em;height:.9em;border-radius:.2em;border:1px solid var(--line);
vertical-align:-.1em;margin-right:.35em}.tr{font-size:.78rem;color:var(--mute)}
dl{margin:0;padding:0 1.1rem 1rem}dt{font-weight:600;margin-top:.6rem}dd{margin:.1rem 0 0 1rem}
footer{color:var(--mute);font-size:.85rem;margin-top:2rem}
"""


def _lines(lines: tuple[str, ...], swatch: str | None = None, hidden: bool = False) -> str:
    mark = f'<span class="sw" style="background:{swatch}"></span>' if swatch and len(lines) == 1 else ""
    cls = ' class="hidden"' if hidden else ""
    if len(lines) == 1:
        return f'<ul class="one"><li{cls}>{mark}{escape(lines[0])}</li></ul>'
    return "<ul>" + "".join(f"<li{cls}>{mark}{escape(line)}</li>" for line in lines) + "</ul>"


def _row(row: Row) -> str:
    translated = "" if row.translated else ' <span class="tr">(текст схемы)</span>'
    return (
        "<tr>"
        f'<td class="k"><code>{escape(row.title)}</code><small>{escape(row.path)}</small></td>'
        f"<td>{escape(row.explanation)}{translated}</td>"
        f"<td>{_lines(row.value, row.swatch, row.hidden)}</td>"
        f"<td>{_lines(row.default, None, row.hidden)}</td>"
        f'<td><span class="badge {row.status}">{escape(STATUS_LABELS[row.status])}</span></td>'
        "</tr>"
    )


def _panel(panel: PanelSection) -> str:
    about = f'<p class="about">{escape(panel.about)}</p>' if panel.about else ""
    head = "<tr><th>Параметр</th><th>Что это</th><th>Сейчас</th><th>По умолчанию</th><th>Статус</th></tr>"
    body = "".join(_row(row) for row in panel.rows)
    return (
        f'<details id="panel-{escape(panel.id)}" open><summary>{escape(panel.title)}'
        f"<small>{len(panel.rows)}</small></summary>{about}<table>{head}{body}</table></details>"
    )


def _entry(entry: Entry) -> str:
    return f"<dt>{escape(entry.label)}</dt><dd>{_lines(entry.lines)}</dd>"


def _extra(section: ExtraSection) -> str:
    return (
        f'<details id="extra-{escape(section.id)}" open><summary>{escape(section.title)}</summary>'
        f'<p class="about">{escape(section.about)}</p><dl>{"".join(_entry(e) for e in section.entries)}</dl></details>'
    )


def render_html(report: Report) -> str:
    mode = "все параметры" if report.mode == "all" else "только изменённое"
    toc = "".join(
        f'<li><a href="#panel-{escape(p.id)}">{escape(p.title)}</a> ({len(p.rows)})</li>' for p in report.panels
    ) + "".join(f'<li><a href="#extra-{escape(e.id)}">{escape(e.title)}</a></li>' for e in report.extras)
    untouched = (
        f"<footer>Панели без изменённых параметров: {escape(', '.join(report.untouched))}</footer>"
        if report.untouched
        else ""
    )
    title = f"Настройки Ubuntu — {report.label}"
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
        f'<h1>{escape(title)}</h1><p class="sum">{escape(report.source)}, режим: {escape(mode)}. '
        f"{escape(summary_line(report))}</p>"
        f"<nav><strong>Содержание</strong><ul>{toc}</ul></nav>"
        + "".join(_panel(panel) for panel in report.panels)
        + "".join(_extra(extra) for extra in report.extras)
        + untouched
        + "</main></body></html>\n"
    )

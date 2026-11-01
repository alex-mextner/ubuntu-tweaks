"""HTML files: a static web-page card (title, description, first heading) instead of rendering the page.

Rendering HTML needs a browser engine and network-free, script-free sandboxing; a card with what the page says it is
(its ``<title>``) is cheap and safe. A page with neither title nor heading is shown as highlighted source instead.
"""

from __future__ import annotations

from html.parser import HTMLParser

from PIL import Image, ImageDraw

from ..card import finish, font, vertical_gradient, wrap_text
from ..files import read_head
from .code import DOTS, highlighted_card

HEAD_BYTES = 64 * 1024
TEXT_LIMIT = 600  # characters kept of a title, heading or description: the card shows far fewer
ACCENT = (225, 98, 38)


class _Found(Exception):
    """Everything the card needs was seen; stop reading."""


class _PageFacts(HTMLParser):
    """Collects the title, the first ``<h1>`` and the description; linear in the input, bounded in what it keeps."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.heading = ""
        self.description = ""
        self._open: str | None = None
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("title", "h1") and tag not in self._seen:
            self._open = tag
        elif tag == "meta":
            fields = {key.lower(): value or "" for key, value in attrs}
            key = (fields.get("name") or fields.get("property") or "").lower()
            content = " ".join(fields.get("content", "").split())[:TEXT_LIMIT]
            if key in ("description", "og:description") and not self.description:
                self.description = content
            elif key == "og:title" and not self.title:
                self.title = content
        elif tag in ("body", "script", "style") and self.title and self.description:
            raise _Found

    def handle_endtag(self, tag: str) -> None:
        if tag == self._open:
            self._seen.add(tag)
            self._open = None
            if self._seen >= {"title", "h1"}:
                raise _Found

    def handle_data(self, data: str) -> None:
        if self._open == "title":
            self.title = " ".join((self.title + " " + data).split())[:TEXT_LIMIT]
        elif self._open == "h1":
            self.heading = " ".join((self.heading + " " + data).split())[:TEXT_LIMIT]


def describe(text: str) -> tuple[str, str, str]:
    """(title, description, heading) of an HTML document, each possibly empty."""
    facts = _PageFacts()
    try:
        facts.feed(text)
        facts.close()
    except _Found:
        pass
    return facts.title, facts.description, facts.heading


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    text = read_head(path, HEAD_BYTES).decode("utf-8", "replace")
    title, description, heading = describe(text)
    headline = title or heading
    if not headline:
        return highlighted_card(text, "html", size, "HTML")
    scale = 3 if size <= 256 else 2
    canvas = size * scale
    image = vertical_gradient(canvas, canvas, (255, 255, 255), (243, 245, 248))
    draw = ImageDraw.Draw(image)
    bar = round(canvas * 0.1)
    draw.rectangle((0, 0, canvas, bar), fill=(228, 231, 236))
    radius = bar * 0.15
    for index, color in enumerate(DOTS):
        cx = bar * 0.6 + index * bar * 0.42
        draw.ellipse((cx - radius, bar / 2 - radius, cx + radius, bar / 2 + radius), fill=color)
    draw.rounded_rectangle((bar * 2.1, bar * 0.25, canvas - bar * 0.5, bar * 0.75), radius=bar * 0.25, fill=(246, 247, 249))

    margin = round(canvas * 0.08)
    width = canvas - 2 * margin
    y = bar + margin * 0.9
    draw.rectangle((margin, y, margin + canvas * 0.12, y + canvas * 0.012), fill=ACCENT)
    y += canvas * 0.04
    title_face = font(round(canvas * 0.085), bold=True)
    for line in wrap_text(draw, headline, title_face, width, 4):
        draw.text((margin, y), line, font=title_face, fill=(28, 32, 40))
        y += canvas * 0.105
    y += canvas * 0.02
    body = description or (heading if heading != headline else "")
    body_face = font(round(canvas * 0.052))
    for line in wrap_text(draw, body, body_face, width, 4) if body else []:
        draw.text((margin, y), line, font=body_face, fill=(104, 112, 126))
        y += canvas * 0.07
    if not body:  # the page's skeleton, so the card does not look unfinished
        for fraction in (0.9, 0.7, 0.8):
            draw.rounded_rectangle((margin, y, margin + width * fraction, y + canvas * 0.025), radius=canvas * 0.012, fill=(222, 226, 232))
            y += canvas * 0.055
    label_face = font(round(canvas * 0.07), bold=True)
    label = "HTML"
    label_width = draw.textlength(label, font=label_face)
    pad = canvas * 0.028
    right, bottom = canvas - canvas * 0.045, canvas - canvas * 0.045
    box = (right - label_width - 2 * pad, bottom - canvas * 0.07 - pad * 1.2, right, bottom)
    draw.rounded_rectangle(box, radius=canvas * 0.025, fill=ACCENT)
    draw.text((box[0] + pad, box[1] + pad * 0.3), label, font=label_face, fill=(255, 255, 255))
    return finish(image.resize((size, size), Image.LANCZOS))


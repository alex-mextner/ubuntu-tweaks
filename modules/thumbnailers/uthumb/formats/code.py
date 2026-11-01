"""Source files as a syntax-highlighted window: the first lines of the file, coloured by Pygments."""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..card import Rgb, finish, font, hex_rgb, luminance
from ..errors import Unsupported
from ..files import read_head

STYLE = "one-dark"
HEAD_BYTES = 24 * 1024
MAX_LINES = 40
DOTS = ((255, 95, 86), (255, 189, 46), (39, 201, 63))
# Extension -> Pygments lexer alias where the filename lookup would guess wrong or not at all.
LEXERS = {
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".mjs": "javascript", ".cjs": "javascript", ".js": "javascript", ".jsonc": "json", ".json5": "json",
    ".webmanifest": "json", ".mdx": "markdown", ".md": "markdown", ".markdown": "markdown",
    ".vue": "vue", ".svelte": "html", ".astro": "html", ".less": "less", ".scss": "scss", ".sass": "sass",
    ".graphql": "graphql", ".gql": "graphql", ".dockerfile": "docker", ".env": "bash", ".mk": "make",
    ".scad": "openscad", ".phtml": "html+php", ".php": "php", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
    ".xsl": "xml", ".xslt": "xml", ".plist": "xml", ".sh": "bash", ".zsh": "bash", ".bash": "bash",
    ".sql": "sql", ".py": "python", ".pyw": "python", ".rs": "rust", ".go": "go", ".css": "css",
    ".json": "json", ".xml": "xml", ".html": "html", ".htm": "html", ".xhtml": "html", ".shtml": "html",
}  # fmt: skip


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    text = read_head(path, HEAD_BYTES).decode("utf-8", "replace")
    suffix = _suffix(name) or next(iter(kind.extensions), "")
    lexer_name = _sniff(text, suffix) or LEXERS.get(suffix) or LEXERS.get(next(iter(kind.extensions), ""))
    badge = _badge(name, kind)
    return highlighted_card(text, lexer_name, size, badge)


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _sniff(text: str, suffix: str) -> str | None:
    """``.ts`` is TypeScript or a Qt translation, ``.tsx`` React or a Tiled tileset: XML says which."""
    if suffix in {".ts", ".tsx"} and text.lstrip().startswith(("<?xml", "<!DOCTYPE", "<TS ")):
        return "xml"
    return None


def _badge(name: str, kind) -> str:
    suffix = _suffix(name)
    return (suffix.lstrip(".") or kind.label).upper()[:6]


def highlighted_card(text: str, lexer_name: str | None, size: int, badge: str) -> Image.Image:
    """The code window itself: reused by the notebook and web-page cards."""
    from pygments import lex
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.styles import get_style_by_name
    from pygments.util import ClassNotFound

    style = get_style_by_name(STYLE)
    try:
        lexer = get_lexer_by_name(lexer_name) if lexer_name else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    text = text.replace("\t", "    ").replace("\r\n", "\n").lstrip("\n")
    if not text.strip():
        raise Unsupported("empty file")
    background = hex_rgb(style.background_color, (40, 44, 52))
    default = (171, 178, 191) if luminance(background) < 0.5 else (40, 40, 40)

    scale = 3 if size <= 256 else 2
    canvas = size * scale
    image = Image.new("RGB", (canvas, canvas), background)
    draw = ImageDraw.Draw(image)
    bar = round(canvas * 0.085)
    draw.rectangle((0, 0, canvas, bar), fill=_shift(background, 10 if luminance(background) < 0.5 else -8))
    radius = bar * 0.17
    for index, color in enumerate(DOTS):
        cx = bar * 0.7 + index * bar * 0.5
        draw.ellipse((cx - radius, bar / 2 - radius, cx + radius, bar / 2 + radius), fill=color)

    margin = round(canvas * 0.055)
    columns = 34
    face_size = max(6, round((canvas - 2 * margin) / (columns * 0.6)))
    regular, bold = font(face_size, mono=True), font(face_size, bold=True, mono=True)
    advance = draw.textlength("M", font=regular)
    columns = int((canvas - 2 * margin) / advance)
    line_height = face_size * 1.32
    top = bar + margin * 0.8
    rows = int((canvas - top - margin * 0.6) / line_height)

    x, row = float(margin), 0
    for token_type, value in lex(text, lexer):
        token_style = _token_style(style, token_type)
        color = hex_rgb(token_style["color"] or "", default) if token_style["color"] else default
        face = bold if token_style["bold"] else regular
        parts = value.split("\n")
        for index, part in enumerate(parts):
            if index > 0:
                row += 1
                x = float(margin)
            if row >= min(rows, MAX_LINES):
                break
            column = round((x - margin) / advance)
            room = columns - column
            if part and room > 0:
                shown = part[:room]
                draw.text((x, top + row * line_height), shown, font=face, fill=color)
            x += advance * len(part)
        if row >= min(rows, MAX_LINES):
            break

    _draw_badge(draw, badge, canvas, bar, background)
    return finish(image.resize((size, size), Image.LANCZOS))


def _token_style(style, token_type):
    """Pygments styles can lack an entry for an unusual token: use its closest known ancestor."""
    while True:
        try:
            return style.style_for_token(token_type)
        except KeyError:
            if token_type.parent is None:
                return {"color": None, "bold": False}
            token_type = token_type.parent


def _shift(color: Rgb, amount: int) -> Rgb:
    return tuple(max(0, min(255, c + amount)) for c in color)  # type: ignore[return-value]


def _draw_badge(draw: ImageDraw.ImageDraw, label: str, canvas: int, bar: int, background: Rgb) -> None:
    """The file type in a pill at the bottom right: readable even when the code is a blur at small sizes."""
    face = font(round(canvas * 0.075), bold=True)
    width = draw.textlength(label, font=face)
    pad = canvas * 0.028
    right, bottom = canvas - canvas * 0.04, canvas - canvas * 0.04
    box = (right - width - 2 * pad, bottom - canvas * 0.075 - 2 * pad * 0.6, right, bottom)
    draw.rounded_rectangle(box, radius=canvas * 0.025, fill=(97, 175, 239) if luminance(background) < 0.5 else (30, 90, 180))
    draw.text((box[0] + pad, box[1] + pad * 0.35), label, font=face, fill=(20, 24, 30))

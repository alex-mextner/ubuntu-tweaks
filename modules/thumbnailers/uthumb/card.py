"""Shared drawing: gradient backgrounds, rounded cards with a hairline border, system font lookup."""

from __future__ import annotations

import glob
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .errors import Unsupported

Rgb = tuple[int, int, int]

# Monospace families the sandbox can see (everything under /usr/share/fonts), best first.
MONO_PATTERNS = (
    "/usr/share/fonts/truetype/firacode/FiraCode-{weight}.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono{bold}.ttf",
    "/usr/share/fonts/truetype/liberation*/LiberationMono-{liberation}.ttf",
    "/usr/share/fonts/**/*Mono*{bold_glob}.ttf",
)
SANS_PATTERNS = (
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-{ubuntu}.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans{bold}.ttf",
    "/usr/share/fonts/truetype/liberation*/LiberationSans-{liberation}.ttf",
)


def _resolve(patterns: tuple[str, ...], bold: bool) -> str | None:
    values = {
        "weight": "Bold" if bold else "Regular",
        "bold": "-Bold" if bold else "",
        "liberation": "Bold" if bold else "Regular",
        "ubuntu": "B" if bold else "R",
        "bold_glob": "*Bold" if bold else "*Regular",
    }
    for pattern in patterns:
        matches = sorted(glob.glob(pattern.format(**values), recursive=True))
        if matches:
            return matches[0]
    return None


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A system TrueType font at ``size`` px; Pillow's built-in face when the sandbox has none."""
    path = _resolve(MONO_PATTERNS if mono else SANS_PATTERNS, bold)
    if path:
        try:
            return ImageFont.truetype(path, max(size, 4))
        except OSError:
            pass
    try:
        return ImageFont.load_default(max(size, 4))
    except (TypeError, OSError) as error:
        raise Unsupported("no usable font") from error


def lerp(a: Rgb, b: Rgb, t: float) -> Rgb:
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))


def vertical_gradient(width: int, height: int, top: Rgb, bottom: Rgb) -> Image.Image:
    strip = Image.new("RGB", (1, height))
    strip.putdata([lerp(top, bottom, y / max(height - 1, 1)) for y in range(height)])
    return strip.resize((width, height))


def hex_rgb(value: str, default: Rgb = (0, 0, 0)) -> Rgb:
    value = (value or "").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return default
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return default


def luminance(color: Rgb) -> float:
    return (0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]) / 255


def finish(image: Image.Image, radius_ratio: float = 0.07) -> Image.Image:
    """Round the corners (anti-aliased alpha) and add a hairline border: the card every preview ends as."""
    width, height = image.size
    radius = round(min(width, height) * radius_ratio)
    scale = 4
    mask = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width * scale - 1, height * scale - 1), radius * scale, fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask.resize((width, height), Image.LANCZOS))
    edge = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1), radius * scale, outline=(0, 0, 0, 56), width=scale
    )
    return Image.alpha_composite(out, edge.resize((width, height), Image.LANCZOS))


def _longest_prefix(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> int:
    """How many leading characters of ``text`` fit in ``width`` px (at least 1): a binary search, not a scan."""
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if draw.textlength(text[:middle], font=face) <= width:
            low = middle
        else:
            high = middle - 1
    return low


def fit_text(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.FreeTypeFont | ImageFont.ImageFont, width: int) -> str:
    """``text`` cut with an ellipsis so that it fits ``width`` px."""
    if draw.textlength(text, font=face) <= width:
        return text
    keep = _longest_prefix(draw, text[:2000] + "…", face, width) - 1
    return text[: max(keep, 0)] + "…"


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    face: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
    max_lines: int,
) -> list[str]:
    """Greedy word wrap into at most ``max_lines`` lines; the last one ends with an ellipsis when cut.

    Stops as soon as one line more than shown exists, so the work is bounded by what is drawn, not by the text."""
    lines: list[str] = []
    line = ""
    for word in text[: 400 * max_lines].split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=face) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        line = word
        while len(line) > 1 and draw.textlength(line, font=face) > width:  # one very long token
            cut = _longest_prefix(draw, line, face, width)
            lines.append(line[:cut])
            line = line[cut:]
            if len(lines) > max_lines:
                break
        if len(lines) > max_lines:
            break
    else:
        if line:
            lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = fit_text(draw, lines[-1] + "…", face, width)
    return lines


def picture_card(picture: Image.Image, size: int, top: Rgb = (236, 240, 246), bottom: Rgb = (201, 209, 221)) -> Image.Image:
    """A slicer's or CAD tool's own preview picture, centred on the studio gradient with a margin."""
    base = vertical_gradient(size, size, top, bottom)
    picture = picture.convert("RGBA")
    inner = round(size * 0.9)
    ratio = min(inner / picture.width, inner / picture.height)
    resized = picture.resize((max(1, round(picture.width * ratio)), max(1, round(picture.height * ratio))), Image.LANCZOS)
    base.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2), resized)
    return finish(base)

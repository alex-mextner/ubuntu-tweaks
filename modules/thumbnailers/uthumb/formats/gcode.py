"""G-code (text) and binary G-code (.bgcode): the slicer's embedded preview, else the toolpath drawn in 3D.

Orca, Bambu Studio, PrusaSlicer, Cura and the Snapmaker firmware slicers embed ``; thumbnail begin WxH N`` blocks of
base64 PNG (or JPG/QOI); PrusaSlicer's binary format keeps them in thumbnail blocks. Only when there is none, the
extruding moves are projected like a mesh and drawn layer by layer, coloured by height.
"""

from __future__ import annotations

import base64
import contextlib
import io
import math
import re
import struct

from PIL import Image, ImageDraw

from ..card import finish, lerp, picture_card, vertical_gradient
from ..errors import Unsupported
from ..files import read_bytes, read_head, read_tail
from ..raster import BACKGROUND_BOTTOM, BACKGROUND_TOP, project

SCAN_BYTES = 2 * 1024 * 1024
MAX_TOOLPATH_BYTES = 80 * 1024 * 1024
MAX_SEGMENTS = 160_000
MAX_COLLECTED = 600_000  # moves kept while reading; a file of arcs must not fill memory
BEGIN = re.compile(rb"^;\s*(thumbnail\w*)\s+begin\s+(\d+)x(\d+)", re.IGNORECASE)
LOW, HIGH = (72, 118, 214), (247, 150, 52)


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    if name.lower().endswith(".bgcode"):
        picture = _bgcode_picture(path)
    else:
        picture = _embedded_picture(path)
    if picture is not None:
        return picture_card(picture, size)
    if name.lower().endswith(".bgcode"):
        raise Unsupported("binary G-code without a preview")
    return _toolpath(path, size)


def _decode(kind: str, payload: bytes) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(payload))
        image.load()
        return image
    except (OSError, ValueError):
        return None


def _thumbnail_blocks(blob: bytes) -> list[tuple[int, bytes]]:
    """(pixels, decoded bytes) of every ``; thumbnail begin WxH N`` ... ``; thumbnail end`` block in ``blob``.

    Line by line on purpose: a block without its end line must cost time linear in the file, never more."""
    found: list[tuple[int, bytes]] = []
    pixels = 0
    name = b""
    chunks: list[bytes] = []
    inside = False
    for line in blob.splitlines():
        text = line.strip()
        begin = BEGIN.match(text)
        if begin:
            inside, name, pixels, chunks = True, begin.group(1).lower(), int(begin.group(2)) * int(begin.group(3)), []
        elif inside and text.lower().startswith(b";") and text[1:].strip().lower() == name + b" end":
            try:
                found.append((pixels, base64.b64decode(b"".join(chunks))))
            except ValueError:
                pass
            inside = False
        elif inside:
            if not text.startswith(b";") or len(chunks) > 20_000:
                inside = False
            else:
                chunks.append(text[1:].strip())
    return found


def _embedded_picture(path: str) -> Image.Image | None:
    candidates = _thumbnail_blocks(read_head(path, SCAN_BYTES)) + _thumbnail_blocks(read_tail(path, SCAN_BYTES))
    for _, payload in sorted(candidates, key=lambda item: -item[0]):
        image = _decode("", payload)
        if image is not None:
            return image
    return None


def _bgcode_picture(path: str) -> Image.Image | None:
    data = read_head(path, SCAN_BYTES)
    if data[:4] != b"GCDE" or len(data) < 10:
        raise Unsupported("not binary G-code")
    checksum = struct.unpack_from("<H", data, 8)[0]
    position = 10
    best: tuple[int, bytes] | None = None
    while position + 8 <= len(data):
        kind, compression, size = struct.unpack_from("<HHI", data, position)
        position += 8
        stored = size
        if compression:
            (stored,) = struct.unpack_from("<I", data, position)
            position += 4
        params = 6 if kind == 5 else 2
        if kind == 5:
            width, height = struct.unpack_from("<HH", data, position + 2)
            payload = data[position + params : position + params + stored]
            if best is None or width * height > best[0]:
                best = (width * height, payload)
        elif kind == 4:
            break  # G-code blocks come after every thumbnail
        position += params + stored + (4 if checksum == 1 else 0)
    if best is None:
        return None
    return _decode("", best[1])


# ---- toolpath ------------------------------------------------------------------------------------------------


class _Toolhead:
    """The position and extruder state a G-code stream changes; ``segments`` collects the extruding moves."""

    def __init__(self) -> None:
        self.x = self.y = self.z = self.e = 0.0
        self.absolute = True
        self.relative_e = False
        self.segments: list[tuple[float, float, float, float, float]] = []

    def command(self, words: list[str]) -> None:
        name = words[0]
        if name in MOVES:
            self._move(words[1:], arc=name if name in ARCS else "")
        elif name in MODES:
            setattr(self, MODES[name][0], MODES[name][1])
        elif name == "G92":
            self._set_extruder(words[1:])

    def _set_extruder(self, words: list[str]) -> None:
        for word in words:
            if word.startswith("E"):
                with contextlib.suppress(ValueError):
                    self.e = float(word[1:])

    def _move(self, words: list[str], arc: str = "") -> None:
        target = {"X": self.x, "Y": self.y, "Z": self.z}
        offsets = {"I": 0.0, "J": 0.0}
        extruder: float | None = None
        for word in words:
            try:
                value = float(word[1:])
            except ValueError:
                continue
            axis = word[0]
            if axis in target:
                target[axis] = value if self.absolute else target[axis] + value
            elif axis == "E":
                extruder = value
            elif axis in offsets:
                offsets[axis] = value
        if extruder is not None and self._extrudes(extruder):
            self._deposit(target, arc, offsets)
        self.x, self.y, self.z = target["X"], target["Y"], target["Z"]

    def _deposit(self, target: dict[str, float], arc: str, offsets: dict[str, float]) -> None:
        """Record the extruding move to ``target``: a line, or an arc (G2 clockwise, G3 counter-clockwise) about
        the point offset by I, J from the start; an arc whose end is its start is a full circle."""
        if len(self.segments) >= MAX_COLLECTED:
            return  # the lowest layers are what has been read so far: enough for a picture
        start = (self.x, self.y)
        end = (target["X"], target["Y"])
        if not arc:
            if end != start:
                self.segments.append((*start, *end, target["Z"]))
            return
        radius = math.hypot(offsets["I"], offsets["J"])
        if radius == 0:
            return
        cx, cy = self.x + offsets["I"], self.y + offsets["J"]
        first = math.atan2(self.y - cy, self.x - cx)
        last = math.atan2(end[1] - cy, end[0] - cx)
        sweep = last - first
        if arc == "G2":  # clockwise: the angle decreases
            sweep = sweep - 2 * math.pi if sweep >= 0 else sweep
        elif sweep <= 0:
            sweep += 2 * math.pi
        steps = max(8, min(72, int(abs(sweep) * radius / 0.8)))
        previous = start
        for step in range(1, steps + 1):
            angle = first + sweep * step / steps
            point = (cx + radius * math.cos(angle), cy + radius * math.sin(angle)) if step < steps else end
            self.segments.append((*previous, *point, target["Z"]))
            previous = point

    def _extrudes(self, extruder: float) -> bool:
        pushed = extruder if self.relative_e else extruder - self.e
        if not self.relative_e:
            self.e = extruder
        return pushed > 0


MOVES = ("G0", "G1", "G2", "G3")
ARCS = ("G2", "G3")
MODES = {"G90": ("absolute", True), "G91": ("absolute", False), "M82": ("relative_e", False), "M83": ("relative_e", True)}


def _segments(text: str) -> list[tuple[float, float, float, float, float]]:
    """Extruding moves as (x0, y0, x1, y1, z)."""
    head = _Toolhead()
    for raw in text.splitlines():
        words = raw.partition(";")[0].split()
        if words:
            head.command(words)
    return head.segments


def _toolpath(path: str, size: int) -> Image.Image:
    text = read_bytes(path, MAX_TOOLPATH_BYTES).decode("ascii", "ignore")
    segments = _segments(text)
    if len(segments) < 10:
        raise Unsupported("no extruding moves")
    stride = max(1, -(-len(segments) // MAX_SEGMENTS))
    segments = segments[::stride]
    supersample = 3 if size <= 256 else 2
    canvas = size * supersample
    points: list[float] = []
    for x0, y0, x1, y1, z in segments:
        points.extend((x0, y0, z, x1, y1, z))
    screen_x, screen_y, _ = project(points, canvas)
    low = min(s[4] for s in segments)
    high = max(s[4] for s in segments)
    image = vertical_gradient(canvas, canvas, BACKGROUND_TOP, BACKGROUND_BOTTOM)
    draw = ImageDraw.Draw(image)
    width = max(1, round(canvas / 220 * min(3, stride * 0.5 + 1)))
    order = sorted(range(len(segments)), key=lambda index: segments[index][4])
    for index in order:
        fraction = (segments[index][4] - low) / (high - low) if high > low else 0.5
        draw.line(
            [(screen_x[2 * index], screen_y[2 * index]), (screen_x[2 * index + 1], screen_y[2 * index + 1])],
            fill=lerp(LOW, HIGH, fraction),
            width=width,
        )
    return finish(image.resize((size, size), Image.LANCZOS))

"""STEP (ISO 10303-21) and IGES: the B-rep edges drawn as a wireframe.

There is no CAD kernel in the thumbnailer sandbox, so surfaces are not tessellated. STEP edges (``EDGE_CURVE``:
straight lines, circle and ellipse arcs, B-spline control polygons) and IGES curve entities (110 line, 100 arc,
126 B-spline) are drawn in the same three-quarter view as meshes: enough to recognise a part.
"""

from __future__ import annotations

import math
import re

from PIL import Image, ImageDraw

from ..card import finish, lerp, vertical_gradient
from ..errors import Unsupported
from ..files import read_text
from ..raster import BACKGROUND_BOTTOM, BACKGROUND_TOP, project

Point = tuple[float, float, float]
Polyline = list[Point]
MAX_POLYLINES = 60_000
ARC_STEPS = 28
INK_NEAR, INK_FAR = (38, 52, 78), (132, 146, 170)
NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    text = read_text(path)
    polylines = _iges(text) if name.lower().endswith((".igs", ".iges")) else _step(text)
    if len(polylines) < 3:
        raise Unsupported("no curves found")
    return draw_wireframe(polylines[:MAX_POLYLINES], size)


def draw_wireframe(polylines: list[Polyline], size: int) -> Image.Image:
    supersample = 3 if size <= 256 else 2
    canvas = size * supersample
    points = [value for line in polylines for point in line for value in point]
    screen_x, screen_y, depth = project(points, canvas)
    near, far = min(depth), max(depth)
    image = vertical_gradient(canvas, canvas, BACKGROUND_TOP, BACKGROUND_BOTTOM)
    draw = ImageDraw.Draw(image)
    width = max(1, round(canvas / 190))
    index = 0
    segments: list[tuple[float, int, int]] = []
    for line in polylines:
        for start in range(len(line) - 1):
            segments.append(((depth[index + start] + depth[index + start + 1]) / 2, index + start, index + start + 1))
        index += len(line)
    segments.sort(reverse=True)  # far first, so near edges stay on top
    for mean_depth, a, b in segments:
        fraction = (mean_depth - near) / (far - near) if far > near else 0
        draw.line([(screen_x[a], screen_y[a]), (screen_x[b], screen_y[b])], fill=lerp(INK_NEAR, INK_FAR, fraction), width=width)
    return finish(image.resize((size, size), Image.LANCZOS))


# ---- STEP ----------------------------------------------------------------------------------------------------


def _step(text: str) -> list[Polyline]:
    entities: dict[int, tuple[str, str]] = {}
    body = text.split("DATA;", 1)[-1]
    for statement in body.split(";"):
        match = re.match(r"\s*#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)\s*$", statement, re.DOTALL)
        if match:
            entities[int(match.group(1))] = (match.group(2), match.group(3))

    def numbers(arguments: str) -> list[float]:
        return [float(v) for v in re.findall(NUMBER, arguments)]

    def reference(token: str) -> int:
        return int(token.strip().lstrip("#"))

    def point(identifier: int) -> Point | None:
        entity = entities.get(identifier)
        if entity and entity[0] == "CARTESIAN_POINT":
            values = numbers(entity[1].split("(", 1)[-1])
            return (values[0], values[1], values[2]) if len(values) >= 3 else None
        return None

    def vertex(identifier: int) -> Point | None:
        entity = entities.get(identifier)
        return point(reference(entity[1].split(",")[-1])) if entity and entity[0] == "VERTEX_POINT" else None

    def direction(identifier: int) -> Point | None:
        entity = entities.get(identifier)
        if entity and entity[0] == "DIRECTION":
            values = numbers(entity[1].split("(", 1)[-1])
            return (values[0], values[1], values[2]) if len(values) >= 3 else None
        return None

    def frame(identifier: int) -> tuple[Point, Point, Point] | None:
        """origin, x axis, y axis of an AXIS2_PLACEMENT_3D."""
        entity = entities.get(identifier)
        if not entity or entity[0] != "AXIS2_PLACEMENT_3D":
            return None
        parts = [p for p in re.findall(r"#\d+|\$", entity[1])]
        origin = point(reference(parts[0])) if parts and parts[0] != "$" else None
        axis = direction(reference(parts[1])) if len(parts) > 1 and parts[1] != "$" else (0.0, 0.0, 1.0)
        ref = direction(reference(parts[2])) if len(parts) > 2 and parts[2] != "$" else None
        if origin is None or axis is None:
            return None
        if ref is None:
            ref = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
        x_axis = _normalise(_reject(ref, axis))
        y_axis = _cross(axis, x_axis)
        return origin, x_axis, y_axis

    polylines: list[Polyline] = []
    for kind_name, arguments in entities.values():
        if kind_name != "EDGE_CURVE":
            continue
        tokens = re.findall(r"#\d+|\.[TF]\.", arguments)
        if len(tokens) < 4:
            continue
        start, end = vertex(reference(tokens[0])), vertex(reference(tokens[1]))
        if start is None or end is None:
            continue
        curve_name, curve_args = entities.get(reference(tokens[2]), ("", ""))
        same_sense = tokens[3] != ".F." if len(tokens) > 3 else True
        line: Polyline | None = None
        if curve_name in ("CIRCLE", "ELLIPSE"):
            placement = re.findall(r"#\d+", curve_args)
            axes = frame(reference(placement[0])) if placement else None
            radii = numbers(curve_args.split(placement[0], 1)[-1]) if placement else []
            if axes and radii:
                line = _arc(axes, radii[0], radii[1] if len(radii) > 1 else radii[0], start, end, same_sense)
        elif curve_name == "B_SPLINE_CURVE_WITH_KNOTS" or curve_name.startswith("B_SPLINE"):
            controls = [p for p in (point(reference(t)) for t in re.findall(r"#\d+", curve_args)) if p]
            if len(controls) >= 2:
                line = controls
        polylines.append(line or [start, end])
    return polylines


def _arc(axes, radius: float, minor: float, start: Point, end: Point, same_sense: bool) -> Polyline:
    origin, x_axis, y_axis = axes

    def angle(p: Point) -> float:
        offset = (p[0] - origin[0], p[1] - origin[1], p[2] - origin[2])
        return math.atan2(_dot(offset, y_axis) / (minor or 1.0), _dot(offset, x_axis) / (radius or 1.0))

    a0, a1 = angle(start), angle(end)
    if math.dist(start, end) < 1e-9 * max(radius, 1.0):
        a1 = a0 + 2 * math.pi
    else:
        if not same_sense:
            a0, a1 = a1, a0
        while a1 <= a0:
            a1 += 2 * math.pi
    return [_on_ellipse(origin, x_axis, y_axis, radius, minor, a0 + (a1 - a0) * k / ARC_STEPS) for k in range(ARC_STEPS + 1)]


def _on_ellipse(origin: Point, x_axis: Point, y_axis: Point, a: float, b: float, theta: float) -> Point:
    c, s = a * math.cos(theta), b * math.sin(theta)
    return (origin[0] + c * x_axis[0] + s * y_axis[0], origin[1] + c * x_axis[1] + s * y_axis[1], origin[2] + c * x_axis[2] + s * y_axis[2])


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Point, b: Point) -> Point:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _reject(v: Point, axis: Point) -> Point:
    """``v`` with its component along ``axis`` removed."""
    n = _normalise(axis)
    d = _dot(v, n)
    return (v[0] - d * n[0], v[1] - d * n[1], v[2] - d * n[2])


def _normalise(v: Point) -> Point:
    length = math.sqrt(_dot(v, v)) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


# ---- IGES ----------------------------------------------------------------------------------------------------


def _iges(text: str) -> list[Polyline]:
    directory: dict[int, int] = {}
    parameter_lines: dict[int, list[str]] = {}
    delimiter = ","
    lines = text.splitlines()
    de_index = 0
    for line in lines:
        if len(line) < 73:
            continue
        section = line[72]
        if section == "D" and int(line[73:80] or 0) % 2 == 1:
            de_index = int(line[73:80])
            directory[de_index] = int(line[0:8])
        elif section == "P":
            pointer = int(line[64:72] or 0)
            parameter_lines.setdefault(pointer, []).append(line[:64])
    polylines: list[Polyline] = []
    for pointer, entity in directory.items():
        record = "".join(parameter_lines.get(pointer, [])).rstrip().rstrip(";")
        values = [v.strip() for v in record.split(delimiter)]
        try:
            numbers = [float(v.replace("D", "E").replace("d", "e")) for v in values[1:] if v]
        except ValueError:
            continue
        if entity == 110 and len(numbers) >= 6:
            polylines.append([(numbers[0], numbers[1], numbers[2]), (numbers[3], numbers[4], numbers[5])])
        elif entity == 100 and len(numbers) >= 7:
            polylines.append(_iges_arc(numbers))
        elif entity == 126 and len(numbers) >= 8:
            line = _iges_spline(numbers)
            if line:
                polylines.append(line)
    return polylines


def _iges_arc(n: list[float]) -> Polyline:
    z, cx, cy, sx, sy, ex, ey = n[:7]
    radius = math.hypot(sx - cx, sy - cy)
    a0, a1 = math.atan2(sy - cy, sx - cx), math.atan2(ey - cy, ex - cx)
    while a1 <= a0:
        a1 += 2 * math.pi
    return [(cx + radius * math.cos(a0 + (a1 - a0) * k / ARC_STEPS), cy + radius * math.sin(a0 + (a1 - a0) * k / ARC_STEPS), z) for k in range(ARC_STEPS + 1)]


def _iges_spline(n: list[float]) -> Polyline | None:
    count, degree = int(n[0]), int(n[1])
    knots = count + degree + 2  # T(0..N+2M+1) with N = 1 + K - M
    start = 6 + knots + (count + 1)
    controls = n[start : start + 3 * (count + 1)]
    if len(controls) < 6:
        return None
    return [(controls[i], controls[i + 1], controls[i + 2]) for i in range(0, len(controls) - 2, 3)]

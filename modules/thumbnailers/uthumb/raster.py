"""Software renderer for a triangle soup: orthographic three-quarter view, flat Lambert shading, painter's order.

No GPU is needed (the thumbnailer sandbox has no ``/dev/dri``): the triangles are projected in plain Python,
sorted far to near and filled with Pillow at 2-3x resolution, then scaled down for anti-aliasing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from PIL import Image, ImageDraw

from .card import Rgb, finish, lerp, vertical_gradient
from .errors import Unsupported
from .geometry import DEFAULT_COLOR, MAX_TRIANGLES, Mesh

BACKGROUND_TOP: Rgb = (236, 240, 246)
BACKGROUND_BOTTOM: Rgb = (201, 209, 221)
AZIMUTH = math.radians(-38)
ELEVATION = math.radians(28)
SIN_A, COS_A = math.sin(AZIMUTH), math.cos(AZIMUTH)
SIN_E, COS_E = math.sin(ELEVATION), math.cos(ELEVATION)
# Direction towards the lights in view space (x right, y up, z away from the camera), unit length.
KEY_LIGHT = (-0.42, 0.62, -0.66)
FILL_LIGHT = (0.74, -0.18, -0.65)
FILL_COLORS_SCALE = 0.22
AMBIENT = 0.30
FILL_FRACTION = 0.82  # the model's longest screen side as a part of the card


def _to_z_up(coords: Sequence[float], up: str) -> list[float]:
    """The same points with +z up. glTF-like data is +y up with -z forward: (x, y, z) -> (x, -z, y)."""
    if up == "z":
        return list(coords)
    out = list(coords)
    for index in range(0, len(out), 3):
        y, z = out[index + 1], out[index + 2]
        out[index + 1], out[index + 2] = -z, y
    return out


def _sample(mesh: Mesh, limit: int) -> tuple[list[float], list[Rgb], int]:
    """At most ``limit`` evenly spread triangles (flat floats, nine per triangle), their colours and the stride."""
    count = mesh.triangle_count
    stride = max(1, math.ceil(count / limit))
    if stride == 1:
        return list(mesh.coords), list(mesh.colors), 1
    coords: list[float] = []
    colors: list[Rgb] = []
    for tri in range(0, count, stride):
        coords.extend(mesh.coords[9 * tri : 9 * tri + 9])
        if mesh.colors:
            colors.append(mesh.colors[tri])
    return coords, colors, stride


def project(points: list[float], size: int) -> tuple[list[float], list[float], list[float]]:
    """Screen x, screen y (down) and depth (larger = farther) per vertex, fitted into ``size`` pixels."""
    xs: list[float] = []
    ys: list[float] = []
    ds: list[float] = []
    for index in range(0, len(points), 3):
        x, y, z = points[index], points[index + 1], points[index + 2]
        x1 = x * COS_A - y * SIN_A
        y1 = x * SIN_A + y * COS_A
        xs.append(x1)
        ys.append(z * COS_E + y1 * SIN_E)
        ds.append(y1 * COS_E - z * SIN_E)
    if not xs:
        raise Unsupported("no vertices")
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    span = max(max_x - min_x, max_y - min_y)
    if span <= 0 or not math.isfinite(span):
        raise Unsupported("degenerate model")
    scale = size * FILL_FRACTION / span
    cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
    half = size / 2
    screen_x = [half + (x - cx) * scale for x in xs]
    screen_y = [half - (y - cy) * scale for y in ys]
    return screen_x, screen_y, ds


MAX_NEIGHBOURS = 24
CREASE_COS = math.cos(math.radians(25))  # edges sharper than this stay crisp, gentler ones are smoothed


def _face_normals(points: list[float]) -> list[tuple[float, float, float]]:
    """Unit normal per triangle; (0, 0, 0) for a degenerate one."""
    normals: list[tuple[float, float, float]] = []
    for base in range(0, len(points), 9):
        ax, ay, az = points[base], points[base + 1], points[base + 2]
        ux, uy, uz = points[base + 3] - ax, points[base + 4] - ay, points[base + 5] - az
        vx, vy, vz = points[base + 6] - ax, points[base + 7] - ay, points[base + 8] - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        normals.append((nx, ny, nz))  # area-weighted: the length is twice the area
    return normals


def _smoothed_normals(points: list[float]) -> list[tuple[float, float, float]]:
    """Per-triangle shading normals: smooth where the surface is smooth, crisp across creases.

    Vertices are welded by position (soups such as STL repeat them). A triangle's normal at a corner is the sum of
    the normals of the triangles around that corner that lie within the crease angle of the triangle itself,
    weighted by area; the three corner normals are averaged.
    """
    faces = _face_normals(points)
    xs, ys, zs = points[0::3], points[1::3], points[2::3]
    diagonal = math.dist((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))) or 1.0
    quantum = diagonal * 1e-5
    around: dict[tuple[int, int, int], list[int]] = {}
    keys: list[tuple[int, int, int]] = []
    for index in range(0, len(points), 3):
        key = (round(points[index] / quantum), round(points[index + 1] / quantum), round(points[index + 2] / quantum))
        keys.append(key)
        neighbours = around.setdefault(key, [])
        if len(neighbours) < MAX_NEIGHBOURS:  # a hub vertex (a fan of 90k triangles) must not make this quadratic
            neighbours.append(index // 9)
    units: list[tuple[float, float, float]] = []
    for fx, fy, fz in faces:
        length = math.sqrt(fx * fx + fy * fy + fz * fz)
        units.append((fx / length, fy / length, fz / length) if length else (0.0, 0.0, 0.0))
    result: list[tuple[float, float, float]] = []
    for tri, (ux, uy, uz) in enumerate(units):
        if ux == 0 and uy == 0 and uz == 0:
            result.append(units[tri])
            continue
        sx = sy = sz = 0.0
        for corner in range(3):
            for other in around[keys[3 * tri + corner]]:
                ox, oy, oz = units[other]
                if ux * ox + uy * oy + uz * oz > CREASE_COS:
                    fx, fy, fz = faces[other]
                    sx += fx
                    sy += fy
                    sz += fz
        length = math.sqrt(sx * sx + sy * sy + sz * sz)
        result.append((sx / length, sy / length, sz / length) if length else (ux, uy, uz))
    return result


def _shade(normal: tuple[float, float, float]) -> float:
    """Lambert intensity under the key and fill lights, two-sided; -1 for a degenerate triangle."""
    nx, ny, nz = normal
    if nx == 0 and ny == 0 and nz == 0:
        return -1.0
    x1 = nx * COS_A - ny * SIN_A
    y1 = nx * SIN_A + ny * COS_A
    vx, vy, vz = x1, nz * COS_E + y1 * SIN_E, y1 * COS_E - nz * SIN_E
    key = abs(vx * KEY_LIGHT[0] + vy * KEY_LIGHT[1] + vz * KEY_LIGHT[2])
    fill = abs(vx * FILL_LIGHT[0] + vy * FILL_LIGHT[1] + vz * FILL_LIGHT[2])
    return min(1.0, AMBIENT + 0.72 * key + FILL_COLORS_SCALE * fill)


def render_points(mesh: Mesh, size: int) -> Image.Image:
    """A point cloud as soft dots, far ones paler, on the studio gradient."""
    supersample = 3 if size <= 256 else 2
    canvas = size * supersample
    points = _to_z_up(mesh.points, mesh.up)
    screen_x, screen_y, depth = project(points, canvas)
    near, far = min(depth), max(depth)
    image = vertical_gradient(canvas, canvas, BACKGROUND_TOP, BACKGROUND_BOTTOM)
    draw = ImageDraw.Draw(image)
    radius = max(1.0, canvas / 150)
    for index in sorted(range(len(depth)), key=depth.__getitem__, reverse=True):
        fraction = (depth[index] - near) / (far - near) if far > near else 0.0
        base = mesh.point_colors[index] if mesh.point_colors else DEFAULT_COLOR
        shade = 1.0 - 0.45 * fraction
        fill = (int(base[0] * shade), int(base[1] * shade), int(base[2] * shade))
        x, y = screen_x[index], screen_y[index]
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
    return finish(image.resize((size, size), Image.LANCZOS))


def render(mesh: Mesh, size: int) -> Image.Image:
    """The shaded model on a soft studio gradient, as a rounded RGBA card of ``size`` x ``size`` pixels."""
    mesh.require_content()
    if mesh.triangle_count == 0:
        return render_points(mesh, size)
    supersample = 3 if size <= 256 else 2
    canvas = size * supersample
    sampled, colors, stride = _sample(mesh, MAX_TRIANGLES)
    grow = min(2.4, math.sqrt(stride))  # sampled triangles are enlarged so the skipped ones leave no holes
    points = _to_z_up(sampled, mesh.up)
    screen_x, screen_y, depth = project(points, canvas)
    count = len(points) // 9
    normals = _smoothed_normals(points)
    order: list[tuple[float, int]] = []
    for tri in range(count):
        i = 3 * tri
        order.append(((depth[i] + depth[i + 1] + depth[i + 2]) / 3, tri))
    order.sort(reverse=True)

    image = vertical_gradient(canvas, canvas, BACKGROUND_TOP, BACKGROUND_BOTTOM)
    draw = ImageDraw.Draw(image)
    _ground_shadow(draw, screen_x, screen_y)
    for _, tri in order:
        light = _shade(normals[tri])
        if light < 0:
            continue
        base = colors[tri] if colors else DEFAULT_COLOR
        fill = (min(255, int(base[0] * light)), min(255, int(base[1] * light)), min(255, int(base[2] * light)))
        i = 3 * tri
        corners = [(screen_x[i], screen_y[i]), (screen_x[i + 1], screen_y[i + 1]), (screen_x[i + 2], screen_y[i + 2])]
        if grow > 1:
            mx = (corners[0][0] + corners[1][0] + corners[2][0]) / 3
            my = (corners[0][1] + corners[1][1] + corners[2][1]) / 3
            corners = [(mx + (px - mx) * grow, my + (py - my) * grow) for px, py in corners]
        draw.polygon(corners, fill=fill, outline=fill)
    return finish(image.resize((size, size), Image.LANCZOS))


def _ground_shadow(draw: ImageDraw.ImageDraw, xs: list[float], ys: list[float]) -> None:
    """A soft elliptical contact shadow under the model's footprint."""
    left, right, bottom = min(xs), max(xs), max(ys)
    width = right - left
    height = width * 0.13
    centre = (left + right) / 2
    for step in range(6, 0, -1):
        grow = 1 + step * 0.09
        shade = lerp(BACKGROUND_BOTTOM, (120, 130, 148), 0.045 * (7 - step))
        draw.ellipse(
            (
                centre - width * grow / 2,
                bottom - height * grow / 2,
                centre + width * grow / 2,
                bottom + height * grow / 2,
            ),
            fill=shade,
        )

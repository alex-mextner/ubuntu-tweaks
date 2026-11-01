"""Triangle soup plus the few matrix helpers every mesh format parser shares.

Matrices are 12-tuples, row-major 3x4 acting on column vectors: ``(a, b, c, tx,  d, e, f, ty,  g, h, i, tz)``.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field

from .errors import Unsupported

Matrix = tuple[float, float, float, float, float, float, float, float, float, float, float, float]
Color = tuple[int, int, int]

DEFAULT_COLOR: Color = (126, 160, 205)  # steel blue: the model colour when a format carries none
IDENTITY: Matrix = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
# Beyond this the renderer samples triangles: a thumbnail has at most ~260k pixels.
MAX_TRIANGLES = 90_000


@dataclass
class Mesh:
    """Triangles as nine floats each (x, y, z for three corners); ``up`` is the axis the model stands on."""

    up: str = "z"
    coords: array = field(default_factory=lambda: array("f"))
    colors: list[Color] = field(default_factory=list)  # one per triangle, or empty: the renderer's default colour
    points: array = field(default_factory=lambda: array("f"))  # a point cloud: x, y, z per point, when no faces
    point_colors: list[Color] = field(default_factory=list)

    @property
    def triangle_count(self) -> int:
        return len(self.coords) // 9

    def add_indexed(
        self,
        positions: list[float] | array,
        faces: list[int],
        matrix: Matrix | None = None,
        color: Color | None = None,
    ) -> None:
        """Append triangles given as vertex indices (three per triangle) into a flat ``positions`` list."""
        count = len(positions) // 3
        before = self.triangle_count
        transform = matrix is not None and matrix != IDENTITY
        added = 0
        coords = self.coords
        for start in range(0, len(faces) - 2, 3):
            a, b, c = faces[start], faces[start + 1], faces[start + 2]
            if not (0 <= a < count and 0 <= b < count and 0 <= c < count):
                continue
            for index in (a, b, c):
                x, y, z = positions[3 * index : 3 * index + 3]
                if transform:
                    x, y, z = apply(matrix, x, y, z)
                coords.extend((x, y, z))
            added += 1
        if color is not None or self.colors:
            # colours stay parallel to the triangles: parts without one get the default
            self.colors.extend([DEFAULT_COLOR] * (before - len(self.colors)))
            self.colors.extend([color or DEFAULT_COLOR] * added)

    def require_content(self) -> Mesh:
        """Triangles, or failing that a point cloud; anything else is not a model."""
        if self.triangle_count == 0 and len(self.points) >= 9:
            return self
        return self.require_triangles()

    def require_triangles(self) -> Mesh:
        if self.triangle_count == 0:
            raise Unsupported("no triangles")
        if self.colors and len(self.colors) != self.triangle_count:
            self.colors = []
        return self


def apply(matrix: Matrix, x: float, y: float, z: float) -> tuple[float, float, float]:
    a, b, c, tx, d, e, f, ty, g, h, i, tz = matrix
    return a * x + b * y + c * z + tx, d * x + e * y + f * z + ty, g * x + h * y + i * z + tz


def multiply(left: Matrix, right: Matrix) -> Matrix:
    """``left`` after ``right``: the matrix that applies ``right`` first."""
    a, b, c, tx, d, e, f, ty, g, h, i, tz = left
    p, q, r, ux, s, t, u, uy, v, w, x, uz = right
    return (
        a * p + b * s + c * v, a * q + b * t + c * w, a * r + b * u + c * x, a * ux + b * uy + c * uz + tx,
        d * p + e * s + f * v, d * q + e * t + f * w, d * r + e * u + f * x, d * ux + e * uy + f * uz + ty,
        g * p + h * s + i * v, g * q + h * t + i * w, g * r + h * u + i * x, g * ux + h * uy + i * uz + tz,
    )  # fmt: skip


def from_column_major(values: list[float]) -> Matrix:
    """A 4x4 given column by column (glTF ``matrix``) as a Matrix."""
    return (
        values[0], values[4], values[8], values[12],
        values[1], values[5], values[9], values[13],
        values[2], values[6], values[10], values[14],
    )  # fmt: skip


def from_row_major(values: list[float]) -> Matrix:
    """A 4x4 given row by row (Collada ``<matrix>``) as a Matrix."""
    return tuple(values[:12])  # type: ignore[return-value]


def from_rows_3mf(values: list[float]) -> Matrix:
    """3MF's twelve numbers: three rows of a row-vector transform (x' = x*m00 + y*m10 + z*m20 + m30)."""
    m = values
    return (m[0], m[3], m[6], m[9], m[1], m[4], m[7], m[10], m[2], m[5], m[8], m[11])


def translation_rotation_scale(
    translate: tuple[float, float, float],
    quaternion: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> Matrix:
    """glTF TRS: scale, then rotate by the unit quaternion (x, y, z, w), then translate."""
    x, y, z, w = quaternion
    norm = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    sx, sy, sz = scale
    return (
        (1 - 2 * (y * y + z * z)) * sx, 2 * (x * y - z * w) * sy, 2 * (x * z + y * w) * sz, translate[0],
        2 * (x * y + z * w) * sx, (1 - 2 * (x * x + z * z)) * sy, 2 * (y * z - x * w) * sz, translate[1],
        2 * (x * z - y * w) * sx, 2 * (y * z + x * w) * sy, (1 - 2 * (x * x + y * y)) * sz, translate[2],
    )  # fmt: skip


def translation(x: float, y: float, z: float) -> Matrix:
    return (1.0, 0.0, 0.0, x, 0.0, 1.0, 0.0, y, 0.0, 0.0, 1.0, z)


def scaling(x: float, y: float, z: float) -> Matrix:
    return (x, 0.0, 0.0, 0.0, 0.0, y, 0.0, 0.0, 0.0, 0.0, z, 0.0)


def axis_angle_degrees(x: float, y: float, z: float, degrees: float) -> Matrix:
    """Rotation by ``degrees`` about the axis (x, y, z), which need not be normalised."""
    length = math.sqrt(x * x + y * y + z * z)
    if length == 0:
        return IDENTITY
    x, y, z = x / length, y / length, z / length
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    t = 1 - c
    return (
        t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0.0,
        t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0.0,
        t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0.0,
    )  # fmt: skip


def euler_xyz_degrees(rx: float, ry: float, rz: float) -> Matrix:
    """Rotation about X, then Y, then Z (FBX's default order), angles in degrees."""
    ax, ay, az = math.radians(rx), math.radians(ry), math.radians(rz)
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    rot_x: Matrix = (1, 0, 0, 0, 0, cx, -sx, 0, 0, sx, cx, 0)
    rot_y: Matrix = (cy, 0, sy, 0, 0, 1, 0, 0, -sy, 0, cy, 0)
    rot_z: Matrix = (cz, -sz, 0, 0, sz, cz, 0, 0, 0, 0, 1, 0)
    return multiply(rot_z, multiply(rot_y, rot_x))


def triangulate_polygons(indices: list[int], last_is_complemented: bool = False) -> list[int]:
    """Fan-triangulate a stream of polygons: each ends at a ``-1`` marker (VRML, X3D), or, with
    ``last_is_complemented`` (FBX), at a negative value that is the polygon's last index as ``~index``."""
    triangles: list[int] = []
    polygon: list[int] = []
    for value in indices:
        if value < 0:
            if last_is_complemented:
                polygon.append(~value)
            _fan(polygon, triangles)
            polygon = []
        else:
            polygon.append(value)
    _fan(polygon, triangles)
    return triangles


def _fan(polygon: list[int], out: list[int]) -> None:
    for k in range(1, len(polygon) - 1):
        out.extend((polygon[0], polygon[k], polygon[k + 1]))

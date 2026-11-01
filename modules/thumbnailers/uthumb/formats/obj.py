"""Wavefront OBJ. Geometry only (the .mtl beside it is out of the sandbox); +y is up by convention."""

from __future__ import annotations

from ..files import read_text
from ..geometry import Mesh, triangulate_polygons


def load(path: str) -> Mesh:
    positions: list[float] = []
    faces: list[int] = []
    for line in read_text(path).splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    positions.extend((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    positions.extend((0.0, 0.0, 0.0))
        elif line.startswith("f "):
            polygon = [_index(token, len(positions) // 3) for token in line.split()[1:]]
            polygon = [value for value in polygon if value >= 0]
            faces.extend(triangulate_polygons(polygon + [-1]))
    mesh = Mesh(up="y")
    mesh.add_indexed(positions, faces)
    return mesh.require_triangles()


def _index(token: str, vertex_count: int) -> int:
    """Zero-based vertex index of an ``f`` token (``7``, ``7/2``, ``7//3``, ``-1``), or -1 when unusable."""
    head = token.split("/", 1)[0]
    try:
        value = int(head)
    except ValueError:
        return -1
    return value - 1 if value > 0 else vertex_count + value

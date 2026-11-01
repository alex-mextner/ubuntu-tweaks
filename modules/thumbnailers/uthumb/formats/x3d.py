"""X3D (XML) and VRML97 (.wrl): IndexedFaceSet / IndexedTriangleSet geometry. +y is up."""

from __future__ import annotations

import re

from ..errors import Unsupported
from ..files import read_bytes
from ..geometry import Mesh, triangulate_polygons
from . import xmlutil

NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def load(path: str) -> Mesh:
    data = read_bytes(path)
    head = data[:200].lstrip()
    mesh = Mesh(up="y")
    if head.startswith(b"<"):
        _xml(xmlutil.parse(data), mesh)
    elif head.startswith(b"#VRML") or head.startswith(b"#X3D"):
        _vrml(data.decode("utf-8", "replace"), mesh)
    else:
        raise Unsupported("not X3D or VRML")
    return mesh.require_triangles()


def _xml(root, mesh: Mesh) -> None:
    for shape in xmlutil.walk(root, "IndexedFaceSet"):
        coordinate = next(xmlutil.walk(shape, "Coordinate"), None)
        if coordinate is not None:
            faces = triangulate_polygons(xmlutil.ints(shape.get("coordIndex")))
            mesh.add_indexed(xmlutil.floats(coordinate.get("point")), faces)
    for shape in xmlutil.walk(root, "IndexedTriangleSet"):
        coordinate = next(xmlutil.walk(shape, "Coordinate"), None)
        if coordinate is not None:
            mesh.add_indexed(xmlutil.floats(coordinate.get("point")), xmlutil.ints(shape.get("index")))


def _vrml(text: str, mesh: Mesh) -> None:
    """VRML97 puts the points inside the IndexedFaceSet; VRML 1.0 declares ``Coordinate3`` before it."""
    version_one = text.startswith("#VRML V1")
    sections = text.split("Coordinate3")[1:] if version_one else text.split("IndexedFaceSet")[1:]
    for block in sections:
        points = re.search(r"point\s*\[([^\]]*)\]", block)
        indices = re.search(r"coordIndex\s*\[([^\]]*)\]", block)
        if points and indices:
            values = [float(v) for v in re.findall(NUMBER, points.group(1))]
            order = [int(float(v)) for v in re.findall(NUMBER, indices.group(1))]
            mesh.add_indexed(values, triangulate_polygons(order))

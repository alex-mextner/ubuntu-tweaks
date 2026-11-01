"""Collada (.dae): geometry libraries placed by the visual scene's node transforms."""

from __future__ import annotations

from ..errors import Unsupported
from ..files import read_bytes
from ..geometry import (
    IDENTITY,
    Matrix,
    Mesh,
    axis_angle_degrees,
    from_row_major,
    multiply,
    scaling,
    translation,
    triangulate_polygons,
)
from . import xmlutil

MAX_DEPTH = 32


def load(path: str) -> Mesh:
    root = xmlutil.parse(read_bytes(path))
    up = next((element.text or "" for element in xmlutil.walk(root, "up_axis")), "Y_UP").strip().upper()
    geometries = {geometry.get("id", ""): _geometry(geometry) for geometry in xmlutil.walk(root, "geometry")}
    mesh = Mesh(up={"Z_UP": "z", "X_UP": "z"}.get(up, "y"))
    scenes = list(xmlutil.walk(root, "visual_scene"))
    placed = False
    for scene in scenes:
        for node in scene:
            if xmlutil.local(node.tag) == "node":
                placed |= _node(node, IDENTITY, geometries, mesh, 0)
    if not placed:  # no scene: draw every geometry where it is
        for positions, faces in geometries.values():
            mesh.add_indexed(positions, faces)
    return mesh.require_triangles()


def _node(node, parent: Matrix, geometries: dict, mesh: Mesh, depth: int) -> bool:
    if depth > MAX_DEPTH:
        return False
    matrix = parent
    placed = False
    for child in node:
        tag = xmlutil.local(child.tag)
        if tag in TRANSFORMS:
            matrix = multiply(matrix, _transform(tag, xmlutil.floats(child.text)))
        elif tag == "instance_geometry":
            target = (child.get("url") or "").lstrip("#")
            if target in geometries:
                positions, faces = geometries[target]
                mesh.add_indexed(positions, faces, matrix)
                placed = True
        elif tag == "node":
            placed |= _node(child, matrix, geometries, mesh, depth + 1)
    return placed


TRANSFORMS = {"matrix", "translate", "rotate", "scale"}


def _transform(tag: str, values: list[float]) -> Matrix:
    """One transform element of a node; applied in document order, each on the right of the ones before it."""
    if tag == "matrix" and len(values) == 16:
        return from_row_major(values)
    if tag == "translate" and len(values) == 3:
        return translation(*values)
    if tag == "scale" and len(values) == 3:
        return scaling(*values)
    if tag == "rotate" and len(values) == 4:
        return axis_angle_degrees(*values)
    return IDENTITY


def _geometry(geometry) -> tuple[list[float], list[int]]:
    sources = {source.get("id", ""): xmlutil.floats(next(xmlutil.walk(source, "float_array"), None).text) for source in xmlutil.walk(geometry, "source") if next(xmlutil.walk(source, "float_array"), None) is not None}
    vertex_sources: dict[str, str] = {}
    for vertices in xmlutil.walk(geometry, "vertices"):
        for entry in vertices:
            if entry.get("semantic") == "POSITION":
                vertex_sources[vertices.get("id", "")] = (entry.get("source") or "").lstrip("#")
    positions: list[float] = []
    faces: list[int] = []
    for primitive in geometry.iter():
        tag = xmlutil.local(primitive.tag)
        if tag not in {"triangles", "polylist", "polygons"}:
            continue
        inputs = [entry for entry in primitive if xmlutil.local(entry.tag) == "input"]
        vertex_input = next((entry for entry in inputs if entry.get("semantic") == "VERTEX"), None)
        if vertex_input is None:
            continue
        source_name = vertex_sources.get((vertex_input.get("source") or "").lstrip("#"))
        source = sources.get(source_name or "")
        if source is None:
            continue
        stride = max(int(entry.get("offset", 0)) for entry in inputs) + 1
        offset = int(vertex_input.get("offset", 0))
        base = len(positions) // 3
        positions.extend(source)
        faces.extend(_faces(primitive, tag, stride, offset, base))
    if not positions:
        raise Unsupported("no geometry")
    return positions, faces


def _faces(primitive, tag: str, stride: int, offset: int, base: int) -> list[int]:
    if tag == "triangles":
        indices = xmlutil.ints(next((p.text for p in primitive if xmlutil.local(p.tag) == "p"), ""))
        return [base + indices[i] for i in range(offset, len(indices), stride)]
    if tag == "polylist":
        counts = xmlutil.ints(next((p.text for p in primitive if xmlutil.local(p.tag) == "vcount"), ""))
        indices = xmlutil.ints(next((p.text for p in primitive if xmlutil.local(p.tag) == "p"), ""))
        corners = [indices[i] for i in range(offset, len(indices), stride)]
        out: list[int] = []
        cursor = 0
        for count in counts:
            out.extend(triangulate_polygons([base + c for c in corners[cursor : cursor + count]] + [-1]))
            cursor += count
        return out
    out = []
    for polygon in primitive:
        if xmlutil.local(polygon.tag) == "p":
            indices = xmlutil.ints(polygon.text)  # parsed once per polygon
            out.extend(triangulate_polygons([base + c for c in indices[offset::stride]] + [-1]))
    return out

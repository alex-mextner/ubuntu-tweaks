"""AMF (Additive Manufacturing Format): plain or zip-compressed XML; vertices and triangles per mesh. +z is up."""

from __future__ import annotations

import zipfile

from ..errors import Unsupported
from ..files import MAX_TEXT_BYTES, read_bytes, read_member
from ..geometry import Mesh
from . import xmlutil


def load(path: str) -> Mesh:
    data = read_bytes(path)
    if data[:2] == b"PK":
        with zipfile.ZipFile(path) as archive:
            member = next((n for n in archive.namelist() if n.lower().endswith(".amf")), None)
            if member is None:
                raise Unsupported("no AMF in the archive")
            data = read_member(archive, member, MAX_TEXT_BYTES)
    root = xmlutil.parse(data)
    mesh = Mesh(up="z")
    for element in xmlutil.walk(root, "mesh"):
        positions: list[float] = []
        for vertex in xmlutil.walk(element, "vertex"):
            coordinates = next(xmlutil.walk(vertex, "coordinates"), None)
            if coordinates is None:
                continue
            values = {xmlutil.local(child.tag): float(child.text or 0) for child in coordinates}
            positions.extend((values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)))
        faces: list[int] = []
        for triangle in xmlutil.walk(element, "triangle"):
            corners = {xmlutil.local(child.tag): int(child.text or 0) for child in triangle}
            faces.extend((corners.get("v1", 0), corners.get("v2", 0), corners.get("v3", 0)))
        mesh.add_indexed(positions, faces)
    return mesh.require_triangles()

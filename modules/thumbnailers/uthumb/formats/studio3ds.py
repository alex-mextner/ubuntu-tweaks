"""Autodesk 3D Studio (.3ds): a tree of ``u16 id, u32 length`` chunks; meshes are TRI_MESH chunks. +z is up."""

from __future__ import annotations

import struct

from ..errors import Unsupported
from ..files import MAX_BINARY_BYTES, read_bytes
from ..geometry import Mesh

MAIN, EDITOR, OBJECT, TRIMESH, VERTICES, FACES = 0x4D4D, 0x3D3D, 0x4000, 0x4100, 0x4110, 0x4120
CONTAINERS = {MAIN, EDITOR, TRIMESH}


def load(path: str) -> Mesh:
    data = read_bytes(path, MAX_BINARY_BYTES)
    if struct.unpack_from("<H", data, 0)[0] != MAIN:
        raise Unsupported("not a 3DS file")
    mesh = Mesh(up="z")
    _chunks(data, 6, len(data), mesh, 0)
    return mesh.require_triangles()


def _chunks(data: bytes, start: int, end: int, mesh: Mesh, depth: int) -> None:
    position = start
    if depth > 8:
        return
    local_vertices: list[float] = []
    faces: list[int] = []
    while position + 6 <= end:
        chunk_id, length = struct.unpack_from("<HI", data, position)
        if length < 6:
            break
        body = position + 6
        stop = min(position + length, end)
        if chunk_id == OBJECT:
            name_end = data.index(b"\x00", body) + 1
            _chunks(data, name_end, stop, mesh, depth + 1)
        elif chunk_id in CONTAINERS:
            _chunks(data, body, stop, mesh, depth + 1)
        elif chunk_id == VERTICES:
            (count,) = struct.unpack_from("<H", data, body)
            local_vertices = list(struct.unpack_from(f"<{3 * count}f", data, body + 2))
        elif chunk_id == FACES:
            (count,) = struct.unpack_from("<H", data, body)
            raw = struct.unpack_from(f"<{4 * count}H", data, body + 2)
            faces = [raw[i] for k in range(count) for i in (4 * k, 4 * k + 1, 4 * k + 2)]
        position += length
    if local_vertices and faces:
        mesh.add_indexed(local_vertices, faces)

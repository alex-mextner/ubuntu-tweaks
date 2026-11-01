"""STL, binary and ASCII. 3D-printing data: +z is up."""

from __future__ import annotations

import math
import mmap
import re
import struct
from array import array

from ..errors import Unsupported
from ..files import MAX_TEXT_BYTES
from ..geometry import MAX_TRIANGLES, Mesh

HEADER = 84
RECORD = 50
VERTEX = re.compile(rb"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)")


def load(path: str) -> Mesh:
    with open(path, "rb") as handle:
        size = handle.seek(0, 2)
        if size < HEADER:
            raise Unsupported("too short for STL")
        data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            (count,) = struct.unpack_from("<I", data, 80)
            if HEADER + RECORD * count == size:
                return _binary(data, count)
            if data[:5].lstrip().lower().startswith(b"solid") or b"facet" in data[:2048]:
                if size > MAX_TEXT_BYTES:
                    raise Unsupported("ASCII STL too large")
                return _ascii(data[:])
            if HEADER + RECORD * count <= size:  # trailing padding after a binary body
                return _binary(data, count)
            raise Unsupported("not an STL")
        finally:
            data.close()


def _binary(data: mmap.mmap, count: int) -> Mesh:
    mesh = Mesh(up="z")
    stride = max(1, math.ceil(count / MAX_TRIANGLES))
    unpack = struct.Struct("<9f").unpack_from
    coords = mesh.coords
    for index in range(0, count, stride):
        coords.extend(unpack(data, HEADER + RECORD * index + 12))
    if not all(math.isfinite(value) for value in coords[:300]):
        raise Unsupported("corrupt STL")
    return mesh.require_triangles()


def _ascii(raw: bytes) -> Mesh:
    values = VERTEX.findall(raw)
    triangles = len(values) // 3
    stride = max(1, math.ceil(triangles / MAX_TRIANGLES))
    coords = array("f")
    for tri in range(0, triangles, stride):
        for x, y, z in values[3 * tri : 3 * tri + 3]:
            coords.extend((float(x), float(y), float(z)))
    mesh = Mesh(up="z", coords=coords)
    return mesh.require_triangles()

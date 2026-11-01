"""glTF 2.0 (.glb binary container, .gltf JSON with embedded buffers). +y is up.

External ``.bin`` buffers are outside the thumbnailer sandbox (only the one input file is visible), so a ``.gltf``
that points at them cannot be drawn; glb and data-URI files can. Draco/meshopt-compressed primitives are skipped.
"""

from __future__ import annotations

import base64
import json
import struct
from array import array

from ..errors import Unsupported
from ..files import MAX_BINARY_BYTES, read_bytes
from ..geometry import IDENTITY, Color, Matrix, Mesh, from_column_major, multiply, translation_rotation_scale

MAGIC = b"glTF"
COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
MAX_DEPTH = 32
MAX_VISITS = 20_000
MAX_TRIANGLES_TOTAL = 3_000_000


def load(path: str) -> Mesh:
    data = read_bytes(path, MAX_BINARY_BYTES)
    document, binary = _split(data)
    buffers = _buffers(document, binary)
    mesh = Mesh(up="y")
    walk = _Walk()
    scene = document.get("scenes", [{}])[document.get("scene", 0)] if document.get("scenes") else {}
    roots = scene.get("nodes") or _root_nodes(document)
    for index in roots:
        _visit(document, buffers, mesh, index, IDENTITY, walk)
    return mesh.require_triangles()


def _split(data: bytes) -> tuple[dict, bytes | None]:
    if data[:4] != MAGIC:
        try:
            return json.loads(data.decode("utf-8")), None
        except ValueError as error:
            raise Unsupported("not glTF") from error
    document: dict = {}
    binary: bytes | None = None
    position = 12
    while position + 8 <= len(data):
        length, kind = struct.unpack_from("<II", data, position)
        chunk = data[position + 8 : position + 8 + length]
        if kind == 0x4E4F534A:
            document = json.loads(chunk.decode("utf-8"))
        elif kind == 0x004E4942 and binary is None:
            binary = chunk
        position += 8 + length
    if not document:
        raise Unsupported("glb without JSON")
    return document, binary


def _buffers(document: dict, binary: bytes | None) -> list[bytes]:
    out: list[bytes] = []
    for entry in document.get("buffers", []):
        uri = entry.get("uri")
        if uri is None and binary is not None:
            out.append(binary)
        elif uri and uri.startswith("data:"):
            out.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            raise Unsupported("external buffer outside the sandbox")
    return out


def _root_nodes(document: dict) -> list[int]:
    nodes = document.get("nodes", [])
    children = {child for node in nodes for child in node.get("children", [])}
    return [index for index in range(len(nodes)) if index not in children]


def _node_matrix(node: dict) -> Matrix:
    if "matrix" in node:
        return from_column_major(node["matrix"])
    return translation_rotation_scale(
        tuple(node.get("translation", (0, 0, 0))),  # type: ignore[arg-type]
        tuple(node.get("rotation", (0, 0, 0, 1))),  # type: ignore[arg-type]
        tuple(node.get("scale", (1, 1, 1))),  # type: ignore[arg-type]
    )


class _Walk:
    """The scene walk's state: the nodes on the current path (a cycle is refused) and the work done so far."""

    def __init__(self) -> None:
        self.path: list[int] = []
        self.visits = 0


def _visit(document: dict, buffers: list[bytes], mesh: Mesh, index: int, parent: Matrix, walk: _Walk) -> None:
    nodes = document.get("nodes", [])
    if len(walk.path) > MAX_DEPTH or not 0 <= index < len(nodes) or index in walk.path:
        return
    walk.visits += 1
    if walk.visits > MAX_VISITS or mesh.triangle_count > MAX_TRIANGLES_TOTAL:
        raise Unsupported("scene too large for a thumbnail")
    node = nodes[index]
    walk.path.append(index)
    matrix = multiply(parent, _node_matrix(node))
    if "mesh" in node:
        for primitive in document.get("meshes", [])[node["mesh"]].get("primitives", []):
            _add_primitive(document, buffers, mesh, primitive, matrix)
    for child in node.get("children", []):
        _visit(document, buffers, mesh, child, matrix, walk)
    walk.path.pop()


def _add_primitive(document: dict, buffers: list[bytes], mesh: Mesh, primitive: dict, matrix: Matrix) -> None:
    if primitive.get("mode", 4) not in (4, 5, 6) or "POSITION" not in primitive.get("attributes", {}):
        return
    if "extensions" in primitive and any("draco" in key.lower() or "meshopt" in key.lower() for key in primitive["extensions"]):
        return
    positions = _accessor(document, buffers, primitive["attributes"]["POSITION"])
    if positions is None:
        return
    count = len(positions) // 3
    if "indices" in primitive:
        indices = list(_accessor(document, buffers, primitive["indices"]) or [])
    else:
        indices = list(range(count))
    mode = primitive.get("mode", 4)
    if mode == 5:
        indices = [v for i in range(len(indices) - 2) for v in ((indices[i], indices[i + 1], indices[i + 2]) if i % 2 == 0 else (indices[i + 1], indices[i], indices[i + 2]))]
    elif mode == 6:
        indices = [v for i in range(1, len(indices) - 1) for v in (indices[0], indices[i], indices[i + 1])]
    mesh.add_indexed(positions, indices, matrix, _color(document, primitive))


def _color(document: dict, primitive: dict) -> Color | None:
    material = primitive.get("material")
    if material is None:
        return None
    factor = document.get("materials", [])[material].get("pbrMetallicRoughness", {}).get("baseColorFactor")
    if not factor or len(factor) < 3:
        return None
    # glTF colours are linear; a gentle gamma keeps pale and dark materials recognisable on the preview
    return tuple(min(255, round((max(0.0, c) ** (1 / 2.2)) * 255)) for c in factor[:3])  # type: ignore[return-value]


def _accessor(document: dict, buffers: list[bytes], index: int) -> array | list | None:
    accessor = document["accessors"][index]
    view_index = accessor.get("bufferView")
    if view_index is None or accessor.get("sparse"):
        return None
    view = document["bufferViews"][view_index]
    data = buffers[view["buffer"]]
    code, size = COMPONENT[accessor["componentType"]]
    width = WIDTH[accessor["type"]]
    stride = view.get("byteStride") or size * width
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    count = accessor["count"]
    scale = _normalisation(accessor, code)
    if stride == size * width and scale is None:
        out = array(code)
        chunk = data[start : start + count * size * width]
        if out.itemsize != size:
            return list(struct.unpack(f"<{count * width}{code}", chunk))
        out.frombytes(chunk)
        if struct.pack("=H", 1) != struct.pack("<H", 1):
            out.byteswap()
        return out
    row = struct.Struct(f"<{width}{code}")
    values: list = []
    for item in range(count):
        offset = start + item * stride
        if offset + row.size > len(data):
            break  # a few exporters leave the last element's padding off the end of the buffer
        values.extend(row.unpack_from(data, offset))
    if scale is not None:
        values = [max(value * scale, -1.0) for value in values]
    return values


def _normalisation(accessor: dict, code: str) -> float | None:
    """Factor turning a normalized integer accessor (KHR_mesh_quantization) into floats, else None."""
    if not accessor.get("normalized") or code in ("f", "I"):
        return None
    return 1.0 / {"b": 127, "B": 255, "h": 32767, "H": 65535}[code]

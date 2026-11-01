"""Autodesk FBX: binary (node records, zlib-compressed arrays) and ASCII; geometry placed by the Model hierarchy.

Only what a thumbnail needs: ``Geometry`` vertices and polygon indices, and each ``Model``'s local translation,
rotation (XYZ Euler) and scaling, followed up the parent chain through the ``Connections``.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field

from ..errors import Unsupported
from ..files import MAX_BINARY_BYTES, read_bytes
from ..geometry import IDENTITY, Matrix, Mesh, euler_xyz_degrees, multiply, triangulate_polygons

MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"
ARRAY_TYPES = {"f": "f", "d": "d", "l": "q", "i": "i", "b": "?"}
SCALAR_TYPES = {"Y": "h", "C": "?", "I": "i", "F": "f", "D": "d", "L": "q"}
MAX_NODES = 2_000_000
MAX_ARRAY_BYTES = 384 * 1024 * 1024


@dataclass
class Node:
    name: str
    props: list = field(default_factory=list)
    children: list[Node] = field(default_factory=list)

    def child(self, name: str) -> Node | None:
        return next((c for c in self.children if c.name == name), None)

    def all(self, name: str) -> list[Node]:
        return [c for c in self.children if c.name == name]


def load(path: str) -> Mesh:
    data = read_bytes(path, MAX_BINARY_BYTES)
    if data.startswith(MAGIC):
        return _from_nodes(_binary_nodes(data))
    if data.lstrip().startswith(b";") or b"FBXHeaderExtension" in data[:4096]:
        return _ascii(data.decode("utf-8", "replace"))
    raise Unsupported("not an FBX file")


# ---- binary --------------------------------------------------------------------------------------------------


def _binary_nodes(data: bytes) -> list[Node]:
    (version,) = struct.unpack_from("<I", data, 23)
    wide = version >= 7500
    budget = [MAX_NODES, MAX_ARRAY_BYTES]  # nodes left, array bytes left
    nodes, _ = _read_nodes(data, 27, len(data), wide, budget)
    return nodes


def _read_nodes(data: bytes, position: int, limit: int, wide: bool, budget: list[int]) -> tuple[list[Node], int]:
    header = struct.Struct("<QQQB") if wide else struct.Struct("<IIIB")
    nodes: list[Node] = []
    while position + header.size <= limit:
        end, count, _length, name_length = header.unpack_from(data, position)
        if end == 0:
            return nodes, position + header.size
        budget[0] -= 1
        if budget[0] < 0 or end > len(data):
            raise Unsupported("FBX too large or corrupt")
        name = data[position + header.size : position + header.size + name_length].decode("latin-1")
        cursor = position + header.size + name_length
        node = Node(name)
        for _ in range(count):
            value, cursor = _property(data, cursor, budget)
            node.props.append(value)
        if cursor < end:
            node.children, _ = _read_nodes(data, cursor, end, wide, budget)
        nodes.append(node)
        position = end
    return nodes, position


def _property(data: bytes, cursor: int, budget: list[int]):
    kind = chr(data[cursor])
    cursor += 1
    if kind in SCALAR_TYPES:
        spec = struct.Struct("<" + SCALAR_TYPES[kind])
        return spec.unpack_from(data, cursor)[0], cursor + spec.size
    if kind in ARRAY_TYPES:
        length, encoding, compressed = struct.unpack_from("<III", data, cursor)
        cursor += 12
        code = ARRAY_TYPES[kind]
        size = struct.calcsize(code)
        expected = length * size
        budget[1] -= expected
        if budget[1] < 0:
            raise Unsupported("FBX arrays exceed the memory budget")
        if encoding == 1:
            inflater = zlib.decompressobj()
            raw = inflater.decompress(data[cursor : cursor + compressed], expected)  # never more than declared
            if inflater.unconsumed_tail or len(raw) != expected:
                raise Unsupported("FBX array does not match its declared size")
            cursor += compressed
        else:
            raw = data[cursor : cursor + expected]
            cursor += expected
        if len(raw) != expected:
            raise Unsupported("truncated FBX array")
        return list(struct.unpack(f"<{length}{code}", raw)), cursor
    if kind in ("S", "R"):
        (length,) = struct.unpack_from("<I", data, cursor)
        cursor += 4
        value = data[cursor : cursor + length]
        return (value.decode("utf-8", "replace") if kind == "S" else value), cursor + length
    raise Unsupported(f"unknown FBX property {kind!r}")


# ---- scene ---------------------------------------------------------------------------------------------------


def _from_nodes(nodes: list[Node]) -> Mesh:
    objects = next((n for n in nodes if n.name == "Objects"), None)
    if objects is None:
        raise Unsupported("FBX without objects")
    up = _up_axis(nodes)
    geometries: dict[int, tuple[list[float], list[int]]] = {}
    models: dict[int, Matrix] = {}
    for node in objects.children:
        if not node.props:
            continue
        identifier = node.props[0]
        if node.name == "Geometry":
            vertices, polygons = node.child("Vertices"), node.child("PolygonVertexIndex")
            if vertices and polygons and vertices.props and polygons.props:
                geometries[identifier] = (vertices.props[0], triangulate_polygons(polygons.props[0], True))
        elif node.name == "Model":
            models[identifier] = _local_matrix(node)
    parents = _connections(nodes)
    mesh = Mesh(up=up)
    for identifier, (positions, faces) in geometries.items():
        mesh.add_indexed(positions, faces, _world(identifier, parents, models))
    return mesh.require_triangles()


def _up_axis(nodes: list[Node]) -> str:
    settings = next((n for n in nodes if n.name == "GlobalSettings"), None)
    if settings is not None:
        properties = settings.child("Properties70")
        for prop in (properties.children if properties else []):
            if prop.props and prop.props[0] == "UpAxis" and len(prop.props) >= 5:
                return "z" if prop.props[4] == 2 else "y"
    return "y"


def _local_matrix(model: Node) -> Matrix:
    translate = (0.0, 0.0, 0.0)
    rotate = (0.0, 0.0, 0.0)
    scale = (1.0, 1.0, 1.0)
    properties = model.child("Properties70")
    for prop in (properties.children if properties else []):
        if len(prop.props) < 7:
            continue
        values = tuple(float(v) for v in prop.props[4:7])
        if prop.props[0] == "Lcl Translation":
            translate = values
        elif prop.props[0] == "Lcl Rotation":
            rotate = values
        elif prop.props[0] == "Lcl Scaling":
            scale = values
    rotation = euler_xyz_degrees(*rotate)
    scaled = multiply(rotation, (scale[0], 0, 0, 0, 0, scale[1], 0, 0, 0, 0, scale[2], 0))
    return (*scaled[:3], translate[0], *scaled[4:7], translate[1], *scaled[8:11], translate[2])  # type: ignore[return-value]


def _connections(nodes: list[Node]) -> dict[int, int]:
    """child id -> parent id for object-to-object ("OO") connections."""
    connections = next((n for n in nodes if n.name == "Connections"), None)
    parents: dict[int, int] = {}
    for link in (connections.children if connections else []):
        if len(link.props) >= 3 and link.props[0] == "OO" and link.props[1] not in parents:
            parents[link.props[1]] = link.props[2]
    return parents


def _world(identifier: int, parents: dict[int, int], models: dict[int, Matrix]) -> Matrix:
    matrix = IDENTITY
    current = parents.get(identifier)
    for _ in range(64):
        if current is None:
            break
        if current in models:
            matrix = multiply(models[current], matrix)
        current = parents.get(current)
    return matrix


# ---- ascii ---------------------------------------------------------------------------------------------------


def _ascii(text: str) -> Mesh:
    mesh = Mesh(up="y")
    for block in re.split(r"\n\s*Geometry:", text)[1:]:
        vertices = re.search(r"Vertices:\s*\*\d+\s*\{\s*a:\s*([^}]*)\}", block)
        polygons = re.search(r"PolygonVertexIndex:\s*\*\d+\s*\{\s*a:\s*([^}]*)\}", block)
        if vertices and polygons:
            positions = [float(v) for v in vertices.group(1).replace("\n", "").split(",") if v.strip()]
            order = [int(v) for v in polygons.group(1).replace("\n", "").split(",") if v.strip()]
            mesh.add_indexed(positions, triangulate_polygons(order, True))
    return mesh.require_triangles()

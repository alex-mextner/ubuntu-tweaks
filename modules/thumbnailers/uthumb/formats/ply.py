"""Stanford PLY: ASCII, binary little/big endian; vertices with optional colours, polygon faces."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..errors import Unsupported
from ..files import MAX_BINARY_BYTES, read_bytes
from ..geometry import Color, Mesh, triangulate_polygons

MAX_ELEMENTS = 700_000
MAX_POINTS = 40_000
TYPES = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B", "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I", "float": "f", "float32": "f", "double": "d", "float64": "d",
}  # fmt: skip


@dataclass
class Element:
    name: str
    count: int
    properties: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)
    # (name, scalar type, list count type, ...) ; a list property has a count type


def load(path: str) -> Mesh:
    data = read_bytes(path, MAX_BINARY_BYTES)
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise Unsupported("not a PLY file")
    body_start = data.index(b"\n", end) + 1
    header = data[:body_start].decode("ascii", "replace")
    elements, fmt = _parse_header(header)
    if any(element.count > MAX_ELEMENTS for element in elements):
        raise Unsupported("too many elements for a thumbnail")
    reader = _AsciiReader(data[body_start:]) if fmt == "ascii" else _BinaryReader(data[body_start:], fmt)
    positions: list[float] = []
    colors: list[Color] = []
    faces: list[int] = []
    for element in elements:
        if element.name == "vertex":
            _read_vertices(reader, element, positions, colors)
        elif element.name == "face":
            _read_faces(reader, element, faces)
        else:
            _skip(reader, element)
    mesh = Mesh(up="z")
    mesh.add_indexed(positions, faces)
    if not faces and len(positions) >= 9:
        step = max(1, len(positions) // 3 // MAX_POINTS)
        for index in range(0, len(positions) // 3, step):
            mesh.points.extend(positions[3 * index : 3 * index + 3])
            if colors:
                mesh.point_colors.append(colors[index])
        return mesh
    if colors and mesh.triangle_count and mesh.triangle_count == len(faces) // 3:
        mesh.colors = _face_colors(faces, colors, mesh.triangle_count)
    return mesh.require_triangles()


def _parse_header(header: str) -> tuple[list[Element], str]:
    fmt = "ascii"
    elements: list[Element] = []
    for line in header.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            elements.append(Element(parts[1], int(parts[2])))
        elif parts[0] == "property" and elements:
            if parts[1] == "list":
                elements[-1].properties.append((parts[4], parts[3], parts[2], None))
            else:
                elements[-1].properties.append((parts[2], parts[1], None, None))
    if fmt not in {"ascii", "binary_little_endian", "binary_big_endian"}:
        raise Unsupported("unknown PLY format")
    return elements, fmt


class _AsciiReader:
    def __init__(self, body: bytes) -> None:
        self.tokens = body.split()
        self.position = 0

    def read(self, kind: str) -> float | int:
        token = self.tokens[self.position]
        self.position += 1
        return float(token) if kind in ("f", "d") else int(token)


class _BinaryReader:
    def __init__(self, body: bytes, fmt: str) -> None:
        self.body = body
        self.position = 0
        prefix = "<" if fmt == "binary_little_endian" else ">"
        self.specs = {kind: struct.Struct(prefix + kind) for kind in set(TYPES.values())}

    def read(self, kind: str) -> float | int:
        spec = self.specs[kind]
        (value,) = spec.unpack_from(self.body, self.position)
        self.position += spec.size
        return value


def _read_vertices(reader: _AsciiReader | _BinaryReader, element: Element, positions: list[float], colors: list[Color]) -> None:
    names = [prop[0] for prop in element.properties]
    has_color = {"red", "green", "blue"} <= set(names)
    for _ in range(element.count):
        row: dict[str, float | int] = {}
        for name, kind, count_kind, _ in element.properties:
            if count_kind:
                for _item in range(int(reader.read(TYPES[count_kind]))):
                    reader.read(TYPES[kind])
            else:
                row[name] = reader.read(TYPES[kind])
        positions.extend((row.get("x", 0.0), row.get("y", 0.0), row.get("z", 0.0)))
        if has_color:
            colors.append((int(row["red"]), int(row["green"]), int(row["blue"])))


def _read_faces(reader: _AsciiReader | _BinaryReader, element: Element, faces: list[int]) -> None:
    for _ in range(element.count):
        for name, kind, count_kind, _ in element.properties:
            if count_kind:
                items = [int(reader.read(TYPES[kind])) for _ in range(int(reader.read(TYPES[count_kind])))]
                if name in ("vertex_indices", "vertex_index"):
                    faces.extend(triangulate_polygons(items + [-1]))
            else:
                reader.read(TYPES[kind])


def _skip(reader: _AsciiReader | _BinaryReader, element: Element) -> None:
    for _ in range(element.count):
        for _name, kind, count_kind, _ in element.properties:
            if count_kind:
                for _item in range(int(reader.read(TYPES[count_kind]))):
                    reader.read(TYPES[kind])
            else:
                reader.read(TYPES[kind])


def _face_colors(faces: list[int], colors: list[Color], count: int) -> list[Color]:
    """A triangle's colour is the mean of its corners' vertex colours."""
    out: list[Color] = []
    for tri in range(count):
        corners = [faces[3 * tri + k] for k in range(3)]
        if any(c >= len(colors) or c < 0 for c in corners):
            return []
        out.append(tuple(sum(colors[c][k] for c in corners) // 3 for k in range(3)))  # type: ignore[arg-type]
    return out


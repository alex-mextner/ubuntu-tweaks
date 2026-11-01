"""Object File Format (OFF, COFF, NOFF): a header, ``vertices faces edges``, a line per vertex, a line per face."""

from __future__ import annotations

from ..errors import Unsupported
from ..files import read_text
from ..geometry import Mesh, triangulate_polygons


def load(path: str) -> Mesh:
    lines = [line.split("#", 1)[0].strip() for line in read_text(path).splitlines()]
    lines = [line for line in lines if line]
    if not lines or "OFF" not in lines[0].split()[0]:
        raise Unsupported("not an OFF file")  # OFF, COFF, NOFF, CNOFF, 4OFF ...
    rest = lines[0].split()[1:]  # "OFF 8 12 0" can be all on one line
    if rest:
        counts, body = rest, lines[1:]
    else:
        counts, body = lines[1].split(), lines[2:]
    try:
        vertex_count, face_count = int(counts[0]), int(counts[1])
        positions: list[float] = []
        for line in body[:vertex_count]:
            positions.extend(float(value) for value in line.split()[:3])
        faces: list[int] = []
        for line in body[vertex_count : vertex_count + face_count]:
            numbers = line.split()
            corners = int(numbers[0])
            faces.extend(triangulate_polygons([int(value) for value in numbers[1 : 1 + corners]] + [-1]))
    except (ValueError, IndexError) as error:
        raise Unsupported(f"malformed OFF: {error}") from error
    mesh = Mesh(up="z")
    mesh.add_indexed(positions, faces)
    return mesh.require_triangles()

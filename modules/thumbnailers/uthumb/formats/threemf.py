"""3MF: the slicer's own preview picture when the package carries one, else the meshes of its build plate.

Orca, Bambu Studio, PrusaSlicer and the Snapmaker slicers write ``Metadata/plate_1.png`` or ``thumbnail.png``;
files from CAD tools carry none and are drawn from ``3D/3dmodel.model`` and the part files it references.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

from PIL import Image

from ..card import picture_card
from ..errors import Unsupported
from ..files import read_member
from ..geometry import Color, IDENTITY, Matrix, Mesh, from_rows_3mf, multiply
from ..raster import render as render_mesh
from . import xmlutil

MAX_MEMBER = 80 * 1024 * 1024
MAX_RELATIONSHIPS = 1024 * 1024
MAX_INSTANCES = 20_000
MAX_TRIANGLES_TOTAL = 3_000_000
MAX_DEPTH = 8
THUMBNAIL_RELATION = "metadata/thumbnail"
PREFERRED_PICTURES = (
    "Metadata/plate_1.png", "Metadata/thumbnail.png", "Metadata/plate_no_light_1.png", "Metadata/top_1.png",
    "Metadata/Thumbnail.png", "Thumbnails/thumbnail.png", "Metadata/thumbnail.jpg",
)  # fmt: skip


@dataclass
class Part:
    positions: list[float] = field(default_factory=list)
    faces: list[int] = field(default_factory=list)
    color: Color | None = None
    components: list[tuple[str, str, Matrix]] = field(default_factory=list)  # (model path, object id, transform)


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    with zipfile.ZipFile(path) as package:
        picture = embedded_picture(package)
        if picture is not None:
            return picture_card(picture, size)
        return render_mesh(load_mesh(package), size)


def embedded_picture(package: zipfile.ZipFile) -> Image.Image | None:
    names = set(package.namelist())
    for target in _relationship_targets(package):
        if target in names:
            image = _open_image(package, target)
            if image is not None:
                return image
    for candidate in PREFERRED_PICTURES:
        if candidate in names:
            image = _open_image(package, candidate)
            if image is not None:
                return image
    return None


def _relationship_targets(package: zipfile.ZipFile) -> list[str]:
    try:
        root = xmlutil.parse(read_member(package, "_rels/.rels", MAX_RELATIONSHIPS))
    except Unsupported:
        return []
    targets = []
    for relation in xmlutil.walk(root, "Relationship"):
        if relation.get("Type", "").lower().endswith(THUMBNAIL_RELATION):
            targets.append((relation.get("Target") or "").lstrip("/"))
    return targets


def _open_image(package: zipfile.ZipFile, member: str) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(read_member(package, member, MAX_MEMBER)))
        image.load()
    except (OSError, ValueError, Unsupported):
        return None
    return image


def load_mesh(package: zipfile.ZipFile) -> Mesh:
    root_path = _model_path(package)
    models: dict[str, tuple[dict[str, Part], list[tuple[str, Matrix]]]] = {}

    def model(path: str):
        if path not in models:
            if path not in package.namelist():
                raise Unsupported(f"missing part {path}")
            models[path] = _parse_model(read_member(package, path, MAX_MEMBER))
        return models[path]

    _, build = model(root_path)
    mesh = Mesh(up="z")
    instances = [0]
    for object_id, transform in build:
        _emit(mesh, model, (root_path, object_id, transform, 0), instances)
    return mesh.require_triangles()


def _model_path(package: zipfile.ZipFile) -> str:
    try:
        root = xmlutil.parse(read_member(package, "_rels/.rels", MAX_RELATIONSHIPS))
        for relation in xmlutil.walk(root, "Relationship"):
            if relation.get("Type", "").endswith("/3dmodel"):
                return (relation.get("Target") or "").lstrip("/")
    except Unsupported:
        pass
    return "3D/3dmodel.model"


def _emit(mesh: Mesh, model, placed: tuple[str, str, Matrix, int], instances: list[int]) -> None:
    """Add one object instance (and its components) to ``mesh``; repeated references are counted, not trusted."""
    path, object_id, transform, depth = placed
    if depth > MAX_DEPTH:
        return
    instances[0] += 1
    if instances[0] > MAX_INSTANCES or mesh.triangle_count > MAX_TRIANGLES_TOTAL:
        raise Unsupported("model too large for a thumbnail")
    objects, _ = model(path)
    part = objects.get(object_id)
    if part is None:
        return
    if part.positions:
        mesh.add_indexed(part.positions, part.faces, transform, part.color)
    for component_path, component_id, component_matrix in part.components:
        _emit(mesh, model, (component_path or path, component_id, multiply(transform, component_matrix), depth + 1), instances)


def _parse_model(data: bytes) -> tuple[dict[str, Part], list[tuple[str, Matrix]]]:
    root = xmlutil.parse(data)
    colors = _material_colors(root)
    objects: dict[str, Part] = {}
    for obj in xmlutil.walk(root, "object"):
        part = Part()
        color = colors.get((obj.get("pid"), obj.get("pindex") or "0"))
        part.color = color
        for child in obj:
            tag = xmlutil.local(child.tag)
            if tag == "mesh":
                _read_mesh(child, part)
            elif tag == "components":
                for component in child:
                    reference = component.get("{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path") or component.get("path")
                    target = reference.lstrip("/") if reference else ""
                    part.components.append((target, component.get("objectid", ""), _matrix(component.get("transform"))))
        objects[obj.get("id", "")] = part
    build = [
        (item.get("objectid", ""), _matrix(item.get("transform"))) for item in xmlutil.walk(root, "item")
    ]
    return objects, build


def _read_mesh(mesh_element, part: Part) -> None:
    for child in mesh_element:
        tag = xmlutil.local(child.tag)
        if tag == "vertices":
            for vertex in child:
                part.positions.extend((float(vertex.get("x", 0)), float(vertex.get("y", 0)), float(vertex.get("z", 0))))
        elif tag == "triangles":
            for triangle in child:
                part.faces.extend((int(triangle.get("v1", 0)), int(triangle.get("v2", 0)), int(triangle.get("v3", 0))))


def _matrix(text: str | None) -> Matrix:
    if not text:
        return IDENTITY
    values = xmlutil.floats(text)
    return from_rows_3mf(values) if len(values) == 12 else IDENTITY


def _material_colors(root) -> dict[tuple[str | None, str], Color]:
    """``(property group id, index)`` -> colour for base materials and colour groups."""
    colors: dict[tuple[str | None, str], Color] = {}
    for group in xmlutil.walk(root, "basematerials"):
        for index, base in enumerate(xmlutil.walk(group, "base")):
            colors[(group.get("id"), str(index))] = _hex(base.get("displaycolor"))
    for group in xmlutil.walk(root, "colorgroup"):
        for index, entry in enumerate(xmlutil.walk(group, "color")):
            colors[(group.get("id"), str(index))] = _hex(entry.get("color"))
    return {key: value for key, value in colors.items() if value is not None}  # type: ignore[misc]


def _hex(value: str | None) -> Color | None:
    value = (value or "").lstrip("#")
    if len(value) < 6:
        return None
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return None

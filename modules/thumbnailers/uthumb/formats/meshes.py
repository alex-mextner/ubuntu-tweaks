"""Mesh formats: pick the parser by extension, then shade the triangles."""

from __future__ import annotations

import importlib
from pathlib import Path

from PIL import Image

from .. import raster
from ..errors import Unsupported
from ..registry import Kind

LOADERS = {
    ".stl": "stl", ".obj": "obj", ".ply": "ply", ".off": "off", ".amf": "amf",
    ".glb": "gltf", ".gltf": "gltf", ".dae": "collada", ".x3d": "x3d", ".x3dv": "x3d", ".wrl": "x3d",
    ".vrml": "x3d", ".3ds": "studio3ds", ".fbx": "fbx",
}  # fmt: skip


def render(path: str, size: int, name: str, kind: Kind, mime: str) -> Image.Image:
    module = LOADERS.get(Path(name).suffix.lower())
    if module is None:
        raise Unsupported(f"no mesh parser for {name}")
    loader = importlib.import_module(f"uthumb.formats.{module}")
    return raster.render(loader.load(path), size)

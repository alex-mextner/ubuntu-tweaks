"""FreeCAD ``.FCStd``: a zip whose ``thumbnails/Thumbnail.png`` FreeCAD writes when the document is saved."""

from __future__ import annotations

import io
import zipfile

from PIL import Image

from ..card import picture_card
from ..errors import Unsupported
from ..files import read_member

MEMBER = "thumbnails/Thumbnail.png"
MAX_BYTES = 32 * 1024 * 1024


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    with zipfile.ZipFile(path) as package:
        if MEMBER not in package.namelist():
            raise Unsupported("FCStd without a thumbnail (saved with thumbnails disabled)")
        picture = Image.open(io.BytesIO(read_member(package, MEMBER, MAX_BYTES)))
        picture.load()
    return picture_card(picture, size, top=(48, 54, 66), bottom=(30, 34, 44))

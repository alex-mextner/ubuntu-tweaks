"""Blender ``.blend``: the preview image Blender stores in the file (the ``TEST`` block near its start).

Handles the plain, gzip and zstd (Blender 3.0+) containers and the old (12 byte) and new (``BLENDER17-01v0502``)
headers. Files saved by a headless script carry no preview and are reported as unsupported.
"""

from __future__ import annotations

import gzip
import struct
from typing import BinaryIO

from PIL import Image

from ..card import picture_card
from ..errors import Unsupported

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
MAX_SCAN = 16 * 1024 * 1024
MAX_PICTURE = 4096
MAX_PREVIEW_BYTES = 8 + MAX_PICTURE * MAX_PICTURE * 4


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    with open(path, "rb") as raw:
        picture = _preview(_open(raw))
    if picture is None:
        raise Unsupported("no preview stored in this .blend")
    return picture_card(picture, size)


def _open(raw: BinaryIO) -> BinaryIO:
    magic = raw.read(4)
    raw.seek(0)
    if magic == ZSTD_MAGIC:
        try:
            from compression import zstd  # Python 3.14+
        except ImportError as error:
            raise Unsupported("zstd-compressed .blend needs Python 3.14") from error
        return zstd.ZstdFile(raw)
    if magic[:2] == GZIP_MAGIC:
        return gzip.GzipFile(fileobj=raw)
    return raw


def _preview(stream: BinaryIO) -> Image.Image | None:
    head = stream.read(12)
    if not head.startswith(b"BLENDER"):
        raise Unsupported("not a .blend file")
    if head[7:9].isdigit():  # BLENDER17-01v0502: 17-byte header, 32-byte block headers, little endian
        header_size = int(head[7:9])
        if not 12 <= header_size <= 64:
            raise Unsupported("bad .blend header size")
        stream.read(header_size - 12)
        layout = struct.Struct("<4sIQqq")
    else:
        pointer_8 = head[7:8] == b"-"
        layout = struct.Struct("<4sIQII" if pointer_8 else "<4sIIII")
        if head[8:9] != b"v":
            layout = struct.Struct(str(layout.format).replace("<", ">"))  # big-endian file
    consumed = 0
    while consumed < MAX_SCAN:
        header = stream.read(layout.size)
        if len(header) < layout.size:
            return None
        fields = layout.unpack(header)
        code = fields[0]
        length = fields[3] if layout.size == 32 else fields[1]
        if code in (b"DNA1", b"ENDB") or length < 0:
            return None
        if code == b"TEST":
            return _decode(stream.read(length)) if length <= MAX_PREVIEW_BYTES else None
        consumed += layout.size + length
        if consumed > MAX_SCAN:  # the block would be read only to be skipped: stop before decompressing it
            return None
        stream.read(length)
    return None


def _decode(block: bytes) -> Image.Image | None:
    if len(block) < 8:
        return None
    width, height = struct.unpack_from("<ii", block)
    if not (0 < width <= MAX_PICTURE and 0 < height <= MAX_PICTURE) or len(block) < 8 + width * height * 4:
        return None
    image = Image.frombytes("RGBA", (width, height), block[8 : 8 + width * height * 4])
    return image.transpose(Image.FLIP_TOP_BOTTOM)  # stored bottom row first

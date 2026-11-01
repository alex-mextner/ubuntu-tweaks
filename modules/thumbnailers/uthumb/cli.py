"""Command line of the thumbnailer: ``uthumb INPUT OUTPUT SIZE [MIME]``, one PNG out, exit 1 when it cannot."""

from __future__ import annotations

import importlib
import os
import sys

from .errors import Unsupported
from .registry import Kind, kind_for, kind_for_mime

MAX_SIZE = 640  # the desktop asks for up to 1024; Files scales, and the render time is quadratic in it


def choose_kind(path: str, mime: str) -> Kind | None:
    return kind_for(os.path.basename(path)) or (kind_for_mime(mime) if mime else None)


def build(path: str, size: int, mime: str = ""):
    """The preview of ``path`` as a Pillow image, or ``Unsupported``."""
    kind = choose_kind(path, mime)
    if kind is None:
        raise Unsupported(f"no handler for {path} ({mime or 'unknown type'})")
    module_name, function = kind.handler.split(":")
    handler = getattr(importlib.import_module(module_name), function)
    return handler(path, max(32, min(size, MAX_SIZE)), os.path.basename(path), kind, mime)


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.stderr.write("usage: uthumb INPUT OUTPUT SIZE [MIME]\n")
        return 2
    path, output, size = argv[1], argv[2], int(argv[3])
    mime = argv[4] if len(argv) > 4 else ""
    try:
        image = build(path, size, mime)
        image.save(output, "PNG")
    except Unsupported as error:
        sys.stderr.write(f"uthumb: {error}\n")
        return 1
    except Exception as error:  # a hostile or truncated file may break any parser: report, never crash noisily
        sys.stderr.write(f"uthumb: {type(error).__name__}: {error}\n")
        return 1
    return 0

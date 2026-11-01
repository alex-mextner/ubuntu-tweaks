"""Packing the package into the ``Exec=`` line of a ``.thumbnailer`` file.

The sandbox shows a thumbnailer only ``/usr`` and its one input file (see ``uthumb/__init__``), so the code travels
inside the command: a deflated zip, base64-encoded, written next to the output PNG, imported with ``zipimport`` and
deleted at exit. ``%`` must not appear in the line (the desktop expands ``%i %o %s %m`` in it), and base64 has none.
"""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

from . import registry

PACKAGE = Path(__file__).resolve().parent
TIMESTAMP = (2026, 1, 1, 0, 0, 0)  # fixed, so the same sources always give the same bytes
PYTHON = "/usr/bin/python3"
# Run as ``python3 -c BOOTSTRAP INPUT OUTPUT SIZE MIME``: no ``%``, no double quote, no backslash, no ``$``.
BOOTSTRAP = (
    "import sys,os,base64,atexit,tempfile;"
    "f=os.path.join(tempfile.gettempdir(),'uthumb-'+str(os.getpid())+'.pyz');"
    "open(f,'wb').write(base64.b64decode('{payload}'));"
    "atexit.register(os.unlink,f);"
    "sys.path.insert(0,f);"
    "from uthumb.cli import main;"
    "sys.exit(main(['uthumb']+sys.argv[1:]))"
)


# Only needed to build the bundle and the MIME file, or by uthumb-cache outside the sandbox.
BUILD_ONLY = {"bundle.py", "mimedb.py", "cache.py"}


def source_files() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts and path.name not in BUILD_ONLY
    )


def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source_files():
            info = zipfile.ZipInfo(f"uthumb/{path.relative_to(PACKAGE).as_posix()}", TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return buffer.getvalue()


def exec_line() -> str:
    payload = base64.b64encode(zip_bytes()).decode("ascii")
    bootstrap = BOOTSTRAP.format(payload=payload)
    assert not any(ch in bootstrap for ch in '%"\\$`\n'), "bootstrap would be mangled by the desktop entry parser"
    return f'{PYTHON} -c "{bootstrap}" %i %o %s %m'


def thumbnailer_file() -> str:
    """The complete ``.thumbnailer`` (freedesktop thumbnailer entry) the installer writes."""
    return (
        "[Thumbnailer Entry]\n"
        f"TryExec={PYTHON}\n"
        f"Exec={exec_line()}\n"
        f"MimeType={';'.join(registry.MIME_TYPES)};\n"
    )


def woff2_thumbnailer_file() -> str:
    """gnome-font-viewer's own thumbnailer, which renders WOFF2 fine, listed for the types its entry forgets."""
    return (
        "[Thumbnailer Entry]\n"
        "TryExec=gnome-thumbnail-font\n"
        "Exec=gnome-thumbnail-font --size %s %u %o\n"
        "MimeType=font/woff2;application/font-woff2;\n"
    )

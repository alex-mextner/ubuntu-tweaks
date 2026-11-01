"""Writing into the freedesktop thumbnail cache from outside the sandbox.

Files fills ``~/.cache/thumbnails`` as folders are browsed; the dock only reads it. ``fill`` makes the same
entries on demand (a Downloads stack before its folder was ever opened) with the same renderers, following the
Thumbnail Managing Standard: ``<md5 of the file URI>.png`` in ``large`` (256 px), the URI, mtime and size in
text chunks so that Files treats the entry as its own and invalidates it when the file changes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import quote

from PIL import Image, PngImagePlugin

from . import cli
from .errors import Unsupported
from .registry import kind_for

SIZES = {"normal": 128, "large": 256, "x-large": 512}
SOFTWARE = "ubuntu-tweaks uthumb"


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "thumbnails"


def file_uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()), safe="/")


def entry_path(uri: str, size_name: str, root: Path | None = None) -> Path:
    return (root or cache_root()) / size_name / (hashlib.md5(uri.encode()).hexdigest() + ".png")


def is_current(entry: Path, source: Path) -> bool:
    """The cached entry exists and was made for this version of the file."""
    try:
        with Image.open(entry) as image:
            return image.text.get("Thumb::MTime") == str(int(source.stat().st_mtime))  # type: ignore[attr-defined]
    except (OSError, AttributeError, ValueError):
        return False


def fill(path: str | Path, size_name: str = "large", root: Path | None = None) -> Path | None:
    """Make (or keep) the cache entry of ``path``; None when no renderer covers it or the file cannot be drawn."""
    source = Path(path)
    if kind_for(source.name) is None or not source.is_file():
        return None
    uri = file_uri(source)
    entry = entry_path(uri, size_name, root)
    if is_current(entry, source):
        return entry
    try:
        image = cli.build(str(source), SIZES[size_name])
    except Unsupported:
        return None
    info = PngImagePlugin.PngInfo()
    info.add_text("Thumb::URI", uri)
    info.add_text("Thumb::MTime", str(int(source.stat().st_mtime)))
    info.add_text("Thumb::Size", str(source.stat().st_size))
    info.add_text("Software", SOFTWARE)
    entry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = entry.with_suffix(f".{os.getpid()}.tmp")
    image.save(temporary, "PNG", pnginfo=info)
    os.chmod(temporary, 0o600)
    os.replace(temporary, entry)
    return entry


def main(argv: list[str]) -> int:
    done = 0
    for name in argv[1:]:
        if fill(name) is not None:
            done += 1
    return 0 if done or len(argv) == 1 else 1

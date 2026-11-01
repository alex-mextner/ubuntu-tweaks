"""Shared code of the ``thumbnailers`` module: which files make Files, the file chooser and the dock show previews.

Accessed via: ``modules/thumbnailers/thumbnailers`` (check / status / plan / install, as the desktop user).
Spec: docs/specs/ubuntu-tweak-cli.md, *Thumbnailers*.

Everything lives under ``$HOME`` and needs no root: a thumbnailer entry in ``~/.local/share/thumbnailers`` (the
Python package ``uthumb`` is inside its ``Exec=`` line: GNOME's sandbox shows a thumbnailer only ``/usr``), the MIME
definitions the system lacks in ``~/.local/share/mime/packages``, and the stale "thumbnail failed" markers of the
types it now covers removed from ``~/.cache/thumbnails/fail``.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse

from ubuntu_tweaks.module_script import Finding, Module, NeedsManual, read_text_or_none, unified_diff_lines, write_file
from ubuntu_tweaks.process import Process
from uthumb import bundle, mimedb, registry

THUMBNAILER_DIR = ".local/share/thumbnailers"
MIME_DIR = ".local/share/mime"
FAIL_DIR = ".cache/thumbnails/fail"
SANDBOX_PACKAGES = {
    "python3-pil": "/usr/lib/python3/dist-packages/PIL",
    "python3-pygments": "/usr/lib/python3/dist-packages/pygments",
}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Names (not extensions) of the files whose previews we draw, to match failure markers for them too.
NAMED_FILES = {"dockerfile", "containerfile", "makefile", "gnumakefile"}


def png_text_chunks(data: bytes) -> dict[str, str]:
    """The tEXt chunks of a PNG (``Thumb::URI`` is how the thumbnail cache names the file it stands for)."""
    chunks: dict[str, str] = {}
    if not data.startswith(PNG_MAGIC):
        return chunks
    position = len(PNG_MAGIC)
    while position + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        body = data[position + 8 : position + 8 + length]
        if kind == b"tEXt" and b"\x00" in body:
            key, _, value = body.partition(b"\x00")
            chunks[key.decode("latin-1")] = value.decode("utf-8", "replace")
        elif kind == b"zTXt" and b"\x00" in body:
            key, _, rest = body.partition(b"\x00")
            try:
                chunks[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode("utf-8", "replace")
            except zlib.error:
                pass
        elif kind == b"IDAT":
            break
        position += 12 + length
    return chunks


def covered(uri: str) -> bool:
    """Whether a thumbnail of the file at ``uri`` is one of ours to make (decided by its name)."""
    name = Path(unquote(urlparse(uri).path)).name
    return registry.kind_for(name) is not None or name.lower() in NAMED_FILES


class Thumbnailers(Module):
    name = "thumbnailers"

    def __init__(self, home: Path, process: Process, dist_packages: Path = Path("/")) -> None:
        self._home = home
        self._process = process
        self._system = dist_packages

    # ---- what should exist ----

    def files(self) -> dict[Path, str]:
        thumbnailers = self._home / THUMBNAILER_DIR
        return {
            thumbnailers / "ubuntu-tweaks-previews.thumbnailer": bundle.thumbnailer_file(),
            thumbnailers / "ubuntu-tweaks-woff2.thumbnailer": bundle.woff2_thumbnailer_file(),
            self._home / MIME_DIR / "packages/ubuntu-tweaks-previews.xml": mimedb.mime_xml(),
        }

    def _missing_packages(self) -> list[str]:
        return [
            package
            for package, location in SANDBOX_PACKAGES.items()
            if not (self._system / location.lstrip("/")).is_dir()
        ]

    def _mime_database_current(self) -> bool:
        """The compiled database knows every type we define: ``globs2`` lists each by name."""
        globs = read_text_or_none(self._home / MIME_DIR / "globs2")
        if globs is None:
            return False
        return all(f":{definition.mime}:" in globs for definition in mimedb.DEFINITIONS)

    def stale_failures(self) -> list[Path]:
        """Failure markers of files we can now preview: Files would never retry them on its own."""
        stale: list[Path] = []
        for marker in sorted((self._home / FAIL_DIR).glob("*/*.png")):
            try:
                uri = png_text_chunks(marker.read_bytes()).get("Thumb::URI", "")
            except OSError:
                continue
            if uri and covered(uri):
                stale.append(marker)
        return stale

    # ---- Module protocol ----

    def findings(self) -> list[Finding]:
        findings = [
            Finding(
                not self._missing_packages(),
                "the system python packages the thumbnailer sandbox reads",
                "" if not self._missing_packages() else f"missing {', '.join(self._missing_packages())} (sudo apt install ...)",
                manual=bool(self._missing_packages()),
            )
        ]
        for path, content in self.files().items():
            findings.append(Finding(read_text_or_none(path) == content, str(path)))
        findings.append(Finding(self._mime_database_current(), f"{MIME_DIR} compiled with the previews' types"))
        return findings

    def plan(self) -> list[str]:
        lines: list[str] = []
        for path, content in self.files().items():
            old = read_text_or_none(path)
            if old != content:
                shown = str(path).replace(str(self._home), "~")
                if path.suffix == ".thumbnailer":
                    lines.append(f"    write {shown} ({len(content) // 1024} KiB, the uthumb package inside Exec=)")
                else:
                    lines.extend(unified_diff_lines(shown, old, content))
        if not self._mime_database_current() or any(read_text_or_none(p) != c for p, c in self.files().items() if p.suffix == ".xml"):
            lines.append(f"    update-mime-database ~/{MIME_DIR}")
        stale = self.stale_failures()
        if stale:
            lines.append(f"    forget {len(stale)} cached thumbnail failure(s) of files that can now be previewed")
        return lines

    def apply(self) -> None:
        missing = self._missing_packages()
        if missing:
            raise NeedsManual(f"install {', '.join(missing)}: sudo apt install {' '.join(missing)}")
        mime_changed = False
        for path, content in self.files().items():
            if read_text_or_none(path) != content:
                write_file(path, content, 0o644)
                mime_changed = mime_changed or path.suffix == ".xml"
        if mime_changed or not self._mime_database_current():
            self._process.execute(["update-mime-database", str(self._home / MIME_DIR)])
        for marker in self.stale_failures():
            marker.unlink(missing_ok=True)

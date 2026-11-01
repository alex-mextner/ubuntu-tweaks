"""Jupyter notebooks: the first cells as one highlighted listing, markdown cells as comments."""

from __future__ import annotations

import json

from PIL import Image

from ..errors import Unsupported
from ..files import read_text
from .code import highlighted_card

MAX_CELLS = 14
NOTEBOOK_LIMIT = 16 * 1024 * 1024


def render(path: str, size: int, name: str, kind, mime: str) -> Image.Image:
    try:
        notebook = json.loads(read_text(path, NOTEBOOK_LIMIT))
    except ValueError as error:
        raise Unsupported("not a notebook") from error
    cells = notebook.get("cells") or [c for sheet in notebook.get("worksheets", []) for c in sheet.get("cells", [])]
    lines: list[str] = []
    for cell in cells[:MAX_CELLS]:
        source = cell.get("source", cell.get("input", ""))
        text = "".join(source) if isinstance(source, list) else str(source)
        if cell.get("cell_type") == "markdown":
            lines.extend(line if line.startswith("#") else f"# {line}" for line in text.splitlines()[:3])
        else:
            lines.extend(text.splitlines())
        lines.append("")
    language = (notebook.get("metadata", {}).get("language_info", {}) or {}).get("name") or "python"
    return highlighted_card("\n".join(lines), language.lower(), size, "IPYNB")

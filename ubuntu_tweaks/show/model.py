"""The data ``ubuntu-tweak show`` collects and the three renderers print. No logic, no system access.

Accessed via: ``show.collect`` builds a ``Report``; ``show.render`` / ``show.html_page`` consume it.
Everything in a ``Report`` is already safe to print: a secret key carries ``hidden=True`` and the
placeholder ``скрыто``, never its value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type Status = Literal["changed", "default", "override"]

STATUS_LABELS: dict[str, str] = {
    "changed": "изменено вами",
    "default": "по умолчанию",
    "override": "задано файлом-переопределением",
}
HIDDEN = "скрыто"


@dataclass(frozen=True)
class Row:
    path: str  # the dconf key path (for a grouped row: the schema id and the number of instances)
    schema: str | None  # None: no installed schema describes the key
    key: str
    status: Status
    explanation: str
    translated: bool  # False: the text is the schema's own English summary, not a hand-written one
    value: tuple[str, ...]  # the current effective value, one line per bullet
    default: tuple[str, ...]
    hidden: bool
    swatch: str | None = None  # a #rrggbb colour to draw next to the value

    @property
    def title(self) -> str:
        """The key name with its directory's last segment (``screensaver / picture-uri``), enough to tell
        same-named keys of different schemas apart; a folded row is just the key name."""
        parts = self.path.split("/")
        return f"{parts[-2]} / {self.key}" if self.path.startswith("/") and len(parts) > 2 else self.key


@dataclass(frozen=True)
class PanelSection:
    id: str
    title: str
    about: str
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class Entry:
    label: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ExtraSection:
    """Something that does not live in dconf: files, timezone/locale/keyboard, packages, modules."""

    id: str
    title: str
    about: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Report:
    label: str  # the machine name used in titles and the default output file name
    source: str  # "локально" or "по ssh <host>"
    mode: Literal["changed", "all"]
    panels: tuple[PanelSection, ...]
    extras: tuple[ExtraSection, ...]
    counts: dict[str, int]
    untouched: tuple[str, ...] = ()  # titles of panels that hold nothing to show in this mode

"""Readable Russian forms of GSettings values for ``ubuntu-tweak show``. Pure functions: no system access.

Accessed via: ``show.collect`` renders every key's effective value and default through ``humanize``.
Input is the native Python value of a ``GLib.Variant`` (``variant.unpack()``), so these helpers need
neither PyGObject nor a schema; what a key means (unit, meaning of enum values, whether it is a
keybinding) comes from a ``ValueSpec`` that ``show.descriptions`` builds.

Public API:

- ``ValueSpec(unit, zero, enums, accel)``: ``unit`` is one of ``seconds``, ``minutes``, ``ms``,
  ``percent``, ``pixels``, ``size`` (a ``(w, h)`` pair), ``unix`` (timestamp, UTC), ``bytes``;
  ``zero`` is the text for the value 0 ("никогда"); ``enums`` maps an enum nick to its meaning;
  ``accel`` marks a keybinding list.
- ``humanize(value, spec) -> Rendered``: ``lines`` (one bullet per line when more than one) and
  ``swatch`` (a ``#rrggbb`` colour to draw next to a colour value, or ``None``).
- ``format_duration``, ``format_duration_ms``, ``format_accel``, ``normalize_color``,
  ``format_bool``: the building blocks, covered by golden tests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

BOOL_TRUE = "Вкл"
BOOL_FALSE = "Выкл"
EMPTY = "(пусто)"
UNASSIGNED = "не назначено"
_MAX_TEXT = 160
_COLOR = re.compile(r"^#(?P<hex>[0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?$")
_GDK_COLOR = re.compile(
    r"^#(?P<r>[0-9a-fA-F]{2})[0-9a-fA-F]{2}(?P<g>[0-9a-fA-F]{2})[0-9a-fA-F]{2}(?P<b>[0-9a-fA-F]{2})[0-9a-fA-F]{2}$"
)
_RGB = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[0-9.]+\s*)?\)$")


@dataclass(frozen=True)
class ValueSpec:
    unit: str | None = None
    zero: str | None = None
    enums: Mapping[str, str] = field(default_factory=dict)
    accel: bool = False


@dataclass(frozen=True)
class Rendered:
    lines: tuple[str, ...]
    swatch: str | None = None


def format_bool(value: bool) -> str:
    return BOOL_TRUE if value else BOOL_FALSE


def format_duration(seconds: float) -> str:
    """``300`` -> ``5 мин``, ``90`` -> ``1 мин 30 с``, ``45`` -> ``45 с``, ``7200`` -> ``2 ч``."""
    total = int(round(seconds))
    if total < 60:
        return f"{total} с"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs:
        parts.append(f"{secs} с")
    return " ".join(parts)


def format_duration_ms(milliseconds: float) -> str:
    if milliseconds < 1000:
        return f"{int(round(milliseconds))} мс"
    if milliseconds % 1000 == 0:
        return format_duration(milliseconds // 1000)
    return f"{milliseconds / 1000:g} с"


_MODIFIERS = {
    "primary": "Ctrl",
    "control": "Ctrl",
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "mod1": "Alt",
    "super": "Super",
    "mod4": "Super",
    "meta": "Meta",
    "hyper": "Hyper",
    "mod2": "Mod2",
    "mod3": "Mod3",
    "mod5": "AltGr",
}
_KEYS = {
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "return": "Enter",
    "kp_enter": "Enter (цифровой блок)",
    "space": "Пробел",
    "escape": "Esc",
    "tab": "Tab",
    "iso_left_tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "page_down": "PageDown",
    "print": "PrtSc",
    "grave": "`",
    "minus": "−",
    "plus": "+",
    "equal": "=",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "bracketleft": "[",
    "bracketright": "]",
    "semicolon": ";",
    "apostrophe": "'",
    "super_l": "Super",
    "super_r": "Super",
}
_ACCEL = re.compile(r"^((?:<[A-Za-z0-9_]+>)*)(\S*)$")


def format_accel(accel: str) -> str:
    """``<Super><Alt>Left`` -> ``Super + Alt + ←``; ``''`` -> ``не назначено``; ``disabled`` -> ``отключено``."""
    if not accel:
        return UNASSIGNED
    if accel == "disabled":
        return "отключено"
    match = _ACCEL.match(accel)
    if match is None:
        return accel
    modifiers = [_MODIFIERS.get(name.lower(), name) for name in re.findall(r"<([A-Za-z0-9_]+)>", match.group(1))]
    key = match.group(2)
    shown = _KEYS.get(key.lower(), key.upper() if len(key) == 1 else key)
    if key.lower() == "iso_left_tab" and "Shift" not in modifiers:
        modifiers.append("Shift")
    return " + ".join([*dict.fromkeys(modifiers), shown]) if shown else " + ".join(modifiers)


def normalize_color(text: str) -> str | None:
    """``#RRGGBB`` / ``#RRGGBBAA`` / ``#RRRRGGGGBBBB`` (GDK) / ``rgb(r,g,b)`` -> lowercase ``#rrggbb``; else ``None``."""
    found = _COLOR.match(text)
    if found:
        return f"#{found.group('hex').lower()}"
    gdk = _GDK_COLOR.match(text)
    if gdk:
        return f"#{gdk.group('r')}{gdk.group('g')}{gdk.group('b')}".lower()
    rgb = _RGB.match(text)
    if rgb and all(int(part) <= 255 for part in rgb.groups()):
        return "#" + "".join(f"{int(part):02x}" for part in rgb.groups())
    return None


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _timestamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return f"{value:g} (не похоже на время в секундах)"


def _number(value: float, spec: ValueSpec) -> str:
    unit = spec.unit
    if spec.zero is not None and value == 0:
        return spec.zero
    if unit == "seconds":
        return format_duration(value)
    if unit == "minutes":
        return format_duration(value * 60)
    if unit == "ms":
        return format_duration_ms(value)
    if unit == "percent":
        return f"{value:g} %"
    if unit == "pixels":
        return f"{value:g} пикс."
    if unit == "bytes":
        return _bytes(value)
    if unit == "unix":
        return "никогда" if value <= 0 else _timestamp(value)
    return f"{value:g}" if isinstance(value, float) else str(value)


def _bytes(value: float) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:g} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def _text(value: str, spec: ValueSpec) -> tuple[str, str | None]:
    if value == "":
        return EMPTY, None
    if value in spec.enums:
        return f"{value} — {spec.enums[value]}", None
    color = normalize_color(value)
    if color is not None:
        return f"{value} (цвет)", color
    return _truncate(value), None


def _scalar(value: object, spec: ValueSpec) -> tuple[str, str | None]:
    if isinstance(value, bool):
        return format_bool(value), None
    if isinstance(value, (int, float)):
        return _number(value, spec), None
    if isinstance(value, str):
        return (format_accel(value), None) if spec.accel else _text(value, spec)
    if isinstance(value, bytes):
        return f"{len(value)} байт", None
    if isinstance(value, (tuple, list)):
        parts = [_scalar(item, ValueSpec(enums=spec.enums))[0] for item in value]
        return "(" + ", ".join(parts) + ")", None
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {_scalar(item, ValueSpec())[0]}" for key, item in value.items()) + "}", None
    return _truncate(str(value)), None


def humanize(value: object, spec: ValueSpec) -> Rendered:
    """The value as readable lines: scalars one line, lists one bullet per item, dicts ``key: value``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Rendered((_number(value, spec),))
    if isinstance(value, tuple) and spec.unit == "size" and len(value) == 2:
        return Rendered((f"{value[0]} × {value[1]} пикс.",))
    if isinstance(value, list):
        if not value:
            return Rendered((UNASSIGNED if spec.accel else EMPTY,))
        lines: list[str] = []
        swatch = None
        for item in value:
            text, color = _scalar(item, spec)
            lines.append(text)
            swatch = swatch or color
        return Rendered(tuple(lines), swatch)
    if isinstance(value, dict):
        if not value:
            return Rendered((EMPTY,))
        return Rendered(tuple(f"{key}: {_scalar(item, ValueSpec())[0]}" for key, item in sorted(value.items())))
    text, color = _scalar(value, spec)
    return Rendered((text,), color)

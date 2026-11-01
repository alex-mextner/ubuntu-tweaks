"""Namespace-free XML reading shared by the XML mesh formats."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator

from ..errors import Unsupported


def local(tag: str) -> str:
    """``{namespace}name`` -> ``name``."""
    return tag.rsplit("}", 1)[-1]


def parse(data: bytes) -> ET.Element:
    """Parse ``data`` refusing the entity declarations that make billion-laughs documents."""
    if b"<!ENTITY" in data[:65536]:
        raise Unsupported("XML entities are not read")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise Unsupported(f"bad XML: {error}") from error


def walk(element: ET.Element, name: str) -> Iterator[ET.Element]:
    """Every descendant whose local tag is ``name``."""
    for child in element.iter():
        if local(child.tag) == name:
            yield child


def floats(text: str | None) -> list[float]:
    return [float(value) for value in (text or "").replace(",", " ").split()]


def ints(text: str | None) -> list[int]:
    return [int(value) for value in (text or "").replace(",", " ").split()]

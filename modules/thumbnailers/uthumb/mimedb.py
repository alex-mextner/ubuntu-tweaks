"""MIME types shared-mime-info 2.4 (Ubuntu 26.04) does not know, or classifies wrongly, for the files we preview.

Written as one ``~/.local/share/mime/packages/ubuntu-tweaks-previews.xml``; ``update-mime-database`` merges it with
the system database, so Files, the file chooser and the dock all see the same types. A glob ``weight`` above the
default 50 makes ours win over a system glob for the same pattern (``*.ts`` is a Qt translation there).
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

NAMESPACE = "http://www.freedesktop.org/standards/shared-mime-info"


@dataclass(frozen=True)
class MimeDefinition:
    mime: str
    comment: str
    globs: tuple[str, ...] = ()
    weight: int = 50
    parent: str | None = "text/plain"
    magic: tuple[str, ...] = ()  # byte strings matched at offset 0
    case_sensitive: bool = False


DEFINITIONS: tuple[MimeDefinition, ...] = (
    # 3D
    MimeDefinition("model/x-ply", "Stanford PLY mesh", ("*.ply",), parent=None, magic=("ply\n", "ply\r\n")),
    MimeDefinition("model/x-off", "OFF mesh", ("*.off",)),
    MimeDefinition("model/x-amf", "Additive Manufacturing File", ("*.amf",), parent="application/xml"),
    MimeDefinition("model/vnd.collada+xml", "Collada 3D scene", ("*.dae",), parent="application/xml"),
    MimeDefinition("model/x3d+xml", "X3D scene", ("*.x3d",), parent="application/xml"),
    MimeDefinition("model/x3d+vrml", "X3D (VRML encoding) scene", ("*.x3dv",)),
    MimeDefinition("model/x-3ds", "Autodesk 3D Studio model", ("*.3ds",), weight=60, parent=None),
    MimeDefinition("model/x-fbx", "Autodesk FBX model", ("*.fbx",), parent=None, magic=("Kaydara FBX Binary",)),
    MimeDefinition("model/step", "STEP CAD model", ("*.step", "*.stp"), weight=60, magic=("ISO-10303-21",)),
    MimeDefinition("application/x-bgcode", "Binary G-code", ("*.bgcode",), parent=None, magic=("GCDE",)),
    MimeDefinition("application/x-extension-fcstd", "FreeCAD document", ("*.FCStd",), parent="application/zip"),
    # web development
    MimeDefinition("text/typescript", "TypeScript source", ("*.ts", "*.mts", "*.cts"), weight=60),
    MimeDefinition("text/tsx", "TypeScript JSX source", ("*.tsx",), weight=60),
    MimeDefinition("text/jsx", "JSX source", ("*.jsx",), weight=60),
    MimeDefinition("text/x-vue", "Vue single-file component", ("*.vue",)),
    MimeDefinition("text/x-svelte", "Svelte component", ("*.svelte",)),
    MimeDefinition("text/x-astro", "Astro component", ("*.astro",)),
    MimeDefinition("text/x-less", "Less stylesheet", ("*.less",)),
    MimeDefinition("text/mdx", "MDX document", ("*.mdx",), weight=60, parent="text/markdown"),
    MimeDefinition("application/graphql", "GraphQL document", ("*.graphql", "*.gql")),
    MimeDefinition("application/jsonc", "JSON with comments", ("*.jsonc", "*.json5"), parent="application/json"),
    MimeDefinition("text/x-dockerfile", "Dockerfile", ("Dockerfile", "Dockerfile.*", "Containerfile", "*.dockerfile"), case_sensitive=True),
    MimeDefinition("text/x-dotenv", "Environment file", (".env", ".env.*")),
)


def mime_xml() -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<mime-info xmlns="{NAMESPACE}">']
    for definition in DEFINITIONS:
        lines.append(f'  <mime-type type="{definition.mime}">')
        lines.append(f"    <comment>{escape(definition.comment)}</comment>")
        if definition.parent:
            lines.append(f'    <sub-class-of type="{definition.parent}"/>')
        for magic in definition.magic:
            lines.append('    <magic priority="60">')
            lines.append(f'      <match type="string" value="{escape(magic)}" offset="0"/>'.replace("\n", "\\n").replace("\r", "\\r"))
            lines.append("    </magic>")
        for pattern in definition.globs:
            attributes = f' weight="{definition.weight}"' if definition.weight != 50 else ""
            if definition.case_sensitive:
                attributes += ' case-sensitive="true"'
            lines.append(f'    <glob pattern="{escape(pattern)}"{attributes}/>')
        lines.append("  </mime-type>")
    # more spellings of types the system already defines
    lines += [
        '  <mime-type type="text/x.gcode">',
        "    <comment>G-code</comment>",
        '    <glob pattern="*.gco"/>',
        '    <glob pattern="*.gc"/>',
        "  </mime-type>",
        "</mime-info>",
        "",
    ]
    return "\n".join(lines)

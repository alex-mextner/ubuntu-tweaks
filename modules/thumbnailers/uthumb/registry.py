"""Which file kinds exist, which MIME types claim them, and which module renders them.

A handler is ``module:function`` taking ``(path, size, name)`` and returning a Pillow image. Handlers are imported
lazily: a thumbnail of a STL must not pay for Pygments. ``MIME_TYPES`` feeds the ``MimeType=`` line of the
``.thumbnailer`` file; ``ext`` routes by file name, which is what survives the sandbox (the input is bound as
``/tmp/gnome-desktop-file-to-thumbnail.<extension>``).
"""

from __future__ import annotations

from dataclasses import dataclass

MESH = "uthumb.formats.meshes:render"


@dataclass(frozen=True)
class Kind:
    """One family of files: its extensions, the MIME types that reach this thumbnailer and the handler."""

    label: str
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]
    handler: str


KINDS: tuple[Kind, ...] = (
    # 3D models: parsed and shaded here
    Kind("STL", (".stl",), ("model/stl", "application/sla", "application/vnd.ms-pki.stl"), MESH),
    Kind("OBJ", (".obj",), ("model/obj",), MESH),
    Kind("PLY", (".ply",), ("model/x-ply",), MESH),
    Kind("OFF", (".off",), ("model/x-off",), MESH),
    Kind("AMF", (".amf",), ("model/x-amf",), MESH),
    Kind("glTF", (".glb", ".gltf"), ("model/gltf-binary", "model/gltf+json"), MESH),
    Kind("Collada", (".dae",), ("model/vnd.collada+xml",), MESH),
    Kind("X3D", (".x3d", ".x3dv", ".wrl", ".vrml"), ("model/x3d+xml", "model/x3d+vrml", "model/vrml"), MESH),
    Kind("3DS", (".3ds",), ("model/x-3ds", "image/x-3ds"), MESH),
    Kind("FBX", (".fbx",), ("model/x-fbx",), MESH),
    Kind("STEP", (".step", ".stp"), ("model/step",), "uthumb.formats.cad:render"),
    Kind("IGES", (".iges", ".igs"), ("model/iges",), "uthumb.formats.cad:render"),
    # 3D files that carry their own picture, or a toolpath to draw
    Kind(
        "3MF",
        (".3mf",),
        ("model/3mf", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"),
        "uthumb.formats.threemf:render",
    ),
    Kind("G-code", (".gcode", ".gco", ".gc", ".nc"), ("text/x.gcode",), "uthumb.formats.gcode:render"),
    Kind("bgcode", (".bgcode",), ("application/x-bgcode",), "uthumb.formats.gcode:render"),
    Kind("Blender", (".blend",), ("application/x-blender",), "uthumb.formats.blend:render"),
    Kind("FreeCAD", (".fcstd",), ("application/x-extension-fcstd",), "uthumb.formats.fcstd:render"),
)

# Source-like files rendered by Pygments onto a window-shaped card. Extensions here only pick the lexer; the MIME
# types are what the desktop sends. Kept apart from KINDS because the handler is one function with a lexer table.
CODE = "uthumb.formats.code:render"
CODE_KINDS: tuple[Kind, ...] = (
    Kind(
        "TypeScript",
        (".ts", ".mts", ".cts"),
        ("text/typescript", "text/vnd.trolltech.linguist"),
        CODE,
    ),
    Kind("TSX", (".tsx",), ("text/tsx", "application/x-tiled-tsx"), CODE),
    Kind("JSX", (".jsx",), ("text/jsx",), CODE),
    Kind(
        "JavaScript",
        (".js", ".mjs", ".cjs"),
        ("text/javascript", "application/javascript", "application/x-javascript"),
        CODE,
    ),
    Kind("CSS", (".css",), ("text/css",), CODE),
    Kind("SCSS", (".scss", ".sass", ".less"), ("text/x-scss", "text/x-sass", "text/x-less"), CODE),
    Kind(
        "JSON",
        (".json", ".jsonc", ".json5", ".webmanifest"),
        ("application/json", "application/jsonc", "application/manifest+json", "application/ld+json"),
        CODE,
    ),
    Kind("YAML", (".yaml", ".yml"), ("application/yaml", "application/x-yaml", "text/yaml"), CODE),
    Kind("TOML", (".toml",), ("application/toml",), CODE),
    Kind("Markdown", (".md", ".markdown", ".mdx"), ("text/markdown", "text/x-markdown", "text/mdx"), CODE),
    Kind("Vue", (".vue",), ("text/x-vue",), CODE),
    Kind("Svelte", (".svelte",), ("text/x-svelte",), CODE),
    Kind("Astro", (".astro",), ("text/x-astro",), CODE),
    Kind("PHP", (".php", ".phtml"), ("application/x-php", "application/x-httpd-php"), CODE),
    Kind("GraphQL", (".graphql", ".gql"), ("application/graphql",), CODE),
    Kind("SQL", (".sql",), ("application/sql", "text/x-sql"), CODE),
    Kind("Dockerfile", (".dockerfile",), ("text/x-dockerfile",), CODE),
    Kind("env", (".env",), ("text/x-dotenv",), CODE),
    Kind("XML", (".xml", ".xsl", ".xslt", ".plist"), ("application/xml", "text/xml"), CODE),
    Kind("Python", (".py", ".pyw"), ("text/x-python", "text/x-python3"), CODE),
    Kind("Shell", (".sh", ".bash", ".zsh"), ("application/x-shellscript",), CODE),
    Kind("Rust", (".rs",), ("text/rust", "text/x-rust"), CODE),
    Kind("Go", (".go",), ("text/x-go",), CODE),
    Kind("OpenSCAD", (".scad",), ("application/x-openscad",), CODE),
    Kind("Makefile", (".mk",), ("text/x-makefile",), CODE),
    Kind("Notebook", (".ipynb",), ("application/x-ipynb+json",), "uthumb.formats.notebook:render"),
    Kind(
        "HTML",
        (".html", ".htm", ".xhtml", ".shtml"),
        (
            "text/html",
            "application/xhtml+xml",
            "application/x-extension-html",
            "application/x-extension-htm",
            "application/x-extension-xhtml",
            "application/x-extension-shtml",
        ),
        "uthumb.formats.webpage:render",
    ),
)

ALL_KINDS: tuple[Kind, ...] = KINDS + CODE_KINDS
MIME_TYPES: tuple[str, ...] = tuple(sorted({mime for kind in ALL_KINDS for mime in kind.mime_types}))
# Files recognised by their whole name, not an extension: the extension their kind is registered under.
NAMED = {"dockerfile": ".dockerfile", "makefile": ".mk", "gnumakefile": ".mk", ".env": ".env"}
NAMED_PREFIXES = {"dockerfile.": ".dockerfile", ".env.": ".env"}


def kind_for(name: str) -> Kind | None:
    """The kind of file called ``name``: longest matching extension wins."""
    lowered = name.lower()
    lowered = NAMED.get(lowered) or next(
        (extension for prefix, extension in NAMED_PREFIXES.items() if lowered.startswith(prefix)), lowered
    )
    best: Kind | None = None
    best_length = 0
    for kind in ALL_KINDS:
        for extension in kind.extensions:
            if lowered.endswith(extension) and len(extension) > best_length:
                best, best_length = kind, len(extension)
    return best


def kind_for_mime(mime: str) -> Kind | None:
    for kind in ALL_KINDS:
        if mime in kind.mime_types:
            return kind
    return None

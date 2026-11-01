"""Tiny valid files of every format the thumbnailer reads, built in memory for the tests."""

from __future__ import annotations

import base64
import io
import json
import struct
import zipfile

CUBE_VERTICES = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
CUBE_TRIANGLES = [
    (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
    (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
]  # fmt: skip


def png(width: int = 24, height: int = 16, color=(200, 60, 30, 255)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def stl_binary() -> bytes:
    body = b"".join(
        struct.pack("<12fH", 0, 0, 0, *CUBE_VERTICES[a], *CUBE_VERTICES[b], *CUBE_VERTICES[c], 0)
        for a, b, c in CUBE_TRIANGLES
    )
    return b"\x00" * 80 + struct.pack("<I", len(CUBE_TRIANGLES)) + body


def stl_ascii() -> bytes:
    facets = "".join(
        "facet normal 0 0 0\nouter loop\n" + "".join("vertex %g %g %g\n" % CUBE_VERTICES[i] for i in tri) + "endloop\nendfacet\n"
        for tri in CUBE_TRIANGLES
    )
    return f"solid cube\n{facets}endsolid cube\n".encode()


def obj() -> bytes:
    lines = ["v %g %g %g" % v for v in CUBE_VERTICES]
    lines += ["f %d//1 %d//1 %d//1" % (a + 1, b + 1, c + 1) for a, b, c in CUBE_TRIANGLES]
    return ("\n".join(lines) + "\n").encode()


def ply_ascii() -> bytes:
    header = "ply\nformat ascii 1.0\nelement vertex 8\nproperty float x\nproperty float y\nproperty float z\nelement face 12\nproperty list uchar int vertex_indices\nend_header\n"
    body = "".join("%g %g %g\n" % v for v in CUBE_VERTICES) + "".join("3 %d %d %d\n" % t for t in CUBE_TRIANGLES)
    return (header + body).encode()


def ply_binary() -> bytes:
    header = b"ply\nformat binary_little_endian 1.0\nelement vertex 8\nproperty float x\nproperty float y\nproperty float z\nelement face 12\nproperty list uchar int vertex_indices\nend_header\n"
    return header + b"".join(struct.pack("<3f", *v) for v in CUBE_VERTICES) + b"".join(struct.pack("<B3i", 3, *t) for t in CUBE_TRIANGLES)


def off() -> bytes:
    lines = ["OFF", "8 12 0"] + ["%g %g %g" % v for v in CUBE_VERTICES] + ["3 %d %d %d" % t for t in CUBE_TRIANGLES]
    return ("\n".join(lines) + "\n").encode()


def model_xml(color: str | None = None) -> str:
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in CUBE_VERTICES)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in CUBE_TRIANGLES)
    return (
        '<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model"><mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh></object>'
        '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 5 5 0"/></build></model>'
    )


def threemf(with_picture: bool) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        )
        package.writestr("3D/3dmodel.model", model_xml())
        if with_picture:
            package.writestr("Metadata/plate_1.png", png())
    return buffer.getvalue()


def gltf_document(buffer_uri: str | None) -> tuple[dict, bytes]:
    positions = b"".join(struct.pack("<3f", *v) for v in CUBE_VERTICES)
    indices = b"".join(struct.pack("<3H", *t) for t in CUBE_TRIANGLES)
    data = positions + indices
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [0, 1, 0]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [0.9, 0.4, 0.1, 1]}}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(indices)},
        ],
        "buffers": [{"byteLength": len(data), **({"uri": buffer_uri} if buffer_uri else {})}],
    }
    return document, data


def glb() -> bytes:
    document, data = gltf_document(None)
    text = json.dumps(document).encode()
    text += b" " * (-len(text) % 4)
    data += b"\x00" * (-len(data) % 4)
    total = 12 + 8 + len(text) + 8 + len(data)
    return (
        b"glTF" + struct.pack("<II", 2, total)
        + struct.pack("<II", len(text), 0x4E4F534A) + text
        + struct.pack("<II", len(data), 0x004E4942) + data
    )  # fmt: skip


def gltf_embedded() -> bytes:
    document, data = gltf_document("data:application/octet-stream;base64," + base64.b64encode(
        b"".join(struct.pack("<3f", *v) for v in CUBE_VERTICES) + b"".join(struct.pack("<3H", *t) for t in CUBE_TRIANGLES)
    ).decode())
    return json.dumps(document).encode()


def gltf_external() -> bytes:
    document, _ = gltf_document("cube.bin")
    return json.dumps(document).encode()


def x3d() -> bytes:
    points = " ".join("%g %g %g" % v for v in CUBE_VERTICES)
    index = " ".join("%d %d %d -1" % t for t in CUBE_TRIANGLES)
    return f'<?xml version="1.0"?><X3D><Scene><Shape><IndexedFaceSet coordIndex="{index}"><Coordinate point="{points}"/></IndexedFaceSet></Shape></Scene></X3D>'.encode()


def vrml() -> bytes:
    points = ", ".join("%g %g %g" % v for v in CUBE_VERTICES)
    index = ", ".join("%d, %d, %d, -1" % t for t in CUBE_TRIANGLES)
    return f"#VRML V2.0 utf8\nShape {{ geometry IndexedFaceSet {{ coord Coordinate {{ point [ {points} ] }} coordIndex [ {index} ] }} }}\n".encode()


def amf() -> bytes:
    vertices = "".join(f"<vertex><coordinates><x>{x}</x><y>{y}</y><z>{z}</z></coordinates></vertex>" for x, y, z in CUBE_VERTICES)
    volume = "".join(f"<triangle><v1>{a}</v1><v2>{b}</v2><v3>{c}</v3></triangle>" for a, b, c in CUBE_TRIANGLES)
    return f'<?xml version="1.0"?><amf><object id="0"><mesh><vertices>{vertices}</vertices><volume>{volume}</volume></mesh></object></amf>'.encode()


def collada() -> bytes:
    positions = " ".join("%g %g %g" % v for v in CUBE_VERTICES)
    indices = " ".join("%d %d %d" % t for t in CUBE_TRIANGLES)
    return (
        '<?xml version="1.0"?><COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema"><asset><up_axis>Z_UP</up_axis></asset>'
        '<library_geometries><geometry id="g"><mesh>'
        f'<source id="p"><float_array id="pa" count="24">{positions}</float_array></source>'
        '<vertices id="v"><input semantic="POSITION" source="#p"/></vertices>'
        f'<triangles count="12"><input semantic="VERTEX" source="#v" offset="0"/><p>{indices}</p></triangles>'
        '</mesh></geometry></library_geometries>'
        '<library_visual_scenes><visual_scene id="s"><node><matrix>1 0 0 2 0 1 0 0 0 0 1 0 0 0 0 1</matrix><instance_geometry url="#g"/></node></visual_scene></library_visual_scenes></COLLADA>'
    ).encode()


def studio3ds() -> bytes:
    vertices = struct.pack("<H", 8) + b"".join(struct.pack("<3f", *v) for v in CUBE_VERTICES)
    faces = struct.pack("<H", 12) + b"".join(struct.pack("<4H", *t, 0) for t in CUBE_TRIANGLES)

    def chunk(chunk_id: int, payload: bytes) -> bytes:
        return struct.pack("<HI", chunk_id, 6 + len(payload)) + payload

    trimesh = chunk(0x4100, chunk(0x4110, vertices) + chunk(0x4120, faces))
    return chunk(0x4D4D, chunk(0x3D3D, chunk(0x4000, b"cube\x00" + trimesh)))


def fbx_ascii() -> bytes:
    vertices = ",".join(str(c) for v in CUBE_VERTICES for c in v)
    polygons = ",".join(str(i) if k < 2 else str(~i) for t in CUBE_TRIANGLES for k, i in enumerate(t))
    return (
        "; FBX 7.4.0 project file\nFBXHeaderExtension:  {\n}\nObjects:  {\n"
        f'\n Geometry: 1, "Geometry::cube", "Mesh" {{\n  Vertices: *24 {{\n   a: {vertices}\n  }}\n'
        f"  PolygonVertexIndex: *36 {{\n   a: {polygons}\n  }}\n }}\n}}\n"
    ).encode()


def step() -> bytes:
    points = {1: (0, 0, 0), 2: (10, 0, 0), 3: (10, 10, 0), 4: (0, 10, 0), 5: (0, 0, 10)}
    lines = ["ISO-10303-21;", "HEADER;", "ENDSEC;", "DATA;"]
    for number, (x, y, z) in points.items():
        lines.append(f"#{number}=CARTESIAN_POINT('',({x}.,{y}.,{z}.));")
        lines.append(f"#{number + 10}=VERTEX_POINT('',#{number});")
    lines += ["#30=DIRECTION('',(0.,0.,1.));", "#31=DIRECTION('',(1.,0.,0.));", "#32=AXIS2_PLACEMENT_3D('',#1,#30,#31);", "#33=CIRCLE('',#32,5.);"]
    edges = [(11, 12), (12, 13), (13, 14), (14, 11), (11, 15)]
    for index, (a, b) in enumerate(edges):
        lines.append(f"#{40 + index}=EDGE_CURVE('',#{a},#{b},#33,.T.);")
    lines += ["ENDSEC;", "END-ISO-10303-21;"]
    return "\n".join(lines).encode()


def gcode(with_thumbnail: bool) -> bytes:
    lines = ["; HEADER_BLOCK_START", "; generated by test"]
    if with_thumbnail:
        encoded = base64.b64encode(png(32, 32, (10, 160, 90, 255))).decode()
        lines += [f"; thumbnail begin 32x32 {len(encoded)}"] + ["; " + encoded[i : i + 70] for i in range(0, len(encoded), 70)] + ["; thumbnail end"]
    lines += ["G90", "M82", "G92 E0"]
    e = 0.0
    for layer in range(1, 6):
        lines.append(f"G1 Z{layer * 0.2:.1f}")
        for corner in ((10, 0), (10, 10), (0, 10), (0, 0)):
            e += 1
            lines.append(f"G1 X{corner[0]} Y{corner[1]} E{e}")
    return ("\n".join(lines) + "\n").encode()


def bgcode() -> bytes:
    picture = png(16, 16)
    header = b"GCDE" + struct.pack("<IH", 1, 0)
    thumbnail = struct.pack("<HHI", 5, 0, len(picture)) + struct.pack("<HHH", 0, 16, 16) + picture
    return header + thumbnail


def blend(width: int = 4, height: int = 2) -> bytes:
    """A new-style (BLENDER17-01v0502) file: a REND block, the TEST preview block and the end marker."""
    pixels = bytes([255, 0, 0, 255] * (width * height))
    preview = struct.pack("<ii", width, height) + pixels

    def block(code: bytes, data: bytes) -> bytes:
        return struct.pack("<4sIQqq", code, 0, 0, len(data), 1) + data

    return b"BLENDER17-01v0502" + block(b"REND", b"\x00" * 12) + block(b"TEST", preview) + block(b"ENDB", b"")


def fcstd(with_thumbnail: bool) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("Document.xml", "<Document/>")
        if with_thumbnail:
            package.writestr("thumbnails/Thumbnail.png", png())
    return buffer.getvalue()


def notebook() -> bytes:
    return json.dumps({
        "cells": [{"cell_type": "markdown", "source": ["# Title"]}, {"cell_type": "code", "source": ["print(1)"]}],
        "metadata": {"language_info": {"name": "python"}},
    }).encode()


HTML_PAGE = '<!doctype html><title>Pocket &amp; Co</title><meta name="description" content="A tiny page."><h1>Hello</h1>'.encode()
TYPESCRIPT = b'export const answer: number = 42;\nexport function greet(name: string) { return `hi ${name}`; }\n'
QT_TS = b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS><TS version="2.1"><context><name>X</name></context></TS>\n'


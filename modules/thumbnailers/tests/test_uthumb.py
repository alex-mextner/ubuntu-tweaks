"""uthumb: every format parses a minimal valid file and renders; the registry routes; the bundle survives the Exec parser."""

import importlib.util
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

HAS_PIL = importlib.util.find_spec("PIL") is not None
HAS_PYGMENTS = importlib.util.find_spec("pygments") is not None

import thumb_fixtures as fixtures  # noqa: E402

from uthumb import bundle, mimedb, registry  # noqa: E402
from uthumb.errors import Unsupported  # noqa: E402


class Registry(unittest.TestCase):
    def test_extensions_route_to_their_kind(self):
        self.assertEqual(registry.kind_for("model.STL").label, "STL")
        self.assertEqual(registry.kind_for("a.b.tsx").label, "TSX")
        self.assertEqual(registry.kind_for("plate_1.gcode").label, "G-code")
        self.assertEqual(registry.kind_for("x.FCStd").label, "FreeCAD")
        self.assertIsNone(registry.kind_for("photo.png"))

    def test_names_without_extension_route_by_name(self):
        self.assertEqual(registry.kind_for("Dockerfile").label, "Dockerfile")
        self.assertEqual(registry.kind_for("Dockerfile.dev").label, "Dockerfile")
        self.assertEqual(registry.kind_for(".env.local").label, "env")
        self.assertEqual(registry.kind_for("Makefile").label, "Makefile")

    def test_the_sandbox_name_has_only_the_extension(self):
        self.assertEqual(registry.kind_for("gnome-desktop-file-to-thumbnail.stl").label, "STL")
        self.assertIsNone(registry.kind_for("gnome-desktop-file-to-thumbnail"))
        self.assertEqual(registry.kind_for_mime("text/x-dockerfile").label, "Dockerfile")

    def test_every_mime_type_belongs_to_one_kind(self):
        owners = {}
        for kind in registry.ALL_KINDS:
            for mime in kind.mime_types:
                self.assertNotIn(mime, owners, f"{mime} claimed by {owners.get(mime)} and {kind.label}")
                owners[mime] = kind.label

    def test_every_handler_module_exists(self):
        for kind in registry.ALL_KINDS:
            module = kind.handler.split(":")[0]
            self.assertTrue((MODULE / (module.replace(".", "/") + ".py")).is_file(), module)

    def test_defined_mime_types_are_all_claimed_by_a_kind(self):
        claimed = set(registry.MIME_TYPES)
        for definition in mimedb.DEFINITIONS:
            self.assertIn(definition.mime, claimed, f"{definition.mime} is defined but never thumbnailed")


class Bundle(unittest.TestCase):
    def test_exec_line_survives_the_desktop_entry_parser(self):
        line = bundle.exec_line()
        self.assertNotIn("\n", line)
        code = line.split(' -c "', 1)[1].rsplit('" %i', 1)[0]
        for forbidden in '%"\\$`':
            self.assertNotIn(forbidden, code)
        self.assertTrue(line.endswith('%i %o %s %m'))
        self.assertLess(len(line), 120_000)  # execve's per-argument limit is 128 KiB

    def test_the_bundle_is_deterministic_and_leaves_out_build_only_code(self):
        self.assertEqual(bundle.zip_bytes(), bundle.zip_bytes())
        import zipfile

        names = zipfile.ZipFile(io.BytesIO(bundle.zip_bytes())).namelist()
        self.assertIn("uthumb/cli.py", names)
        self.assertNotIn("uthumb/bundle.py", names)
        self.assertNotIn("uthumb/mimedb.py", names)
        self.assertNotIn("uthumb/cache.py", names)

    def test_thumbnailer_entry_lists_every_type_once(self):
        text = bundle.thumbnailer_file()
        mimes = [line for line in text.splitlines() if line.startswith("MimeType=")][0][len("MimeType=") :].split(";")
        self.assertEqual(sorted(m for m in mimes if m), sorted(registry.MIME_TYPES))

    def test_mime_xml_is_well_formed(self):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(mimedb.mime_xml())
        self.assertGreater(len(list(root)), 20)


@unittest.skipUnless(HAS_PIL and HAS_PYGMENTS, "Pillow and Pygments are the thumbnailer's runtime")
class Rendering(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def thumbnail(self, name: str, data: bytes, size: int = 128, mime: str = ""):
        from uthumb import cli

        path = self.directory / name
        path.write_bytes(data)
        return cli.build(str(path), size, mime)

    def assertCard(self, image, size: int = 128):
        self.assertEqual(image.size, (size, size))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0, "the corner is transparent: a rounded card")
        centre = image.getpixel((size // 2, size // 2))
        self.assertEqual(centre[3], 255)

    def assertDrawn(self, image):
        """More than the flat background gradient: the model or text is on the card."""
        colors = image.convert("RGB").getcolors(maxcolors=100_000)
        self.assertGreater(len(colors), 12)

    def test_mesh_formats(self):
        cases = {
            "cube.stl": fixtures.stl_binary(),
            "ascii.stl": fixtures.stl_ascii(),
            "cube.obj": fixtures.obj(),
            "ascii.ply": fixtures.ply_ascii(),
            "binary.ply": fixtures.ply_binary(),
            "cube.off": fixtures.off(),
            "cube.glb": fixtures.glb(),
            "embedded.gltf": fixtures.gltf_embedded(),
            "cube.x3d": fixtures.x3d(),
            "cube.wrl": fixtures.vrml(),
            "cube.amf": fixtures.amf(),
            "cube.dae": fixtures.collada(),
            "cube.3ds": fixtures.studio3ds(),
            "ascii.fbx": fixtures.fbx_ascii(),
            "cube.3mf": fixtures.threemf(with_picture=False),
        }
        for name, data in cases.items():
            with self.subTest(name):
                image = self.thumbnail(name, data)
                self.assertCard(image)
                self.assertDrawn(image)

    def test_model_is_larger_than_a_speck_and_centred(self):
        image = self.thumbnail("cube.stl", fixtures.stl_binary(), 128)
        rgb = image.convert("RGB")
        background = rgb.getpixel((64, 4))
        covered = [(x, y) for x in range(128) for y in range(128) if sum(abs(a - b) for a, b in zip(rgb.getpixel((x, y)), background)) > 90]
        self.assertGreater(len(covered), 128 * 128 * 0.08)
        mean_x = sum(x for x, _ in covered) / len(covered)
        self.assertAlmostEqual(mean_x, 64, delta=14)

    def test_external_gltf_buffers_are_reported_not_guessed(self):
        with self.assertRaises(Unsupported):
            self.thumbnail("ext.gltf", fixtures.gltf_external())

    def test_garbage_is_unsupported_not_a_crash(self):
        from uthumb import cli

        for name in ("bad.stl", "bad.obj", "bad.glb", "bad.3mf", "bad.blend"):
            with self.subTest(name):
                path = self.directory / name
                path.write_bytes(b"not a model at all")
                code = cli.main(["uthumb", str(path), str(self.directory / "out.png"), "64"])
                self.assertEqual(code, 1)
                self.assertFalse((self.directory / "out.png").exists())

    def test_3mf_uses_the_slicers_own_picture_when_present(self):
        image = self.thumbnail("plate.3mf", fixtures.threemf(with_picture=True))
        rgb = image.convert("RGB").getpixel((64, 64))
        self.assertGreater(rgb[0], 150)  # the fixture's red picture, not the blue-grey default model
        self.assertLess(rgb[2], 100)

    def test_gcode_embedded_thumbnail_then_toolpath_fallback(self):
        picture = self.thumbnail("a.gcode", fixtures.gcode(True)).convert("RGB").getpixel((64, 64))
        self.assertGreater(picture[1], picture[0])  # the green fixture picture
        self.assertDrawn(self.thumbnail("b.gcode", fixtures.gcode(False)))

    def test_binary_gcode_preview(self):
        image = self.thumbnail("a.bgcode", fixtures.bgcode())
        self.assertCard(image)
        with self.assertRaises(Unsupported):
            self.thumbnail("b.bgcode", b"GCDE" + b"\x01\x00\x00\x00\x00\x00")

    def test_blender_preview_is_flipped_into_place_and_missing_one_is_unsupported(self):
        self.assertDrawn(self.thumbnail("a.blend", fixtures.blend(8, 8)))
        with self.assertRaises(Unsupported):
            self.thumbnail("b.blend", fixtures.blend()[:17] + b"\x00" * 4)

    def test_freecad_thumbnail(self):
        self.assertCard(self.thumbnail("a.FCStd", fixtures.fcstd(True)))
        with self.assertRaises(Unsupported):
            self.thumbnail("b.FCStd", fixtures.fcstd(False))

    def test_step_wireframe(self):
        self.assertDrawn(self.thumbnail("a.step", fixtures.step()))

    def test_code_cards_for_the_web_stack(self):
        samples = {
            "a.ts": fixtures.TYPESCRIPT, "a.tsx": b"export const A = () => <div>hi</div>;\n", "a.css": b"a { color: red }\n",
            "a.json": b'{"a": [1, 2, {"b": null}]}\n', "a.yaml": b"a:\n  - b\n", "a.toml": b"[a]\nb = 1\n", "a.md": b"# T\n\ntext\n",
            "a.vue": b"<template><p>x</p></template>\n", "a.svelte": b"<script>let a = 1</script>\n", "a.php": b"<?php echo 1;\n",
            "a.scad": b"cube([1,2,3]);\n", "a.sh": b"#!/bin/sh\necho hi\n", "a.py": b"print(1)\n", "a.ipynb": fixtures.notebook(),
        }
        for name, data in samples.items():
            with self.subTest(name):
                image = self.thumbnail(name, data)
                self.assertCard(image)
                self.assertDrawn(image)

    def test_extensionless_files_route_by_mime(self):
        image = self.thumbnail("gnome-desktop-file-to-thumbnail", b"FROM alpine\nRUN true\n", mime="text/x-dockerfile")
        self.assertDrawn(image)

    def test_ts_is_typescript_unless_it_is_a_qt_translation(self):
        from uthumb.formats import code

        self.assertEqual(code._sniff(fixtures.TYPESCRIPT.decode(), ".ts"), None)
        self.assertEqual(code._sniff(fixtures.QT_TS.decode(), ".ts"), "xml")

    def test_html_card_shows_the_title_and_unescapes_it(self):
        from uthumb.formats import webpage

        self.assertEqual(webpage.describe(fixtures.HTML_PAGE.decode()), ("Pocket & Co", "A tiny page.", "Hello"))
        self.assertCard(self.thumbnail("a.html", fixtures.HTML_PAGE))
        self.assertDrawn(self.thumbnail("b.html", b"<div>no title</div>\n"))

    def test_empty_text_file_is_unsupported(self):
        with self.assertRaises(Unsupported):
            self.thumbnail("a.ts", b"\n\n")

    def test_cache_entry_follows_the_thumbnail_standard_and_is_kept_while_current(self):
        import hashlib

        from PIL import Image

        from uthumb import cache

        source = self.directory / "my model.stl"
        source.write_bytes(fixtures.stl_binary())
        root = self.directory / "cache"
        entry = cache.fill(source, "large", root)
        uri = "file://" + str(source).replace(" ", "%20")
        self.assertEqual(entry, root / "large" / (hashlib.md5(uri.encode()).hexdigest() + ".png"))
        with Image.open(entry) as image:
            self.assertEqual(image.size, (256, 256))
            self.assertEqual(image.text["Thumb::URI"], uri)
            self.assertEqual(image.text["Thumb::MTime"], str(int(source.stat().st_mtime)))
        before = entry.stat().st_mtime_ns
        self.assertEqual(cache.fill(source, "large", root), entry)
        self.assertEqual(entry.stat().st_mtime_ns, before, "a current entry is not rewritten")
        os.utime(source, (1_700_000_000, 1_700_000_000))
        cache.fill(source, "large", root)
        with Image.open(entry) as image:
            self.assertEqual(image.text["Thumb::MTime"], "1700000000")
        (self.directory / "photo.png").write_bytes(fixtures.png())
        self.assertIsNone(cache.fill(self.directory / "photo.png", "large", root), "other types are Files' business")

    def test_cache_skips_a_file_that_changes_while_it_is_drawn_and_survives_a_bad_file(self):
        from uthumb import cache, cli

        root = self.directory / "cache"
        source = self.directory / "moving.stl"
        source.write_bytes(fixtures.stl_binary())
        real_build = cli.build

        def build_and_touch(path, size, mime=""):
            image = real_build(path, size, mime)
            os.utime(path, (1_800_000_000, 1_800_000_000))  # the file is replaced after the render
            return image

        cli.build = build_and_touch
        try:
            self.assertIsNone(cache.fill(source, "large", root))
        finally:
            cli.build = real_build
        self.assertFalse(list(root.glob("*/*.png")))
        bad = self.directory / "bad.ipynb"
        bad.write_text("[]")
        good = self.directory / "good.stl"
        good.write_bytes(fixtures.stl_binary())
        self.assertIsNone(cache.fill(bad, "large", root))
        self.assertIsNotNone(cache.fill(good, "large", root))

    def test_large_mesh_is_sampled_not_refused(self):
        import struct

        triangles = 150_000
        record = struct.Struct("<12fH")
        body = b"".join(record.pack(0, 0, 1, i * 1e-3, 0, 0, 1 + i * 1e-3, 0, 0, 0, 1, i * 1e-3, 0) for i in range(triangles))
        self.assertCard(self.thumbnail("big.stl", b"\x00" * 80 + struct.pack("<I", triangles) + body))

    def test_a_hub_vertex_does_not_make_smoothing_quadratic(self):
        import struct
        import time

        triangles = 60_000
        record = struct.Struct("<12fH")
        body = b"".join(
            record.pack(0, 0, 1, 0, 0, 0, math.cos(i * 1e-4), math.sin(i * 1e-4), 0, math.cos(i * 1e-4 + 1e-4), math.sin(i * 1e-4 + 1e-4), 0, 0)
            for i in range(triangles)
        )
        started = time.monotonic()
        self.assertCard(self.thumbnail("fan.stl", b"\x00" * 80 + struct.pack("<I", triangles) + body))
        self.assertLess(time.monotonic() - started, 20)


@unittest.skipUnless(HAS_PIL and HAS_PYGMENTS, "Pillow and Pygments are the thumbnailer's runtime")
class HostileInput(unittest.TestCase):
    """Files built to cost a thumbnailer memory or time must be refused or bounded, never followed."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def thumbnail(self, name: str, data: bytes, size: int = 64, mime: str = ""):
        from uthumb import cli

        path = self.directory / name
        path.write_bytes(data)
        return cli.build(str(path), size, mime)

    def bounded(self, call, seconds: float = 5.0):
        import time

        started = time.monotonic()
        try:
            return call()
        finally:
            self.assertLess(time.monotonic() - started, seconds)

    def test_fbx_array_that_inflates_beyond_its_declared_size_is_refused(self):
        import struct
        import zlib

        from uthumb.formats import fbx

        bomb = zlib.compress(b"\x00" * (32 * 1024 * 1024))
        data = b"i" + struct.pack("<III", 1, 1, len(bomb)) + bomb
        with self.assertRaises(Unsupported):
            self.bounded(lambda: fbx._property(data, 0, [10, 1 << 30]))

    def test_fbx_zero_length_array_with_compressed_payload_is_refused(self):
        import struct
        import zlib

        from uthumb.formats import fbx

        payload = zlib.compress(b"\x00" * (4 * 1024 * 1024))
        data = b"i" + struct.pack("<III", 0, 1, len(payload)) + payload
        with self.assertRaises(Unsupported):
            self.bounded(lambda: fbx._property(data, 0, [10, 1 << 30]))

    def test_blend_header_length_below_twelve_is_refused(self):
        import gzip

        for header in (b"BLENDER11abc", b"BLENDER00-01v"):
            with self.subTest(header=header), self.assertRaises(Unsupported):
                self.thumbnail("odd.blend", gzip.compress(header + b"\x00" * 4_000_000))

    def test_fbx_arrays_share_a_memory_budget(self):
        import struct

        from uthumb.formats import fbx

        data = b"d" + struct.pack("<III", 1_000_000, 0, 0) + b"\x00" * 8_000_000
        with self.assertRaises(Unsupported):
            fbx._property(data, 0, [10, 1_000])

    def test_gltf_node_cycle_and_duplicate_children_terminate(self):
        import json

        document = {"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}], "nodes": [{"children": [0, 0]}]}
        with self.assertRaises(Unsupported):
            self.bounded(lambda: self.thumbnail("loop.gltf", json.dumps(document).encode()))

    def test_gltf_wide_acyclic_graph_hits_the_visit_budget(self):
        import json

        nodes = [{"children": [i + 1, i + 1]} for i in range(25)] + [{}]
        document = {"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}], "nodes": nodes}
        with self.assertRaises(Unsupported):
            self.bounded(lambda: self.thumbnail("wide.gltf", json.dumps(document).encode()))

    def test_3mf_repeated_component_references_hit_the_instance_budget(self):
        import zipfile

        layers = 8
        objects = ['<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>']
        for layer in range(2, layers + 2):
            components = "".join(f'<component objectid="{layer - 1}"/>' for _ in range(10))
            objects.append(f'<object id="{layer}"><components>{components}</components></object>')
        model = (
            '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
            + "".join(objects) + f'</resources><build><item objectid="{layers + 1}"/></build></model>'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("3D/3dmodel.model", model)
        with self.assertRaises(Unsupported):
            self.bounded(lambda: self.thumbnail("bomb.3mf", buffer.getvalue()))

    def test_3mf_relationship_part_larger_than_its_limit_is_ignored_not_inflated(self):
        import zipfile

        from uthumb.formats import threemf

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
            package.writestr("_rels/.rels", "<Relationships/>" + " " * (threemf.MAX_RELATIONSHIPS + 10))
            package.writestr("3D/3dmodel.model", fixtures.model_xml())
        image = self.bounded(lambda: self.thumbnail("big-rels.3mf", buffer.getvalue()))
        self.assertEqual(image.size, (64, 64))  # falls back to the default model path and still draws

    def test_blend_block_longer_than_the_scan_budget_is_not_read(self):
        import gzip
        import struct

        from uthumb.formats import blend

        def file_with_length(length: int) -> bytes:
            header = struct.pack("<4sIQqq", b"REND", 0, 0, length, 1)
            return gzip.compress(b"BLENDER17-01v0502" + header + b"\x00" * 4096)

        for length in (blend.MAX_SCAN * 4, -5):
            with self.subTest(length=length), self.assertRaises(Unsupported):
                self.bounded(lambda: self.thumbnail("evil.blend", file_with_length(length)))

    def test_long_unbroken_title_does_not_blow_up_wrapping(self):
        page = ("<html><title>" + "W" * 32_000 + "</title></html>").encode()
        image = self.bounded(lambda: self.thumbnail("long.html", page, 256), 4.0)
        self.assertEqual(image.size, (256, 256))

    def test_pathological_html_is_linear(self):
        from uthumb.formats import webpage

        self.bounded(lambda: webpage.describe("<title>" * 16_384), 2.0)
        self.bounded(lambda: webpage.describe("<meta " + "a" * 32_768 + ">"), 2.0)
        self.bounded(lambda: webpage.describe("<meta " + 'a="' * 8_000), 2.0)

    def test_collada_polygons_are_parsed_once_and_transforms_are_applied(self):
        from uthumb.formats import collada

        corners = 1_000
        ring = "".join("%f %f 0 " % (math.cos(i), math.sin(i)) for i in range(corners))
        document = (
            '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema"><asset><up_axis>Z_UP</up_axis></asset>'
            f'<library_geometries><geometry id="g"><mesh><source id="p"><float_array>{ring}</float_array></source>'
            '<vertices id="v"><input semantic="POSITION" source="#p"/></vertices>'
            f'<polygons><input semantic="VERTEX" source="#v" offset="0"/><p>{" ".join(map(str, range(corners)))}</p></polygons>'
            '</mesh></geometry></library_geometries><library_visual_scenes><visual_scene id="s">'
            '<node><scale>10 1 1</scale><translate>0 5 0</translate><instance_geometry url="#g"/></node>'
            "</visual_scene></library_visual_scenes></COLLADA>"
        )
        path = self.directory / "scaled.dae"
        path.write_text(document)
        mesh = self.bounded(lambda: collada.load(str(path)), 2.0)
        xs = mesh.coords[0::3]
        ys = mesh.coords[1::3]
        self.assertGreater(max(xs), 9.0)  # scaled by 10 along x
        self.assertGreater(min(ys), 3.9)  # then moved up by 5 (translate is applied inside the scale)

    def test_iges_spline_control_points_are_read_after_the_right_number_of_knots(self):
        from uthumb.formats import cad

        knots, weights = [0, 0, 1, 2, 2], [1, 1, 1]  # K=2 (3 points), M=1: K+M+2 = 5 knots
        controls = [0, 0, 0, 10, 0, 0, 10, 10, 0]
        parameters = [2, 1, 0, 0, 1, 0, *knots, *weights, *controls, 0, 1]
        self.assertEqual(cad._iges_spline([float(v) for v in parameters]), [(0, 0, 0), (10, 0, 0), (10, 10, 0)])

    def test_gcode_arcs_cannot_fill_memory(self):
        from uthumb.formats import gcode

        text = "M83\n" + "G2 I100 E1\n" * 20_000  # 72 segments each if unbounded: 1.4 million
        self.assertLessEqual(len(self.bounded(lambda: gcode._segments(text), 10.0)), gcode.MAX_COLLECTED + 72)

    def test_gcode_full_circle_arcs_make_a_toolpath(self):
        lines = ["G90", "M83", "G1 X10 Y0 F3000"]
        for layer in range(1, 4):
            lines += [f"G1 Z{layer * 0.2:.1f}", "G2 I-10 J0 E1"]
        image = self.thumbnail("circle.gcode", ("\n".join(lines) + "\n").encode(), 128)
        colors = image.convert("RGB").getcolors(maxcolors=100_000)
        self.assertGreater(len(colors), 8)


@unittest.skipUnless(HAS_PIL and HAS_PYGMENTS and shutil.which("bwrap"), "needs bubblewrap and the system python's Pillow/Pygments")
class SandboxedExec(unittest.TestCase):
    """The shipped Exec= line, run the way libgnome-desktop runs it: inside bwrap with only /usr and the input visible."""

    def test_bundle_runs_in_a_sandbox_that_sees_only_usr(self):
        if not Path("/usr/lib/python3/dist-packages/PIL").is_dir():
            self.skipTest("system python has no Pillow in dist-packages")
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        source = directory / "cube.stl"
        source.write_bytes(fixtures.stl_binary())
        work = directory / "out"
        work.mkdir()
        line = bundle.exec_line()
        code = line.split(' -c "', 1)[1].rsplit('" %i', 1)[0]
        command = [
            "bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--unshare-all", "--die-with-parent",
            "--bind", str(work), "/tmp", "--ro-bind", str(source), "/tmp/gnome-desktop-file-to-thumbnail.stl",
            "/usr/bin/python3", "-c", code, "/tmp/gnome-desktop-file-to-thumbnail.stl", "/tmp/out.png", "128", "model/stl",
        ]  # fmt: skip
        result = subprocess.run(command, capture_output=True, text=True, env={"PATH": "/usr/bin"})
        if result.stderr.startswith("bwrap:"):
            self.skipTest(f"bubblewrap cannot create a sandbox here: {result.stderr.strip()}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((work / "out.png").is_file())
        leftovers = sorted(p.name for p in work.iterdir() if not p.name.startswith("gnome-desktop-file-to-thumbnail"))
        self.assertEqual(leftovers, ["out.png"], "the bundle must delete its own temporary file")


if __name__ == "__main__":
    unittest.main()

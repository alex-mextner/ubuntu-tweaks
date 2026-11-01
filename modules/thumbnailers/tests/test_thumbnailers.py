"""thumbnailers module against a scratch home: writes only what differs, forgets stale failures, is idempotent."""

import importlib.machinery
import importlib.util
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.path.insert(0, str(MODULE))
sys.dont_write_bytecode = True

import thumbnailers_lib as lib  # noqa: E402

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.module_script import NeedsManual  # noqa: E402
from ubuntu_tweaks.process import FakeProcess  # noqa: E402


def failure_marker(uri: str) -> bytes:
    """A PNG shaped like the cache's failure markers: a tEXt chunk naming the file, then pixel data."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"tEXt", b"Thumb::URI\x00" + uri.encode())
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + chunk(b"IEND", b"")
    )  # fmt: skip


class ThumbnailersModule(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/alice"
        self.system = self.root / "system"
        for location in lib.SANDBOX_PACKAGES.values():
            (self.system / location.lstrip("/")).mkdir(parents=True)
        self.executed = []

        def effects(argv, _input):
            self.executed.append(argv)
            if argv[0] == "update-mime-database":  # what the real tool leaves behind
                target = Path(argv[1])
                (target / "globs2").write_text("".join(f"50:{d.mime}:*.x\n" for d in lib.mimedb.DEFINITIONS))

        self.process = FakeProcess(effects=effects)

    def module(self):
        return lib.Thumbnailers(self.home, self.process, self.system)

    def fail_dir(self):
        directory = self.home / lib.FAIL_DIR / "gnome-thumbnail-factory"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def test_fresh_home_is_unsatisfied_install_converges_and_second_run_changes_nothing(self):
        self.assertFalse(all(f.ok for f in self.module().findings()))
        self.module().apply()
        self.assertTrue(all(f.ok for f in self.module().findings()), [f.line() for f in self.module().findings() if not f.ok])
        self.assertEqual([argv[0] for argv in self.executed], ["update-mime-database"])
        self.module().apply()
        self.assertEqual(len(self.executed), 1, "nothing changed, so the MIME database is not rebuilt again")
        self.assertEqual(self.module().plan(), [])

    def test_files_land_where_gnome_and_glib_read_them(self):
        self.module().apply()
        entry = (self.home / ".local/share/thumbnailers/ubuntu-tweaks-previews.thumbnailer").read_text()
        self.assertTrue(entry.startswith("[Thumbnailer Entry]\nTryExec=/usr/bin/python3\nExec=/usr/bin/python3 -c "))
        self.assertIn("model/stl", entry)
        self.assertIn("font/woff2", (self.home / ".local/share/thumbnailers/ubuntu-tweaks-woff2.thumbnailer").read_text())
        self.assertIn("model/x-fbx", (self.home / ".local/share/mime/packages/ubuntu-tweaks-previews.xml").read_text())

    def test_a_changed_entry_is_rewritten_and_only_a_changed_mime_file_rebuilds_the_database(self):
        self.module().apply()
        entry = self.home / ".local/share/thumbnailers/ubuntu-tweaks-previews.thumbnailer"
        entry.write_text("stale\n")
        self.assertFalse(self.module().findings()[1].ok)
        self.module().apply()
        self.assertNotEqual(entry.read_text(), "stale\n")
        self.assertEqual(len(self.executed), 1)

    def test_missing_system_packages_are_a_manual_step_and_write_nothing(self):
        shutil.rmtree(self.system / "usr/lib/python3/dist-packages/PIL")
        finding = self.module().findings()[0]
        self.assertTrue(finding.manual and not finding.ok)
        with self.assertRaisesRegex(NeedsManual, "python3-pil"):
            self.module().apply()
        self.assertFalse((self.home / ".local").exists())

    def test_failure_markers_of_files_we_now_cover_are_forgotten_others_stay(self):
        directory = self.fail_dir()
        stale = directory / "a.png"
        stale.write_bytes(failure_marker("file:///home/alice/models/benchy%20v2.stl"))
        dockerfile = directory / "b.png"
        dockerfile.write_bytes(failure_marker("file:///srv/app/Dockerfile"))
        other = directory / "c.png"
        other.write_bytes(failure_marker("file:///home/alice/movie.mkv"))
        self.module().apply()
        self.assertFalse(stale.exists())
        self.assertFalse(dockerfile.exists())
        self.assertTrue(other.exists())

    def test_plan_names_every_change_and_never_prints_the_bundle(self):
        text = "\n".join(self.module().plan())
        self.assertIn("ubuntu-tweaks-previews.thumbnailer", text)
        self.assertIn("update-mime-database", text)
        self.assertNotIn("base64", text)
        self.assertLess(len(text), 20_000)

    def test_manifest_declares_a_user_level_system_module(self):
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.source.kind, "system")
        self.assertFalse(manifest.source.root)
        self.assertEqual(manifest.source.check, ("thumbnailers", "check"))
        self.assertIn("python3-pil", manifest.deps.apt)


class ScriptEntry(unittest.TestCase):
    def test_script_loads(self):
        loader = importlib.machinery.SourceFileLoader("thumbnailers_script", str(MODULE / "thumbnailers"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        script = importlib.util.module_from_spec(spec)
        loader.exec_module(script)
        self.assertTrue(callable(script.build))


if __name__ == "__main__":
    unittest.main()

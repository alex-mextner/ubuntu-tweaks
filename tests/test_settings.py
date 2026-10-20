"""Settings backends and keyfile helpers. Needs PyGObject, so it is skipped under an interpreter
without ``gi`` (scripts/ci.sh runs it under the distro python3)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from gi.repository import GLib  # noqa: E402

from ubuntu_tweaks.context import REPO_ROOT  # noqa: E402
from ubuntu_tweaks.errors import ConfigError  # noqa: E402
from ubuntu_tweaks.settings import (  # noqa: E402
    FakeBackend,
    FakeKey,
    FakeSchema,
    SettingsError,
    UserKey,
    default_extension_roots,
    dump_keyfile,
    extension_schema_dirs,
    format_value,
    is_satisfied,
    parse_keyfile,
    parse_value,
    path_to_section,
    read_keyfile,
    section_to_path,
    write_keyfile,
)
from tests.gio_scenario import FIXED, FIXED_PATH, ITEM, ITEM_PATH  # noqa: E402

SCHEMA_XML = """<schemalist>
  <schema id="test.ubuntu.tweaks" path="/test/ubuntu/tweaks/">
    <key name="count" type="u"><default>3</default></key>
    <key name="names" type="as"><default>['a']</default></key>
    <key name="ratio" type="d"><default>0.5</default></key>
    <key name="limited" type="i"><range min="0" max="10"/><default>1</default></key>
  </schema>
  <schema id="test.ubuntu.tweaks.item">
    <key name="name" type="s"><default>'x'</default></key>
  </schema>
</schemalist>
"""

DCONF_DUMP = """[/]
root-key=true

[org/gnome/desktop/interface]
clock-format='24h'
font-name='Ubuntu 11'
text-scaling-factor=1.25

[org/gnome/shell]
enabled-extensions=['a@b', 'c@d']
favorite-apps=@as []
idle-delay=uint32 300
"""


def fake_backend() -> FakeBackend:
    return FakeBackend(
        [
            FakeSchema(
                FIXED,
                {
                    "count": FakeKey("u", "3"),
                    "names": FakeKey("as", "['a']"),
                    "limited": FakeKey("i", "1", minimum="0", maximum="10"),
                },
                path=FIXED_PATH,
            ),
            FakeSchema(ITEM, {"name": FakeKey("s", "'x'")}),
        ]
    )


class FakeBackendTests(unittest.TestCase):
    def test_unset_key_is_effective_default_and_typed_compare_works(self) -> None:
        backend = fake_backend()
        self.assertIsNone(backend.user_value(FIXED, None, "count"))
        self.assertTrue(backend.effective(FIXED, None, "count").equal(backend.default(FIXED, None, "count")))
        self.assertTrue(is_satisfied(backend, FIXED, None, "count", parse_value(backend, FIXED, "count", "3")))
        self.assertFalse(backend.effective(FIXED, None, "count").equal(GLib.Variant("i", 3)))

    def test_write_then_effective_and_reset(self) -> None:
        backend = fake_backend()
        backend.write(FIXED, None, "count", parse_value(backend, FIXED, "count", "uint32 5"))

        self.assertTrue(is_satisfied(backend, FIXED, None, "count", parse_value(backend, FIXED, "count", "5")))
        self.assertEqual(len(backend.writes), 1)
        self.assertEqual(backend.list_user_keys(), (UserKey("/test/ubuntu/tweaks/", "count"),))

        backend.reset(FIXED, None, "count")
        self.assertIsNone(backend.user_value(FIXED, None, "count"))
        self.assertEqual(backend.resets, [(FIXED, None, "count")])
        self.assertEqual(backend.list_user_keys(), ())

    def test_wrong_type_is_refused(self) -> None:
        with self.assertRaisesRegex(SettingsError, "expected type 'u'"):
            fake_backend().write(FIXED, None, "count", GLib.Variant("s", "5"))

    def test_range_is_enforced_like_the_real_backend(self) -> None:
        backend = fake_backend()
        backend.write(FIXED, None, "limited", GLib.Variant("i", 10))
        for outside in (-1, 11):
            with self.subTest(value=outside), self.assertRaisesRegex(SettingsError, "outside the allowed range"):
                backend.write(FIXED, None, "limited", GLib.Variant("i", outside))
        self.assertEqual(backend.user_value(FIXED, None, "limited").unpack(), 10)

    def test_relocatable_schema_needs_a_path_and_keeps_values_per_path(self) -> None:
        backend = fake_backend()
        with self.assertRaisesRegex(SettingsError, "relocatable"):
            backend.effective(ITEM, None, "name")
        backend.write(ITEM, ITEM_PATH, "name", GLib.Variant("s", "one"))
        self.assertEqual(backend.effective(ITEM, ITEM_PATH, "name").unpack(), "one")
        self.assertEqual(backend.effective(ITEM, "/test/ubuntu/tweaks/items/two/", "name").unpack(), "x")

    def test_fixed_schema_rejects_a_different_path(self) -> None:
        with self.assertRaisesRegex(SettingsError, "fixed path"):
            fake_backend().effective(FIXED, "/other/", "count")

    def test_relocatable_path_must_be_a_dconf_directory(self) -> None:
        for bad in ("a/b/", "/a/b", "/a//b/", "", "/a/../b/", "/a/./b/", "/"):
            with self.subTest(path=bad), self.assertRaisesRegex(SettingsError, "not a dconf directory path"):
                fake_backend().effective(ITEM, bad, "name")

    def test_missing_schema_or_key(self) -> None:
        backend = fake_backend()
        self.assertTrue(backend.has_schema(FIXED))
        self.assertFalse(backend.has_schema("no.such"))
        self.assertTrue(backend.has_key(FIXED, "count"))
        self.assertFalse(backend.has_key(FIXED, "nope"))
        with self.assertRaisesRegex(SettingsError, "not installed"):
            backend.effective("no.such", None, "k")
        with self.assertRaisesRegex(SettingsError, "no key nope"):
            backend.effective(FIXED, None, "nope")

    def test_set_user_seeds_without_recording_a_write(self) -> None:
        backend = fake_backend()
        backend.set_user(FIXED, None, "names", "['x', 'y']")
        self.assertEqual(backend.user_value(FIXED, None, "names").unpack(), ["x", "y"])
        self.assertEqual(backend.writes, [])


class ParseValueTests(unittest.TestCase):
    def test_text_is_parsed_with_the_schema_type(self) -> None:
        backend = fake_backend()
        self.assertEqual(parse_value(backend, FIXED, "count", "5").get_type_string(), "u")
        self.assertEqual(parse_value(backend, FIXED, "names", "[]").get_type_string(), "as")

    def test_invalid_text_is_a_settings_error(self) -> None:
        with self.assertRaisesRegex(SettingsError, "not a valid 'u' value"):
            parse_value(fake_backend(), FIXED, "count", "'five'")

    def test_format_value_is_gvariant_text(self) -> None:
        self.assertEqual(format_value(GLib.Variant("u", 5)), "uint32 5")
        self.assertEqual(format_value(GLib.Variant("as", ["a", "b"])), "['a', 'b']")
        self.assertEqual(format_value(GLib.Variant("s", "it's")), '"it\'s"')


class KeyfileTests(unittest.TestCase):
    def test_parse_reads_sections_keys_and_raw_gvariant_text(self) -> None:
        data = parse_keyfile(DCONF_DUMP)
        self.assertEqual(data["/"], {"root-key": "true"})
        self.assertEqual(data["org/gnome/shell"]["idle-delay"], "uint32 300")
        self.assertEqual(data["org/gnome/shell"]["favorite-apps"], "@as []")
        self.assertEqual(data["org/gnome/shell"]["enabled-extensions"], "['a@b', 'c@d']")
        self.assertEqual(data["org/gnome/desktop/interface"]["font-name"], "'Ubuntu 11'")

    def test_round_trip_is_lossless_and_sorted(self) -> None:
        data = parse_keyfile(DCONF_DUMP)
        dumped = dump_keyfile(data)
        self.assertEqual(parse_keyfile(dumped), data)
        self.assertEqual(dumped, dump_keyfile(parse_keyfile(dumped)))
        sections = [line for line in dumped.splitlines() if line.startswith("[")]
        self.assertEqual(sections, sorted(sections))

    def test_dump_output_matches_dconf_syntax(self) -> None:
        self.assertEqual(
            dump_keyfile({"org/x": {"b": "2", "a": "'one'"}}),
            "[org/x]\na='one'\nb=2\n",
        )

    def test_values_with_quotes_unicode_and_backslashes_survive(self) -> None:
        values = {"k1": "'it\\'s'", "k2": "'привет'", "k3": "'a\\\\b'", "k4": '"say \\"hi\\""'}
        self.assertEqual(parse_keyfile(dump_keyfile({"s": values}))["s"], values)

    def test_file_round_trip_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings" / "power.ini"
            write_keyfile(path, {"org/gnome/x": {"k": "1"}})
            self.assertEqual(read_keyfile(path), {"org/gnome/x": {"k": "1"}})
            self.assertEqual([entry.name for entry in path.parent.iterdir()], ["power.ini"])
            write_keyfile(path, {"org/gnome/x": {"k": "2"}})
            self.assertEqual(read_keyfile(path), {"org/gnome/x": {"k": "2"}})
            self.assertEqual([entry.name for entry in path.parent.iterdir()], ["power.ini"])

    def test_malformed_and_unreadable_files_are_config_errors(self) -> None:
        with self.assertRaisesRegex(ConfigError, "invalid keyfile"):
            parse_keyfile("not a keyfile\n")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "cannot read"):
                read_keyfile(Path(tmp) / "absent.ini")
            bad = Path(tmp) / "bad.ini"
            bad.write_text("[broken\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "bad.ini: invalid keyfile"):
                read_keyfile(bad)

    def test_section_and_path_conversion(self) -> None:
        self.assertEqual(section_to_path("org/gnome/shell"), "/org/gnome/shell/")
        self.assertEqual(section_to_path("/"), "/")
        self.assertEqual(path_to_section("/org/gnome/shell/"), "org/gnome/shell")
        self.assertEqual(path_to_section("/"), "/")


class ExtensionSchemaDirTests(unittest.TestCase):
    def test_only_extensions_with_compiled_schemas_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "with@x/schemas").mkdir(parents=True)
            (root / "with@x/schemas/gschemas.compiled").write_bytes(b"")
            (root / "without@x/schemas").mkdir(parents=True)
            (root / "no-schemas@x").mkdir()

            self.assertEqual(extension_schema_dirs([root, root / "absent"]), (root / "with@x/schemas",))

    def test_default_roots_ascend_in_precedence_and_honor_xdg_data_home(self) -> None:
        home = Path("/home/u")
        self.assertEqual(
            default_extension_roots(home, {}),
            (
                Path("/usr/share/gnome-shell/extensions"),
                Path("/usr/local/share/gnome-shell/extensions"),
                home / ".local/share/gnome-shell/extensions",
            ),
        )
        self.assertEqual(
            default_extension_roots(home, {"XDG_DATA_HOME": "/data"})[-1], Path("/data/gnome-shell/extensions")
        )


def find_tool(name: str) -> str | None:
    """Prefer the distro binary: a brew dbus-daemon cannot activate the distro's dconf service."""
    return shutil.which(name, path="/usr/bin:/bin") or shutil.which(name)


class GioBackendTests(unittest.TestCase):
    def test_typed_reads_writes_and_relocatable_paths_in_a_scratch_session(self) -> None:
        dbus_run_session = find_tool("dbus-run-session")
        compile_schemas = find_tool("glib-compile-schemas")
        if dbus_run_session is None or compile_schemas is None or find_tool("dconf") is None:
            self.skipTest("dbus-run-session, glib-compile-schemas and dconf are required")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schemas = root / "schemas"
            schemas.mkdir()
            (schemas / "test.ubuntu.tweaks.gschema.xml").write_text(SCHEMA_XML, encoding="utf-8")
            subprocess.run([compile_schemas, str(schemas)], check=True, capture_output=True)

            profile = root / "dconf-profile"
            profile.write_text("user-db:user\n", encoding="utf-8")
            runtime = root / "run"
            runtime.mkdir(mode=0o700)
            env = {
                key: os.environ[key]
                for key in ("PATH", "LANG", "XDG_DATA_DIRS", "LD_LIBRARY_PATH", "GI_TYPELIB_PATH")
                if key in os.environ
            }
            env.update(
                HOME=str(root / "home"),
                XDG_CONFIG_HOME=str(root / "config"),
                XDG_DATA_HOME=str(root / "data"),
                XDG_CACHE_HOME=str(root / "cache"),
                XDG_STATE_HOME=str(root / "state"),
                XDG_RUNTIME_DIR=str(runtime),
                DCONF_PROFILE=str(profile),
                GSETTINGS_BACKEND="dconf",
                PYTHONPATH=str(REPO_ROOT),
                UBUNTU_TWEAKS_SCRATCH="1",
            )

            result = subprocess.run(
                [dbus_run_session, "--", sys.executable, str(REPO_ROOT / "tests/gio_scenario.py"), str(schemas)],
                env=env,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )

        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

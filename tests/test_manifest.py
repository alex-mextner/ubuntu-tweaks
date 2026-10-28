"""module.toml parsing and validation."""

import tempfile
import unittest
from pathlib import Path

from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import (
    Deps,
    ExtensionSpec,
    Migration,
    Seed,
    SettingsFile,
    SettingsMode,
    SourceKind,
    discover_modules,
    load_manifest,
    module_defaults,
    Manifest,
    parse_manifest,
)

HYPERDOCK = """
description = "macOS-style dock"

[source]
kind = "submodule"
path = "gnome-extensions/hyperdock"

[extension]
conflicts = ["ubuntu-dock@ubuntu.com"]
retire = ["compiz-alike-magic-lamp-effect@hermes83.github.com"]

[[settings]]
file = "dock.ini"
mode = "enforce"

[[settings]]
file = "extra.ini"
schema = "org.example.extra"
mode = "seed"

[[seeds]]
key = "folder-stacks"
provider = "screenshots-stack"

[[migrations]]
from_uuid = "dash2dock-lite@icedman.github.com"
from_path = "/org/gnome/shell/extensions/dash2dock-lite/"

[deps]
apt = ["gir1.2-gtk-4.0"]
"""

SYSTEM = """
enabled = false

[source]
kind = "system"
install = "install.sh"
check = "install.sh --check"
"""

DIRECTORY = Path("/repo/modules/x")


def parse(text: str, name: str = "x") -> Manifest:
    return parse_manifest(name, text, DIRECTORY)


class ParseTests(unittest.TestCase):
    def test_full_submodule_manifest(self) -> None:
        manifest = parse(HYPERDOCK, "hyperdock")

        self.assertEqual(manifest.name, "hyperdock")
        self.assertEqual(manifest.description, "macOS-style dock")
        self.assertTrue(manifest.enabled)
        self.assertEqual(
            (manifest.source.kind, manifest.source.path), (SourceKind.SUBMODULE, "gnome-extensions/hyperdock")
        )
        self.assertEqual(
            manifest.extension,
            ExtensionSpec(("ubuntu-dock@ubuntu.com",), ("compiz-alike-magic-lamp-effect@hermes83.github.com",)),
        )
        self.assertEqual(
            manifest.settings,
            (
                SettingsFile("dock.ini", None, SettingsMode.ENFORCE),
                SettingsFile("extra.ini", "org.example.extra", SettingsMode.SEED),
            ),
        )
        self.assertEqual(manifest.seeds, (Seed("folder-stacks", "screenshots-stack"),))
        self.assertEqual(
            manifest.migrations,
            (Migration("dash2dock-lite@icedman.github.com", "/org/gnome/shell/extensions/dash2dock-lite/"),),
        )
        self.assertEqual(manifest.deps, Deps(("gir1.2-gtk-4.0",)))

    def test_system_manifest_splits_commands_and_can_default_to_disabled(self) -> None:
        manifest = parse(SYSTEM, "apple-hid-battery")

        self.assertFalse(manifest.enabled)
        self.assertEqual(manifest.source.kind, SourceKind.SYSTEM)
        self.assertEqual(manifest.source.install, ("install.sh",))
        self.assertEqual(manifest.source.check, ("install.sh", "--check"))

    def test_minimal_local_and_builtin(self) -> None:
        local = parse('[source]\nkind = "local"\npath = "gnome-extensions/english-in-overview@ubuntu-tweaks"\n')
        self.assertEqual((local.source.kind, local.description, local.extension), (SourceKind.LOCAL, "", None))
        builtin = parse(
            '[source]\nkind = "builtin"\n[[settings]]\nfile = "k.ini"\nschema = "org.gnome.desktop.interface"\n'
        )
        self.assertEqual(builtin.source.kind, SourceKind.BUILTIN)

    def test_module_defaults_layer(self) -> None:
        manifests = (parse(HYPERDOCK, "hyperdock"), parse(SYSTEM, "memguard"))
        self.assertEqual(
            module_defaults(manifests),
            {"modules": {"hyperdock": {"enabled": True}, "memguard": {"enabled": False}}},
        )


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, text: str, pattern: str, name: str = "x") -> None:
        with self.assertRaisesRegex(ConfigError, pattern):
            parse(text, name)

    def test_stall_seconds_is_a_whole_number_of_seconds_from_5_to_86400(self) -> None:
        self.assertIsNone(parse('[source]\nkind = "builtin"\n', "x").stall_seconds)
        self.assertEqual(parse('stall_seconds = 600\n[source]\nkind = "builtin"\n', "x").stall_seconds, 600)
        for bad in ("4", "86401", "0", "-1", '"120"', "1.5", "true"):
            with self.subTest(bad=bad):
                self.assertInvalid(f'stall_seconds = {bad}\n[source]\nkind = "builtin"\n', "'stall_seconds' must be")

    def test_ego_is_reserved_and_rejected(self) -> None:
        self.assertInvalid('[source]\nkind = "ego"\nuuid = "a@b"\n', r"not supported yet \(#20\)")

    def test_unknown_kind_lists_valid_ones(self) -> None:
        self.assertInvalid('[source]\nkind = "magic"\n', "valid kinds: submodule, local, builtin, system")

    def test_source_table_is_required(self) -> None:
        self.assertInvalid('description = "no source"\n', r"missing required table \[source\]")

    def test_unknown_keys_are_rejected_at_every_level(self) -> None:
        self.assertInvalid('colour = "red"\n[source]\nkind = "builtin"\n', "unknown key.*colour")
        self.assertInvalid('[source]\nkind = "builtin"\npath = "x"\n', "unknown key.*path")
        self.assertInvalid(
            '[source]\nkind = "local"\npath = "p"\n[extension]\nconflict = []\n', "unknown key.*conflict"
        )

    def test_code_sources_need_a_relative_path(self) -> None:
        self.assertInvalid('[source]\nkind = "submodule"\n', "missing required key 'path'")
        self.assertInvalid('[source]\nkind = "local"\npath = "/etc/x"\n', "relative path")
        self.assertInvalid('[source]\nkind = "local"\npath = "a/../../x"\n', "without '..'")
        for dot in (".", "./"):
            self.assertInvalid(f'[source]\nkind = "local"\npath = "{dot}"\n', "inside the module")

    def test_system_needs_install_and_check_inside_the_module(self) -> None:
        self.assertInvalid('[source]\nkind = "system"\ninstall = "install.sh"\n', "missing required key 'check'")
        self.assertInvalid('[source]\nkind = "system"\ninstall = "../x.sh"\ncheck = "x --check"\n', "without '..'")
        self.assertInvalid(
            '[source]\nkind = "system"\ninstall = "\'unterminated"\ncheck = "x"\n', "not a valid command line"
        )
        for empty in ('" "', "\"''\""):
            self.assertInvalid(
                f'[source]\nkind = "system"\ninstall = {empty}\ncheck = "x"\n', "must name a program inside the module"
            )

    def test_system_takes_no_extension_settings_seeds_or_migrations(self) -> None:
        base = '[source]\nkind = "system"\ninstall = "i.sh"\ncheck = "i.sh --check"\n'
        self.assertInvalid(base + "[extension]\n", r"\[extension\] is only for submodule, local and builtin")
        self.assertInvalid(base + '[[settings]]\nfile = "a.ini"\n', "system module takes no")

    def test_builtin_settings_need_a_schema(self) -> None:
        self.assertInvalid('[source]\nkind = "builtin"\n[[settings]]\nfile = "a.ini"\n', "'schema' is required")

    def test_settings_mode_and_seed_and_migration_fields(self) -> None:
        code = '[source]\nkind = "local"\npath = "p"\n'
        self.assertInvalid(code + '[[settings]]\nfile = "a.ini"\nmode = "force"\n', "unknown mode 'force'")
        self.assertInvalid(code + '[[seeds]]\nkey = "k"\n', "missing required key 'provider'")
        self.assertInvalid(code + "[[migrations]]\n", "needs 'from_uuid' or 'from_path'")
        for bad in ("org/x", "/", "//", "/a//b/", "/a/../b/"):
            self.assertInvalid(code + f'[[migrations]]\nfrom_path = "{bad}"\n', "dconf path")

    def test_wrong_types(self) -> None:
        code = '[source]\nkind = "local"\npath = "p"\n'
        self.assertInvalid('enabled = "yes"\n' + code, "'enabled' must be true or false")
        self.assertInvalid(code + "[deps]\napt = 3\n", "list of non-empty strings")
        self.assertInvalid('settings = "x"\n' + code, r"array of tables \(\[\[settings\]\]\)")

    def test_invalid_toml_and_invalid_name(self) -> None:
        self.assertInvalid("[source\n", "invalid TOML")
        self.assertInvalid('[source]\nkind = "builtin"\n', "invalid module name", name="Bad Name")

    def test_errors_name_the_manifest_file(self) -> None:
        self.assertInvalid('[source]\nkind = "magic"\n', "/repo/modules/x/module.toml")


class DiscoveryTests(unittest.TestCase):
    def test_discover_sorts_and_skips_directories_without_a_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("zeta", "alpha"):
                (root / name).mkdir()
                (root / name / "module.toml").write_text('[source]\nkind = "builtin"\n', encoding="utf-8")
            (root / "legacy").mkdir()

            self.assertEqual([m.name for m in discover_modules(root)], ["alpha", "zeta"])
            self.assertEqual(load_manifest(root / "alpha").directory, root / "alpha")

    def test_missing_modules_dir_means_no_modules(self) -> None:
        self.assertEqual(discover_modules(Path("/nonexistent/modules")), ())

    def test_unreadable_manifest_is_a_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ConfigError, "cannot read"):
            load_manifest(Path(tmp))

    def test_every_manifest_in_this_repo_is_valid(self) -> None:
        modules = discover_modules(REPO_ROOT / "modules")
        for manifest in modules:
            if manifest.source.path is not None:
                self.assertTrue((REPO_ROOT / manifest.source.path).exists(), manifest.name)

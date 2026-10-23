"""`ubuntu-tweak capture`: glob and secret-scan units, rule tables, classifier, settings scan,
report golden, and `--commit` into a temporary profile and HOME. No live dconf or HOME is touched:
settings come from ``FakeBackend``, system tools from a fake runner."""

import argparse
import contextlib
import gzip
import io
import os
import re
import tomllib
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks.cli import build_parser
from ubuntu_tweaks.capture import commit as commit_module
from ubuntu_tweaks.capture.files_scan import classify_file
from ubuntu_tweaks.capture.globs import GlobSet, compile_glob
from ubuntu_tweaks.capture.plan import CapturePlan, build_plan, refusals
from ubuntu_tweaks.capture.report import build_document, render_markdown, render_text
from ubuntu_tweaks.capture.rules import PANEL_IDS, PANELS_TOML, Rules, load_rules
from ubuntu_tweaks.capture.secrets import key_names, scan_text, scan_value
from ubuntu_tweaks.capture.system_scan import scan_packages
from ubuntu_tweaks.capture.settings_scan import SettingsResult, has_machine_path
from ubuntu_tweaks.commands import capture as capture_command
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.errors import ConfigError, RefusedError
from ubuntu_tweaks.profile import Packages, Profile
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema, UserKey, parse_value, read_keyfile

# Shaped like real secrets so the scanner fires, assembled here so no scanner flags this file.
FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" + "k1L2m3N4o5P6"
FAKE_PEM = "-----BEGIN " + "PRIVATE KEY-----"
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

SCHEMAS = (
    FakeSchema(
        "org.gnome.desktop.interface",
        {
            "color-scheme": FakeKey("s", "'default'"),
            "clock-format": FakeKey("s", "'24h'"),
            "show-battery-percentage": FakeKey("b", "false"),
        },
        "/org/gnome/desktop/interface/",
    ),
    FakeSchema("org.gnome.desktop.background", {"picture-uri": FakeKey("s", "''")}, "/org/gnome/desktop/background/"),
    FakeSchema(
        "org.gnome.settings-daemon.plugins.power",
        {"sleep-inactive-ac-timeout": FakeKey("i", "1200")},
        "/org/gnome/settings-daemon/plugins/power/",
    ),
    FakeSchema(
        "org.gnome.shell",
        {"enabled-extensions": FakeKey("as", "@as []"), "app-picker-layout": FakeKey("s", "''")},
        "/org/gnome/shell/",
    ),
    FakeSchema("org.gnome.Ptyxis", {"window-size": FakeKey("(ii)", "(0, 0)")}, "/org/gnome/Ptyxis/"),
    FakeSchema("org.example.App", {"contact": FakeKey("s", "''"), "mode": FakeKey("s", "'a'")}, "/org/example/app/"),
    FakeSchema("org.example.Weird", {"thing": FakeKey("i", "0")}, "/org/example/weird/"),
    FakeSchema("org.gnome.desktop.notifications.application", {"enable": FakeKey("b", "true")}, None),
)


# Volatile keys of a real laptop dump (`dconf dump /`): schema -> (dconf directory, {key: (type, default, user value)}).
# The coordinates are made up; the key names, schemas and paths are the real ones.
LAPTOP_VOLATILE = {
    "org.gnome.settings-daemon.plugins.color": (
        "/org/gnome/settings-daemon/plugins/color/",
        {"night-light-last-coordinates": ("(dd)", "(91.0, 181.0)", "(10.5, 20.25)")},
    ),
    "org.gnome.deja-dup": (
        "/org/gnome/deja-dup/",
        {
            "prompt-check": ("s", "''", "'disabled'"),
            "window-height": ("i", "0", "460"),
            "window-width": ("i", "0", "550"),
        },
    ),
    "org.gnome.shell": (
        "/org/gnome/shell/",
        {
            "last-selected-power-profile": ("s", "''", "'power-saver'"),
            "welcome-dialog-last-shown-version": ("s", "''", "'50.1'"),
        },
    ),
    "org.gnome.control-center": (
        "/org/gnome/control-center/",
        {"last-panel": ("s", "''", "'bluetooth'"), "window-state": ("(iib)", "(0, 0, false)", "(980, 874, true)")},
    ),
}


class NoSchemaBackend(FakeBackend):
    """Also lists a user key that no schema describes, as ``dconf dump`` would."""

    def list_user_keys(self) -> tuple[UserKey, ...]:
        return (*super().list_user_keys(), UserKey("/org/example/ghost/", "level"))


def seeded_backend() -> FakeBackend:
    backend = NoSchemaBackend(SCHEMAS)
    backend.set_user("org.gnome.desktop.interface", None, "color-scheme", "'prefer-dark'")
    backend.set_user("org.gnome.desktop.interface", None, "clock-format", "'24h'")  # equals the default
    backend.set_user("org.gnome.desktop.interface", None, "show-battery-percentage", "true")
    backend.set_user("org.gnome.desktop.background", None, "picture-uri", "'file:///home/me/Pictures/wall.jpg'")
    backend.set_user("org.gnome.settings-daemon.plugins.power", None, "sleep-inactive-ac-timeout", "3600")
    backend.set_user("org.gnome.shell", None, "enabled-extensions", "['ding@rastersoft.com']")
    backend.set_user("org.gnome.shell", None, "app-picker-layout", "'x'")
    backend.set_user("org.gnome.Ptyxis", None, "window-size", "(100, 200)")
    backend.set_user("org.example.App", None, "contact", "'someone@example.com'")
    backend.set_user("org.example.App", None, "mode", "'b'")
    backend.set_user("org.example.Weird", None, "thing", "7")
    backend.set_user(
        "org.gnome.desktop.notifications.application",
        "/org/gnome/desktop/notifications/application/foo/",
        "enable",
        "false",
    )
    return backend


def fake_runner(argv: Sequence[str]) -> str | None:
    if argv[0] == "env":  # `env NAME=value cmd ...`: the scan isolates flatpak's cache this way
        return fake_runner(argv[2:])
    command = argv[0]
    if command == "apt-mark":
        return "curl\nlibssl-dev\nlibfoo1\nlinux-image-1\nripgrep\nubuntu-desktop\ntmux\n"
    if command == "apt-cache":
        return "ubuntu-minimal\n  Depends: curl\n  Depends: <virtual>\n |Depends: bash\n"
    if command == "dpkg-query":
        return "curl\tstandard\nbash\trequired\nripgrep\toptional\n"
    if command == "snap":
        return (
            "Name Version Rev Tracking Publisher Notes\ncore24 1 1 x y base\nfirefox 1 2 x y -\ncode 1 3 x y classic\n"
        )
    if command == "flatpak":
        return "org.example.Flat\n"
    if command == "timedatectl":
        return "Europe/Belgrade\n"
    if command == "localectl":
        return "   System Locale: LANG=en_US.UTF-8\n       VC Keymap: us\n"
    return None


def commit_plan(plan: CapturePlan, rules: Rules, home: Path) -> list[str]:
    return commit_module.commit(plan, email_exempt_keys=rules.email_exempt_keys, home=home, now=NOW)


def sources(case: TempDirCase) -> dict[str, Path]:
    keyboard = write(
        case.tmp / "keyboard", 'XKBMODEL="pc105"\nXKBLAYOUT="us,ru"\nXKBVARIANT=","\nXKBOPTIONS="grp_led:scroll"\n'
    )
    return {
        "installer_baseline": case.tmp / "no-initial-status.gz",
        "snap_seed": case.tmp / "no-seed.yaml",
        "keyboard_file": keyboard,
    }


def make_plan(
    case: TempDirCase,
    backend: FakeBackend | None = None,
    *,
    capture: dict[str, object] | None = None,
    only: frozenset[str] = frozenset(),
) -> tuple[CapturePlan, Rules]:
    home = case.tmp / "home"
    home.mkdir(exist_ok=True)
    rules = load_rules(capture)
    plan = build_plan(
        Profile(case.tmp / "profile", "testbox", "flag"),
        rules,
        backend or seeded_backend(),
        home=home,
        only=only,
        run=fake_runner,
        **sources(case),
    )
    return plan, rules


class GlobTests(unittest.TestCase):
    def test_star_stays_in_a_segment_and_double_star_crosses_them(self) -> None:
        self.assertTrue(compile_glob("/org/gnome/mutter/*").fullmatch("/org/gnome/mutter/edge-tiling"))
        self.assertFalse(compile_glob("/org/gnome/mutter/*").fullmatch("/org/gnome/mutter/wayland/x"))
        self.assertTrue(compile_glob("/org/gnome/mutter/**").fullmatch("/org/gnome/mutter/wayland/x"))

    def test_double_star_between_slashes_also_matches_zero_segments(self) -> None:
        self.assertTrue(compile_glob("/**/window-*").fullmatch("/window-size"))
        self.assertTrue(compile_glob("/**/window-*").fullmatch("/a/b/window-size"))
        self.assertFalse(compile_glob("/**/window-*").fullmatch("/a/b/xwindow-size"))

    def test_first_returns_the_matching_pattern(self) -> None:
        self.assertEqual(GlobSet(["/a/*", "/b/**"]).first("/b/c/d"), "/b/**")
        self.assertIsNone(GlobSet(["/a/*"]).first("/x"))


class SecretScanTests(unittest.TestCase):
    def kinds(self, text: str) -> set[str]:
        return {finding.kind for finding in scan_value(text)}

    def test_each_pattern_class_is_detected(self) -> None:
        samples = {
            "pem-block": FAKE_PEM,
            "api-token": FAKE_TOKEN,
            "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghijk",
            "email": "me@example.com",
            "url-credentials": "https://user:hunter2@host.example/x",
            "hex-run": "d" * 40,
            "base64-run": "Ab3" * 15,
            "lan-ip": "192.168.1.20",
        }
        for kind, sample in samples.items():
            self.assertIn(kind, self.kinds(sample), kind)
        self.assertIn("api-token", self.kinds("sk-" + "x1" * 15))
        self.assertIn("api-token", self.kinds("AKIA" + "ABCDEFGHIJKLMNOP"))

    def test_ordinary_values_are_clean(self) -> None:
        for value in (
            "'Yaru-blue-dark'",
            "'file:///usr/share/backgrounds/a_b-C.jpg'",
            "uint32 5",
            "['<Super>Left']",
            "'/usr/share/x'",
        ):
            self.assertEqual(scan_value(value), (), value)

    def test_a_long_mixed_case_path_is_not_a_base64_token(self) -> None:
        self.assertEqual(scan_value("'file:///home/Alex/Pictures/Wallpapers/2024/Mountains.jpg'"), ())
        self.assertEqual(scan_text("Exec=/opt/MyApp2/bin/Launcher/run/x/y/z-app-startup\n"), ())

    def test_lan_ip_is_an_identity_flag_and_public_ip_is_clean(self) -> None:
        self.assertEqual({f.cls for f in scan_value("10.0.0.5")}, {"identity"})
        self.assertEqual(scan_value("8.8.8.8"), ())

    def test_findings_never_carry_the_matched_text(self) -> None:
        for finding in scan_text(f"token = {FAKE_TOKEN}\nmail = me@example.com"):
            self.assertNotIn("ghp_", repr(finding))
            self.assertNotIn("example", repr(finding))
        self.assertEqual([f.line for f in scan_text("a\nb = me@example.com")], [2])

    def test_extension_uuids_are_exempt_only_for_listed_keys(self) -> None:
        text = "enabled-extensions=['ding@rastersoft.com']"
        self.assertEqual(scan_text(text, exempt_email_keys=["enabled-extensions"]), ())
        self.assertEqual({f.kind for f in scan_text(text)}, {"email"})
        self.assertTrue(scan_text("other=['ding@rastersoft.com']", exempt_email_keys=["enabled-extensions"]))

    def test_key_names_lists_names_not_values(self) -> None:
        names = key_names(
            'api_key = "x"\nlevel = 3\n"password": "y"\nempty_token =\n', ["api[_-]?key", "passw", "token"]
        )
        self.assertEqual(names, ("api_key", "password"))


class RuleTableTests(unittest.TestCase):
    def test_panel_table_has_exactly_the_contract_ids_plus_shell_extensions(self) -> None:
        rules = load_rules()
        self.assertEqual(set(rules.panels), set(PANEL_IDS) | {"shell-extensions"})
        for rule in rules.panel_rules:
            self.assertIn(rule.panel, rules.panels)

    def test_spec_table_and_panels_toml_list_the_same_panels(self) -> None:
        spec = (REPO_ROOT / "docs/specs/ubuntu-tweak-cli.md").read_text(encoding="utf-8")
        section = spec.split("### Panel coverage", 1)[1].split("### Per-machine", 1)[0]
        in_spec = set(re.findall(r"^\| `([a-z-]+)` \|", section, re.MULTILINE))
        self.assertEqual(in_spec, set(load_rules().panels), PANELS_TOML)

    def test_specific_rules_beat_general_ones(self) -> None:
        rules = load_rules()

        def panel(full: str) -> str | None:
            rule = rules.panel_rule(full)
            return rule.panel if rule else None

        self.assertEqual(panel("/org/gnome/desktop/interface/color-scheme"), "appearance")
        self.assertEqual(panel("/org/gnome/desktop/interface/clock-format"), "date-time")
        self.assertEqual(panel("/org/gnome/desktop/interface/toolkit-accessibility"), "accessibility")
        self.assertEqual(panel("/org/gnome/desktop/interface/show-battery-percentage"), "power")
        self.assertEqual(panel("/org/gnome/mutter/keybindings/toggle-tiled-left"), "keyboard")
        self.assertEqual(panel("/org/gnome/mutter/edge-tiling"), "multitasking")
        self.assertEqual(panel("/org/gnome/shell/extensions/tiling-assistant/tile-left-half"), "keyboard")
        self.assertEqual(panel("/org/gnome/shell/extensions/tiling-assistant/enable-tiling-popup"), "ubuntu-desktop")
        self.assertEqual(panel("/org/gnome/shell/extensions/some-other/key"), "shell-extensions")
        self.assertIsNone(panel("/org/example/app/mode"))

    def test_profile_capture_unknown_key_is_invalid_config(self) -> None:
        with self.assertRaises(ConfigError):
            load_rules({"exlude": []})
        with self.assertRaises(ConfigError):
            load_rules({"exclude": "not a list"})

    def test_profile_cannot_switch_off_a_secret_exclude(self) -> None:
        rules = load_rules({"machine": ["/**"]})
        hard = rules.hard_exclude("/org/gnome/desktop/remote-desktop/rdp/tls-key")
        self.assertIsNotNone(hard)
        self.assertEqual(hard.cls if hard else None, "secret")

    def test_a_profile_machine_match_protects_a_key_from_the_state_globs(self) -> None:
        full = "/org/gnome/shell/last-selected-power-profile"
        self.assertIsNotNone(load_rules().state_rule(full))
        rules = load_rules({"machine": [full]})
        self.assertIsNotNone(rules.machine_rule(full))

    def assert_state(self, full: str) -> None:
        rules = load_rules()
        self.assertIsNotNone(rules.state_rule(full), full)
        self.assertIsNone(rules.machine_rule(full), f"{full} must not be a machine key: it would be captured")

    def test_night_light_last_coordinates_is_state_not_a_machine_key(self) -> None:
        self.assert_state("/org/gnome/settings-daemon/plugins/color/night-light-last-coordinates")

    def test_last_selected_power_profile_is_state_not_a_machine_key(self) -> None:
        self.assert_state("/org/gnome/shell/last-selected-power-profile")

    def test_prompt_check_is_state(self) -> None:
        self.assert_state("/org/gnome/DejaDup/prompt-check")

    def test_last_star_is_state(self) -> None:
        self.assert_state("/org/gnome/gnome-system-monitor/last-tab")

    def test_star_last_star_is_state(self) -> None:
        self.assert_state("/org/gnome/shell/welcome-dialog-last-shown-version")

    def test_last_selected_star_is_state(self) -> None:
        self.assert_state("/org/gnome/Characters/last-selected-category")

    def test_window_geometry_is_state(self) -> None:
        for name in (
            "window-size",
            "window-width",
            "window-height",
            "window-state",
            "window-position",
            "window-maximized",
        ):
            self.assert_state(f"/org/gnome/Ptyxis/{name}")

    def test_a_profile_include_lifts_a_volatile_key_back(self) -> None:
        rules = load_rules({"include": ["/org/gnome/shell/last-selected-power-profile"]})
        self.assertIsNone(rules.state_rule("/org/gnome/shell/last-selected-power-profile"))

    def test_state_globs_do_not_swallow_keybindings_or_privacy_preferences(self) -> None:
        rules = load_rules()
        for full in (
            "/org/gnome/desktop/wm/keybindings/toggle-maximized",
            "/org/gnome/desktop/wm/keybindings/activate-window-menu",
            "/org/gnome/desktop/privacy/remember-recent-files",
            "/org/gnome/desktop/privacy/recent-files-max-age",
        ):
            self.assertIsNone(rules.state_rule(full), full)
        self.assertIsNotNone(rules.state_rule("/org/gnome/Ptyxis/window-size"))

    def test_dconf_object_paths_are_not_filesystem_paths(self) -> None:
        backend = seeded_backend()
        text = "'/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/'"
        self.assertFalse(has_machine_path(parse_value(backend, "org.example.App", "mode", text)))
        self.assertFalse(has_machine_path(parse_value(backend, "org.example.App", "mode", "'https://example.org/a'")))

    def test_absolute_paths_outside_usr_share_are_machine_paths(self) -> None:
        backend = seeded_backend()

        def machine(text: str) -> bool:
            return has_machine_path(parse_value(backend, "org.example.App", "mode", text))

        self.assertTrue(machine("'/home/x/y'"))
        self.assertTrue(machine("'file:///home/x/y.jpg'"))
        self.assertFalse(machine("'file:///usr/share/b/y.jpg'"))
        self.assertFalse(machine("'plain'"))


class ClassifierTests(unittest.TestCase):
    rules = load_rules().files

    def classify(self, name: str, data: bytes) -> str:
        return classify_file(name, len(data), data, self.rules)[0]

    def test_small_plain_text_is_portable_config(self) -> None:
        self.assertEqual(self.classify("htoprc", b"fields=0 48\ncolor_scheme=0\n"), "portable-config")
        self.assertEqual(self.classify("settings.ini", b"[Settings]\ngtk-theme-name=Yaru\n"), "portable-config")

    def test_credential_names_are_secret_bearing_without_reading_content(self) -> None:
        for name in ("credentials.json", "id_ed25519", ".env", "desktop-password", "x.pem", "tg.env"):
            self.assertEqual(classify_file(name, 10, None, self.rules)[0], "secret-bearing", name)

    def test_secret_value_inside_makes_it_secret_bearing(self) -> None:
        self.assertEqual(self.classify("notes.conf", f"x = {FAKE_TOKEN}\n".encode()), "secret-bearing")

    def test_credential_key_names_without_a_secret_value_are_mixed(self) -> None:
        cls, reason = classify_file("app.toml", 30, b'api_key = "short"\nport = 1\n', self.rules)
        self.assertEqual(cls, "mixed")
        self.assertIn("api_key", reason)
        self.assertNotIn("short", reason)

    def test_binary_large_empty_and_log_files_are_cache_state(self) -> None:
        self.assertEqual(self.classify("blob", b"\x00\x01binary"), "cache-state")
        self.assertEqual(classify_file("big.json", 10**6, None, self.rules)[0], "cache-state")
        self.assertEqual(classify_file("marker", 0, b"", self.rules)[0], "cache-state")
        self.assertEqual(classify_file("run.log", 10, b"x", self.rules)[0], "cache-state")
        self.assertEqual(classify_file("a.sqlite3", 10, b"x", self.rules)[0], "cache-state")


class SettingsScanTests(TempDirCase):
    def test_every_user_key_is_accounted_for_exactly_once(self) -> None:
        plan, _rules = make_plan(self)
        result = plan.settings
        assert result is not None
        self.assertEqual(
            len(result.entries) + len(result.skipped) + len(result.unmapped) + result.at_default,
            result.user_keys,
        )
        self.assertEqual(result.user_keys, 13)

    def test_classification_of_the_scenario(self) -> None:
        plan, _rules = make_plan(self)
        result = plan.settings
        assert result is not None
        self.assertEqual(
            {(e.panel, e.target, e.key, e.mark) for e in result.entries},
            {
                ("appearance", "shared", "color-scheme", "+"),
                ("power", "machine", "show-battery-percentage", "+"),
                ("power", "machine", "sleep-inactive-ac-timeout", "+"),
                ("background", "machine", "picture-uri", "+"),
                ("notifications", "shared", "enable", "+"),
            },
        )
        self.assertEqual(
            {(s.cls, s.full) for s in result.skipped},
            {
                ("state", "/org/gnome/Ptyxis/window-size"),
                ("state", "/org/gnome/shell/app-picker-layout"),
                ("excluded-by-rule", "/org/gnome/shell/enabled-extensions"),
                ("secret", "/org/example/app/contact"),
                ("no-schema", "/org/example/ghost/level"),
            },
        )
        self.assertEqual(
            {(u.full, u.schema) for u in result.unmapped},
            {("/org/example/weird/thing", "org.example.Weird"), ("/org/example/app/mode", "org.example.App")},
        )
        self.assertEqual(result.at_default, 1)

    def test_absolute_picture_path_is_flagged_and_goes_to_the_machine_overlay(self) -> None:
        plan, _rules = make_plan(self)
        assert plan.settings is not None
        picture = next(e for e in plan.settings.entries if e.key == "picture-uri")
        self.assertEqual((picture.target, picture.flags), ("machine", ("path",)))

    def test_profile_exclude_and_machine_rules_apply(self) -> None:
        plan, _rules = make_plan(
            self,
            capture={
                "exclude": ["/org/gnome/desktop/interface/color-scheme"],
                "machine": ["/org/gnome/desktop/notifications/**"],
            },
        )
        assert plan.settings is not None
        self.assertIn(
            ("excluded-by-rule", "/org/gnome/desktop/interface/color-scheme"),
            {(s.cls, s.full) for s in plan.settings.skipped},
        )
        self.assertEqual(next(e for e in plan.settings.entries if e.key == "enable").target, "machine")

    def test_marks_new_differs_and_same_against_the_existing_profile(self) -> None:
        write(self.tmp / "profile/settings/appearance.ini", "[org/gnome/desktop/interface]\ncolor-scheme='default'\n")
        write(
            self.tmp / "profile/machines/testbox/settings/power.ini",
            "[org/gnome/settings-daemon/plugins/power]\nsleep-inactive-ac-timeout=3600\n",
        )
        plan, _rules = make_plan(self)
        assert plan.settings is not None
        marks = {e.key: (e.mark, e.previous) for e in plan.settings.entries}
        self.assertEqual(marks["color-scheme"], ("~", "'default'"))
        self.assertEqual(marks["sleep-inactive-ac-timeout"], ("=", None))
        self.assertEqual(marks["enable"], ("+", None))

    def test_keyfile_override_is_reported_not_captured(self) -> None:
        write(
            self.tmp / "home/.config/glib-2.0/settings/keyfile",
            "[org/gnome/settings-daemon/plugins/power]\nsleep-inactive-ac-timeout=0\n\n"
            "[org/gnome/desktop/interface]\nclock-format='12h'\n",
        )
        plan, _rules = make_plan(self)
        assert plan.settings is not None
        overrides = {o.full: o for o in plan.settings.overrides}
        timeout_key = "/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-timeout"
        self.assertEqual(set(overrides), {timeout_key, "/org/gnome/desktop/interface/clock-format"})
        self.assertEqual(overrides[timeout_key].override_value, "0")
        timeout = next(e for e in plan.settings.entries if e.key == "sleep-inactive-ac-timeout")
        self.assertEqual((timeout.value, timeout.flags), ("3600", ("override",)))  # dconf value, never the override
        self.assertFalse(any(e.key == "clock-format" for e in plan.settings.entries))

    def test_only_limits_to_the_named_panels(self) -> None:
        plan, _rules = make_plan(self, only=frozenset({"power"}))
        assert plan.settings is not None
        self.assertEqual({e.panel for e in plan.settings.entries}, {"power"})
        self.assertEqual((plan.files, plan.packages, plan.system), (None, None, None))

    def test_only_rejects_an_unknown_name(self) -> None:
        with self.assertRaises(ConfigError):
            make_plan(self, only=frozenset({"powr"}))

    def test_section_only_run_does_not_scan_settings(self) -> None:
        plan, _rules = make_plan(self, only=frozenset({"files"}))
        self.assertIsNone(plan.settings)
        self.assertIsNotNone(plan.files)

    def scan_settings_of(self, backend: FakeBackend, capture: dict[str, object] | None = None) -> SettingsResult:
        plan = build_plan(
            Profile(self.tmp / "profile", "testbox", "flag"),
            load_rules(capture),
            backend,
            home=self.tmp / "home",
            only=frozenset(),
            run=fake_runner,
            **sources(self),
        )
        assert plan.settings is not None
        return plan.settings

    def test_an_explicit_profile_machine_rule_still_wins_over_state_globs(self) -> None:
        schemas = (
            *SCHEMAS,
            FakeSchema(
                "org.example.Wall", {"last-selected-image": FakeKey("s", "''")}, "/org/gnome/desktop/background/"
            ),
        )
        backend = NoSchemaBackend(schemas)
        backend.set_user("org.example.Wall", None, "last-selected-image", "'a.jpg'")
        key = "/org/gnome/desktop/background/last-selected-image"
        self.assertEqual(
            {(s.cls, s.full) for s in self.scan_settings_of(backend).skipped if s.full == key}, {("state", key)}
        )
        result = self.scan_settings_of(backend, {"machine": [key]})
        self.assertIn(
            ("background", "machine", "last-selected-image"), {(e.panel, e.target, e.key) for e in result.entries}
        )

    def test_the_laptop_dump_volatile_keys_are_skipped_as_state_and_never_captured(self) -> None:
        backend = FakeBackend(
            [
                FakeSchema(schema, {key: FakeKey(kind, default) for key, (kind, default, _value) in keys.items()}, path)
                for schema, (path, keys) in LAPTOP_VOLATILE.items()
            ]
        )
        for schema, (_path, keys) in LAPTOP_VOLATILE.items():
            for key, (_kind, _default, value) in keys.items():
                backend.set_user(schema, None, key, value)
        result = self.scan_settings_of(backend)
        full_names = {entry.key for entry in result.entries}
        self.assertEqual(full_names, set(), "no volatile key may be proposed")
        skipped = {s.full: s.cls for s in result.skipped}
        self.assertEqual(
            skipped,
            {path + key: "state" for path, keys in LAPTOP_VOLATILE.values() for key in keys},
        )

    def test_a_profile_include_lifts_a_volatile_key_back_into_the_machine_overlay(self) -> None:
        backend = FakeBackend(
            [
                FakeSchema(s, {k: FakeKey(t, d) for k, (t, d, _v) in keys.items()}, p)
                for s, (p, keys) in LAPTOP_VOLATILE.items()
            ]
        )
        for schema, (_path, keys) in LAPTOP_VOLATILE.items():
            for key, (_kind, _default, value) in keys.items():
                backend.set_user(schema, None, key, value)
        include = [
            "/org/gnome/settings-daemon/plugins/color/night-light-last-coordinates",
            "/org/gnome/shell/last-selected-power-profile",
        ]
        result = self.scan_settings_of(backend, {"include": include})
        routed = {(e.panel, e.target, e.key) for e in result.entries}
        self.assertIn(("displays", "machine", "night-light-last-coordinates"), routed)
        self.assertIn(("power", "machine", "last-selected-power-profile"), routed)


class SystemScanTests(TempDirCase):
    def test_package_and_system_proposals(self) -> None:
        plan, _rules = make_plan(self)
        assert plan.packages is not None and plan.system is not None
        self.assertEqual(
            [(i.manager, i.name) for i in plan.packages],
            [
                ("apt", "libssl-dev"),
                ("apt", "ripgrep"),
                ("apt", "tmux"),
                ("snap", "code"),
                ("snap", "firefox"),
                ("flatpak", "org.example.Flat"),
            ],
        )
        self.assertEqual(
            [(i.key, i.value, i.machine) for i in plan.system],
            [
                ("timezone", "Europe/Belgrade", True),
                ("locale", "en_US.UTF-8", False),
                ("keyboard.model", "pc105", False),
                ("keyboard.layout", "us,ru", False),
                ("keyboard.options", "grp_led:scroll", False),
            ],
        )

    def test_packages_already_in_the_profile_are_marked_equal(self) -> None:
        write(self.tmp / "profile/packages.toml", 'apt = ["tmux"]\n')
        plan, _rules = make_plan(self)
        assert plan.packages is not None
        self.assertEqual({i.name: i.mark for i in plan.packages if i.manager == "apt"}["tmux"], "=")


class FilesScanTests(TempDirCase):
    def setup_home(self) -> Path:
        home = self.tmp / "home"
        write(home / ".config/mimeapps.list", "[Default Applications]\ntext/plain=x.desktop\n")
        write(home / ".config/monitors.xml", "<monitors/>\n")
        write(
            home / ".config/glib-2.0/settings/keyfile",
            "[org/gnome/shell]\nenabled-extensions=['ding@rastersoft.com']\n",
        )
        write(home / ".config/autostart/a.desktop", "[Desktop Entry]\nName=A\n")
        write(home / ".config/htop/htoprc", "color_scheme=0\n")
        write(home / ".config/svc/credentials.json", '{"a": 1}\n')
        write(home / ".ssh/id_ed25519", "key\n")
        write(home / ".cache/x/y", "z")
        return home

    def test_only_the_allow_list_is_adopted_and_the_rest_is_listed_by_class(self) -> None:
        self.setup_home()
        plan, _rules = make_plan(self)
        assert plan.files is not None
        self.assertEqual(
            {a.rel: (a.machine, a.status) for a in plan.files.adopted},
            {
                ".config/mimeapps.list": (False, "new"),
                ".config/monitors.xml": (True, "new"),
                ".config/glib-2.0/settings/keyfile": (False, "new"),
                ".config/autostart/a.desktop": (False, "new"),
            },
        )
        candidates = {c.rel: c.cls for c in plan.files.candidates}
        self.assertEqual(candidates[".config/htop/htoprc"], "portable-config")
        self.assertEqual(candidates[".config/svc/credentials.json"], "secret-bearing")
        self.assertEqual(candidates[".ssh"], "secret-bearing")
        self.assertEqual(candidates[".cache"], "cache-state")
        self.assertNotIn(".config/mimeapps.list", candidates)
        self.assertEqual(refusals(plan), [])  # the extension uuids in the keyfile are not e-mail findings

    def test_identical_and_differing_profile_copies(self) -> None:
        self.setup_home()
        write(self.tmp / "profile/files/.config/mimeapps.list", "[Default Applications]\ntext/plain=x.desktop\n")
        write(self.tmp / "profile/files/.config/autostart/a.desktop", "[Desktop Entry]\nName=B\nExec=b\n")
        plan, _rules = make_plan(self)
        assert plan.files is not None
        by_rel = {a.rel: a for a in plan.files.adopted}
        self.assertEqual(by_rel[".config/mimeapps.list"].status, "identical")
        differs = by_rel[".config/autostart/a.desktop"]
        self.assertEqual((differs.status, differs.lines_added, differs.lines_removed), ("differs", 1, 2))

    def test_symlink_into_the_profile_is_linked_and_elsewhere_is_not_adopted(self) -> None:
        home = self.setup_home()
        target = write(self.tmp / "profile/files/.config/mimeapps.list", "x\n")
        (home / ".config/mimeapps.list").unlink()
        (home / ".config/mimeapps.list").symlink_to(target)
        (home / ".config/autostart/a.desktop").unlink()
        (home / ".config/autostart/a.desktop").symlink_to(write(self.tmp / "elsewhere", "e"))
        plan, _rules = make_plan(self)
        assert plan.files is not None
        self.assertEqual({a.rel: a.status for a in plan.files.adopted}[".config/mimeapps.list"], "linked")
        self.assertIn(".config/autostart/a.desktop", {s.rel for s in plan.files.skipped})

    def test_include_files_outside_home_is_refused(self) -> None:
        for bad in ("../etc/passwd", "/etc/passwd"):
            with self.assertRaises(RefusedError):
                make_plan(self, capture={"include_files": [bad]})

    def test_a_secret_in_an_allow_listed_file_is_a_refusal_finding(self) -> None:
        home = self.setup_home()
        write(home / ".config/leak.conf", f"token = {FAKE_TOKEN}\nowner = me@example.com\n")
        plan, _rules = make_plan(self, capture={"include_files": [".config/leak.conf"]})
        self.assertEqual(
            {(where, kind) for where, kind, _cls, _line in refusals(plan)},
            {("~/.config/leak.conf", "api-token"), ("~/.config/leak.conf", "email")},
        )


class ReportTests(TempDirCase):
    def render(self, *, verbose: bool = False, markdown: bool = False) -> str:
        home = self.tmp / "home"
        write(home / ".config/mimeapps.list", "[Default Applications]\ntext/plain=x.desktop\n")
        write(home / ".config/htop/htoprc", "color_scheme=0\n")
        plan, _rules = make_plan(self)
        document = build_document(plan, machine="testbox", commit=False, verbose=verbose)
        text = render_markdown(document) if markdown else render_text(document)
        return text.replace(str(self.tmp), "<tmp>")

    def test_markdown_report_golden(self) -> None:
        expected = (Path(__file__).parent / "golden" / "capture_report.md").read_text(encoding="utf-8")
        self.assertEqual(self.render(markdown=True), expected)

    def test_text_report_has_the_sections_in_contract_order(self) -> None:
        report = self.render()
        order = [
            "= SUMMARY",
            "= 1. Settings",
            "= 2. Files",
            "= 3. Packages and System",
            "= 4. Skipped",
            "= 5. Totals",
        ]
        positions = [report.index(heading) for heading in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("totals: settings: 5 keys in 4 bundles", report)

    def test_only_scopes_the_totals_to_the_selected_panels(self) -> None:
        plan, _rules = make_plan(self, only=frozenset({"power"}))
        assert plan.settings is not None
        result = plan.settings
        accounted = len(result.entries) + len(result.skipped) + len(result.unmapped) + result.at_default
        self.assertEqual(accounted, result.user_keys)
        self.assertEqual(result.user_keys, 2)

    def test_override_values_of_excluded_keys_are_masked(self) -> None:
        write(
            self.tmp / "home/.config/glib-2.0/settings/keyfile",
            "[org/example/app]\nmode='hunter2'\n\n[org/gnome/desktop/interface]\nclock-format='12h'\n",
        )
        excluded = ["/org/example/app/mode", "/org/gnome/desktop/interface/clock-format"]
        plan, _rules = make_plan(self, capture={"exclude": excluded})
        assert plan.settings is not None
        self.assertTrue(plan.settings.overrides)
        self.assertTrue(all(o.masked for o in plan.settings.overrides))
        markdown = render_markdown(build_document(plan, machine="testbox", commit=False, verbose=True))
        self.assertNotIn("hunter2", markdown)
        self.assertNotIn("12h", markdown)

    def test_report_never_contains_a_skipped_value(self) -> None:
        for markdown in (False, True):
            report = self.render(markdown=markdown, verbose=True)
            self.assertNotIn("someone@example.com", report)
            self.assertNotIn("(100, 200)", report)  # state value
            self.assertIn("/org/example/app/contact", report)  # the key is listed, the value is not

    def test_verbose_lists_equal_entries_only_when_asked(self) -> None:
        write(
            self.tmp / "profile/machines/testbox/settings/power.ini",
            "[org/gnome/settings-daemon/plugins/power]\nsleep-inactive-ac-timeout=3600\n",
        )
        quiet, loud = self.render(), self.render(verbose=True)
        self.assertNotIn("sleep-inactive-ac-timeout", quiet.split("= 4. Skipped")[0])
        self.assertIn("sleep-inactive-ac-timeout", loud.split("= 4. Skipped")[0])
        self.assertIn("1 already in the profile", quiet)


class CommitTests(TempDirCase):
    def setup_home(self) -> Path:
        home = self.tmp / "home"
        write(home / ".config/mimeapps.list", "[Default Applications]\ntext/plain=x.desktop\n")
        write(home / ".config/monitors.xml", "<monitors/>\n")
        write(home / ".config/autostart/a.desktop", "[Desktop Entry]\nName=A\n")
        return home

    def test_commit_writes_panels_links_files_and_backs_originals_up(self) -> None:
        home = self.setup_home()
        plan, rules = make_plan(self)
        lines = commit_plan(plan, rules, home)
        profile = self.tmp / "profile"

        self.assertEqual(
            read_keyfile(profile / "settings/appearance.ini"),
            {"org/gnome/desktop/interface": {"color-scheme": "'prefer-dark'"}},
        )
        self.assertEqual(
            read_keyfile(profile / "machines/testbox/settings/power.ini")["org/gnome/settings-daemon/plugins/power"],
            {"sleep-inactive-ac-timeout": "3600"},
        )
        self.assertTrue((profile / "machines/testbox/settings/background.ini").is_file())
        self.assertFalse((profile / "settings/power.ini").exists())

        link = home / ".config/mimeapps.list"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), profile / "files/.config/mimeapps.list")
        self.assertEqual(link.read_text(), "[Default Applications]\ntext/plain=x.desktop\n")
        self.assertEqual(
            Path(os.readlink(home / ".config/monitors.xml")), profile / "machines/testbox/files/.config/monitors.xml"
        )
        self.assertFalse((profile / "files/.config/monitors.xml").exists())

        backup = (
            home
            / ".local/state/ubuntu-tweaks/backup/20261020T120000Z"
            / (home / ".config/mimeapps.list").as_posix().lstrip("/")
        )
        self.assertEqual(backup.read_text(), "[Default Applications]\ntext/plain=x.desktop\n")
        self.assertFalse(backup.is_symlink())
        self.assertTrue(any("adopted" in line for line in lines))
        self.assertIn('timezone = "Europe/Belgrade"', (profile / "machines/testbox/system.toml").read_text())
        self.assertIn('locale = "en_US.UTF-8"', (profile / "system.toml").read_text())
        self.assertIn("ripgrep", (profile / "packages.toml").read_text())

    def test_second_capture_is_a_no_op(self) -> None:
        home = self.setup_home()
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        again, _rules = make_plan(self)
        assert again.settings and again.files and again.packages is not None and again.system is not None
        self.assertEqual({e.mark for e in again.settings.entries}, {"="})
        self.assertEqual({a.status for a in again.files.adopted}, {"linked"})
        self.assertEqual({i.mark for i in again.packages}, {"="})
        self.assertEqual({i.mark for i in again.system}, {"="})

    def test_existing_profile_keys_survive_and_differing_ones_are_replaced(self) -> None:
        home = self.setup_home()
        write(
            self.tmp / "profile/settings/appearance.ini",
            "[org/gnome/desktop/interface]\ncolor-scheme='default'\nicon-theme='Old'\n",
        )
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        self.assertEqual(
            read_keyfile(self.tmp / "profile/settings/appearance.ini"),
            {"org/gnome/desktop/interface": {"color-scheme": "'prefer-dark'", "icon-theme": "'Old'"}},
        )

    def test_a_secret_in_an_adopted_file_refuses_and_writes_nothing(self) -> None:
        home = self.setup_home()
        write(home / ".config/leak.conf", f"token = {FAKE_TOKEN}\n")
        plan, rules = make_plan(self, capture={"include_files": [".config/leak.conf"]})
        with self.assertRaises(RefusedError) as caught:
            commit_plan(plan, rules, home)
        self.assertIn("refused: secret finding", str(caught.exception))
        self.assertIn(".config/leak.conf", str(caught.exception))
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        self.assertFalse((self.tmp / "profile").exists())
        self.assertFalse((home / ".config/leak.conf").is_symlink())
        self.assertFalse((home / ".local/state").exists())

    def test_a_secret_already_in_a_panel_file_refuses_the_commit(self) -> None:
        home = self.setup_home()
        write(
            self.tmp / "profile/settings/appearance.ini",
            f"[org/gnome/desktop/interface]\nicon-theme='{FAKE_TOKEN}'\n",
        )
        plan, rules = make_plan(self)
        with self.assertRaises(RefusedError):
            commit_plan(plan, rules, home)
        self.assertFalse((home / ".config/mimeapps.list").is_symlink())

    def test_a_home_directory_that_escapes_home_is_refused(self) -> None:
        home = self.setup_home()
        outside = self.tmp / "outside"
        write(outside / "x.conf", "a=1\n")
        (home / ".config/link").symlink_to(outside)
        plan, rules = make_plan(self, capture={"include_files": [".config/link/x.conf"]})
        with self.assertRaises(RefusedError):
            commit_plan(plan, rules, home)
        self.assertFalse((outside / "x.conf").is_symlink())

    def test_a_file_that_changed_since_the_preview_is_refused_and_nothing_is_written(self) -> None:
        home = self.setup_home()
        plan, rules = make_plan(self)
        write(home / ".config/mimeapps.list", f"[Default Applications]\ntoken = {FAKE_TOKEN}\n")  # an app rewrote it
        with self.assertRaises(RefusedError) as caught:
            commit_plan(plan, rules, home)
        self.assertIn("changed since the preview", str(caught.exception))
        self.assertFalse((self.tmp / "profile").exists())
        self.assertFalse((home / ".config/mimeapps.list").is_symlink())

    def test_a_destination_that_is_a_symlink_is_refused(self) -> None:
        home = self.setup_home()
        target = write(self.tmp / "elsewhere/mimeapps.list", "x\n")
        (self.tmp / "profile/files/.config").mkdir(parents=True)
        (self.tmp / "profile/files/.config/mimeapps.list").symlink_to(target)
        plan, rules = make_plan(self)
        with self.assertRaises(RefusedError):
            commit_plan(plan, rules, home)
        self.assertEqual(target.read_text(), "x\n")

    def test_existing_packages_keep_their_order_and_comments_and_new_ones_are_appended(self) -> None:
        home = self.setup_home()
        write(
            self.tmp / "profile/packages.toml",
            '# my notes\nflatpak_remote = "flathub-beta"  # beta remote\napt_update = true\nflatpak = ["org.keep.Me"]  # needed for X\nsnap = [\n  "firefox",  # the browser\n]\nbrew = [\n  "ripgrep",  # formula\n]\n',
        )
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        text = (self.tmp / "profile/packages.toml").read_text()
        data = tomllib.loads(text)
        self.assertEqual(data["flatpak"], ["org.keep.Me", "org.example.Flat"])
        self.assertEqual(data["snap"], ["firefox", "code"])
        self.assertEqual(data["apt"], ["libssl-dev", "ripgrep", "tmux"])
        self.assertEqual(data["flatpak_remote"], "flathub-beta")
        self.assertIs(data["apt_update"], True)
        self.assertEqual(data["brew"], ["ripgrep"])
        for kept in (
            "# my notes",
            "# needed for X",
            "# beta remote",
            '"firefox",  # the browser',
            '"ripgrep",  # formula',
        ):
            self.assertIn(kept, text)

    def test_system_value_set_in_the_overlay_is_replaced_there_so_the_commit_converges(self) -> None:
        home = self.setup_home()
        write(self.tmp / "profile/machines/testbox/system.toml", '[keyboard]\nlayout = "de"\n')
        plan, rules = make_plan(self)
        assert plan.system is not None
        layout = next(i for i in plan.system if i.key == "keyboard.layout")
        self.assertEqual((layout.machine, layout.mark, layout.previous), (True, "~", "de"))
        commit_plan(plan, rules, home)
        again, _rules = make_plan(self)
        assert again.system is not None
        self.assertEqual({i.mark for i in again.system}, {"="})
        overlay = tomllib.loads((self.tmp / "profile/machines/testbox/system.toml").read_text())
        self.assertEqual(overlay["keyboard"]["layout"], "us,ru")


class CaptureRunner(TempDirCase):
    def run_capture(self, *flags: str, only: frozenset[str] = frozenset()) -> tuple[int, str]:
        profile_dir = self.tmp / "profile"
        profile_dir.mkdir(exist_ok=True)
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        args = build_parser().parse_args(["capture", *flags])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = capture_command.execute(
                Context(REPO_ROOT, Profile(profile_dir, "testbox", "flag"), only),
                args,
                seeded_backend(),
                home=home,
                now=NOW,
                run=fake_runner,
                **sources(self),
            )
        return code, out.getvalue()


class CommandTests(CaptureRunner):
    def test_preview_changes_nothing_and_writes_the_markdown_report(self) -> None:
        write(self.tmp / "home/.config/mimeapps.list", "x\n")
        code, out = self.run_capture("--report", str(self.tmp / "out/r.md"))
        self.assertEqual(code, 0)
        self.assertIn("mode: preview", out)
        self.assertEqual(list((self.tmp / "profile").iterdir()), [])
        self.assertFalse((self.tmp / "home/.config/mimeapps.list").is_symlink())
        self.assertIn("# ubuntu-tweak capture", (self.tmp / "out/r.md").read_text())

    def test_commit_flag_writes_the_profile_and_links_the_file(self) -> None:
        write(self.tmp / "home/.config/mimeapps.list", "x\n")
        code, out = self.run_capture("--commit")
        self.assertEqual(code, 0)
        self.assertIn("nothing was committed to git", out)
        self.assertTrue((self.tmp / "home/.config/mimeapps.list").is_symlink())
        self.assertTrue((self.tmp / "profile/settings/appearance.ini").is_file())

    def test_an_unsafe_include_file_is_refused(self) -> None:
        write(self.tmp / "profile/profile.toml", '[capture]\ninclude_files = ["../x"]\n')
        with self.assertRaises(RefusedError):
            self.run_capture()

    def test_command_registers_itself(self) -> None:
        parser = argparse.ArgumentParser()
        capture_command.register(parser.add_subparsers(dest="command"))
        parsed = parser.parse_args(["capture", "--commit", "--report", "x.md", "-v"])
        self.assertEqual((parsed.commit, parsed.report, parsed.verbose), (True, "x.md", True))


class ReviewRegressionTests(TempDirCase):
    def test_dock_preferences_are_not_state_and_system_paths_are_not_machine_paths(self) -> None:
        self.assertIsNone(load_rules().state_rule("/org/gnome/shell/extensions/dash-to-dock/animation-time"))
        backend = seeded_backend()
        command = parse_value(backend, "org.example.App", "mode", "'/usr/bin/flameshot gui'")
        self.assertFalse(has_machine_path(command))

    def test_corrupt_installer_baseline_falls_back_to_the_metapackages(self) -> None:
        baseline = self.tmp / "initial-status.gz"
        baseline.write_bytes(b"\x1f\x8b\x08\x00truncated")
        _items, note = scan_packages(fake_runner, load_rules().packages, Packages(), installer_baseline=baseline)
        self.assertIn("dependencies of", note)

    def test_a_fully_unchanged_panel_still_reports_its_hidden_count(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        again, _rules = make_plan(self)
        report = render_text(build_document(again, machine="testbox", commit=False, verbose=False))
        self.assertIn("already in the profile", report.split("= 2. Files")[0])

    def test_state_keep_covers_preferences_whose_names_look_like_state(self) -> None:
        rules = load_rules()
        for full in (
            "/org/gnome/desktop/a11y/mouse/dwell-time",
            "/org/gnome/desktop/interface/cursor-blink-time",
            "/org/gnome/shell/extensions/tiling-assistant/activate-window-layout1",
        ):
            self.assertIsNone(rules.state_rule(full), full)

    def test_a_key_set_in_the_overlay_is_captured_into_the_overlay(self) -> None:
        write(
            self.tmp / "profile/machines/testbox/settings/appearance.ini",
            "[org/gnome/desktop/interface]\ncolor-scheme='default'\n",
        )
        plan, _rules = make_plan(self)
        assert plan.settings is not None
        entry = next(e for e in plan.settings.entries if e.key == "color-scheme")
        self.assertEqual((entry.target, entry.mark, entry.previous), ("machine", "~", "'default'"))

    def test_only_accepts_the_shell_extensions_pseudo_panel(self) -> None:
        plan, _rules = make_plan(self, only=frozenset({"shell-extensions"}))
        assert plan.settings is not None
        self.assertEqual(
            {(s.cls, s.full) for s in plan.settings.skipped},
            {("excluded-by-rule", "/org/gnome/shell/enabled-extensions")},
        )

    def test_a_linked_file_is_scanned_and_a_dangling_link_is_not_called_linked(self) -> None:
        home = self.tmp / "home"
        profile_copy = write(self.tmp / "profile/files/.config/mimeapps.list", f"token = {FAKE_TOKEN}\n")
        write(home / ".config/x", "")
        (home / ".config/mimeapps.list").symlink_to(profile_copy)
        plan, rules = make_plan(self)
        assert plan.files is not None
        linked = next(a for a in plan.files.adopted if a.rel == ".config/mimeapps.list")
        self.assertEqual((linked.status, [f.kind for f in linked.findings]), ("linked", ["api-token"]))
        with self.assertRaises(RefusedError):
            commit_plan(plan, rules, home)
        profile_copy.unlink()
        plan, _rules = make_plan(self)
        assert plan.files is not None
        self.assertNotIn(".config/mimeapps.list", {a.rel for a in plan.files.adopted})
        self.assertIn(".config/mimeapps.list", {s.rel for s in plan.files.skipped})

    def test_local_is_one_mixed_row_that_mentions_keyrings(self) -> None:
        home = self.tmp / "home"
        write(home / ".local/share/keyrings/login.keyring", "x")
        plan, _rules = make_plan(self)
        assert plan.files is not None
        row = next(c for c in plan.files.candidates if c.rel == ".local")
        self.assertEqual(row.cls, "mixed")
        self.assertIn("keyrings", row.reason)

    def test_an_identical_profile_copy_that_changed_since_the_preview_is_refused(self) -> None:
        home = self.tmp / "home"
        write(home / ".config/mimeapps.list", "a\n")
        copy = write(self.tmp / "profile/files/.config/mimeapps.list", "a\n")
        plan, rules = make_plan(self)
        copy.write_text("b\n")  # a git pull in the profile
        with self.assertRaises(RefusedError):
            commit_plan(plan, rules, home)
        self.assertFalse((home / ".config/mimeapps.list").is_symlink())

    def test_adoption_symlink_is_absolute(self) -> None:
        home = self.tmp / "home"
        write(home / ".config/mimeapps.list", "a\n")
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        self.assertTrue(Path(os.readlink(home / ".config/mimeapps.list")).is_absolute())

    def test_installer_baseline_and_snap_seed_are_subtracted(self) -> None:
        baseline = self.tmp / "initial-status.gz"
        with gzip.open(baseline, "wt", encoding="utf-8") as handle:
            handle.write("Package: ripgrep\nStatus: install ok installed\n\nPackage: other\n")
        seed = write(self.tmp / "seed.yaml", "snaps:\n  - name: firefox\n    channel: stable\n")
        items, note = scan_packages(
            fake_runner, load_rules().packages, Packages(), installer_baseline=baseline, snap_seed=seed
        )
        self.assertEqual(
            [(i.manager, i.name) for i in items if i.manager != "flatpak"],
            [("apt", "curl"), ("apt", "libssl-dev"), ("apt", "tmux"), ("snap", "code")],  # ripgrep: in the baseline
        )
        self.assertIn("initial-status.gz", note)

    def test_flatpak_scan_does_not_create_the_user_installation(self) -> None:
        calls: list[Sequence[str]] = []

        def recording(argv: Sequence[str]) -> str | None:
            calls.append(argv)
            return fake_runner(argv)

        user_dir = self.tmp / "data/flatpak"
        items, _note = scan_packages(
            recording,
            load_rules().packages,
            Packages(),
            installer_baseline=self.tmp / "none",
            flatpak_user_dir=user_dir,
        )

        flatpak_calls = [c for c in calls if "flatpak" in c]
        self.assertEqual(len(flatpak_calls), 1)
        self.assertIn("--system", flatpak_calls[0])
        self.assertNotIn("--user", flatpak_calls[0])
        self.assertTrue(flatpak_calls[0][1].startswith("XDG_CACHE_HOME="))  # flatpak's cache stays out of $HOME
        self.assertIn(("flatpak", "org.example.Flat"), [(i.manager, i.name) for i in items])

        user_dir.mkdir(parents=True)  # an existing user installation is read too, without duplicates
        calls.clear()
        items, _note = scan_packages(
            recording,
            load_rules().packages,
            Packages(),
            installer_baseline=self.tmp / "none",
            flatpak_user_dir=user_dir,
        )
        self.assertEqual([c[c.index("list") + 1] for c in calls if "flatpak" in c], ["--system", "--user"])
        self.assertEqual([i.name for i in items if i.manager == "flatpak"], ["org.example.Flat"])

    def test_text_report_clips_long_cells_but_markdown_keeps_them(self) -> None:
        backend = seeded_backend()
        long_value = "'" + "x" * 200 + "'"
        backend.set_user("org.gnome.desktop.background", None, "picture-uri", long_value)
        plan, _rules = make_plan(self, backend)
        document = build_document(plan, machine="testbox", commit=False, verbose=False)
        self.assertNotIn("x" * 200, render_text(document))
        self.assertIn("…", render_text(document))
        self.assertIn("x" * 200, render_markdown(document))


class BundleRoutingTests(TempDirCase):
    """Where a captured key goes: the bundle that already holds it, then [capture.bundles], then the panel."""

    def entry(self, plan: CapturePlan, key: str):  # noqa: ANN202
        assert plan.settings is not None
        return next(e for e in plan.settings.entries if e.key == key)

    def test_a_key_already_in_a_bundle_stays_there_and_shows_its_reason(self) -> None:
        write(
            self.tmp / "profile/settings/mine.ini",
            "[org/gnome/desktop/interface]\n# dark on the OLED\ncolor-scheme='default'\n",
        )
        plan, _rules = make_plan(self, capture={"bundles": {"dev": ["org/gnome/desktop/interface/**"]}})
        found = self.entry(plan, "color-scheme")
        self.assertEqual(
            (found.bundle, found.target, found.mark, found.previous, found.reason, found.route),
            ("mine", "shared", "~", "'default'", "dark on the OLED", "profile"),
        )
        report = render_text(build_document(plan, machine="testbox", commit=False, verbose=False))
        self.assertIn("settings/mine.ini (shared)", report)
        self.assertIn("was 'default'; why: dark on the OLED; stays in its bundle", report)

    def test_a_key_held_by_an_overlay_bundle_under_another_panel_is_not_proposed_again(self) -> None:
        # ticket #37: show-battery-percentage belongs to the power panel but the profile keeps it in an
        # overlay file named after another panel
        write(
            self.tmp / "profile/machines/testbox/settings/appearance.ini",
            "[org/gnome/desktop/interface]\nshow-battery-percentage=true\n",
        )
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        found = self.entry(plan, "show-battery-percentage")
        self.assertEqual((found.mark, found.target, found.bundle, found.panel), ("=", "machine", "appearance", "power"))
        commit_plan(plan, rules, home)
        self.assertEqual(
            "[org/gnome/desktop/interface]\nshow-battery-percentage=true\n",
            (self.tmp / "profile/machines/testbox/settings/appearance.ini").read_text(),
        )
        power = read_keyfile(self.tmp / "profile/machines/testbox/settings/power.ini")
        self.assertNotIn("show-battery-percentage", {k for keys in power.values() for k in keys})
        again, _rules = make_plan(self)
        self.assertEqual(self.entry(again, "show-battery-percentage").mark, "=")

    def test_a_different_value_for_a_key_in_an_other_panels_bundle_is_a_change_not_a_new_key(self) -> None:
        write(
            self.tmp / "profile/machines/testbox/settings/appearance.ini",
            "[org/gnome/desktop/interface]\nshow-battery-percentage=false\n",
        )
        plan, _rules = make_plan(self)
        found = self.entry(plan, "show-battery-percentage")
        self.assertEqual((found.mark, found.previous), ("~", "false"))

    def test_capture_bundles_routes_new_keys_first_match_wins_and_the_panel_is_the_fallback(self) -> None:
        table = {
            "bundles": {
                "dev-keyboard": ["org/gnome/desktop/interface/color-scheme"],
                "late": ["org/gnome/desktop/interface/**", "/org/gnome/settings-daemon/plugins/power/**"],
            }
        }
        plan, _rules = make_plan(self, capture=table)
        self.assertEqual(
            {e.key: (e.bundle, e.target, e.route) for e in plan.settings.entries},  # type: ignore[union-attr]
            {
                "color-scheme": ("dev-keyboard", "shared", "rule"),
                "show-battery-percentage": ("late", "machine", "rule"),
                "sleep-inactive-ac-timeout": ("late", "machine", "rule"),
                "picture-uri": ("background", "machine", "panel"),
                "enable": ("notifications", "shared", "panel"),
            },
        )

    def test_commit_writes_routed_keys_into_the_named_bundle_in_the_right_layer(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(
            self,
            capture={
                "bundles": {"laptop/mix": ["org/gnome/settings-daemon/**", "org/gnome/desktop/interface/color-scheme"]}
            },
        )
        commit_plan(plan, rules, home)
        profile = self.tmp / "profile"
        self.assertEqual(
            read_keyfile(profile / "settings/laptop/mix.ini"),
            {"org/gnome/desktop/interface": {"color-scheme": "'prefer-dark'"}},
        )
        self.assertEqual(
            read_keyfile(profile / "machines/testbox/settings/laptop/mix.ini"),
            {"org/gnome/settings-daemon/plugins/power": {"sleep-inactive-ac-timeout": "3600"}},
        )
        again, _rules = make_plan(self, capture={"bundles": {"laptop/mix": ["org/gnome/settings-daemon/**"]}})
        self.assertEqual({e.mark for e in again.settings.entries}, {"="})  # type: ignore[union-attr]

    def test_a_bad_capture_bundles_table_is_invalid_config(self) -> None:
        for bad in ({"bundles": ["x"]}, {"bundles": {"../x": ["a/**"]}}, {"bundles": {"ok": "a/**"}}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_rules(bad)

    def test_routing_into_a_bundle_that_does_not_apply_here_is_refused(self) -> None:
        write(self.tmp / "profile/settings/dev.ini", '[ubuntu-tweak]\nmachines = ["laptop"]\n')
        with self.assertRaisesRegex(ConfigError, "does not apply on this machine"):
            make_plan(self, capture={"bundles": {"dev": ["org/gnome/desktop/interface/**"]}})

    def test_profile_bundles_that_conflict_stop_capture_with_exit_2(self) -> None:
        write(self.tmp / "profile/settings/a.ini", "[org/x]\nk=1\n")
        write(self.tmp / "profile/settings/b.ini", "[org/x]\nk=2\n")
        with self.assertRaises(ConfigError):
            make_plan(self)


class CommitKeepsHandWrittenBundlesTests(TempDirCase):
    SHARED = (
        '[ubuntu-tweak]\ndescription = "My settings"\n\n'
        "# Dark everywhere, white hurts.\n[org/gnome/desktop/interface]\n"
        "# keep dark\ncolor-scheme='default'\n"
        "# set by hand, capture knows nothing about it\nicon-theme='Old'\n\n"
        "# free-floating note\n"
    )
    OVERLAY = (
        "# this box only\n[org/gnome/settings-daemon/plugins/power]\n"
        "# AC must never sleep: the render job\nsleep-inactive-ac-timeout=1800\n"
    )

    def test_commit_changes_only_the_values_and_keeps_every_comment_and_the_order(self) -> None:
        shared = write(self.tmp / "profile/settings/mine.ini", self.SHARED)
        overlay = write(self.tmp / "profile/machines/testbox/settings/laptop.ini", self.OVERLAY)
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        assert plan.settings is not None
        marks = {e.key: e.mark for e in plan.settings.entries}
        self.assertEqual((marks["color-scheme"], marks["sleep-inactive-ac-timeout"]), ("~", "~"))

        commit_plan(plan, rules, home)

        self.assertEqual(
            shared.read_text(), self.SHARED.replace("color-scheme='default'", "color-scheme='prefer-dark'")
        )
        self.assertEqual(overlay.read_text(), self.OVERLAY.replace("=1800", "=3600"))
        again, _rules = make_plan(self)
        assert again.settings is not None
        self.assertEqual({e.mark for e in again.settings.entries}, {"="})

    def test_new_keys_for_an_existing_bundle_section_are_appended_after_its_last_key(self) -> None:
        shared = write(
            self.tmp / "profile/settings/mine.ini",
            "[org/gnome/desktop/interface]\n# why\ncolor-scheme='prefer-dark'\n\n[org/other]\nz=1\n",
        )
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(
            self, capture={"bundles": {"mine": ["org/gnome/desktop/interface/show-battery-percentage"]}}
        )
        commit_plan(plan, rules, home)
        self.assertEqual(
            read_keyfile(self.tmp / "profile/machines/testbox/settings/mine.ini"),
            {"org/gnome/desktop/interface": {"show-battery-percentage": "true"}},
        )
        self.assertEqual(
            shared.read_text(), "[org/gnome/desktop/interface]\n# why\ncolor-scheme='prefer-dark'\n\n[org/other]\nz=1\n"
        )


class CommitKeepsTomlCommentsTests(TempDirCase):
    def test_system_toml_keeps_its_comments_and_order_when_values_are_added_and_replaced(self) -> None:
        system = write(
            self.tmp / "profile/system.toml",
            '# root-level settings of my boxes\nlocale = "de_DE.UTF-8"  # the old one\n\n'
            '[keyboard]\n# two layouts\nlayout = "us"\n',
        )
        overlay = write(self.tmp / "profile/machines/testbox/system.toml", '# this box\ntimezone = "UTC"  # server\n')
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)

        text = system.read_text()
        for kept in (
            "# root-level settings of my boxes",
            '"en_US.UTF-8"  # the old one',
            "# two layouts",
            'layout = "us,ru"',
        ):
            self.assertIn(kept, text)
        self.assertLess(text.index("locale"), text.index("[keyboard]"))
        data = tomllib.loads(text)
        self.assertEqual(
            (data["locale"], data["keyboard"]["layout"], data["keyboard"]["model"]), ("en_US.UTF-8", "us,ru", "pc105")
        )
        self.assertEqual(overlay.read_text(), '# this box\ntimezone = "Europe/Belgrade"  # server\n')


class ReviewFixCaptureTests(TempDirCase):
    def test_a_key_that_only_another_machines_bundle_holds_is_captured_into_the_overlay(self) -> None:
        write(
            self.tmp / "profile/settings/laptop.ini",
            "[ubuntu-tweak]\nmachines = [\"laptop\"]\n[org/gnome/desktop/interface]\ncolor-scheme='default'\n",
        )
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        assert plan.settings is not None
        found = next(e for e in plan.settings.entries if e.key == "color-scheme")
        self.assertEqual((found.target, found.mark, found.flags), ("machine", "+", ("other-machines",)))
        commit_plan(plan, rules, home)
        self.assertFalse((self.tmp / "profile/settings/appearance.ini").exists())
        # both layers now apply on each machine without a conflict
        from ubuntu_tweaks.bundles import load_bundles

        load_bundles(Profile(self.tmp / "profile", "laptop", "flag")).require_valid()
        load_bundles(Profile(self.tmp / "profile", "testbox", "flag")).require_valid()

    def test_a_key_the_profile_holds_is_not_dropped_as_auto_or_state(self) -> None:
        schemas = (
            *SCHEMAS,
            FakeSchema(
                "org.gnome.clocks.state.window", {"panel-id": FakeKey("s", "''")}, "/org/gnome/clocks/state/window/"
            ),
        )
        backend = FakeBackend(schemas)
        backend.set_user("org.gnome.clocks.state.window", None, "panel-id", "'timer'")
        write(
            self.tmp / "profile/settings/mine.ini",
            "[org/gnome/clocks/state/window]\n# I want this one\npanel-id='alarm'\n",
        )
        plan, _rules = make_plan(self, backend)
        assert plan.settings is not None
        found = next(e for e in plan.settings.entries if e.key == "panel-id")
        self.assertEqual((found.bundle, found.mark, found.previous), ("mine", "~", "'alarm'"))
        self.assertEqual([s for s in plan.settings.skipped if s.cls == "auto"], [])

    def test_a_section_only_capture_does_not_read_the_settings_bundles(self) -> None:
        write(self.tmp / "profile/settings/a.ini", "[org/x]\nk=1\n")
        write(self.tmp / "profile/settings/b.ini", "[org/x]\nk=2\n")
        plan, _rules = make_plan(self, only=frozenset({"files"}))
        self.assertIsNone(plan.settings)
        with self.assertRaises(ConfigError):
            make_plan(self)

    def test_a_comment_the_profile_already_has_never_blocks_the_commit(self) -> None:
        reason = "# see upstream commit " + "a1b2c3d4e5" * 4 + " and ask bob@example.com"
        shared = write(
            self.tmp / "profile/settings/mine.ini", f"[org/gnome/desktop/interface]\n{reason}\ncolor-scheme='default'\n"
        )
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        self.assertIn(reason, shared.read_text())
        self.assertIn("color-scheme='prefer-dark'", shared.read_text())

    def test_identical_duplicates_all_follow_a_live_change_so_the_profile_never_conflicts(self) -> None:
        for name in ("a", "b"):
            write(self.tmp / f"profile/settings/{name}.ini", "[org/gnome/desktop/interface]\ncolor-scheme='default'\n")
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        commit_plan(plan, rules, home)
        from ubuntu_tweaks.bundles import load_bundles

        bundles = load_bundles(Profile(self.tmp / "profile", "testbox", "flag"))
        bundles.require_valid()
        self.assertEqual({b.keys[0].value for b in bundles.bundles if b.name in ("a", "b")}, {"'prefer-dark'"})

    def test_a_secret_in_a_value_of_a_touched_bundle_still_refuses(self) -> None:
        write(
            self.tmp / "profile/settings/appearance.ini", f"[org/gnome/desktop/interface]\nicon-theme='{FAKE_TOKEN}'\n"
        )
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        with self.assertRaises(RefusedError):
            commit_plan(plan, rules, home)


class ChangedOnlyTests(TempDirCase):
    """R1: a value the user did not change is never captured and never written."""

    def test_a_user_value_equal_to_the_default_is_counted_never_captured_never_written(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        plan, rules = make_plan(self)
        assert plan.settings is not None
        self.assertEqual(plan.settings.at_default, 1)  # clock-format = '24h' is the schema default
        self.assertNotIn("clock-format", {e.key for e in plan.settings.entries})
        self.assertNotIn("/org/gnome/desktop/interface/clock-format", {s.full for s in plan.settings.skipped})
        commit_plan(plan, rules, home)
        written = "\n".join(p.read_text() for p in (self.tmp / "profile").rglob("*.ini"))
        self.assertNotIn("clock-format", written)

    def test_the_default_is_whatever_the_backend_computes_including_overrides(self) -> None:
        class OverriddenDefaults(FakeBackend):
            def default(self, schema: str, path: str | None, key: str):  # noqa: ANN202
                if key == "color-scheme":
                    return parse_value(self, schema, key, "'prefer-dark'")  # a system override changed the default
                return super().default(schema, path, key)

        backend = OverriddenDefaults(SCHEMAS)
        backend.set_user("org.gnome.desktop.interface", None, "color-scheme", "'prefer-dark'")
        plan, _rules = make_plan(self, backend)
        assert plan.settings is not None
        self.assertEqual((len(plan.settings.entries), plan.settings.at_default), (0, 1))


class AutoClassTests(TempDirCase):
    AUTO_SCHEMAS = (
        *SCHEMAS,
        FakeSchema(
            "org.gnome.clocks.state.window", {"panel-id": FakeKey("s", "''")}, "/org/gnome/clocks/state/window/"
        ),
        FakeSchema("org.gnome.Ptyxis.Extra", {"profile-uuids": FakeKey("as", "@as []")}, "/org/gnome/Ptyxis/"),
    )

    def backend(self) -> FakeBackend:
        backend = FakeBackend(self.AUTO_SCHEMAS)
        backend.set_user("org.gnome.clocks.state.window", None, "panel-id", "'timer'")
        backend.set_user("org.gnome.Ptyxis.Extra", None, "profile-uuids", "['5291476bbbd7289aa32a7bf96a09e10c']")
        backend.set_user("org.gnome.desktop.interface", None, "color-scheme", "'prefer-dark'")
        return backend

    def test_registered_by_itself_keys_are_skipped_as_auto_with_a_reason(self) -> None:
        plan, _rules = make_plan(self, self.backend())
        assert plan.settings is not None
        auto = {s.full: s.reason for s in plan.settings.skipped if s.cls == "auto"}
        self.assertEqual(set(auto), {"/org/gnome/clocks/state/window/panel-id", "/org/gnome/Ptyxis/profile-uuids"})
        self.assertTrue(all(auto.values()))
        self.assertEqual({e.key for e in plan.settings.entries}, {"color-scheme"})
        self.assertEqual(plan.settings.unmapped, [])

    def test_auto_has_its_own_counted_section_in_the_report(self) -> None:
        plan, _rules = make_plan(self, self.backend())
        report = render_text(build_document(plan, machine="testbox", commit=False, verbose=False))
        self.assertIn("auto 2", report)
        self.assertIn("- auto (2)", report)
        self.assertIn("[capture] include", report)
        self.assertRegex(report, r"auto\s+2\n")  # the summary table row

    def test_include_promotes_an_auto_key_back_and_accepts_a_relative_glob(self) -> None:
        for glob in ("/org/gnome/clocks/state/**", "org/gnome/clocks/state/**"):
            with self.subTest(glob=glob):
                plan, _rules = make_plan(self, self.backend(), capture={"include": [glob]})
                assert plan.settings is not None
                self.assertIn("panel-id", {e.key for e in plan.settings.entries})
                self.assertEqual(
                    {s.full for s in plan.settings.skipped if s.cls == "auto"}, {"/org/gnome/Ptyxis/profile-uuids"}
                )

    def test_include_never_lifts_a_secret_exclude(self) -> None:
        rules = load_rules({"include": ["/**"]})
        self.assertIsNotNone(rules.hard_exclude("/org/gnome/desktop/remote-desktop/rdp/tls-key"))

    def test_every_auto_rule_has_a_reason_and_the_shipped_keys_are_covered(self) -> None:
        rules = load_rules()
        self.assertTrue(rules.auto and all(rule.reason for rule in rules.auto))
        for full in (
            "/org/gnome/desktop/notifications/application-children",
            "/org/gnome/desktop/notifications/application/firefox/application-id",
            "/org/gnome/Ptyxis/default-profile-uuid",
            "/org/gnome/clocks/state/window/panel-id",
            "/org/gnome/shell/world-clocks/locations",
            "/org/gnome/settings-daemon/global-shortcuts/applications",
            "/org/gnome/settings-daemon/global-shortcuts/com.anthropic.Claude/shortcuts",
            "/org/gnome/desktop/input-sources/mru-sources",
        ):
            self.assertIsNotNone(rules.auto_rule(full), full)
        for full in (  # real user choices stay
            "/org/gnome/desktop/notifications/application/firefox/enable",
            "/org/gnome/desktop/input-sources/sources",
            "/org/gnome/desktop/interface/color-scheme",
            "/org/gnome/shell/extensions/tiling-assistant/tile-left-half",
            "/org/gnome/nautilus/preferences/default-folder-viewer",
            "/org/gnome/settings-daemon/plugins/color/night-light-enabled",
        ):
            self.assertIsNone(rules.auto_rule(full), full)


class WellbeingPanelTests(unittest.TestCase):
    def test_wellbeing_and_installed_third_party_apps_have_a_panel(self) -> None:
        rules = load_rules()

        def panel(full: str) -> str | None:
            rule = rules.panel_rule(full)
            return rule.panel if rule else None

        self.assertEqual(panel("/org/gnome/desktop/break-reminders/movement/duration-seconds"), "wellbeing")
        self.assertEqual(panel("/org/gnome/desktop/screen-time-limits/daily-limit-seconds"), "wellbeing")
        for full in (
            "/net/nokyan/Resources/apps-sort-by",
            "/org/gnome/deja-dup/backend",
            "/org/gnome/deja-dup/local/folder",
            "/apps/seahorse/listing/keyrings-selected",
        ):
            self.assertEqual(panel(full), "apps", full)


class SummaryAndModesTests(CaptureRunner):
    def test_dry_run_is_the_preview_and_writes_nothing(self) -> None:
        write(self.tmp / "home/.config/mimeapps.list", "x\n")
        code, out = self.run_capture("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("mode: dry-run (nothing is written", out)
        self.assertEqual(list((self.tmp / "profile").iterdir()), [])
        self.assertFalse((self.tmp / "home/.config/mimeapps.list").is_symlink())
        self.assertNotIn("nothing was committed to git", out)

    def test_dry_run_refuses_a_report_file_because_it_writes_nothing(self) -> None:
        with self.assertRaisesRegex(ConfigError, "--report writes a file"):
            self.run_capture("--dry-run", "--report", str(self.tmp / "r.md"))
        self.assertFalse((self.tmp / "r.md").exists())

    def test_summary_opens_the_output_before_any_detail_table(self) -> None:
        _code, out = self.run_capture()
        self.assertLess(out.index("= SUMMARY"), out.index("= 1. Settings"))
        summary = out.split("= 1. Settings")[0]
        for needle in (
            "dconf user keys in scope: 13 = 5 captured + 5 skipped + 2 unmapped + 1 at schema default",
            "captured, per destination bundle",
            "settings/appearance.ini",
            "machines/testbox/settings/power.ini",
            "per Settings panel",
            "not captured, per class",
            "files:",
            "packages: 6 new (3 apt, 2 snap, 1 flatpak)",
            "system: 5 to write",
            "secret findings in adopted items: 0",
            "unmapped: 2",
        ):
            self.assertIn(needle, summary)

    def test_summary_flag_prints_only_the_summary_and_the_totals(self) -> None:
        _code, out = self.run_capture("--summary")
        self.assertIn("= SUMMARY", out)
        self.assertNotIn("= 1. Settings", out)
        self.assertNotIn("= 4. Skipped", out)
        self.assertIn("totals:", out)

    def test_summary_cannot_be_combined_with_commit(self) -> None:
        with self.assertRaisesRegex(ConfigError, "--summary"):
            self.run_capture("--summary", "--commit")
        self.assertEqual(list((self.tmp / "profile").iterdir()), [])

    def test_summary_counts_keyfile_overrides_per_panel(self) -> None:
        write(
            self.tmp / "home/.config/glib-2.0/settings/keyfile",
            "[org/gnome/settings-daemon/plugins/power]\nsleep-inactive-ac-timeout=0\n",
        )
        _code, out = self.run_capture()
        summary = out.split("= 1. Settings")[0]
        self.assertRegex(summary, r"power\s+2\s+0\s+1")
        self.assertIn("1 keys have their effective value from", summary)


if __name__ == "__main__":
    unittest.main()

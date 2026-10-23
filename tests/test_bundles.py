"""Settings bundles: discovery, header, merge rules, conflicts, and the ``bundles`` / ``bundle-move`` /
``note`` commands. Pure text work: runs without PyGObject."""

import contextlib
import io
import unittest
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks import cli
from ubuntu_tweaks.bundles import load_bundles
from ubuntu_tweaks.errors import ConfigError, ExitCode
from ubuntu_tweaks.profile import Profile

IFACE = "[org/gnome/desktop/interface]\n"


class BundleCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "profile"
        self.root.mkdir()
        self.profile = Profile(self.root, "home", "flag")

    def shared(self, name: str, text: str) -> Path:
        return write(self.root / "settings" / f"{name}.ini", text)

    def overlay(self, name: str, text: str, machine: str = "home") -> Path:
        return write(self.root / "machines" / machine / "settings" / f"{name}.ini", text)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--profile", str(self.root), "--machine", "home"])
        return code, out.getvalue() + err.getvalue()


class DiscoveryTests(BundleCase):
    def test_any_ini_below_settings_is_a_bundle_named_by_its_relative_path(self) -> None:
        self.shared("dev-keyboard", IFACE + "gtk-theme='A'\n")
        self.shared("laptop/power", "[org/gnome/mutter]\nedge-tiling=false\n")
        self.overlay("laptop/power", "[org/gnome/mutter]\nedge-tiling=true\n")
        write(self.root / "settings/.hidden.ini", IFACE + "x=1\n")
        write(self.root / "settings/notes.txt", "not a bundle")

        found = [(b.name, b.origin) for b in load_bundles(self.profile).bundles]

        self.assertEqual(found, [("dev-keyboard", "shared"), ("laptop/power", "shared"), ("laptop/power", "overlay")])

    def test_sections_may_come_from_any_dconf_path_in_one_bundle(self) -> None:
        self.shared("mixed", "[org/gnome/mutter]\na=1\n[org/gnome/desktop/wm/keybindings]\nb=2\n")
        keys = load_bundles(self.profile).bundles[0].keys
        self.assertEqual([k.full for k in keys], ["/org/gnome/mutter/a", "/org/gnome/desktop/wm/keybindings/b"])


class MergeRuleTests(BundleCase):
    def test_same_key_with_different_values_in_two_shared_bundles_is_exit_2_naming_both(self) -> None:
        self.shared("a", IFACE + "gtk-theme='A'\n")
        self.shared("b", "# note\n" + IFACE + "gtk-theme='B'\n")
        bundles = load_bundles(self.profile)
        with self.assertRaises(ConfigError) as caught:
            bundles.require_valid()
        message = str(caught.exception)
        self.assertEqual(caught.exception.exit_code, ExitCode.INVALID_CONFIG)
        self.assertIn("settings/a.ini:2", message)
        self.assertIn("settings/b.ini:3", message)
        self.assertIn("/org/gnome/desktop/interface/gtk-theme", message)

    def test_identical_values_are_a_duplicate_warning_not_an_error(self) -> None:
        self.shared("a", IFACE + "gtk-theme='A'\n")
        self.shared("b", IFACE + "gtk-theme='A'\n")
        bundles = load_bundles(self.profile)
        bundles.require_valid()
        self.assertEqual(len(bundles.duplicates), 1)
        self.assertIn("settings/a.ini:2 and settings/b.ini:2", bundles.duplicate_warnings()[0])

    def test_two_overlay_bundles_conflict_like_two_shared_ones(self) -> None:
        self.overlay("a", IFACE + "gtk-theme='A'\n")
        self.overlay("b", IFACE + "gtk-theme='B'\n")
        with self.assertRaises(ConfigError):
            load_bundles(self.profile).require_valid()

    def test_an_overlay_key_shadows_a_shared_one_without_a_conflict(self) -> None:
        self.shared("a", IFACE + "gtk-theme='A'\n")
        self.overlay("other", IFACE + "gtk-theme='B'\n")
        bundles = load_bundles(self.profile)
        bundles.require_valid()
        self.assertEqual(
            [(s.shared.where, s.overlay.where) for s in bundles.shadows],
            [("settings/a.ini:2", "machines/home/settings/other.ini:2")],
        )
        winner = bundles.locate("org/gnome/desktop/interface", "gtk-theme")
        assert winner is not None
        self.assertEqual((winner.bundle.origin, winner.key.value), ("overlay", "'B'"))

    def test_reasons_are_the_comment_above_the_key_joined_into_one_line(self) -> None:
        self.shared("a", IFACE + "# first line\n# second line\ngtk-theme='A'\nicon-theme='B'\n")
        keys = {k.key: k.reason for k in load_bundles(self.profile).bundles[0].keys}
        self.assertEqual(keys, {"gtk-theme": "first line second line", "icon-theme": ""})


class HeaderTests(BundleCase):
    def test_header_is_not_a_dconf_section_and_carries_the_description(self) -> None:
        self.shared("a", '[ubuntu-tweak]\ndescription = "Keys for dev work"\n\n' + IFACE + "gtk-theme='A'\n")
        bundle = load_bundles(self.profile).bundles[0]
        self.assertEqual(bundle.description, "Keys for dev work")
        self.assertEqual([k.section for k in bundle.keys], ["org/gnome/desktop/interface"])

    def test_bare_description_text_is_accepted(self) -> None:
        self.shared("a", "[ubuntu-tweak]\ndescription = plain words\n" + IFACE + "k=1\n")
        self.assertEqual(load_bundles(self.profile).bundles[0].description, "plain words")

    def test_unknown_header_key_is_invalid_config(self) -> None:
        self.shared("a", '[ubuntu-tweak]\ndescripton = "typo"\n' + IFACE + "k=1\n")
        with self.assertRaisesRegex(ConfigError, "unknown key.*descripton"):
            load_bundles(self.profile)

    def test_machines_must_be_a_list_of_names(self) -> None:
        self.shared("a", '[ubuntu-tweak]\nmachines = "home"\n' + IFACE + "k=1\n")
        with self.assertRaisesRegex(ConfigError, "machines must be a non-empty list"):
            load_bundles(self.profile)

    def test_machines_filter_applies_the_bundle_only_on_listed_hostnames(self) -> None:
        self.shared("laptop-only", '[ubuntu-tweak]\nmachines = ["laptop"]\n' + IFACE + "gtk-theme='L'\n")
        self.shared("everywhere", '[ubuntu-tweak]\nmachines = ["laptop", "home"]\n' + IFACE + "icon-theme='E'\n")
        bundles = load_bundles(self.profile)
        self.assertEqual([b.name for b in bundles.active()], ["everywhere"])
        inactive = next(b for b in bundles.bundles if b.name == "laptop-only")
        self.assertIn("does not include home", inactive.inactive or "")

    def test_an_inactive_bundle_takes_no_part_in_conflicts(self) -> None:
        self.shared("laptop-only", '[ubuntu-tweak]\nmachines = ["laptop"]\n' + IFACE + "gtk-theme='L'\n")
        self.shared("home-only", '[ubuntu-tweak]\nmachines = ["home"]\n' + IFACE + "gtk-theme='H'\n")
        load_bundles(self.profile).require_valid()

    def test_requires_module_follows_the_module_gate_and_rejects_unknown_modules(self) -> None:
        self.shared("dock", '[ubuntu-tweak]\nrequires-module = "hyperdock"\n' + IFACE + "k=1\n")
        on = load_bundles(self.profile, lambda name: True)
        off = load_bundles(self.profile, lambda name: False)
        self.assertEqual((len(on.active()), len(off.active())), (1, 0))
        self.assertIn("hyperdock is disabled", off.bundles[0].inactive or "")
        with self.assertRaisesRegex(ConfigError, "no such module"):
            load_bundles(self.profile, lambda name: None)


class BundlesCommandTests(BundleCase):
    def test_list_shows_origin_keys_state_description_and_shadows(self) -> None:
        self.shared("a", '[ubuntu-tweak]\ndescription = "A bundle"\n' + IFACE + "gtk-theme='A'\nicon-theme='I'\n")
        self.shared("far", '[ubuntu-tweak]\nmachines = ["laptop"]\n' + IFACE + "k=1\n")
        self.overlay("a", IFACE + "gtk-theme='B'\n")
        code, out = self.run_cli("bundles")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"a\s+shared\s+2\s+A bundle")
        self.assertRegex(out, r"a\s+overlay\s+1")
        self.assertIn("settings/a.ini:4 = 'A' shadowed by machines/home/settings/a.ini:2 = 'B'", out)

    def test_list_reports_conflicts_and_exits_2(self) -> None:
        self.shared("a", IFACE + "gtk-theme='A'\n")
        self.shared("b", IFACE + "gtk-theme='B'\n")
        code, out = self.run_cli("bundles")
        self.assertEqual(code, 2)
        self.assertIn("CONFLICTS", out)
        self.assertIn("settings/b.ini:2", out)

    def test_show_prints_the_merged_result_with_reasons_and_origins(self) -> None:
        self.shared("a", IFACE + "# keep dark\ngtk-theme='A'\nicon-theme='I'\n")
        self.overlay("a", IFACE + "gtk-theme='B'\n")
        code, out = self.run_cli("bundles", "--show", "a")
        self.assertEqual(code, 0)
        self.assertIn(
            "# keep dark\ngtk-theme='B'\n# from machines/home/settings/a.ini:2, overrides settings/a.ini:3 = 'A'", out
        )
        self.assertIn("icon-theme='I'\n# from settings/a.ini:4", out)

    def test_show_unknown_bundle_is_invalid(self) -> None:
        code, _out = self.run_cli("bundles", "--show", "nope")
        self.assertEqual(code, 2)


class ReviewFixTests(BundleCase):
    def test_a_description_of_the_wrong_type_is_invalid_config(self) -> None:
        for raw in ("5", "true", '["a"]'):
            self.shared("a", f"[ubuntu-tweak]\ndescription = {raw}\n" + IFACE + "k=1\n")
            with self.subTest(raw=raw), self.assertRaisesRegex(ConfigError, "description must be text"):
                load_bundles(self.profile)

    def test_move_between_bundles_with_different_filters_is_refused(self) -> None:
        self.shared("plain", "[org/gnome/mutter]\nedge-tiling=false\n")
        self.shared("dev", '[ubuntu-tweak]\nmachines = ["home"]\n[org/gnome/mutter]\ndynamic-workspaces=true\n')
        code, out = self.run_cli("bundle-move", "dev", "org/gnome/mutter/edge-tiling", "--commit")
        self.assertEqual(code, 2)
        self.assertIn("differ in machines / requires-module", out)
        self.assertIn("edge-tiling", (self.root / "settings/plain.ini").read_text())

    def test_a_new_destination_inherits_the_filters_of_the_source(self) -> None:
        self.shared("old", '[ubuntu-tweak]\nmachines = ["home"]\n[org/gnome/mutter]\nedge-tiling=false\n')
        code, _out = self.run_cli("bundle-move", "fresh", "org/gnome/mutter/edge-tiling", "--commit")
        self.assertEqual(code, 0)
        fresh = (self.root / "settings/fresh.ini").read_text()
        self.assertEqual(fresh, '[ubuntu-tweak]\nmachines=["home"]\n\n[org/gnome/mutter]\nedge-tiling=false\n')

    def test_keys_from_differently_filtered_bundles_cannot_share_a_new_bundle(self) -> None:
        self.shared("a", "[org/gnome/mutter]\nedge-tiling=false\n")
        self.shared("b", '[ubuntu-tweak]\nmachines = ["home"]\n[org/gnome/mutter]\ndynamic-workspaces=true\n')
        code, _out = self.run_cli("bundle-move", "fresh", "org/gnome/mutter/**", "--commit")
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "settings/fresh.ini").exists())

    def test_a_merge_keeps_the_comment_of_a_section_it_empties(self) -> None:
        self.shared("src", "# the mutter section\n[org/gnome/mutter]\n# tile by hand\nedge-tiling=false\n")
        self.shared("dev", "[org/gnome/mutter]\nedge-tiling=false\n")
        self.run_cli("bundle-move", "dev", "org/gnome/mutter/edge-tiling", "--commit")
        dev = (self.root / "settings/dev.ini").read_text()
        self.assertIn("# the mutter section", dev)
        self.assertIn("# tile by hand", dev)

    def test_a_destination_directory_that_escapes_the_profile_is_refused(self) -> None:
        self.shared("src", "[org/gnome/mutter]\nedge-tiling=false\n")
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "settings/alias").symlink_to(outside)
        code, out = self.run_cli("bundle-move", "alias/dev", "org/gnome/mutter/edge-tiling", "--commit")
        self.assertEqual(code, 5)
        self.assertIn("outside", out)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("edge-tiling", (self.root / "settings/src.ini").read_text())


class BundleMoveTests(BundleCase):
    def setUp(self) -> None:
        super().setUp()
        self.shared(
            "keyboard",
            "# panel file\n\n[org/gnome/desktop/wm/keybindings]\n# why left\nswitch-to-workspace-left=['<Super>a']\n"
            "switch-to-workspace-right=['<Super>b']\n\n"
            "# the mutter section\n[org/gnome/mutter]\n# tile by hand\nedge-tiling=false\n",
        )
        self.shared("appearance", IFACE + "gtk-theme='A'\n")

    def test_preview_changes_nothing(self) -> None:
        before = (self.root / "settings/keyboard.ini").read_text()
        code, out = self.run_cli("bundle-move", "dev", "org/gnome/mutter/edge-tiling")
        self.assertEqual(code, 0)
        self.assertIn(
            "move /org/gnome/mutter/edge-tiling: settings/keyboard.ini:11 -> settings/dev.ini  # tile by hand", out
        )
        self.assertIn("preview", out)
        self.assertEqual((self.root / "settings/keyboard.ini").read_text(), before)
        self.assertFalse((self.root / "settings/dev.ini").exists())

    def test_commit_moves_keys_with_their_comments_and_loses_none(self) -> None:
        code, _out = self.run_cli(
            "bundle-move", "dev", "org/gnome/desktop/wm/keybindings/**", "org/gnome/mutter/edge-tiling", "--commit"
        )
        self.assertEqual(code, 0)
        dev = (self.root / "settings/dev.ini").read_text()
        for kept in (
            "# why left\nswitch-to-workspace-left=",
            "switch-to-workspace-right=",
            "# tile by hand\nedge-tiling=false",
            "# the mutter section",
        ):
            self.assertIn(kept, dev)
        keyboard = (self.root / "settings/keyboard.ini").read_text()
        self.assertEqual(keyboard, "# panel file\n")
        bundles = load_bundles(self.profile)
        bundles.require_valid()
        self.assertEqual(sum(len(b.keys) for b in bundles.bundles), 4)  # moved, never duplicated

    def test_a_key_stays_in_its_layer(self) -> None:
        self.overlay("power", "[org/gnome/mutter]\nedge-tiling=true\n")
        self.shared("power", "[org/gnome/desktop/interface]\nicon-theme='P'\n")
        code, _out = self.run_cli("bundle-move", "grouped", "org/gnome/mutter/edge-tiling", "--commit")
        self.assertEqual(code, 0)  # the shared copy (false) and the overlay copy (true) each stay in their layer
        self.assertTrue((self.root / "machines/home/settings/grouped.ini").is_file())
        self.assertTrue((self.root / "settings/grouped.ini").is_file())

    def test_a_pattern_that_matches_nothing_is_invalid(self) -> None:
        code, out = self.run_cli("bundle-move", "dev", "org/nowhere/**")
        self.assertEqual(code, 2)
        self.assertIn("org/nowhere/**", out)

    def test_moving_a_key_the_destination_holds_with_the_same_value_merges_it(self) -> None:
        self.shared("dev", "[org/gnome/mutter]\nedge-tiling=false\n")
        code, out = self.run_cli("bundle-move", "dev", "org/gnome/mutter/edge-tiling", "--commit")
        self.assertEqual(code, 0)
        self.assertIn("merge", out)
        dev = (self.root / "settings/dev.ini").read_text()
        self.assertEqual(dev.count("edge-tiling"), 1)
        self.assertIn("# tile by hand", dev)  # the reason survived the merge

    def test_a_destination_that_conflicts_stops_the_move_at_load_and_writes_nothing(self) -> None:
        self.shared("dev", "[org/gnome/mutter]\nedge-tiling=true\n")
        before = (self.root / "settings/keyboard.ini").read_text()
        code, out = self.run_cli("bundle-move", "dev", "org/gnome/mutter/edge-tiling", "--commit")
        self.assertEqual(code, 2)
        self.assertIn("conflicting values", out)
        self.assertEqual((self.root / "settings/keyboard.ini").read_text(), before)

    def test_invalid_bundle_name_is_refused(self) -> None:
        code, _out = self.run_cli("bundle-move", "../x", "org/gnome/mutter/edge-tiling")
        self.assertEqual(code, 2)


class NoteTests(BundleCase):
    def test_note_previews_then_commits_a_reason_above_the_key(self) -> None:
        path = self.shared("a", IFACE + "gtk-theme='A'\nicon-theme='I'\n")
        target = "a:org/gnome/desktop/interface/gtk-theme"
        code, out = self.run_cli("note", target, "dark for the night shift")
        self.assertEqual(code, 0)
        self.assertIn("reason '' -> 'dark for the night shift'", out)
        self.assertNotIn("night shift", path.read_text())
        self.run_cli("note", target, "dark for the night shift", "--commit")
        self.assertEqual(path.read_text(), IFACE + "# dark for the night shift\ngtk-theme='A'\nicon-theme='I'\n")
        self.run_cli("note", target, "", "--commit")
        self.assertEqual(path.read_text(), IFACE + "gtk-theme='A'\nicon-theme='I'\n")

    def test_note_updates_an_existing_multiline_reason(self) -> None:
        path = self.shared("a", IFACE + "# old one\n# old two\ngtk-theme='A'\n")
        self.run_cli("note", "a:org/gnome/desktop/interface/gtk-theme", "new", "--commit")
        self.assertEqual(path.read_text(), IFACE + "# new\ngtk-theme='A'\n")

    def test_note_in_both_layers_needs_origin(self) -> None:
        self.shared("a", IFACE + "gtk-theme='A'\n")
        overlay = self.overlay("a", IFACE + "gtk-theme='B'\n")
        target = "a:org/gnome/desktop/interface/gtk-theme"
        code, out = self.run_cli("note", target, "why")
        self.assertEqual(code, 2)
        self.assertIn("--origin", out)
        self.run_cli("note", target, "why", "--origin", "overlay", "--commit")
        self.assertIn("# why", overlay.read_text())
        self.assertNotIn("# why", (self.root / "settings/a.ini").read_text())

    def test_note_for_a_missing_key_or_bundle_is_invalid(self) -> None:
        self.shared("a", IFACE + "gtk-theme='A'\n")
        for target in ("a:org/gnome/desktop/interface/nope", "zzz:org/x/y", "no-colon"):
            with self.subTest(target=target):
                self.assertEqual(self.run_cli("note", target, "why")[0], 2)

    def test_note_on_a_package_adds_a_trailing_comment(self) -> None:
        path = write(self.root / "packages.toml", '# mine\napt = ["tmux", "ripgrep"]\n')
        code, _out = self.run_cli("note", "packages:apt:ripgrep", "fast grep", "--commit")
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(), '# mine\napt = [\n  "tmux",\n  "ripgrep",  # fast grep\n]\n')
        self.assertEqual(self.run_cli("note", "packages:apt:nothing", "x")[0], 2)

    def test_a_multiline_reason_stays_one_trailing_comment_in_packages_toml(self) -> None:
        path = write(self.root / "packages.toml", 'apt = ["tmux"]\n')
        self.run_cli("note", "packages:apt:tmux", "shell\nmultiplexer", "--commit")
        import tomllib

        self.assertEqual(tomllib.loads(path.read_text())["apt"], ["tmux"])
        self.assertIn("# shell multiplexer", path.read_text())

    def test_note_on_a_tool_package_finds_the_entry_by_its_name(self) -> None:
        path = write(
            self.root / "packages.toml",
            'uv = ["glances[web]", "hyper-qa @ git+https://git.example.com/u/qa.git"]\nnpm = ["@openai/codex@0.5"]\n',
        )
        for target, reason in (
            ("packages:uv:glances", "system monitor"),
            ("packages:uv:Hyper_QA", "private qa tool"),
            ("packages:npm:@openai/codex", "coding agent"),
        ):
            self.assertEqual(self.run_cli("note", target, reason, "--commit")[0], 0, target)
        text = path.read_text()
        self.assertIn('"glances[web]",  # system monitor', text)
        self.assertIn('"hyper-qa @ git+https://git.example.com/u/qa.git",  # private qa tool', text)
        self.assertIn('"@openai/codex@0.5",  # coding agent', text)
        self.assertEqual(self.run_cli("note", "packages:uv:nothing", "x")[0], 2)
        self.assertEqual(self.run_cli("note", "packages:uv:bad name;id", "x")[0], 2)

    def test_note_on_a_brew_formula(self) -> None:
        path = write(self.root / "packages.toml", 'brew = ["gh"]\n')
        self.assertEqual(self.run_cli("note", "packages:brew:gh", "github cli", "--commit")[0], 0)
        self.assertIn("# github cli", path.read_text())

    def test_note_on_a_system_value_is_the_comment_above_it(self) -> None:
        path = write(self.root / "system.toml", 'timezone = "Europe/Belgrade"\n')
        self.run_cli("note", "system:timezone", "where I live", "--commit")
        self.assertEqual(path.read_text(), '# where I live\ntimezone = "Europe/Belgrade"\n')


if __name__ == "__main__":
    unittest.main()

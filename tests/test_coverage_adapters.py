"""The coverage adapters on scratch machines: FakeProcess, a FakeBackend and temporary HOME / root directories.

Nothing here reads the real $HOME or /etc, runs a real tool or touches the live dconf database.
"""

import gzip
import hashlib
import inspect
import os
import sys
import unittest
from collections.abc import Iterable, Mapping
from pathlib import Path
from unittest import mock

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from tests.util import TempDirCase, write
from ubuntu_tweaks.adapters import discover_adapters, scan
from ubuntu_tweaks.adapters import dconf, etc, extensions, files, packages, units, usr_local
from ubuntu_tweaks.adapters._system import System, system, use
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import ITEM_PREFIXES, Coverage, Finding, Item, Rules
from ubuntu_tweaks.coverage_rules import load_rules
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema, SettingsError, UserKey

INTERFACE = FakeSchema(
    "org.gnome.desktop.interface",
    {"gtk-theme": FakeKey("s", "'Adwaita'"), "clock-show-weekday": FakeKey("b", "false")},
    path="/org/gnome/desktop/interface/",
)
SHELL = FakeSchema(
    "org.gnome.shell",
    {"enabled-extensions": FakeKey("as", "@as []"), "disabled-extensions": FakeKey("as", "@as []")},
    path="/org/gnome/shell/",
)
PASSWORDS = FakeSchema("org.example.vault", {"master-password": FakeKey("s", "''")}, path="/org/example/vault/")


def ok(stdout: str) -> CommandResult:
    return CommandResult(0, stdout)


class AdapterCase(TempDirCase):
    """A scratch machine: ``self.home``, ``self.rootfs``, a profile and a repository, all under ``self.tmp``."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.rootfs = self.tmp / "rootfs"
        self.repo = self.tmp / "repo"
        self.profile_root = self.tmp / "profile"
        for directory in (self.home, self.rootfs, self.repo):
            directory.mkdir()
        self.ctx = Context(self.repo, Profile(self.profile_root, "box"))

    def machine(
        self, responses: Mapping[tuple[str, ...], CommandResult] | None = None, backend: FakeBackend | None = None
    ) -> None:
        self.process = FakeProcess(responses)
        self.backend_in_use = backend
        manager = use(System(self.process, self.home, self.rootfs, backend))
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)

    def module(self, name: str, text: str) -> None:
        write(self.repo / "modules" / name / "module.toml", f'description = "{name}"\n{text}')

    def ids(self, items: Iterable[Item]) -> list[str]:
        return [item.id for item in items]


class DiscoveryTests(unittest.TestCase):
    def test_every_adapter_is_found_by_dropping_a_file(self) -> None:
        names = [module.NAME for module in discover_adapters()]
        self.assertEqual(names, ["dconf", "disk", "etc", "extensions", "files", "packages", "units", "usr-local"])

    def test_the_machine_seam_defaults_to_the_real_one_and_is_restorable(self) -> None:
        before = system()
        scratch = System(FakeProcess(), Path("/nonexistent-home"))
        with use(scratch):
            self.assertIs(system(), scratch)
        self.assertIsNot(system(), scratch)
        self.assertEqual(before.home, system().home)


class DconfTests(AdapterCase):
    def backend(self) -> FakeBackend:
        backend = FakeBackend([INTERFACE, SHELL, PASSWORDS])
        backend.set_user(INTERFACE.id, None, "gtk-theme", "'Yaru'")
        backend.set_user(INTERFACE.id, None, "clock-show-weekday", "false")  # equals the default
        backend.set_user(SHELL.id, None, "enabled-extensions", "['a@x']")
        return backend

    def test_only_keys_that_differ_from_the_schema_default_are_items(self) -> None:
        self.machine(backend=self.backend())
        (item,) = dconf.discover(self.ctx)
        self.assertEqual(
            (item.adapter, item.id, item.kind), ("dconf", "dconf:/org/gnome/desktop/interface/gtk-theme", "key")
        )
        self.assertEqual(item.detail, "'Yaru' (default 'Adwaita')")

    def test_a_key_no_installed_schema_describes_is_still_reported(self) -> None:
        class WithOrphan(FakeBackend):
            def list_user_keys(self) -> tuple[UserKey, ...]:
                return (*super().list_user_keys(), UserKey("/apps/orphan/", "size"))

        backend = WithOrphan([INTERFACE])
        self.machine(backend=backend)
        (item,) = dconf.discover(self.ctx)
        self.assertEqual(item.id, "dconf:/apps/orphan/size")
        self.assertIn("no installed schema", item.detail)

    def test_an_unreadable_database_is_one_item_not_an_exception(self) -> None:
        class Broken(FakeBackend):
            def list_user_keys(self) -> tuple[UserKey, ...]:
                raise SettingsError("dconf is not installed")

        self.machine(backend=Broken())
        (item,) = dconf.discover(self.ctx)
        self.assertEqual(item.id, "dconf:/")
        self.assertIn("dconf is not installed", item.detail)

    def test_keys_a_bundle_or_module_preset_declares_are_owned(self) -> None:
        write(
            self.profile_root / "settings" / "look.ini",
            "[ubuntu-tweak]\ndescription = \"look\"\n\n[org/gnome/desktop/interface]\ngtk-theme='Yaru'\n",
        )
        self.module("preset", '[source]\nkind = "builtin"\n')
        self.machine(backend=self.backend())
        self.assertEqual(dconf.owned(self.ctx), {"dconf:/org/gnome/desktop/interface/gtk-theme": "settings/look.ini"})

    def test_a_bundle_key_with_no_installed_schema_is_declared_all_the_same(self) -> None:
        write(
            self.profile_root / "settings" / "far.ini",
            '[ubuntu-tweak]\ndescription = "far"\n\n[org/nowhere]\nthing=1\n',
        )
        self.machine(backend=FakeBackend([INTERFACE]))
        self.assertEqual(dconf.owned(self.ctx), {"dconf:/org/nowhere/thing": "settings/far.ini"})

    def test_capture_proposes_bundle_text_with_only_the_user_values_grouped_by_panel(self) -> None:
        backend = self.backend()
        backend.set_user(PASSWORDS.id, None, "master-password", "'hunter2'")
        self.machine(backend=backend)
        found = list(dconf.discover(self.ctx))
        proposals = list(dconf.capture(self.ctx, found))
        self.assertEqual([p.title for p in proposals], ["settings/appearance.ini"])
        self.assertIn("[org/gnome/desktop/interface]", proposals[0].text)
        self.assertIn("gtk-theme='Yaru'", proposals[0].text)
        self.assertNotIn("clock-show-weekday", proposals[0].text)  # at its default
        self.assertNotIn("hunter2", "".join(p.text for p in proposals))  # a credential-named key is never proposed
        self.assertEqual(self.ids(proposals[0].items), ["dconf:/org/gnome/desktop/interface/gtk-theme"])


class FilesTests(AdapterCase):
    def lay_out(self) -> None:
        write(self.home / ".config/foo/app.conf", "x")
        write(self.home / ".config/mimeapps.list", "x" * 2048)
        write(self.home / ".local/share/bar/data", "x")
        write(self.home / ".local/bin/tool", "x")
        write(self.home / ".local/state/skipped/x", "x")
        write(self.home / ".bashrc", "x")
        write(self.home / "Documents/not-a-dotfile", "x")

    def test_top_level_entries_of_the_scanned_places_are_the_items(self) -> None:
        self.lay_out()
        self.machine()
        found = {item.id: item for item in files.discover(self.ctx)}
        self.assertEqual(
            sorted(found),
            [
                "file:~/.bashrc",
                "file:~/.config/foo",
                "file:~/.config/mimeapps.list",
                "file:~/.local/bin/tool",
                "file:~/.local/share/bar",
            ],
        )
        self.assertEqual(found["file:~/.config/foo"].kind, "dir")
        self.assertEqual(found["file:~/.config/foo"].detail, "directory, 1 entry")
        self.assertEqual(found["file:~/.config/mimeapps.list"].detail, "2.0 KiB")

    def test_a_missing_home_lists_nothing(self) -> None:
        self.machine()
        self.assertEqual(list(files.discover(self.ctx)), [])

    def test_a_symlink_is_reported_with_its_target_and_never_followed(self) -> None:
        os.symlink("/nonexistent/target", self.home / ".gitconfig")
        self.machine()
        (item,) = files.discover(self.ctx)
        self.assertEqual((item.kind, item.detail), ("symlink", "-> /nonexistent/target"))

    def test_the_profile_files_own_their_top_level_entry(self) -> None:
        write(self.profile_root / "files/.config/foo/app.conf", "x")
        write(self.profile_root / "files/.local/share/bar/data", "x")
        write(self.profile_root / "files/.vimrc", "x")
        write(self.profile_root / "files/notes.txt", "x")  # not a scanned place
        write(self.profile_root / "machines/box/files/.config/baz/b.conf", "x")
        self.machine()
        self.assertEqual(
            files.owned(self.ctx),
            {
                "file:~/.config/foo": "files/.config/foo/app.conf",
                "file:~/.local/share/bar": "files/.local/share/bar/data",
                "file:~/.vimrc": "files/.vimrc",
                "file:~/.config/baz": "machines/box/files/.config/baz/b.conf",
            },
        )

    def test_a_symlink_into_the_profile_or_the_engine_checkout_is_owned(self) -> None:
        write(self.profile_root / "files/x", "x")
        write(self.repo / "bin/ubuntu-tweak", "x")
        (self.home / ".local/bin").mkdir(parents=True)
        os.symlink(self.repo / "bin/ubuntu-tweak", self.home / ".local/bin/ubuntu-tweak")
        os.symlink(self.profile_root / "files/x", self.home / ".xrc")
        os.symlink("/usr/bin/env", self.home / ".elsewhere")
        self.machine()
        self.assertEqual(
            files.owned(self.ctx),
            {
                "file:~/.local/bin/ubuntu-tweak": "engine checkout (symlink)",
                "file:~/.xrc": "profile (symlink)",
            },
        )

    def test_an_extension_module_owns_the_gnome_shell_data_directory(self) -> None:
        self.module("dock", '[source]\nkind = "local"\npath = "ext/dock"\n')
        self.machine()
        self.assertEqual(files.owned(self.ctx)["file:~/.local/share/gnome-shell"], "module dock")

    def test_capture_proposes_include_files_globs_for_real_entries_only(self) -> None:
        self.lay_out()
        os.symlink("/nonexistent", self.home / ".zshrc")
        self.machine()
        found = [item for item in files.discover(self.ctx) if item.id != "file:~/.local/share/bar"]
        (proposal,) = files.capture(self.ctx, found)
        self.assertEqual(proposal.title, "profile.toml [capture] include_files")
        self.assertIn('"' + ".config/foo/**" + '"', proposal.text)
        self.assertIn('".bashrc"', proposal.text)
        self.assertNotIn(".zshrc", proposal.text)
        self.assertTrue(proposal.text.startswith("[capture]\ninclude_files = [\n"))


JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcdefghijk"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" + "k1L2m3N4o5P6"


class FilesClassifyTests(AdapterCase):
    """The files adapter asks capture's file classifier what an entry holds (never reading a secret's value)."""

    def verdict(self, rel: str) -> Finding | None:
        self.machine()
        (item,) = [i for i in files.discover(self.ctx) if i.id == f"file:~/{rel}"]
        return files.classify(self.ctx, item, Rules())

    def test_a_directory_of_credential_named_files_is_secret_by_name(self) -> None:
        write(self.home / ".config/foo/credentials.json", TOKEN)
        write(self.home / ".config/foo/api_token", "x")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual((finding.coverage, finding.reason), (Coverage.SECRET, "credential store by name"))

    def test_a_secret_named_file_is_never_opened(self) -> None:
        path = write(self.home / ".config/foo/credentials", TOKEN)
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.SECRET)

    def test_a_secret_pattern_inside_a_plainly_named_file_is_secret_and_the_match_is_never_shown(self) -> None:
        write(self.home / ".config/foo/session.conf", f"jwt = {JWT}\n")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual((finding.coverage, finding.reason), (Coverage.SECRET, "content matches: jwt"))
        self.assertNotIn("eyJ", repr(finding))

    def test_a_github_token_in_a_file_is_secret_without_the_token_in_the_finding(self) -> None:
        write(self.home / ".config/foo/notes", f"{TOKEN}\n")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.SECRET)
        self.assertNotIn("ghp_", repr(finding))

    def test_only_caches_state_logs_and_bytecode_is_excluded_as_cache_state(self) -> None:
        write(self.home / ".config/foo/app.log", "x")
        write(self.home / ".config/foo/state.sqlite", "x")
        write(self.home / ".config/foo/mod.pyc", "x")
        write(self.home / ".config/foo/lock", "")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.EXCLUDED)
        self.assertTrue(finding.reason.startswith("cache-state: "), finding.reason)

    def test_a_named_pipe_is_state_and_is_never_opened(self) -> None:
        (self.home / ".config/foo").mkdir(parents=True)
        os.mkfifo(self.home / ".config/foo/ipc")  # opening it for reading would block the test forever
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.EXCLUDED)

    def test_an_empty_directory_is_excluded(self) -> None:
        (self.home / ".config/foo").mkdir(parents=True)
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual((finding.coverage, finding.reason), (Coverage.EXCLUDED, "empty directory: nothing to track"))

    def test_portable_config_only_stays_untracked_and_says_capture_can_adopt_it(self) -> None:
        write(self.home / ".config/foo/app.conf", "theme = dark\n")
        write(self.home / ".config/foo/keys.toml", "a = 1\n")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.UNTRACKED)
        self.assertEqual(finding.item.detail, "directory, 2 entries; portable config: capture can adopt")

    def test_a_portable_file_at_the_top_level_is_portable_config(self) -> None:
        write(self.home / ".config/mimeapps.list", "[Default Applications]\n")
        finding = self.verdict(".config/mimeapps.list")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.UNTRACKED)
        self.assertTrue(finding.item.detail.endswith("portable config: capture can adopt"), finding.item.detail)

    def test_portable_config_next_to_a_secret_is_mixed_and_needs_a_rule(self) -> None:
        write(self.home / ".config/foo/app.conf", "theme = dark\n")
        write(self.home / ".config/foo/credentials", "x")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.UNTRACKED)
        self.assertEqual(finding.item.detail, "directory, 2 entries; mixed: needs a rule")

    def test_portable_config_next_to_state_is_mixed_and_needs_a_rule(self) -> None:
        write(self.home / ".config/foo/app.conf", "theme = dark\n")
        write(self.home / ".config/foo/app.log", "x")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertTrue(finding.item.detail.endswith("mixed: needs a rule"), finding.item.detail)

    def test_a_large_application_data_directory_is_mixed(self) -> None:
        for number in range(70):
            write(self.home / f".local/share/bigapp/f{number}.conf", "x")
        finding = self.verdict(".local/share/bigapp")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.UNTRACKED)
        self.assertTrue(finding.item.detail.endswith("mixed: needs a rule"), finding.item.detail)

    def test_a_file_the_classifier_cannot_read_is_mixed_not_hidden_as_cache(self) -> None:
        path = write(self.home / ".config/foo/app.conf", "theme = dark\n")
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        if os.access(path, os.R_OK):
            self.skipTest("this user reads files regardless of their mode")
        finding = self.verdict(".config/foo")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.UNTRACKED)
        self.assertTrue(finding.item.detail.endswith("mixed: needs a rule"), finding.item.detail)

    def test_a_link_into_a_package_manager_tree_is_excluded_naming_the_installer(self) -> None:
        (self.home / ".local/bin").mkdir(parents=True)
        for name, target, installer in (
            ("httpie", self.home / ".local/share/uv/tools/httpie/bin/http", "uv"),
            ("tsc", self.home / ".local/lib/node_modules/typescript/bin/tsc", "npm"),
            ("tool", self.home / ".local/opt/tool/bin/tool", "a manual install under ~/.local/opt"),
            ("app", self.home / "Applications/App.AppImage", "an AppImage installer"),
        ):
            os.symlink(target, self.home / ".local/bin" / name)
            finding = self.verdict(f".local/bin/{name}")
            assert finding is not None
            self.assertEqual(finding.coverage, Coverage.EXCLUDED, name)
            self.assertEqual(finding.reason, f"link created by {installer}; the package is declared elsewhere", name)

    def test_a_relative_link_into_a_package_manager_tree_is_recognised(self) -> None:
        (self.home / ".local/bin").mkdir(parents=True)
        os.symlink("../share/uv/tools/ruff/bin/ruff", self.home / ".local/bin/ruff")
        finding = self.verdict(".local/bin/ruff")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.EXCLUDED)

    def test_a_link_elsewhere_gives_no_opinion(self) -> None:
        os.symlink("/usr/bin/env", self.home / ".elsewhere")
        self.assertIsNone(self.verdict(".elsewhere"))

    def test_a_link_that_only_looks_like_a_tree_name_is_not_excluded(self) -> None:
        os.symlink(self.home / ".local/share/uv/tools-backup/x", self.home / ".lookalike")
        self.assertIsNone(self.verdict(".lookalike"))

    def test_classifying_changes_nothing_on_disk(self) -> None:
        write(self.home / ".config/foo/app.conf", "theme = dark\n")
        write(self.home / ".config/foo/credentials", "x")
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        self.verdict(".config/foo")
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), before)
        self.assertEqual(self.process.executed, [])


class DconfClassifyTests(AdapterCase):
    """The dconf adapter classifies a key the way ``capture`` would treat it."""

    PTYXIS = FakeSchema(
        "org.gnome.Ptyxis",
        {"window-size": FakeKey("(ii)", "(0, 0)"), "default-profile-uuid": FakeKey("s", "''")},
        path="/org/gnome/Ptyxis/",
    )
    POWER = FakeSchema(
        "org.gnome.settings-daemon.plugins.power",
        {"sleep-inactive-ac-timeout": FakeKey("i", "1200")},
        path="/org/gnome/settings-daemon/plugins/power/",
    )
    APP = FakeSchema(
        "org.example.App",
        {"email": FakeKey("s", "''"), "blob": FakeKey("s", "''")},
        path="/org/example/app/",
    )
    BLUETOOTH = FakeSchema("org.gnome.bluetooth", {"powered": FakeKey("b", "false")}, path="/org/gnome/bluetooth/")
    EXTENSION = FakeSchema(
        "org.gnome.shell.extensions.foo", {"opt": FakeKey("b", "false")}, path="/org/gnome/shell/extensions/foo/"
    )
    NIGHT = FakeSchema(
        "org.gnome.settings-daemon.plugins.color",
        {"night-light-last-coordinates": FakeKey("(dd)", "(91.0, 181.0)")},
        path="/org/gnome/settings-daemon/plugins/color/",
    )

    def setUp(self) -> None:
        super().setUp()
        self.backend = FakeBackend(
            [INTERFACE, PASSWORDS, self.PTYXIS, self.POWER, self.APP, self.BLUETOOTH, self.EXTENSION, self.NIGHT]
        )
        for schema, key, value in (
            (INTERFACE, "gtk-theme", "'Yaru'"),
            (PASSWORDS, "master-password", "'hunter2'"),
            (self.PTYXIS, "window-size", "(100, 200)"),
            (self.PTYXIS, "default-profile-uuid", "'abc'"),
            (self.POWER, "sleep-inactive-ac-timeout", "3600"),
            (self.APP, "email", "'me@example.com'"),
            (self.APP, "blob", f"'{TOKEN}'"),
            (self.BLUETOOTH, "powered", "true"),
            (self.EXTENSION, "opt", "true"),
            (self.NIGHT, "night-light-last-coordinates", "(10.5, 20.25)"),
        ):
            self.backend.set_user(schema.id, None, key, value)

    def verdict(self, full: str) -> Finding | None:
        self.machine(backend=self.backend)
        (item,) = [i for i in dconf.discover(self.ctx) if i.id == f"dconf:{full}"]
        return dconf.classify(self.ctx, item, Rules())

    def test_a_key_capture_would_capture_stays_untracked(self) -> None:
        self.assertIsNone(self.verdict("/org/gnome/desktop/interface/gtk-theme"))

    def test_a_machine_bound_key_stays_untracked_because_capture_puts_it_in_the_overlay(self) -> None:
        self.assertIsNone(self.verdict("/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-timeout"))

    def test_a_state_key_is_excluded_with_capture_reason(self) -> None:
        finding = self.verdict("/org/gnome/Ptyxis/window-size")
        assert finding is not None
        self.assertEqual((finding.coverage, finding.reason), (Coverage.EXCLUDED, "window geometry and UI position"))

    def test_night_light_coordinates_are_excluded_as_state(self) -> None:
        finding = self.verdict("/org/gnome/settings-daemon/plugins/color/night-light-last-coordinates")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.EXCLUDED)
        self.assertNotIn("10.5", repr(finding))

    def test_an_auto_key_is_excluded_with_capture_reason(self) -> None:
        finding = self.verdict("/org/gnome/Ptyxis/default-profile-uuid")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.EXCLUDED)
        self.assertIn("uuids Ptyxis generates", finding.reason)

    def test_a_panel_skip_rule_of_class_excluded_by_rule_is_excluded(self) -> None:
        finding = self.verdict("/org/gnome/shell/extensions/foo/opt")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.EXCLUDED)
        self.assertIn("belong to modules", finding.reason)

    def test_a_profile_capture_exclude_is_excluded(self) -> None:
        write(self.profile_root / "profile.toml", '[capture]\nexclude = ["/org/gnome/desktop/interface/**"]\n')
        finding = self.verdict("/org/gnome/desktop/interface/gtk-theme")
        assert finding is not None
        self.assertEqual((finding.coverage, finding.reason), (Coverage.EXCLUDED, "profile [capture] exclude"))

    def test_a_profile_include_lifts_a_state_key_back_to_untracked(self) -> None:
        write(self.profile_root / "profile.toml", '[capture]\ninclude = ["/org/gnome/Ptyxis/window-size"]\n')
        self.assertIsNone(self.verdict("/org/gnome/Ptyxis/window-size"))

    def test_a_credential_named_key_is_secret_and_its_value_is_never_in_the_finding(self) -> None:
        finding = self.verdict("/org/example/vault/master-password")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.SECRET)
        self.assertNotIn("hunter2", repr(finding))

    def test_an_identity_key_is_secret(self) -> None:
        finding = self.verdict("/org/example/app/email")
        assert finding is not None
        self.assertEqual((finding.coverage, finding.reason), (Coverage.SECRET, "key name says personal identity"))
        self.assertNotIn("example.com", repr(finding))

    def test_a_hardware_key_is_secret(self) -> None:
        finding = self.verdict("/org/gnome/bluetooth/powered")
        assert finding is not None
        self.assertEqual(
            (finding.coverage, finding.reason), (Coverage.SECRET, "Bluetooth pairings are machine hardware")
        )

    def test_a_value_that_looks_like_a_credential_is_secret_and_masked(self) -> None:
        finding = self.verdict("/org/example/app/blob")
        assert finding is not None
        self.assertEqual(finding.coverage, Coverage.SECRET)
        self.assertIn("value matches: api-token", finding.reason)
        self.assertNotIn("ghp_", repr(finding))

    def test_a_key_no_installed_schema_describes_is_excluded_as_capture_skips_it(self) -> None:
        class WithOrphan(FakeBackend):
            def list_user_keys(self) -> tuple[UserKey, ...]:
                return (*super().list_user_keys(), UserKey("/apps/orphan/", "level"))

        self.backend = WithOrphan([INTERFACE])
        finding = self.verdict("/apps/orphan/level")
        assert finding is not None
        self.assertEqual(
            (finding.coverage, finding.reason), (Coverage.EXCLUDED, "no installed schema describes this key")
        )

    def test_an_unreadable_database_gives_no_opinion(self) -> None:
        self.machine(backend=self.backend)
        self.assertIsNone(dconf.classify(self.ctx, Item("dconf", "dconf:/", "database", "cannot read"), Rules()))


class ClassifyRobustnessTests(AdapterCase):
    def test_an_invalid_capture_table_gives_no_opinion_instead_of_aborting_the_scan(self) -> None:
        write(self.profile_root / "profile.toml", '[capture]\nexclude = "x"\n')
        backend = FakeBackend([INTERFACE])
        backend.set_user(INTERFACE.id, None, "gtk-theme", "'Yaru'")
        self.machine(backend=backend)
        (item,) = dconf.discover(self.ctx)
        self.assertIsNone(dconf.classify(self.ctx, item, Rules()))

    def test_an_invalid_capture_table_gives_the_files_adapter_no_opinion_either(self) -> None:
        write(self.profile_root / "profile.toml", '[capture]\nexclude = "x"\n')
        write(self.home / ".config/foo/app.conf", "x")
        self.machine()
        (item,) = files.discover(self.ctx)
        self.assertIsNone(files.classify(self.ctx, item, Rules()))

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_a_directory_this_user_cannot_list_stays_a_finding_not_an_empty_directory(self) -> None:
        (self.home / ".config/locked").mkdir(parents=True)
        (self.home / ".config/locked").chmod(0o000)
        self.addCleanup((self.home / ".config/locked").chmod, 0o700)
        self.machine()
        (item,) = [i for i in files.discover(self.ctx) if i.id.endswith("locked")]
        self.assertIsNone(files.classify(self.ctx, item, Rules()))


class ScanOpinionTests(AdapterCase):
    """``adapters.scan``: an adapter's opinion applies only to what no rule and no declaration claims."""

    def setUp(self) -> None:
        super().setUp()
        write(self.home / ".config/foo/app.conf", "x")
        write(self.home / ".cache/x", "x")
        write(self.home / ".config/bar/credentials", "x")

    def scan(self) -> dict[str, Finding]:
        from ubuntu_tweaks.adapters import scan
        from ubuntu_tweaks.coverage_rules import load_rules

        self.machine(backend=FakeBackend([INTERFACE]))
        return {f.item.id: f for f in scan(self.ctx, load_rules(self.ctx)) if f.item.adapter == "files"}

    def test_an_unclaimed_item_gets_the_adapter_opinion(self) -> None:
        found = self.scan()
        self.assertEqual(found["file:~/.config/bar"].coverage, Coverage.SECRET)
        self.assertIn("portable config", found["file:~/.config/foo"].item.detail)

    def test_an_engine_rule_wins_over_the_opinion(self) -> None:
        self.assertEqual(self.scan()["file:~/.cache"].reason, "cache: rebuilt by the tool that owns it")

    def test_a_profile_rule_wins_over_the_opinion(self) -> None:
        write(
            self.profile_root / "coverage.toml",
            '[[coverage]]\nmatch = ["file:~/.config/bar"]\nclass = "untracked"\nreason = "later"\nticket = "#9"\n',
        )
        finding = self.scan()["file:~/.config/bar"]
        self.assertEqual((finding.coverage, finding.ticket), (Coverage.UNTRACKED, "#9"))

    def test_a_declaration_wins_over_the_opinion(self) -> None:
        write(self.profile_root / "files/.config/bar/settings.conf", "x")
        self.assertEqual(self.scan()["file:~/.config/bar"].coverage, Coverage.TRACKED)


class PackagesTests(AdapterCase):
    RESPONSES = {
        ("apt-mark", "showmanual"): ok("curl\ngit\n"),
        ("snap", "list"): ok(
            "Name  Version  Rev  Tracking  Publisher  Notes\ncore22  1  2  latest  canonical  base\nfirefox 1 2 x y -\n"
        ),
        ("flatpak", "list", "--app", "--columns=application"): ok("org.gimp.GIMP\n"),
        ("brew", "leaves"): ok("jq\n"),
        ("pipx", "list", "--short"): ok("ruff 0.5.0\n"),
        ("uv", "tool", "list"): ok("httpie v3.2.4\n- http\n- https\n"),
        ("npm", "ls", "-g", "--depth=0", "--parseable", "--prefix", "{home}/.local"): ok(
            "{home}/.local/lib\n{home}/.local/lib/node_modules/@scope/tool\n{home}/.local/lib/node_modules/corepack\n"
        ),
        ("bun", "pm", "ls", "-g"): ok(
            "/home/u/.bun/install/global node_modules (2)\n├── prettier@3.0.0\n└── @a/b@1.2.3\n"
        ),
        ("cargo", "install", "--list"): ok("ripgrep v14.1.0:\n    rg\nfd-find v10.0.0:\n    fd\n"),
    }

    def responses(self) -> dict[tuple[str, ...], CommandResult]:
        """``RESPONSES`` with ``{home}`` replaced by the scratch home (npm is listed under ``~/.local``)."""
        home = str(self.home)
        return {
            tuple(part.replace("{home}", home) for part in argv): CommandResult(
                result.returncode, result.stdout.replace("{home}", home), result.stderr
            )
            for argv, result in self.RESPONSES.items()
        }

    def test_each_manager_is_listed_and_ids_carry_the_manager(self) -> None:
        self.machine(self.responses())
        self.assertEqual(
            self.ids(packages.discover(self.ctx)),
            [
                "apt:curl",
                "apt:git",
                "snap:core22",
                "snap:firefox",
                "flatpak:org.gimp.GIMP",
                "brew:jq",
                "uv:httpie",
                "npm:@scope/tool",
                "npm:corepack",
                "bun:@a/b",
                "bun:prettier",
                "cargo:fd-find",
                "cargo:ripgrep",
                "pipx:ruff",
            ],
        )

    def test_a_manager_that_is_not_installed_or_fails_yields_nothing(self) -> None:
        self.machine({("apt-mark", "showmanual"): ok("curl\n"), ("snap", "list"): CommandResult(1, "", "boom")})
        self.assertEqual(self.ids(packages.discover(self.ctx)), ["apt:curl"])
        self.assertIn(("brew", "leaves"), self.process.queries)

    def test_packages_toml_and_module_installs_are_owned(self) -> None:
        write(
            self.profile_root / "packages.toml",
            'apt = ["curl"]\nsnap = ["firefox"]\nsnap_classic = ["code"]\nbrew = ["jq"]\n',
        )
        write(self.profile_root / "machines/box/packages.toml", 'flatpak = ["org.gimp.GIMP"]\n')
        self.module("tools", '[source]\nkind = "builtin"\n[install]\napt = ["git"]\n[deps]\napt = ["zip"]\n')
        self.module(
            "off",
            '[source]\nkind = "builtin"\n[install]\napt = ["nginx"]\n',
        )
        write(self.profile_root / "profile.toml", "[modules.off]\nenabled = false\n")
        self.machine()
        self.assertEqual(
            packages.owned(self.ctx),
            {
                "apt:curl": "packages.toml",
                "snap:firefox": "packages.toml",
                "snap:code": "packages.toml",
                "brew:jq": "packages.toml",
                "flatpak:org.gimp.GIMP": "packages.toml",
                "apt:git": "module tools",
                "apt:zip": "module tools",
            },
        )

    def test_capture_writes_packages_toml_for_every_manager_it_has_a_key_for(self) -> None:
        self.machine(self.responses())
        (proposal,) = packages.capture(self.ctx, list(packages.discover(self.ctx)))
        self.assertEqual(proposal.title, "packages.toml")
        self.assertIn('apt = [\n  "curl",\n  "git",\n]\n', proposal.text)
        self.assertIn('brew = [\n  "jq",\n]\n', proposal.text)
        self.assertIn('pipx = [\n  "ruff",\n]\n', proposal.text)
        self.assertIn('npm = [\n  "@scope/tool",\n  "corepack",\n]\n', proposal.text)
        self.assertIn('bun = [\n  "@a/b",\n  "prettier",\n]\n', proposal.text)
        self.assertIn('cargo = [\n  "fd-find",\n  "ripgrep",\n]\n', proposal.text)
        self.assertEqual(len(proposal.items), 13)  # all but uv:httpie, which has no receipt and is only a comment

    def test_tool_manager_entries_are_owned_by_name_not_by_spec(self) -> None:
        write(
            self.profile_root / "packages.toml",
            'uv = ["glances[web]", "Git_Filter.Repo", "hyper-qa @ git+https://git.example.com/acme/qa-cli.git"]\n'
            'npm = ["@openai/codex@0.5.1"]\nbun = ["cf"]\ncargo = ["wasm-bindgen-cli@0.2.100"]\n',
        )
        write(self.profile_root / "machines/box/packages.toml", 'pipx = ["black"]\n')
        self.machine()
        self.assertEqual(
            packages.owned(self.ctx),
            {
                "uv:glances": "packages.toml",
                "uv:git-filter-repo": "packages.toml",
                "uv:hyper-qa": "packages.toml",
                "pipx:glances": "packages.toml",  # what apply installs when uv fails: owned too
                "pipx:git-filter-repo": "packages.toml",
                "pipx:hyper-qa": "packages.toml",
                "bun:@openai/codex": "packages.toml",
                "npm:@openai/codex": "packages.toml",
                "bun:cf": "packages.toml",
                "cargo:wasm-bindgen-cli": "packages.toml",
                "pipx:black": "packages.toml",
            },
        )

    def test_a_fallback_install_is_owned_too_so_coverage_agrees_with_apply(self) -> None:
        write(self.profile_root / "packages.toml", 'uv = ["Glances[web]"]\nnpm = ["agent-browser"]\n')
        self.machine()
        owned = packages.owned(self.ctx)
        self.assertEqual(owned["pipx:glances"], "packages.toml")
        self.assertEqual(owned["bun:agent-browser"], "packages.toml")
        self.assertNotIn("pipx:agent-browser", owned)

    def test_npm_is_listed_under_the_user_prefix(self) -> None:
        self.machine(self.responses())
        list(packages.discover(self.ctx))
        self.assertIn(
            ("npm", "ls", "-g", "--depth=0", "--parseable", "--prefix", str(self.home / ".local")),
            self.process.queries,
        )


class PackagesClassifyTests(AdapterCase):
    """apt and snap items capture refuses are ``excluded`` with capture's own reason, not ``untracked``."""

    MANUAL = ["base-pkg", "bash-completion", "libfoo-runtime", "libssl-dev", "mytool", "ubuntu-desktop", "vim-tiny"]

    def machine_with(self, *, baseline: bool, snaps: str = "") -> None:
        if baseline:
            path = self.rootfs / "var/log/installer/initial-status.gz"
            path.parent.mkdir(parents=True)
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write("Package: base-pkg\nStatus: install ok installed\n\nPackage: libfoo-runtime\n")
        manual = "".join(f"{name}\n" for name in self.MANUAL)
        priorities = "".join(f"{name}\t{'important' if name == 'vim-tiny' else 'optional'}\n" for name in self.MANUAL)
        self.machine(
            {
                ("apt-mark", "showmanual"): ok(manual),
                ("dpkg-query", "-W", "-f=${Package}\t${Priority}\n", *self.MANUAL): ok(priorities),
                ("apt-cache", "depends", "--no-recommends", "--no-suggests", "--no-conflicts", "--no-breaks",
                 "--no-replaces", "--no-enhances", "ubuntu-desktop"): ok("ubuntu-desktop\n  Depends: bash-completion\n"),
                ("snap", "list"): ok(snaps),
            }
        )  # fmt: skip

    def classified(self) -> dict[str, tuple[Coverage, str]]:
        rules = load_rules(self.ctx)
        return {
            finding.item.id: (finding.coverage, finding.reason)
            for finding in scan(self.ctx, rules)
            if finding.item.adapter == "packages"
        }

    def test_the_installer_baseline_and_capture_ignore_rules_exclude_with_the_capture_reason(self) -> None:
        self.machine_with(baseline=True)
        found = self.classified()

        self.assertEqual(found["apt:base-pkg"], (Coverage.EXCLUDED, "installer baseline"))
        self.assertEqual(found["apt:libfoo-runtime"], (Coverage.EXCLUDED, "installer baseline"))
        self.assertEqual(
            found["apt:vim-tiny"],
            (Coverage.EXCLUDED, "distribution base (capture rule: priority required or important)"),
        )
        self.assertEqual(found["apt:ubuntu-desktop"][0], Coverage.EXCLUDED)  # an engine rule claims it first
        self.assertEqual(found["apt:mytool"][0], Coverage.UNTRACKED)  # a decision capture would propose
        self.assertEqual(found["apt:libssl-dev"][0], Coverage.UNTRACKED)  # `keep` wins over the lib* ignore

    def test_without_the_baseline_file_the_metapackage_dependencies_are_the_base(self) -> None:
        self.machine_with(baseline=False)
        found = self.classified()

        self.assertEqual(
            found["apt:bash-completion"],
            (
                Coverage.EXCLUDED,
                "distribution base (capture rule: dependency of ubuntu-desktop, ubuntu-desktop-minimal, "
                "ubuntu-minimal, ubuntu-standard, ubuntu-server)",
            ),
        )
        self.assertEqual(found["apt:base-pkg"][0], Coverage.UNTRACKED)

    def test_ignored_and_seeded_snaps_are_excluded_and_applications_stay_untracked(self) -> None:
        seed = self.rootfs / "var/lib/snapd/seed/seed.yaml"
        write(seed, "snaps:\n  - name: seeded-app\n")
        self.machine_with(
            baseline=True,
            snaps="Name Version Rev Tracking Publisher Notes\n"
            "core22 1 2 latest canonical base\ngnome-42-2204 1 2 latest canonical -\n"
            "seeded-app 1 2 latest x -\nfirefox 1 2 latest mozilla -\n",
        )
        found = self.classified()

        self.assertEqual(found["snap:core22"][0], Coverage.EXCLUDED)
        self.assertEqual(found["snap:gnome-42-2204"][0], Coverage.EXCLUDED)
        self.assertEqual(found["snap:seeded-app"], (Coverage.EXCLUDED, "installer baseline"))
        self.assertEqual(found["snap:firefox"][0], Coverage.UNTRACKED)

    def test_a_declared_name_stays_tracked_even_when_capture_would_refuse_it(self) -> None:
        write(self.profile_root / "packages.toml", 'apt = ["base-pkg"]\n')
        self.machine_with(baseline=True)
        self.assertEqual(self.classified()["apt:base-pkg"][0], Coverage.TRACKED)

    def test_a_profile_coverage_rule_beats_the_capture_opinion(self) -> None:
        write(
            self.profile_root / "coverage.toml",
            '[[coverage]]\nmatch = ["apt:base-pkg"]\nclass = "secret"\nreason = "profile says so"\n',
        )
        self.machine_with(baseline=True)
        self.assertEqual(self.classified()["apt:base-pkg"], (Coverage.SECRET, "profile says so"))

    def test_the_opinion_names_the_capture_rule_for_what_no_other_rule_claims(self) -> None:
        self.machine_with(baseline=True, snaps="Name Version Rev Tracking Publisher Notes\nbare 1 2 latest c base\n")
        for item_id, reason in (
            ("apt:ubuntu-desktop", "distribution base (capture rule: Ubuntu metapackage)"),
            ("snap:bare", "distribution base (capture rule: snap base or snapd)"),
            ("apt:mytool", None),
        ):
            with self.subTest(item=item_id):
                finding = packages.classify(self.ctx, Item("packages", item_id, "package"), Rules())
                self.assertEqual(None if finding is None else finding.reason, reason)
        self.assertIsNone(packages.classify(self.ctx, Item("packages", "uv:glances", "package"), Rules()))

    def test_a_second_scan_sees_what_changed_since_the_first(self) -> None:
        self.machine_with(baseline=True)
        self.assertEqual(self.classified()["apt:base-pkg"][0], Coverage.EXCLUDED)
        write(self.profile_root / "packages.toml", 'apt = ["base-pkg"]\n')
        self.assertEqual(self.classified()["apt:base-pkg"][0], Coverage.TRACKED)

    def test_the_classification_reads_without_executing_anything(self) -> None:
        self.machine_with(baseline=True)
        self.classified()
        self.assertEqual(self.process.executed, [])


class PackagesCaptureSourceTests(AdapterCase):
    def tool(self, name: str, receipt: str | None) -> None:
        if receipt is not None:
            write(self.home / ".local/share/uv/tools" / name / "uv-receipt.toml", receipt)

    def proposal_text(self, uv_listing: str) -> str:
        self.machine({("uv", "tool", "list"): ok(uv_listing)})
        (proposal,) = packages.capture(self.ctx, list(packages.discover(self.ctx)))
        return proposal.text

    def test_a_git_receipt_gives_a_pep_508_direct_reference(self) -> None:
        self.tool(
            "hyper-qa", '[tool]\nrequirements = [{ name = "hyper-qa", git = "https://git.example.com/u/qa-cli.git" }]\n'
        )
        text = self.proposal_text("hyper-qa v0.1\n- hyper-qa\n")
        self.assertEqual(text, 'uv = [\n  "hyper-qa @ git+https://git.example.com/u/qa-cli.git",\n]\n')

    def test_uvs_wire_form_of_a_git_source_is_translated(self) -> None:
        self.tool(
            "netprint",
            '[tool]\nrequirements = [{ name = "netprint", git = "https://h/x.git?rev=v1.2&subdirectory=cli#0123abcd" }]\n',
        )
        self.tool("plain", '[tool]\nrequirements = [{ name = "plain", git = "https://h/y.git#0123abcd" }]\n')
        text = self.proposal_text("netprint v1\nplain v1\n")
        self.assertIn('"netprint @ git+https://h/x.git@v1.2#subdirectory=cli"', text)
        self.assertIn('"plain @ git+https://h/y.git"', text)  # no ref asked for: the resolved commit is not pinned

    def test_a_cargo_crate_installed_from_git_is_flagged_not_reduced_to_a_name(self) -> None:
        self.machine(
            {
                ("cargo", "install", "--list"): ok(
                    "ripgrep v14.1.0:\n    rg\nqa-cli v0.1.0 (https://git.example.com/acme/qa-cli#abc):\n    qa\n"
                )
            }
        )
        (proposal,) = packages.capture(self.ctx, list(packages.discover(self.ctx)))
        self.assertEqual(
            proposal.text, 'cargo = [\n  # "qa-cli",  # source unknown: set the spec by hand\n  "ripgrep",\n]\n'
        )
        self.assertEqual([item.id for item in proposal.items], ["cargo:ripgrep"])

    def test_a_pinned_git_ref_and_subdirectory_are_kept(self) -> None:
        self.tool(
            "netprint",
            '[tool]\nrequirements = [{ name = "netprint", git = "https://h/x.git", rev = "abc123", subdirectory = "cli" }]\n',
        )
        text = self.proposal_text("netprint v1\n")
        self.assertIn('"netprint @ git+https://h/x.git@abc123#subdirectory=cli"', text)

    def test_a_registry_receipt_keeps_extras_and_specifier(self) -> None:
        self.tool("glances", '[tool]\nrequirements = [{ name = "glances", extras = ["web"], specifier = ">=4" }]\n')
        self.tool("esphome", '[tool]\nrequirements = [{ name = "esphome" }]\n')
        text = self.proposal_text("esphome v2026\nglances v4\n")
        self.assertEqual(text, 'uv = [\n  "esphome",\n  "glances[web]>=4",\n]\n')

    def test_a_path_receipt_gives_a_file_url(self) -> None:
        self.tool("local-tool", '[tool]\nrequirements = [{ name = "local-tool", directory = "/src/local-tool" }]\n')
        self.assertIn('"local-tool @ file:///src/local-tool"', self.proposal_text("local-tool v1\n"))

    def test_without_a_usable_receipt_the_source_is_flagged_not_invented(self) -> None:
        self.tool("broken", "this is not toml [[[")
        self.tool("empty", "[tool]\n")
        self.tool("wrong-name", '[tool]\nrequirements = [{ name = "other", git = "https://h/x.git" }]\n')
        text = self.proposal_text("broken v1\nempty v1\nmissing v1\nwrong-name v1\n")
        for name in ("broken", "empty", "missing", "wrong-name"):
            self.assertIn(f'# "{name}",  # source unknown: set the spec by hand', text)
        self.assertNotIn("git+", text)

    def test_a_receipt_with_a_source_the_adapter_does_not_know_is_flagged_not_reduced_to_a_name(self) -> None:
        self.tool("mytool", '[tool]\nrequirements = [{ name = "mytool", editable = "/src/mytool" }]\n')
        self.tool(
            "acme-cli", '[tool]\nrequirements = [{ name = "acme-cli", index = "https://pypi.acme.internal/simple" }]\n'
        )
        text = self.proposal_text("acme-cli v1\nmytool v1\n")
        self.assertIn('# "mytool",  # source unknown', text)
        self.assertIn('# "acme-cli",  # source unknown', text)
        self.assertNotIn('  "mytool",\n', text)

    def test_items_written_only_as_a_comment_are_not_claimed_by_the_proposal(self) -> None:
        self.tool("esphome", '[tool]\nrequirements = [{ name = "esphome" }]\n')
        self.machine({("uv", "tool", "list"): ok("esphome v1\nmissing v1\n")})
        (proposal,) = packages.capture(self.ctx, list(packages.discover(self.ctx)))
        self.assertEqual([item.id for item in proposal.items], ["uv:esphome"])

    def test_a_requirement_with_a_marker_is_flagged_because_the_spec_would_lose_it(self) -> None:
        self.tool(
            "platform-tool",
            '[tool]\nrequirements = [{ name = "platform-tool", marker = "python_version < \'3.12\'" }]\n',
        )
        self.assertIn('# "platform-tool",  # source unknown', self.proposal_text("platform-tool v1\n"))

    def test_a_receipt_that_records_more_than_a_name_and_a_source_is_flagged(self) -> None:
        self.tool("mkdocs", '[tool]\nrequirements = [{ name = "mkdocs" }, { name = "mkdocs-material" }]\n')
        self.tool(
            "acme-cli",
            '[tool]\nrequirements = [{ name = "acme-cli" }]\n[tool.options]\nindex-url = "https://pypi.acme.internal/simple"\n',
        )
        self.tool("pinned", '[tool]\nrequirements = [{ name = "pinned" }]\npython = "3.11"\n')
        self.tool(
            "marked",
            '[tool]\nrequirements = [{ name = "marked", git = "https://h/x.git", marker = "sys_platform == \'linux\'" }]\n',
        )
        text = self.proposal_text("acme-cli v1\nmarked v1\nmkdocs v1\npinned v1\n")
        for name in ("acme-cli", "marked", "mkdocs", "pinned"):
            self.assertIn(f'# "{name}",  # source unknown', text)

    def test_a_credential_in_the_receipt_url_is_never_proposed(self) -> None:
        self.tool(
            "acme",
            '[tool]\nrequirements = [{ name = "acme", git = "https://user:ghp_secret@github.com/acme/cli.git" }]\n',
        )
        self.tool(
            "ssh-tool", '[tool]\nrequirements = [{ name = "ssh-tool", git = "ssh://git@github.com/acme/cli.git" }]\n'
        )
        text = self.proposal_text("acme v1\nssh-tool v1\n")
        self.assertNotIn("ghp_secret", text)
        self.assertIn('# "acme",  # source unknown', text)
        self.assertIn('"ssh-tool @ git+ssh://git@github.com/acme/cli.git"', text)  # a bare user is no secret

    def test_a_token_as_the_user_of_an_https_url_or_in_its_query_is_never_proposed(self) -> None:
        self.tool(
            "tok", '[tool]\nrequirements = [{ name = "tok", git = "https://ghp_abc123@github.com/acme/cli.git" }]\n'
        )
        self.tool("private", '[tool]\nrequirements = [{ name = "private", git = "https://h/x.git?token=s3cret" }]\n')
        self.tool("wheel", '[tool]\nrequirements = [{ name = "wheel", url = "https://host/pkg.whl?token=abc" }]\n')
        text = self.proposal_text("private v1\ntok v1\nwheel v1\n")
        self.assertNotIn("s3cret", text)
        self.assertNotIn("ghp_abc123", text)
        self.assertNotIn("token=abc", text)
        self.assertIn('# "tok",  # source unknown', text)
        self.assertIn('# "wheel",  # source unknown', text)

    def test_a_name_with_a_quote_or_newline_cannot_become_toml(self) -> None:
        self.machine({("bun", "pm", "ls", "-g"): ok('x\n├── a"b@1\n')})
        (proposal,) = packages.capture(
            self.ctx, [Item("packages", 'bun:bad",\napt = ["x', "package"), Item("packages", 'uv:q"uote', "package")]
        )
        import tomllib

        data = tomllib.loads(proposal.text)
        self.assertEqual(sorted(data), ["bun", "uv"])  # no extra key smuggled in
        self.assertEqual(data["bun"], ['bad",\napt = ["x'])
        self.assertEqual(data["uv"], [])  # the uv item has no receipt: only a comment, still valid TOML

    def test_a_receipt_value_that_is_not_a_valid_spec_is_flagged(self) -> None:
        self.tool("odd", '[tool]\nrequirements = [{ name = "odd", git = "https://h/x.git; rm -rf ~" }]\n')
        self.assertIn('# "odd",  # source unknown', self.proposal_text("odd v1\n"))


class UnitsTests(AdapterCase):
    def test_user_unit_files_and_enabled_units_merge_by_name(self) -> None:
        directory = self.home / ".config/systemd/user"
        write(directory / "foo.service", "x")
        write(directory / "foo.service.d/override.conf", "x")
        write(directory / "bar.timer", "x")
        write(directory / "default.target.wants/baz.service", "x")
        write(directory / "notes.txt", "x")
        self.machine(
            {
                ("systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"): ok(
                    "foo.service enabled enabled\nbaz.service enabled enabled\n"
                )
            }
        )
        found = {item.id: item.detail for item in units.discover(self.ctx)}
        self.assertEqual(
            found,
            {
                "unit:bar.timer": "user unit file",
                "unit:baz.service": "enabled",
                "unit:foo.service": "enabled, user unit file",
            },
        )

    def test_without_a_user_systemd_only_the_directory_is_read(self) -> None:
        write(self.home / ".config/systemd/user/foo.service", "x")
        self.machine()
        self.assertEqual(self.ids(units.discover(self.ctx)), ["unit:foo.service"])

    def test_units_the_profile_links_are_owned(self) -> None:
        write(self.profile_root / "files/.config/systemd/user/foo.service", "x")
        write(self.profile_root / "files/.config/systemd/user/bar.service.d/o.conf", "x")
        write(self.profile_root / "files/.config/other/x", "x")
        self.machine()
        self.assertEqual(
            units.owned(self.ctx),
            {
                "unit:foo.service": "files/.config/systemd/user/foo.service",
                "unit:bar.service": "files/.config/systemd/user/bar.service.d/o.conf",
            },
        )

    def test_a_unit_that_is_a_symlink_into_the_checkout_is_owned(self) -> None:
        write(self.repo / "units/x.service", "x")
        (self.home / ".config/systemd/user").mkdir(parents=True)
        os.symlink(self.repo / "units/x.service", self.home / ".config/systemd/user/x.service")
        self.machine()
        self.assertEqual(units.owned(self.ctx), {"unit:x.service": "engine checkout (symlink)"})


class ExtensionsTests(AdapterCase):
    def test_enabled_extensions_are_items_with_where_their_code_lives(self) -> None:
        backend = FakeBackend([SHELL])
        backend.set_user(SHELL.id, None, "enabled-extensions", "['mine@me', 'ubuntu-dock@ubuntu.com']")
        write(self.home / ".local/share/gnome-shell/extensions/mine@me/metadata.json", "{}")
        self.machine(backend=backend)
        self.assertEqual(
            [(item.id, item.detail) for item in extensions.discover(self.ctx)],
            [
                ("extension:mine@me", "enabled, user-installed"),
                ("extension:ubuntu-dock@ubuntu.com", "enabled, system"),
            ],
        )

    def test_a_machine_without_the_shell_schema_has_no_extensions(self) -> None:
        self.machine(backend=FakeBackend([INTERFACE]))
        self.assertEqual(list(extensions.discover(self.ctx)), [])

    def test_module_extension_uuids_are_owned_and_a_broken_one_is_skipped(self) -> None:
        self.module("dock", '[source]\nkind = "local"\npath = "ext/dock"\n')
        self.module("broken", '[source]\nkind = "local"\npath = "ext/broken"\n')
        self.module("unborn", '[source]\nkind = "submodule"\npath = "ext/unborn"\n')
        write(self.repo / "ext/dock/metadata.json", '{"uuid": "dock@me"}')
        write(self.repo / "ext/broken/metadata.json", "{not json")
        self.machine()
        self.assertEqual(extensions.owned(self.ctx), {"extension:dock@me": "module dock"})


def md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


class EtcTests(AdapterCase):
    CONFFILES = ("dpkg-query", "-W", "-f=${Conffiles}\\n")

    def lay_out(self) -> dict[tuple[str, ...], CommandResult]:
        etc_dir = self.rootfs / "etc"
        write(etc_dir / "default/grub", "GRUB_TIMEOUT=0\n")  # stock: matches its md5
        write(etc_dir / "default/useradd", "mine\n")  # a conffile somebody edited
        write(etc_dir / "default/custom", "x\n")  # no package owns it
        write(etc_dir / "systemd/system/mine.service", "x")
        write(etc_dir / "systemd/system/mine.service.d/o.conf", "x")
        write(etc_dir / "systemd/system/notes.txt", "x")
        os.makedirs(etc_dir / "systemd/system", exist_ok=True)
        os.symlink("/lib/systemd/system/ssh.service", etc_dir / "systemd/system/sshd.service")
        write(etc_dir / "apt/sources.list.d/extra.list", "x")
        conffiles = (
            f" /etc/default/grub {md5('GRUB_TIMEOUT=0' + chr(10))}\n"
            f" /etc/default/useradd {md5('original' + chr(10))}\n"
            f" /etc/gone.conf {md5('x')}\n"
            " /etc/obsolete.conf deadbeef obsolete\n"
        )
        return {self.CONFFILES: ok(conffiles)}

    def test_modified_deleted_and_local_files_are_items_stock_conffiles_are_not(self) -> None:
        self.machine(self.lay_out())
        found = {item.id: item.detail for item in etc.discover(self.ctx)}
        self.assertEqual(
            found,
            {
                "etc:/etc/apt/sources.list.d/extra.list": "local file",
                "etc:/etc/default/custom": "local file",
                "etc:/etc/default/useradd": "conffile modified: differs from the package",
                "etc:/etc/gone.conf": "conffile deleted: differs from the package",
                "etc:/etc/systemd/system/mine.service": "local file",
                "etc:/etc/systemd/system/mine.service.d": "local directory",
            },
        )

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_a_directory_that_needs_root_is_one_item_saying_so(self) -> None:
        self.machine(self.lay_out())
        locked = self.rootfs / "etc/sudoers.d"
        write(locked / "90-secret", "x")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        found = {item.id: item.detail for item in etc.discover(self.ctx)}
        self.assertEqual(found["etc:/etc/sudoers.d"], "needs root to read: sudo ls -l /etc/sudoers.d")
        self.assertNotIn("etc:/etc/sudoers.d/90-secret", found)

    def test_an_unreadable_conffile_is_counted_in_one_item(self) -> None:
        responses = self.lay_out()
        unreadable = self.rootfs / "etc/default/useradd"
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o600)
        self.machine(responses)
        if os.access(unreadable, os.R_OK):
            self.skipTest("this user reads files regardless of their mode")
        found = {item.id: item.detail for item in etc.discover(self.ctx)}
        self.assertEqual(
            found["etc:/etc/<conffiles>"],
            "1 conffile(s): needs root to read: sudo dpkg --verify",
        )

    def test_without_dpkg_every_local_entry_is_listed_and_nothing_fails(self) -> None:
        self.lay_out()
        self.machine()
        ids = self.ids(etc.discover(self.ctx))
        self.assertIn("etc:/etc/default/grub", ids)
        self.assertNotIn("etc:/etc/systemd/system/sshd.service", ids)

    def test_a_machine_without_the_directories_has_nothing(self) -> None:
        self.machine()
        self.assertEqual(list(etc.discover(self.ctx)), [])

    def test_system_toml_owns_the_locale_and_keyboard_files(self) -> None:
        write(self.profile_root / "system.toml", 'locale = "en_US.UTF-8"\n[keyboard]\nlayout = "us"\n')
        self.machine()
        self.assertEqual(
            etc.owned(self.ctx),
            {"etc:/etc/default/locale": "system.toml", "etc:/etc/default/keyboard": "system.toml"},
        )
        write(self.profile_root / "system.toml", 'timezone = "Europe/Berlin"\n')
        self.assertEqual(etc.owned(self.ctx), {})


class EveryDiscoverIsReadOnlyTests(AdapterCase):
    def test_a_scan_of_a_populated_scratch_machine_changes_nothing(self) -> None:
        write(self.home / ".config/foo/app.conf", "x")
        write(self.home / ".config/systemd/user/foo.service", "x")
        write(self.rootfs / "etc/default/custom", "x")
        self.machine(PackagesTests.RESPONSES, FakeBackend([INTERFACE, SHELL]))
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        for module in discover_adapters():
            list(module.discover(self.ctx))
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), before)
        self.assertEqual(self.process.executed, [])
        self.assertIsNotNone(self.backend_in_use)
        if self.backend_in_use is not None:
            self.assertEqual(self.backend_in_use.writes, [])


class SchemeTests(AdapterCase):
    def test_every_item_id_an_adapter_produces_starts_with_a_known_scheme_prefix(self) -> None:
        write(self.home / ".config/foo/app.conf", "x")
        write(self.home / ".config/systemd/user/foo.service", "x")
        write(self.rootfs / "etc/default/custom", "x")
        backend = FakeBackend([INTERFACE, SHELL])
        backend.set_user(INTERFACE.id, None, "gtk-theme", "'Yaru'")
        backend.set_user(SHELL.id, None, "enabled-extensions", "['a@x']")
        self.machine(PackagesTests.RESPONSES, backend)
        seen: set[str] = set()
        for module in discover_adapters():
            for item in module.discover(self.ctx):
                seen.add(item.id.split(":", 1)[0])
                self.assertTrue(item.id.startswith(ITEM_PREFIXES), item.id)
        self.assertGreaterEqual(seen, {"dconf", "file", "etc", "apt", "snap", "unit", "extension"})


class OwnedByPackages(FakeProcess):
    """``dpkg-query -S``: the paths in ``owned`` belong to a package, the others are not found (exit 1)."""

    def __init__(self, owned: Iterable[str] = ()) -> None:
        super().__init__()
        self.owned = set(owned)

    def query(self, argv, *, timeout: float = 60.0, cwd=None) -> CommandResult:
        self.queries.append(tuple(argv))
        if tuple(argv[:3]) != ("dpkg-query", "-S", "--"):
            return CommandResult(127, "", f"{argv[0]}: command not found")
        found = [f"somepkg: {path}\n" for path in argv[3:] if path in self.owned]
        return CommandResult(0 if len(found) == len(argv[3:]) else 1, "".join(found), "")


class UsrLocalTests(AdapterCase):
    """Hand-installed configuration under /usr/local and /opt (spec, *Coverage*, the ``usr-local`` adapter)."""

    ARCH = "usr/local/lib/x86_64-linux-gnu"

    def lay_out(self) -> None:
        local = self.rootfs / "usr/local"
        # config-ish places: one item per file
        write(local / "etc/tool/tool.conf", "x")
        write(local / "share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf", "x")
        write(local / "share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf.disabled", "x")
        write(local / "share/pipewire/pipewire.conf.d/20-mine.conf", "x")
        write(local / "share/dbus-1/services/org.example.Svc.service", "x")
        write(local / "share/xdg-desktop-portal/portals/mine.portal", "x")
        write(local / "lib/systemd/user/mine.service", "x")
        write(self.rootfs / self.ARCH / "wireplumber-0.5/libxdg-desktop-portal.so", "elf")
        write(self.rootfs / self.ARCH / "pipewire-0.3/libxdg-desktop-portal.so", "elf")
        # a data tree the config areas do not reach, in a directory that is listed through its files: not listed again
        write(local / "share/dbus-1/interfaces/org.example.Iface.xml", "x")
        # a hand-installed tree nothing above lists: one directory item
        write(local / "share/credentialsd/a.xml", "x")
        write(local / "share/credentialsd/b.zip", "x")
        write(local / "lib/mytool/helper.py", "x")
        os.symlink("/opt/vendor", local / "share/vendor-link")
        # plain data, libraries, headers and binaries: never listed
        write(local / "share/locale/de/LC_MESSAGES/x.mo", "x")
        write(local / "share/man/man1/x.1", "x")
        write(local / "share/icons/hicolor/48x48/apps/x.png", "x")
        write(self.rootfs / self.ARCH / "libdex-1.so", "elf")
        write(self.rootfs / self.ARCH / "pkgconfig/libdex-1.pc", "x")
        write(local / "include/libdex-1/dex.h", "x")
        write(local / "bin/tool", "x")
        write(local / "sbin/tool-admin", "x")
        write(local / "libexec/helper", "x")
        # /opt: one item per top-level entry
        write(self.rootfs / "opt/vendor/bin/app", "x")
        write(self.rootfs / "opt/vendor/lib/libapp.so", "x")
        write(self.rootfs / "opt/from-a-package/bin/pkgapp", "x")

    def discover(self, owned: Iterable[str] = ()) -> dict[str, Item]:
        self.lay_out()
        self.process = OwnedByPackages(owned)
        manager = use(System(self.process, self.home, self.rootfs, None))
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return {item.id: item for item in usr_local.discover(self.ctx)}

    def test_config_files_and_unlisted_trees_are_items_and_data_libraries_and_binaries_are_not(self) -> None:
        found = self.discover(owned=["/opt/from-a-package"])
        self.assertEqual(
            sorted(found),
            [
                "etc:/opt/vendor",
                "etc:/usr/local/etc/tool/tool.conf",
                "etc:/usr/local/lib/mytool",
                "etc:/usr/local/lib/systemd/user/mine.service",
                "etc:/usr/local/lib/x86_64-linux-gnu/pipewire-0.3/libxdg-desktop-portal.so",
                "etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so",
                "etc:/usr/local/share/credentialsd",
                "etc:/usr/local/share/dbus-1/services/org.example.Svc.service",
                "etc:/usr/local/share/pipewire/pipewire.conf.d/20-mine.conf",
                "etc:/usr/local/share/vendor-link",
                "etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf",
                "etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf.disabled",
                "etc:/usr/local/share/xdg-desktop-portal/portals/mine.portal",
            ],
        )

    def test_a_config_next_to_a_listed_drop_in_is_not_hidden_by_it(self) -> None:
        """WirePlumber reads share/wireplumber/wireplumber.conf from XDG_DATA_DIRS before /usr/share: it is a finding too."""
        local = self.rootfs / "usr/local"
        write(local / "share/wireplumber/wireplumber.conf", "x")
        write(local / "share/wireplumber/scripts/main.lua", "x")
        write(local / "share/xdg-desktop-portal/gnome-portals.conf", "x")
        found = self.discover()
        self.assertEqual(found["etc:/usr/local/share/wireplumber/wireplumber.conf"].kind, "file")
        self.assertEqual(found["etc:/usr/local/share/wireplumber/scripts"].detail, "hand-installed directory, 1 files")
        self.assertIn("etc:/usr/local/share/xdg-desktop-portal/gnome-portals.conf", found)
        # what the areas already listed is not listed a second time, and plain data under a listed tree stays out
        self.assertNotIn("etc:/usr/local/share/wireplumber", found)
        self.assertNotIn("etc:/usr/local/share/wireplumber/wireplumber.conf.d", found)
        self.assertNotIn("etc:/usr/local/share/dbus-1/interfaces", found)

    def test_a_systemd_drop_in_directory_next_to_a_listed_unit_is_an_item(self) -> None:
        local = self.rootfs / "usr/local"
        write(local / "lib/systemd/user/mine.service.d/override.conf", "x")
        os.symlink("../mine.service", local / "lib/systemd/user/default.target.wants-mine")
        found = self.discover()
        self.assertEqual(
            found["etc:/usr/local/lib/systemd/user/mine.service.d"].detail, "hand-installed directory, 1 files"
        )
        self.assertEqual(found["etc:/usr/local/lib/systemd/user/default.target.wants-mine"].kind, "link")
        self.assertIn("etc:/usr/local/lib/systemd/user/mine.service", found)
        self.assertNotIn("etc:/usr/local/lib/systemd", found)
        self.assertNotIn("etc:/usr/local/lib/systemd/user", found)

    def test_libraries_installed_straight_into_usr_local_lib_are_not_listed(self) -> None:
        """An autotools or CMake build with the default prefix puts libfoo.so* there, not in a triplet directory."""
        lib = self.rootfs / "usr/local/lib"
        for name in ("libfoo.so.1.2.3", "libfoo.a", "libfoo.la"):
            write(lib / name, "elf")
        os.symlink("libfoo.so.1.2.3", lib / "libfoo.so.1")
        os.symlink("libfoo.so.1.2.3", lib / "libfoo.so")
        found = self.discover()
        self.assertEqual([item for item in found if "libfoo" in item], [])
        self.assertIn("etc:/usr/local/lib/mytool", found)

    def test_a_directory_link_in_an_area_is_not_followed(self) -> None:
        """share/foo -> a packaged tree: the link is the finding, never the packaged files behind it."""
        local = self.rootfs / "usr/local"
        write(self.rootfs / "srv/foo-data/foo.d/10-packaged.conf", "x")
        (local / "share").mkdir(parents=True, exist_ok=True)
        os.symlink("../../../srv/foo-data", local / "share/foo")
        found = self.discover()
        self.assertEqual(found["etc:/usr/local/share/foo"].kind, "link")
        self.assertEqual([item for item in found if item.startswith("etc:/usr/local/share/foo/")], [])

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_an_area_directory_this_user_cannot_list_is_one_item_saying_so(self) -> None:
        """A hand-built sudo leaves /usr/local/etc/sudoers.d at 0750: the adapter contract says an unreadable area is an item."""
        locked = self.rootfs / "usr/local/etc/sudoers.d"
        write(locked / "50-me", "x")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        found = self.discover()
        self.assertEqual(
            found["etc:/usr/local/etc/sudoers.d"].detail, "needs root to read: sudo ls -lR /usr/local/etc/sudoers.d"
        )

    def test_a_link_that_loops_back_to_itself_is_a_link_item_and_does_not_stop_the_scan(self) -> None:
        local = self.rootfs / "usr/local"
        (local / "lib").mkdir(parents=True, exist_ok=True)
        os.symlink("vendor", local / "lib/vendor")
        found = self.discover()
        self.assertEqual(found["etc:/usr/local/lib/vendor"].kind, "link")

    def test_a_link_standing_for_a_config_directory_is_an_item(self) -> None:
        """/usr/local/etc -> a directory elsewhere: the link is the finding, whatever pattern step it sits on."""
        write(self.rootfs / "srv/config/tool.conf", "x")
        (self.rootfs / "usr/local").mkdir(parents=True)
        os.symlink("../../srv/config", self.rootfs / "usr/local/etc")
        self.machine()
        found = {item.id: item.kind for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local/etc": "link"})

    def test_a_tree_holding_only_directory_links_is_not_empty(self) -> None:
        os.makedirs(self.rootfs / "etc/vendor-config")
        os.makedirs(self.rootfs / "opt/linked")
        os.symlink("../../etc/vendor-config", self.rootfs / "opt/linked/config")
        self.machine()
        found = {item.id: item.detail for item in usr_local.discover(self.ctx)}
        self.assertEqual(found["etc:/opt/linked"], "hand-installed directory, 1 files")

    def test_a_linked_share_is_reported_as_the_link_and_not_followed(self) -> None:
        local = self.rootfs / "usr/local"
        write(self.rootfs / "srv/shared/credentialsd/a.xml", "x")
        local.mkdir(parents=True)
        os.symlink("../../srv/shared", local / "share")
        self.machine()
        found = {item.id: item.kind for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local/share": "link"})

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_an_opt_this_user_cannot_list_is_one_item_saying_so(self) -> None:
        write(self.rootfs / "opt/vendor/x", "x")
        (self.rootfs / "opt").chmod(0o000)
        self.addCleanup((self.rootfs / "opt").chmod, 0o700)
        self.machine()
        found = {item.id: item.detail for item in usr_local.discover(self.ctx)}
        self.assertEqual(found["etc:/opt"], "needs root to read: sudo ls -lR /opt")

    def test_a_file_name_that_is_not_utf8_is_listed_and_never_sent_to_dpkg(self) -> None:
        odd = os.fsdecode(b"odd-\xff.conf")
        write(self.rootfs / "usr/local/etc" / odd, "x")
        write(self.rootfs / "usr/local/etc/plain.conf", "x")
        found = self.discover()
        self.assertIn(f"etc:/usr/local/etc/{odd}", found)
        (query,) = self.process.queries
        self.assertIn("/usr/local/etc/plain.conf", query)
        self.assertEqual([path for path in query if "\udcff" in path], [])

    def test_a_linked_multiarch_directory_is_an_item(self) -> None:
        write(self.rootfs / "srv/arch/wireplumber-0.5/libxdg-desktop-portal.so", "elf")
        (self.rootfs / "usr/local/lib").mkdir(parents=True)
        os.symlink("../../../srv/arch", self.rootfs / self.ARCH)
        self.machine()
        found = {item.id: item.kind for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local/lib/x86_64-linux-gnu": "link"})

    def test_a_linked_usr_local_is_reported_as_the_link_and_not_followed(self) -> None:
        write(self.rootfs / "srv/local/share/applications/mine.desktop", "x")
        (self.rootfs / "usr").mkdir(exist_ok=True)
        os.symlink("../srv/local", self.rootfs / "usr/local")
        self.machine()
        found = {item.id: item.kind for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local": "link"})

    def test_details_say_what_each_item_is(self) -> None:
        found = self.discover()
        self.assertEqual(found["etc:/usr/local/etc/tool/tool.conf"].detail, "hand-installed file")
        self.assertEqual(found["etc:/usr/local/etc/tool/tool.conf"].kind, "file")
        self.assertEqual(found["etc:/opt/vendor"].detail, "hand-installed directory, 2 files")
        self.assertEqual(found["etc:/opt/vendor"].kind, "dir")
        self.assertEqual(found["etc:/usr/local/share/credentialsd"].detail, "hand-installed directory, 2 files")
        self.assertEqual(found["etc:/usr/local/share/vendor-link"].detail, "hand-installed link -> /opt/vendor")

    def test_what_a_package_owns_is_not_hand_installed(self) -> None:
        found = self.discover(owned=["/usr/local/etc/tool/tool.conf", "/opt/vendor"])
        self.assertNotIn("etc:/usr/local/etc/tool/tool.conf", found)
        self.assertNotIn("etc:/opt/vendor", found)

    def test_candidates_beyond_the_argument_budget_are_asked_in_batches_and_every_answer_counts(self) -> None:
        etc = self.rootfs / "usr/local/etc"
        names = [f"{index:04d}-{'x' * 200}.conf" for index in range(700)]
        for name in names:
            write(etc / name, "x")
        found = self.discover(owned=[f"/usr/local/etc/{names[0]}", f"/usr/local/etc/{names[-1]}"])
        self.assertGreater(len(self.process.queries), 1)
        for query in self.process.queries:
            self.assertLessEqual(sum(len(path) + 1 for path in query[3:]), 120_000)
        self.assertNotIn(f"etc:/usr/local/etc/{names[0]}", found)
        self.assertNotIn(f"etc:/usr/local/etc/{names[-1]}", found)
        self.assertIn(f"etc:/usr/local/etc/{names[1]}", found)

    def test_a_link_that_vanishes_during_the_scan_is_not_an_item_and_does_not_stop_it(self) -> None:
        write(self.rootfs / "opt/vendor/x", "x")
        os.symlink("/srv/x", self.rootfs / "opt/flaky")
        self.machine()
        real = os.readlink

        def racing(path, *args, **kwargs):
            if str(path).endswith("opt/flaky"):
                raise FileNotFoundError(path)
            return real(path, *args, **kwargs)

        with mock.patch.object(usr_local.os, "readlink", racing):
            found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/opt/vendor"})

    def test_config_beside_the_plugins_in_a_multiarch_directory_is_listed(self) -> None:
        arch = self.rootfs / self.ARCH
        write(arch / "wireplumber-0.5/libknown.so", "elf")
        write(arch / "wireplumber-0.5/scripts/local.lua", "x")
        write(arch / "libdex-1.so", "elf")
        write(arch / "pkgconfig/libdex-1.pc", "x")
        self.machine()
        found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertEqual(
            found,
            {
                "etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libknown.so",
                "etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/scripts",
            },
        )

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_an_unreadable_area_is_one_item_and_not_also_its_parent(self) -> None:
        services = self.rootfs / "usr/local/share/dbus-1/services"
        write(services / "x.service", "x")
        services.chmod(0o000)
        self.addCleanup(services.chmod, 0o700)
        self.machine()
        found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local/share/dbus-1/services"})

    def test_a_file_name_that_differs_from_a_packaged_one_by_whitespace_is_still_hand_installed(self) -> None:
        write(self.rootfs / "usr/local/etc/test.conf", "x")
        write(self.rootfs / "usr/local/etc/test.conf ", "x")
        found = self.discover(owned=["/usr/local/etc/test.conf "])
        self.assertIn("etc:/usr/local/etc/test.conf", found)
        self.assertNotIn("etc:/usr/local/etc/test.conf ", found)

    def test_very_deep_trees_do_not_hit_the_recursion_limit(self) -> None:
        depth = 300  # one file descriptor per level while the scratch tree is removed: stay below `ulimit -n`
        deep = "/".join(["d"] * depth)
        for tree, name in (("etc", "x.conf"), ("share/ca-certificates", "x.crt")):
            level = self.rootfs / "usr/local" / tree
            level.mkdir(parents=True)
            for _ in range(depth):
                level /= "d"
                level.mkdir()
            (level / name).write_text("x", encoding="utf-8")
        self.machine()
        self.addCleanup(sys.setrecursionlimit, sys.getrecursionlimit())
        sys.setrecursionlimit(len(inspect.stack(0)) + 150)  # a walk that recurses once per level needs 300+
        found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertIn(f"etc:/usr/local/etc/{deep}/x.conf", found)
        self.assertIn(f"etc:/usr/local/share/ca-certificates/{deep}/x.crt", found)

    def test_a_file_that_vanishes_during_the_package_query_is_not_an_item(self) -> None:
        gone = self.rootfs / "usr/local/etc/gone.conf"
        write(gone, "x")
        write(self.rootfs / "usr/local/etc/stays.conf", "x")
        self.machine()

        def removing(_paths):
            gone.unlink()
            return set()

        with mock.patch.object(usr_local, "_owned_by_packages", removing):
            found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local/etc/stays.conf"})

    @unittest.skipIf(os.geteuid() == 0, "root can search every directory")
    def test_a_directory_that_can_be_listed_but_not_searched_is_a_needs_root_item(self) -> None:
        locked = self.rootfs / "usr/local/etc/locked"
        write(locked / "tool.conf", "x")
        locked.chmod(0o400)
        self.addCleanup(locked.chmod, 0o700)
        self.machine()
        found = {item.id: item.detail for item in usr_local.discover(self.ctx)}
        self.assertEqual(
            found["etc:/usr/local/etc/locked/tool.conf"],
            "needs root to read: sudo ls -ld /usr/local/etc/locked/tool.conf",
        )

    def test_a_parent_replaced_by_a_file_during_the_package_query_does_not_stop_the_scan(self) -> None:
        write(self.rootfs / "usr/local/etc/tool/tool.conf", "x")
        write(self.rootfs / "usr/local/etc/stays.conf", "x")
        self.machine()

        def replacing(_paths):
            (self.rootfs / "usr/local/etc/tool/tool.conf").unlink()
            (self.rootfs / "usr/local/etc/tool").rmdir()
            write(self.rootfs / "usr/local/etc/tool", "now a file")
            return set()

        with mock.patch.object(usr_local, "_owned_by_packages", replacing):
            found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertIn("etc:/usr/local/etc/stays.conf", found)

    def test_data_trees_are_not_findings_below_lib_either(self) -> None:
        write(self.rootfs / "usr/local/lib/locale/de/LC_MESSAGES/tool.mo", "x")
        write(self.rootfs / "usr/local/share/locale/de/LC_MESSAGES/tool.mo", "x")
        self.machine()
        self.assertEqual(list(usr_local.discover(self.ctx)), [])

    @unittest.skipIf(os.geteuid() == 0, "root can search every directory")
    def test_pathlib_probes_that_raise_on_a_permission_error_do_not_stop_the_scan(self) -> None:
        """Python 3.12 and 3.13 let ``Path.is_dir`` and ``Path.is_symlink`` raise PermissionError (3.14 does not)."""
        locked = self.rootfs / "usr/local/etc/locked"
        write(locked / "tool.conf", "x")
        locked.chmod(0o400)
        self.addCleanup(locked.chmod, 0o700)
        self.machine()

        def raising(self, *args, **kwargs):
            if "locked" in self.parts and self.name == "tool.conf":
                raise PermissionError(13, "Permission denied", str(self))
            return False

        with mock.patch.object(Path, "is_dir", raising), mock.patch.object(Path, "is_symlink", raising):
            found = {item.id: item.detail for item in usr_local.discover(self.ctx)}
        self.assertEqual(
            found["etc:/usr/local/etc/locked/tool.conf"],
            "needs root to read: sudo ls -ld /usr/local/etc/locked/tool.conf",
        )

    def test_a_local_diversion_is_not_package_ownership(self) -> None:
        write(self.rootfs / "usr/local/etc/tool.conf", "x")
        self.machine(
            {
                (
                    "dpkg-query",
                    "-S",
                    "--",
                    "/usr/local/etc/tool.conf",
                ): CommandResult(
                    1,
                    "local diversion from: /usr/local/etc/tool.conf\nlocal diversion to: /usr/local/etc/tool.conf.d\n",
                    "",
                )
            }
        )
        found = {item.id for item in usr_local.discover(self.ctx)}
        self.assertEqual(found, {"etc:/usr/local/etc/tool.conf"})

    def test_one_dpkg_query_asks_about_every_candidate(self) -> None:
        self.discover()
        (query,) = self.process.queries
        self.assertEqual(query[:3], ("dpkg-query", "-S", "--"))
        self.assertIn("/opt/vendor", query)
        self.assertIn("/usr/local/etc/tool/tool.conf", query)
        self.assertEqual(list(query[3:]), sorted(query[3:]))

    def test_without_dpkg_everything_found_is_listed_and_nothing_fails(self) -> None:
        self.lay_out()
        self.machine()
        ids = self.ids(usr_local.discover(self.ctx))
        self.assertIn("etc:/opt/vendor", ids)
        self.assertIn("etc:/opt/from-a-package", ids)

    def test_a_machine_without_either_tree_has_nothing_and_asks_dpkg_nothing(self) -> None:
        self.machine()
        self.assertEqual(list(usr_local.discover(self.ctx)), [])
        self.assertEqual(self.process.queries, [])

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_a_directory_this_user_cannot_list_is_one_item_saying_so(self) -> None:
        locked = self.rootfs / "opt/locked"
        write(locked / "x", "x")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        self.machine()
        found = {item.id: item.detail for item in usr_local.discover(self.ctx)}
        self.assertEqual(found["etc:/opt/locked"], "needs root to read: sudo ls -lR /opt/locked")

    @unittest.skipIf(os.geteuid() == 0, "root can list every directory")
    def test_a_package_directory_this_user_cannot_list_is_not_reported_as_unreadable(self) -> None:
        locked = self.rootfs / "opt/pkg-locked"
        write(locked / "x", "x")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        self.process = OwnedByPackages(["/opt/pkg-locked"])
        manager = use(System(self.process, self.home, self.rootfs, None))
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.assertNotIn("etc:/opt/pkg-locked", self.ids(usr_local.discover(self.ctx)))

    def scan_with_modules(self, *modules: str) -> dict[str, Finding]:
        from ubuntu_tweaks.adapters import scan
        from ubuntu_tweaks.context import REPO_ROOT
        from ubuntu_tweaks.coverage_rules import load_rules

        write(self.rootfs / "usr/local/lib/memguard/memguard_swap.py", "x")
        write(self.rootfs / "usr/local/share/ubuntu-tweaks/apfs-fuse.commit", "x")
        write(self.rootfs / "usr/local/share/glib-2.0/schemas/gschemas.compiled", "x")
        write(self.rootfs / "usr/local/share/glib-2.0/schemas/org.example.gschema.xml", "x")
        for name in modules:
            write(
                self.repo / "modules" / name / "module.toml",
                (REPO_ROOT / "modules" / name / "module.toml").read_text(encoding="utf-8"),
            )
        self.machine(backend=FakeBackend([INTERFACE]))
        return {f.item.id: f for f in scan(self.ctx, load_rules(self.ctx)) if f.item.adapter == "usr-local"}

    def test_the_modules_that_install_there_claim_their_files(self) -> None:
        found = self.scan_with_modules("memguard", "disks")
        self.assertEqual(found["etc:/usr/local/lib/memguard"].coverage, Coverage.TRACKED)
        self.assertEqual(found["etc:/usr/local/lib/memguard"].owner, "memguard")
        self.assertEqual(found["etc:/usr/local/share/ubuntu-tweaks"].coverage, Coverage.TRACKED)
        self.assertEqual(found["etc:/usr/local/share/ubuntu-tweaks"].owner, "disks")

    def test_without_those_modules_the_same_entries_are_untracked(self) -> None:
        found = self.scan_with_modules()
        self.assertEqual(found["etc:/usr/local/lib/memguard"].coverage, Coverage.UNTRACKED)
        self.assertEqual(found["etc:/usr/local/share/ubuntu-tweaks"].coverage, Coverage.UNTRACKED)

    def test_the_compiled_schema_cache_is_excluded_and_the_schema_source_is_not(self) -> None:
        found = self.scan_with_modules()
        cache = found["etc:/usr/local/share/glib-2.0/schemas/gschemas.compiled"]
        self.assertEqual(cache.coverage, Coverage.EXCLUDED)
        self.assertIn("glib-compile-schemas", cache.reason)
        source = found["etc:/usr/local/share/glib-2.0/schemas/org.example.gschema.xml"]
        self.assertEqual(source.coverage, Coverage.UNTRACKED)

    def test_a_scan_with_the_wireplumber_module_claims_its_files_and_leaves_the_rest_untracked(self) -> None:
        from ubuntu_tweaks.adapters import scan
        from ubuntu_tweaks.context import REPO_ROOT
        from ubuntu_tweaks.coverage_rules import load_rules

        self.lay_out()
        write(
            self.repo / "modules/wireplumber-portal-conf/module.toml",
            (REPO_ROOT / "modules/wireplumber-portal-conf/module.toml").read_text(encoding="utf-8"),
        )
        self.machine(backend=FakeBackend([INTERFACE]))
        found = {f.item.id: f for f in scan(self.ctx, load_rules(self.ctx)) if f.item.adapter == "usr-local"}
        tracked = {id_ for id_, finding in found.items() if finding.coverage is Coverage.TRACKED}
        self.assertEqual(
            tracked,
            {
                "etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf",
                "etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf.disabled",
                "etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so",
                "etc:/usr/local/lib/x86_64-linux-gnu/pipewire-0.3/libxdg-desktop-portal.so",
            },
        )
        untracked = {id_ for id_, finding in found.items() if finding.coverage is Coverage.UNTRACKED}
        self.assertIn("etc:/usr/local/etc/tool/tool.conf", untracked)
        self.assertIn("etc:/opt/vendor", untracked)

    def test_a_profile_rule_can_exclude_an_entry_with_a_reason(self) -> None:
        from ubuntu_tweaks.adapters import scan
        from ubuntu_tweaks.coverage_rules import load_rules

        self.lay_out()
        write(
            self.profile_root / "coverage.toml",
            '[[coverage]]\nmatch = ["etc:/opt/vendor"]\nclass = "excluded"\nreason = "a vendor tarball, not configuration"\n',
        )
        self.machine(backend=FakeBackend([INTERFACE]))
        found = {f.item.id: f for f in scan(self.ctx, load_rules(self.ctx)) if f.item.adapter == "usr-local"}
        self.assertEqual(found["etc:/opt/vendor"].coverage, Coverage.EXCLUDED)

    def test_discovery_changes_nothing(self) -> None:
        self.lay_out()
        self.machine()
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        list(usr_local.discover(self.ctx))
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), before)


class ItemTests(unittest.TestCase):
    def test_an_item_is_hashable_and_compares_by_value(self) -> None:
        self.assertEqual(Item("a", "a:b", "k"), Item("a", "a:b", "k", ""))
        self.assertEqual(len({Item("a", "a:b", "k"), Item("a", "a:b", "k")}), 1)


if __name__ == "__main__":
    unittest.main()

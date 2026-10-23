"""The coverage adapters on scratch machines: FakeProcess, a FakeBackend and temporary HOME / root directories.

Nothing here reads the real $HOME or /etc, runs a real tool or touches the live dconf database.
"""

import hashlib
import os
import unittest
from collections.abc import Iterable, Mapping
from pathlib import Path

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from tests.util import TempDirCase, write
from ubuntu_tweaks.adapters import discover_adapters
from ubuntu_tweaks.adapters import dconf, etc, extensions, files, packages, units
from ubuntu_tweaks.adapters._system import System, system, use
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import ITEM_PREFIXES, Coverage, Finding, Item, Rules
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
        self.assertEqual(names, ["dconf", "disk", "etc", "extensions", "files", "packages", "units"])

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
        ("npm", "ls", "-g", "--depth=0", "--parseable"): ok(
            "/usr/lib\n/usr/lib/node_modules/npm\n/usr/lib/node_modules/@scope/tool\n"
        ),
        ("bun", "pm", "ls", "-g"): ok(
            "/home/u/.bun/install/global node_modules (2)\n├── prettier@3.0.0\n└── @a/b@1.2.3\n"
        ),
        ("cargo", "install", "--list"): ok("ripgrep v14.1.0:\n    rg\nfd-find v10.0.0:\n    fd\n"),
    }

    def test_each_manager_is_listed_and_ids_carry_the_manager(self) -> None:
        self.machine(self.RESPONSES)
        self.assertEqual(
            self.ids(packages.discover(self.ctx)),
            [
                "apt:curl",
                "apt:git",
                "snap:core22",
                "snap:firefox",
                "flatpak:org.gimp.GIMP",
                "brew:jq",
                "pipx:ruff",
                "uv:httpie",
                "npm:@scope/tool",
                "npm:npm",
                "bun:@a/b",
                "bun:prettier",
                "cargo:fd-find",
                "cargo:ripgrep",
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

    def test_capture_writes_packages_toml_for_the_managers_it_has_a_key_for(self) -> None:
        self.machine(self.RESPONSES)
        (proposal,) = packages.capture(self.ctx, list(packages.discover(self.ctx)))
        self.assertEqual(proposal.title, "packages.toml")
        self.assertIn('apt = [\n  "curl",\n  "git",\n]\n', proposal.text)
        self.assertIn('brew = [\n  "jq",\n]\n', proposal.text)
        self.assertNotIn("pipx", proposal.text)
        self.assertEqual(len(proposal.items), 6)  # 2 apt, 2 snap, 1 flatpak, 1 brew: not pipx, uv, npm, bun, cargo


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


class ItemTests(unittest.TestCase):
    def test_an_item_is_hashable_and_compares_by_value(self) -> None:
        self.assertEqual(Item("a", "a:b", "k"), Item("a", "a:b", "k", ""))
        self.assertEqual(len({Item("a", "a:b", "k"), Item("a", "a:b", "k")}), 1)


if __name__ == "__main__":
    unittest.main()

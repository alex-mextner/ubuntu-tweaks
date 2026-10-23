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
from ubuntu_tweaks.coverage import Item
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
        write(self.profile_root / "packages.toml", 'apt = ["curl"]\nsnap = ["firefox"]\nbrew = ["jq"]\n')
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
        self.assertEqual(found["etc:/etc/sudoers.d"], "needs root to read")
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
        self.assertEqual(found["etc:/etc/<conffiles>"], "1 conffile(s): needs root to read")

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


class ItemTests(unittest.TestCase):
    def test_an_item_is_hashable_and_compares_by_value(self) -> None:
        self.assertEqual(Item("a", "a:b", "k"), Item("a", "a:b", "k", ""))
        self.assertEqual(len({Item("a", "a:b", "k"), Item("a", "a:b", "k")}), 1)


if __name__ == "__main__":
    unittest.main()

"""Profile resolution, machine detection and the module < shared < overlay merge."""

from pathlib import PurePosixPath
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.identity import Location, RepoRef, Source
from ubuntu_tweaks.profile import PROFILE_ENV, Keyboard, Packages, Profile, SystemConfig, TomlValue, merge_layers


class ProfileCase(TempDirCase):
    def profile(self, machine: str = "home") -> Profile:
        root = self.tmp / "profile"
        root.mkdir(exist_ok=True)
        return Profile(root, machine)


class ResolveTests(TempDirCase):
    def test_location_precedence_flag_then_env_then_default(self) -> None:
        flag, from_env, home = self.tmp / "flag", self.tmp / "env", self.tmp / "home"
        flag.mkdir()
        from_env.mkdir()
        hostname = write(self.tmp / "hostname", "box\n")
        env = {PROFILE_ENV: str(from_env)}

        self.assertEqual(Profile.resolve(str(flag), env=env, hostname_file=hostname, home=home).root, flag)
        self.assertEqual(Profile.resolve(None, env=env, hostname_file=hostname, home=home).root, from_env)
        self.assertEqual(Profile.resolve(None, env={}, hostname_file=hostname, home=home).root, home / ".files/ubuntu")

    def test_the_default_location_is_what_the_locator_found_and_it_keeps_the_finding(self) -> None:
        found = Location(self.tmp / "dotfiles/ubuntu", self.tmp / "dotfiles", RepoRef("alice/dotfiles"), Source.GH)
        hostname = write(self.tmp / "hostname", "box\n")
        profile = Profile.resolve(None, env={}, hostname_file=hostname, home=self.tmp, locate=lambda: found)
        self.assertEqual((profile.root, profile.location), (self.tmp / "dotfiles/ubuntu", found))

    def test_a_flag_or_the_environment_never_asks_the_locator(self) -> None:
        (self.tmp / "p").mkdir()
        hostname = write(self.tmp / "hostname", "box\n")

        def boom() -> Location:
            raise AssertionError("the locator must not run")

        for profile, env in ((str(self.tmp / "p"), {}), (None, {PROFILE_ENV: str(self.tmp / "p")})):
            self.assertEqual(
                Profile.resolve(profile, env=env, hostname_file=hostname, home=self.tmp, locate=boom).root,
                self.tmp / "p",
            )

    def test_missing_default_profile_is_allowed(self) -> None:
        profile = Profile.resolve(None, env={}, hostname_file=write(self.tmp / "h", "box"), home=self.tmp / "nohome")
        self.assertFalse(profile.exists)

    def test_empty_profile_flag_is_a_config_error_not_the_current_directory(self) -> None:
        for empty in ("", "  "):
            with self.subTest(value=empty), self.assertRaisesRegex(ConfigError, "empty value"):
                Profile.resolve(empty, env={}, home=self.tmp)

    def test_explicit_missing_profile_is_a_config_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            Profile.resolve(str(self.tmp / "nope"), env={}, home=self.tmp)
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            Profile.resolve(None, env={PROFILE_ENV: str(self.tmp / "nope")}, home=self.tmp)

    def test_machine_comes_from_hostname_file_or_flag(self) -> None:
        hostname = write(self.tmp / "hostname", "  home\n")
        detected = Profile.resolve(None, env={}, hostname_file=hostname, home=self.tmp)
        self.assertEqual((detected.machine, detected.machine_source), ("home", "hostname"))
        forced = Profile.resolve(None, "laptop", env={}, hostname_file=hostname, home=self.tmp)
        self.assertEqual((forced.machine, forced.machine_source), ("laptop", "flag"))

    def test_machine_falls_back_to_socket_hostname_when_file_is_missing(self) -> None:
        with mock.patch("socket.gethostname", return_value="fallback"):
            profile = Profile.resolve(None, env={}, hostname_file=self.tmp / "absent", home=self.tmp)
        self.assertEqual(profile.machine, "fallback")

    def test_machine_name_must_be_one_path_component(self) -> None:
        for bad in ("", ".", "..", "a/b", "../x"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                Profile.resolve(None, bad, env={}, home=self.tmp)


class ConfigMergeTests(ProfileCase):
    def test_precedence_module_then_shared_then_overlay(self) -> None:
        profile = self.profile()
        write(profile.profile_toml, "[modules.memguard]\nenabled = true\n")
        write(profile.machine_toml, "[modules.memguard]\nenabled = false\n")
        defaults = {"modules": {"memguard": {"enabled": True}, "hyperdock": {"enabled": True}}}

        config = profile.load_config(defaults)

        self.assertFalse(config.module_enabled("memguard"))
        self.assertEqual(config.origin("modules", "memguard", "enabled"), "overlay")
        self.assertTrue(config.module_enabled("hyperdock"))
        self.assertEqual(config.origin("modules", "hyperdock", "enabled"), "module")

    def test_tables_merge_and_scalars_replace(self) -> None:
        profile = self.profile()
        write(profile.profile_toml, '[modules.hyperdock.settings]\nicon-size = "0.2"\nkeep = "shared"\n')
        write(profile.machine_toml, '[modules.hyperdock.settings]\nicon-size = "0.15"\n')

        config = profile.load_config()

        self.assertEqual(config.table("modules", "hyperdock", "settings"), {"icon-size": "0.15", "keep": "shared"})
        self.assertEqual(config.origin("modules", "hyperdock", "settings", "icon-size"), "overlay")
        self.assertEqual(config.origin("modules", "hyperdock", "settings", "keep"), "shared")

    def test_lists_replace_in_profile_toml(self) -> None:
        profile = self.profile()
        write(profile.profile_toml, '[capture]\nexclude = ["/a", "/b"]\n')
        write(profile.machine_toml, '[capture]\nexclude = ["/c"]\n')
        self.assertEqual(profile.load_config().get("capture", "exclude"), ["/c"])

    def test_absent_profile_gives_module_defaults_only(self) -> None:
        profile = Profile(self.tmp / "absent", "home")
        config = profile.load_config({"modules": {"x": {"enabled": False}}})
        self.assertFalse(config.module_enabled("x"))

    def test_module_enabled_defaults_to_true_and_rejects_non_bool(self) -> None:
        profile = self.profile()
        self.assertTrue(profile.load_config().module_enabled("unknown"))
        write(profile.profile_toml, '[modules.x]\nenabled = "yes"\n')
        with self.assertRaisesRegex(ConfigError, "enabled must be true or false"):
            profile.load_config().module_enabled("x")

    def test_a_scalar_where_a_module_table_belongs_is_not_silently_enabled(self) -> None:
        profile = self.profile()
        write(profile.profile_toml, "[modules]\nmemguard = false\n")
        with self.assertRaisesRegex(ConfigError, "modules.memguard must be a table"):
            profile.load_config({"modules": {"memguard": {"enabled": False}}}).module_enabled("memguard")

    def test_a_malformed_shared_modules_entry_is_not_masked_by_an_overlay_table(self) -> None:
        profile = self.profile()
        write(profile.profile_toml, "[modules]\nmemguard = false\n")
        write(profile.machine_toml, '[modules.memguard.settings]\nfoo = "x"\n')
        with self.assertRaisesRegex(ConfigError, "profile.toml: modules.memguard must be a table"):
            profile.load_config({"modules": {"memguard": {"enabled": False}}})

    def test_modules_itself_must_be_a_table(self) -> None:
        profile = self.profile()
        write(profile.machine_toml, "modules = 3\n")
        with self.assertRaisesRegex(ConfigError, "machine.toml: modules must be a table"):
            profile.load_config()

    def test_invalid_toml_names_the_file(self) -> None:
        profile = self.profile()
        write(profile.profile_toml, "[broken\n")
        with self.assertRaisesRegex(ConfigError, "profile.toml: invalid TOML"):
            profile.load_config()

    def test_merge_layers_does_not_alias_inputs(self) -> None:
        items: list[TomlValue] = [1]
        config = merge_layers([("shared", {"a": {"list": items}})])
        items.append(2)
        self.assertEqual(config.get("a", "list"), [1])


class PackagesAndSystemTests(ProfileCase):
    def test_packages_union_lists_shared_order_first(self) -> None:
        profile = self.profile()
        write(profile.packages_toml, 'apt = ["tmux", "ripgrep"]\nsnap = ["firefox"]\n')
        write(profile.overlay_packages_toml, 'apt = ["ripgrep", "htop"]\nflatpak = ["org.gnome.Extensions"]\n')

        self.assertEqual(
            profile.load_packages(),
            Packages(apt=("tmux", "ripgrep", "htop"), snap=("firefox",), flatpak=("org.gnome.Extensions",)),
        )

    def test_no_files_means_empty_packages_and_system(self) -> None:
        profile = self.profile()
        self.assertEqual(profile.load_packages(), Packages())
        self.assertEqual(profile.load_system(), SystemConfig())

    def test_system_scalars_replace_and_keyboard_merges_per_key(self) -> None:
        profile = self.profile()
        write(
            profile.system_toml, 'timezone = "Europe/Belgrade"\nlocale = "en_US.UTF-8"\n[keyboard]\nlayout = "us,ru"\n'
        )
        write(profile.overlay_system_toml, 'timezone = "UTC"\n[keyboard]\noptions = "grp_led:scroll"\n')

        self.assertEqual(
            profile.load_system(),
            SystemConfig("UTC", "en_US.UTF-8", Keyboard(layout="us,ru", options="grp_led:scroll")),
        )

    def test_unknown_or_mistyped_keys_are_config_errors(self) -> None:
        profile = self.profile()
        write(profile.packages_toml, 'pip = ["x"]\n')
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            profile.load_packages()
        write(profile.packages_toml, 'apt = "tmux"\n')
        with self.assertRaisesRegex(ConfigError, "list of non-empty strings"):
            profile.load_packages()
        write(profile.system_toml, "timezone = 3\n")
        with self.assertRaisesRegex(ConfigError, "timezone must be a string"):
            profile.load_system()

    def test_a_typo_in_one_layer_is_not_masked_by_the_other(self) -> None:
        profile = self.profile()
        write(profile.packages_toml, 'apt = "tmux"\n')
        write(profile.overlay_packages_toml, 'apt = ["htop"]\n')
        with self.assertRaisesRegex(ConfigError, r"profile/packages.toml: apt must be a list"):
            profile.load_packages()

    def test_an_error_in_the_overlay_names_the_overlay_file(self) -> None:
        profile = self.profile()
        write(profile.packages_toml, 'apt = ["tmux"]\n')
        write(profile.overlay_packages_toml, 'pip = ["x"]\n')
        with self.assertRaisesRegex(ConfigError, r"machines/home/packages.toml: unknown key"):
            profile.load_packages()

    def test_duplicate_package_names_collapse(self) -> None:
        profile = self.profile()
        write(profile.packages_toml, 'apt = ["htop", "htop"]\n')
        write(profile.overlay_packages_toml, 'apt = ["htop", "tmux", "tmux"]\n')
        self.assertEqual(profile.load_packages().apt, ("htop", "tmux"))

    def test_empty_package_names_are_rejected(self) -> None:
        profile = self.profile()
        write(profile.packages_toml, 'apt = [""]\n')
        with self.assertRaisesRegex(ConfigError, "non-empty strings"):
            profile.load_packages()


class OverlayInventoryTests(ProfileCase):
    def test_files_overlay_path_wins_and_reports_shadowed_file(self) -> None:
        profile = self.profile()
        shared_b = write(profile.files_dir / ".config/b.conf")
        write(profile.files_dir / ".config/a.conf")
        overlay_b = write(profile.overlay_files_dir / ".config/b.conf")
        write(profile.overlay_files_dir / ".config/c.conf")

        found = {str(entry.rel): entry for entry in profile.files()}

        self.assertEqual(list(found), [".config/a.conf", ".config/b.conf", ".config/c.conf"])
        self.assertEqual(found[".config/a.conf"].origin, "shared")
        self.assertEqual((found[".config/b.conf"].origin, found[".config/b.conf"].source), ("overlay", overlay_b))
        self.assertEqual(found[".config/b.conf"].shadows, (shared_b,))
        self.assertEqual(found[".config/c.conf"].shadows, ())
        self.assertEqual(found[".config/a.conf"].shadows, ())
        self.assertEqual(found[".config/a.conf"].rel, PurePosixPath(".config/a.conf"))

    def test_overlay_entry_that_contains_or_is_contained_by_a_shared_one_replaces_it(self) -> None:
        profile = self.profile()
        write(profile.files_dir / ".config/foo/a.conf")
        write(profile.files_dir / ".config/bar")
        write(profile.files_dir / ".config/keep/x.conf")
        write(profile.overlay_files_dir / ".config/foo")  # a leaf where shared has a directory
        write(profile.overlay_files_dir / ".config/bar/inner.conf")  # a directory where shared has a leaf
        write(profile.overlay_files_dir / ".config/keep/y.conf")  # siblings do not collide

        self.assertEqual(
            [(str(entry.rel), entry.origin) for entry in profile.files()],
            [
                (".config/bar/inner.conf", "overlay"),
                (".config/foo", "overlay"),
                (".config/keep/x.conf", "shared"),
                (".config/keep/y.conf", "overlay"),
            ],
        )
        found = {str(entry.rel): entry for entry in profile.files()}
        self.assertEqual(found[".config/foo"].shadows, (profile.files_dir / ".config/foo/a.conf",))
        self.assertEqual(found[".config/bar/inner.conf"].shadows, (profile.files_dir / ".config/bar",))
        self.assertEqual(found[".config/keep/y.conf"].shadows, ())

    def test_symlinked_directory_is_a_leaf_not_followed(self) -> None:
        profile = self.profile()
        target = self.tmp / "target"
        write(target / "inner.txt")
        (profile.files_dir / ".config").mkdir(parents=True)
        (profile.files_dir / ".config/linked").symlink_to(target)

        self.assertEqual([str(entry.rel) for entry in profile.files()], [".config/linked"])

    def test_bundle_files_list_every_ini_below_settings_in_both_layers(self) -> None:
        profile = self.profile()
        write(profile.settings_dir / "appearance.ini")
        write(profile.settings_dir / "laptop/power.ini")
        write(profile.settings_dir / "notes.txt")
        write(profile.settings_dir / ".draft.ini")
        write(profile.overlay_settings_dir / "laptop/power.ini")
        write(profile.overlay_settings_dir / "sound.ini")

        found = [(bundle.name, bundle.origin) for bundle in profile.bundle_files()]

        self.assertEqual(
            found,
            [
                ("appearance", "shared"),
                ("laptop/power", "shared"),
                ("laptop/power", "overlay"),
                ("sound", "overlay"),
            ],
        )

    def test_overlay_presence(self) -> None:
        profile = self.profile("home")
        self.assertFalse(profile.has_overlay)
        profile.overlay_dir.mkdir(parents=True)
        self.assertTrue(profile.has_overlay)

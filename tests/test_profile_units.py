"""``units.toml``: parsing, the shared + overlay merge, validation and the reasons kept as comments."""

import tempfile
import unittest
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.profile import Profile, Units, unit_template


class ProfileUnitsCase(TempDirCase):
    def profile(self, machine: str = "home") -> Profile:
        root = self.tmp / "profile"
        root.mkdir(exist_ok=True)
        return Profile(root, machine)


class ParseTests(ProfileUnitsCase):
    def test_no_file_means_nothing_declared_and_start_by_default(self) -> None:
        units = self.profile().load_units()
        self.assertEqual(units, Units())
        self.assertEqual((units.enable, units.disable, units.start, units.linger), ((), (), True, None))

    def test_every_key_is_read(self) -> None:
        profile = self.profile()
        write(
            profile.units_toml,
            'enable = ["glances.service", "backup.timer"]\ndisable = ["old.service"]\nstart = false\nlinger = true\n',
        )
        self.assertEqual(
            profile.load_units(),
            Units(enable=("glances.service", "backup.timer"), disable=("old.service",), start=False, linger=True),
        )

    def test_a_list_with_a_repeated_name_collapses(self) -> None:
        profile = self.profile()
        write(profile.units_toml, 'enable = ["a.service", "a.service"]\n')
        self.assertEqual(profile.load_units().enable, ("a.service",))

    def test_template_instances_are_names(self) -> None:
        profile = self.profile()
        write(profile.units_toml, 'enable = ["3d-printer-watch@u1.service", "app@my\\\\x2dinst.service"]\n')
        self.assertEqual(profile.load_units().enable, ("3d-printer-watch@u1.service", "app@my\\x2dinst.service"))


class MergeTests(ProfileUnitsCase):
    def test_lists_union_shared_order_first(self) -> None:
        profile = self.profile()
        write(profile.units_toml, 'enable = ["a.service", "b.timer"]\n')
        write(profile.overlay_units_toml, 'enable = ["b.timer", "c.socket"]\n')
        self.assertEqual(profile.load_units().enable, ("a.service", "b.timer", "c.socket"))

    def test_the_overlay_disables_a_name_the_shared_file_enables(self) -> None:
        profile = self.profile()
        write(profile.units_toml, 'enable = ["a.service", "b.service"]\n')
        write(profile.overlay_units_toml, 'disable = ["a.service"]\n')
        units = profile.load_units()
        self.assertEqual((units.enable, units.disable), (("b.service",), ("a.service",)))

    def test_the_overlay_can_enable_what_the_shared_file_disables(self) -> None:
        profile = self.profile()
        write(profile.units_toml, 'disable = ["a.service", "b.service"]\n')
        write(profile.overlay_units_toml, 'enable = ["a.service"]\n')
        units = profile.load_units()
        self.assertEqual((units.enable, units.disable), (("a.service",), ("b.service",)))

    def test_scalars_of_the_overlay_replace_and_an_absent_one_keeps_the_shared_value(self) -> None:
        profile = self.profile()
        write(profile.units_toml, "start = false\nlinger = true\n")
        write(profile.overlay_units_toml, 'enable = ["a.service"]\n')
        units = profile.load_units()
        self.assertEqual((units.start, units.linger), (False, True))
        write(profile.overlay_units_toml, "start = true\nlinger = false\n")
        units = profile.load_units()
        self.assertEqual((units.start, units.linger), (True, False))


class ValidationTests(ProfileUnitsCase):
    def rejects(self, text: str, pattern: str, *, overlay: bool = False) -> None:
        profile = self.profile()
        write(profile.overlay_units_toml if overlay else profile.units_toml, text)
        with self.assertRaisesRegex(ConfigError, pattern):
            profile.load_units()

    def test_unknown_keys(self) -> None:
        self.rejects('mask = ["a.service"]\n', r"units.toml: unknown key\(s\) mask")

    def test_names_need_a_unit_suffix(self) -> None:
        self.rejects('enable = ["glances"]\n', "not a unit name")
        self.rejects('enable = ["a.mount"]\n', "not a unit name")

    def test_names_cannot_hold_a_path_or_an_option(self) -> None:
        self.rejects('enable = ["../a.service"]\n', "not a unit name")
        self.rejects('enable = ["/etc/systemd/user/a.service"]\n', "not a unit name")
        self.rejects('enable = ["--now.service"]\n', "not a unit name")
        self.rejects('enable = ["a b.service"]\n', "not a unit name")

    def test_a_bare_template_cannot_be_enabled(self) -> None:
        self.rejects('enable = ["foo@.service"]\n', "template")

    def test_the_wrong_types(self) -> None:
        self.rejects('enable = "a.service"\n', "enable must be a list")
        self.rejects('start = "yes"\n', "start must be true or false")
        self.rejects("linger = 1\n", "linger must be true or false")

    def test_one_layer_cannot_enable_and_disable_the_same_unit(self) -> None:
        self.rejects('enable = ["a.service"]\ndisable = ["a.service"]\n', "both enable and disable")

    def test_an_error_in_the_overlay_names_the_overlay_file(self) -> None:
        self.rejects('enable = ["x"]\n', r"machines/home/units.toml: .*not a unit name", overlay=True)

    def test_invalid_toml_is_a_config_error(self) -> None:
        self.rejects("enable = [\n", "invalid TOML")


class TemplateNameTests(unittest.TestCase):
    def test_an_instance_maps_to_its_template_whatever_dots_the_instance_has(self) -> None:
        for name, template in (
            ("foo@bar.service", "foo@.service"),
            ("vpn@client.conf.service", "vpn@.service"),
            ("ssh-tunnel@10.0.0.1.service", "ssh-tunnel@.service"),
            ("foo@bar.timer", "foo@.timer"),
            ("plain.service", "plain.service"),
            ("foo@.service", "foo@.service"),
        ):
            self.assertEqual(unit_template(name), template, name)

    def test_the_template_is_cut_at_the_first_at_sign(self) -> None:
        self.assertEqual(unit_template("ssh-tunnel@user@host.service"), "ssh-tunnel@.service")

    def test_a_dotted_instance_is_a_valid_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Profile(Path(directory), "home")
            write(profile.units_toml, 'enable = ["vpn@client.conf.service"]\n')
            self.assertEqual(profile.load_units().enable, ("vpn@client.conf.service",))


class ReasonTests(ProfileUnitsCase):
    def test_a_trailing_comment_is_the_reason_and_the_overlay_wins(self) -> None:
        profile = self.profile()
        write(
            profile.units_toml,
            'enable = [\n  "glances.service",  # system monitor web UI\n  "backup.timer",\n]\n',
        )
        write(profile.overlay_units_toml, 'enable = [\n  "backup.timer",  # nightly pull on this machine\n]\n')
        self.assertEqual(
            profile.unit_reasons(),
            {
                ("enable", "glances.service"): "system monitor web UI",
                ("enable", "backup.timer"): "nightly pull on this machine",
            },
        )

    def test_a_unit_without_a_comment_has_no_reason(self) -> None:
        profile = self.profile()
        write(profile.units_toml, 'enable = ["a.service"]\n')
        self.assertEqual(profile.unit_reasons(), {})

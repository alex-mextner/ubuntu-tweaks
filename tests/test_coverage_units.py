"""Coverage of systemd user units declared in ``units.toml``: ownership, templates, and module rules."""

from tests.test_coverage_adapters import AdapterCase
from tests.util import write
from ubuntu_tweaks.adapters import scan, units
from ubuntu_tweaks.coverage import Coverage, Layer, Rule, Rules
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.process import CommandResult

LIST_ENABLED = ("systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager")


class UnitsTomlOwnershipTests(AdapterCase):
    def test_every_name_in_units_toml_is_owned_by_it_enable_and_disable_alike(self) -> None:
        write(
            self.profile_root / "units.toml",
            'enable = ["glances.service", "backup.timer"]\ndisable = ["old.service"]\n',
        )
        self.machine()
        self.assertEqual(
            units.owned(self.ctx),
            {"unit:glances.service": "units.toml", "unit:backup.timer": "units.toml", "unit:old.service": "units.toml"},
        )

    def test_the_machine_overlay_names_are_owned_too(self) -> None:
        write(self.profile_root / "machines/box/units.toml", 'enable = ["only-here.service"]\n')
        self.machine()
        self.assertEqual(units.owned(self.ctx), {"unit:only-here.service": "units.toml"})

    def test_an_instance_owns_its_template(self) -> None:
        write(self.profile_root / "units.toml", 'enable = ["3d-printer-watch@u1.service"]\n')
        self.machine()
        self.assertEqual(
            units.owned(self.ctx),
            {"unit:3d-printer-watch@u1.service": "units.toml", "unit:3d-printer-watch@.service": "units.toml"},
        )

    def test_an_instance_with_dots_owns_its_template(self) -> None:
        write(self.profile_root / "units.toml", 'enable = ["vpn@client.conf.service"]\n')
        self.machine()
        self.assertEqual(
            units.owned(self.ctx),
            {"unit:vpn@client.conf.service": "units.toml", "unit:vpn@.service": "units.toml"},
        )

    def test_a_template_file_of_the_profile_is_owned_as_before(self) -> None:
        write(self.profile_root / "files/.config/systemd/user/x@.service", "x")
        self.machine()
        self.assertEqual(units.owned(self.ctx), {"unit:x@.service": "files/.config/systemd/user/x@.service"})

    def test_a_name_in_units_toml_wins_the_owner_label_over_its_unit_file(self) -> None:
        write(self.profile_root / "files/.config/systemd/user/foo.service", "x")
        write(self.profile_root / "units.toml", 'enable = ["foo.service"]\n')
        self.machine()
        self.assertEqual(units.owned(self.ctx), {"unit:foo.service": "units.toml"})

    def test_an_invalid_units_toml_is_a_config_error_not_silence(self) -> None:
        write(self.profile_root / "units.toml", 'enable = ["nonsense"]\n')
        self.machine()
        with self.assertRaises(ConfigError):
            units.owned(self.ctx)


class ScanTests(AdapterCase):
    def findings(self, rules: Rules = Rules()) -> dict[str, tuple[Coverage, str]]:
        return {f.item.id: (f.coverage, f.owner) for f in scan(self.ctx, rules) if f.item.adapter == "units"}

    def test_declared_units_are_tracked_and_an_undeclared_enabled_one_is_untracked(self) -> None:
        write(self.profile_root / "units.toml", 'enable = ["glances.service"]\n')
        self.machine(
            {LIST_ENABLED: CommandResult(0, "glances.service enabled enabled\nstray.service enabled enabled\n", "")}
        )
        self.assertEqual(
            self.findings(),
            {
                "unit:glances.service": (Coverage.TRACKED, "units.toml"),
                "unit:stray.service": (Coverage.UNTRACKED, ""),
            },
        )

    def test_a_module_rule_claims_the_units_it_runs_and_beats_untracked(self) -> None:
        self.machine({LIST_ENABLED: CommandResult(0, "autoheal.timer enabled enabled\n", "")})
        self.assertEqual(self.findings()["unit:autoheal.timer"][0], Coverage.UNTRACKED)
        claim = Rule(("unit:autoheal.*",), Coverage.TRACKED, "", Layer.MODULE, "module autoheal")
        self.assertEqual(self.findings(Rules((claim,)))["unit:autoheal.timer"], (Coverage.TRACKED, "module autoheal"))

"""`capture` and systemd user units: the unit files and drop-ins are allow-listed, a unit with a secret is skipped
alone, and the enabled units of the machine are proposed for ``units.toml``."""

import os
from pathlib import Path

from tests.test_capture import FAKE_TOKEN, commit_plan, make_plan
from tests.util import TempDirCase, write
from ubuntu_tweaks.capture.plan import refusals
from ubuntu_tweaks.capture.report import build_document, render_text
from ubuntu_tweaks.errors import ConfigError

USER = ".config/systemd/user"


class UnitCaptureCase(TempDirCase):
    def home(self) -> Path:
        return self.tmp / "home"

    def enable(self, name: str, text: str = "[Unit]\nDescription=Demo job\n[Service]\nExecStart=/bin/true\n") -> None:
        """A unit file in the user directory, enabled the way `systemctl --user enable` does."""
        write(self.home() / USER / name, text)
        wants = self.home() / USER / "default.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        (wants / name).symlink_to(self.home() / USER / name)


class AllowListTests(UnitCaptureCase):
    def test_unit_files_and_drop_ins_are_adopted(self) -> None:
        self.enable("demo.service")
        write(self.home() / USER / "demo.timer", "[Timer]\nOnCalendar=daily\n")
        write(self.home() / USER / "demo.service.d/10-limits.conf", "[Service]\nMemoryMax=1G\n")
        write(self.home() / USER / "notes.txt", "not a unit\n")
        plan, _rules = make_plan(self)
        assert plan.files is not None
        self.assertEqual(
            {a.rel for a in plan.files.adopted if a.rel.startswith(USER)},
            {f"{USER}/demo.service", f"{USER}/demo.timer", f"{USER}/demo.service.d/10-limits.conf"},
        )

    def test_a_unit_with_a_secret_is_skipped_alone_and_the_commit_goes_on(self) -> None:
        self.enable("clean.service")
        self.enable("leaky.service", f"[Service]\nEnvironment=TOKEN={FAKE_TOKEN}\nExecStart=/bin/true\n")
        write(self.home() / ".config/mimeapps.list", "[Default Applications]\ntext/plain=x.desktop\n")
        plan, rules = make_plan(self)
        assert plan.files is not None
        self.assertEqual(refusals(plan), [])  # nothing makes --commit refuse
        skipped = {s.rel: s.reason for s in plan.files.skipped}
        self.assertIn("api-token", skipped[f"{USER}/leaky.service"])
        self.assertNotIn(FAKE_TOKEN, skipped[f"{USER}/leaky.service"])
        assert plan.units is not None
        self.assertEqual([i.name for i in plan.units], ["clean.service"])  # apply could not enable the skipped one
        commit_plan(plan, rules, self.home())
        profile = self.tmp / "profile"
        self.assertNotIn("leaky", (profile / "units.toml").read_text(encoding="utf-8"))
        self.assertTrue((profile / "files" / USER / "clean.service").is_file())
        self.assertFalse((profile / "files" / USER / "leaky.service").exists())
        self.assertFalse((self.home() / USER / "leaky.service").is_symlink())  # left where it was
        self.assertTrue((self.home() / USER / "clean.service").is_symlink())
        self.assertTrue((profile / "files/.config/mimeapps.list").is_file())  # the rest was captured


class ProposalTests(UnitCaptureCase):
    def test_enabled_units_of_the_user_directory_are_proposed_with_their_description(self) -> None:
        self.enable("demo.service")
        write(self.home() / USER / "idle.service", "[Unit]\nDescription=Never enabled\n")
        write(self.home() / USER / "pipewire.service.d/x.conf", "")  # a drop-in of a system unit: no unit file here
        plan, _rules = make_plan(self)
        assert plan.units is not None
        self.assertEqual([(i.name, i.mark, i.reason) for i in plan.units], [("demo.service", "+", "Demo job")])

    def test_a_system_unit_enabled_by_the_user_is_not_proposed(self) -> None:
        wants = self.home() / USER / "default.target.wants"
        wants.mkdir(parents=True)
        (wants / "pipewire.service").symlink_to("/usr/lib/systemd/user/pipewire.service")
        plan, _rules = make_plan(self)
        self.assertEqual(plan.units, [])

    def test_an_instance_is_proposed_through_its_template(self) -> None:
        write(self.home() / USER / "watch@.service", "[Unit]\nDescription=Watcher %i\n")
        wants = self.home() / USER / "default.target.wants"
        wants.mkdir(parents=True)
        (wants / "watch@u1.service").symlink_to(self.home() / USER / "watch@.service")
        plan, _rules = make_plan(self)
        assert plan.units is not None
        self.assertEqual([(i.name, i.reason) for i in plan.units], [("watch@u1.service", "Watcher %i")])

    def test_what_units_toml_already_holds_is_equal_and_what_it_disables_is_left_out(self) -> None:
        self.enable("a.service")
        self.enable("b.service")
        self.enable("c.service")
        write(self.tmp / "profile/units.toml", 'enable = ["a.service"]\ndisable = ["c.service"]\n')
        plan, _rules = make_plan(self)
        assert plan.units is not None
        self.assertEqual([(i.name, i.mark) for i in plan.units], [("a.service", "="), ("b.service", "+")])

    def test_an_invalid_units_toml_stops_the_capture(self) -> None:
        write(self.tmp / "profile/units.toml", 'enable = ["nonsense"]\n')
        with self.assertRaises(ConfigError):
            make_plan(self)

    def test_only_selects_the_section(self) -> None:
        self.enable("demo.service")
        plan, _rules = make_plan(self, only=frozenset({"units"}))
        self.assertIsNotNone(plan.units)
        self.assertIsNone(plan.files)
        plan, _rules = make_plan(self, only=frozenset({"files"}))
        self.assertIsNone(plan.units)

    def test_the_report_lists_the_proposal(self) -> None:
        self.enable("demo.service")
        plan, _rules = make_plan(self)
        text = render_text(build_document(plan, machine="testbox", commit=False, verbose=False))
        self.assertIn("3b. Units", text)
        self.assertIn("demo.service", text)
        self.assertIn("units: 1 new to enable", text)


class OnlyUnitsSecretTests(UnitCaptureCase):
    def test_only_units_does_not_propose_a_unit_whose_file_or_drop_in_holds_a_secret(self) -> None:
        self.enable("leaky.service", f"[Service]\nEnvironment=TOKEN={FAKE_TOKEN}\n")
        self.enable("dropin.service")
        write(self.home() / USER / "dropin.service.d/env.conf", f"[Service]\nEnvironment=KEY={FAKE_TOKEN}\n")
        self.enable("fine.service")
        plan, _rules = make_plan(self, only=frozenset({"units"}))
        assert plan.units is not None
        self.assertEqual([i.name for i in plan.units], ["fine.service"])

    def test_an_empty_only_units_report_says_so(self) -> None:
        plan, _rules = make_plan(self, only=frozenset({"units"}))
        text = render_text(build_document(plan, machine="testbox", commit=False, verbose=False))
        self.assertIn("3b. Units", text)
        self.assertIn("units: 0 new to enable", text)


class DescriptionTests(UnitCaptureCase):
    def test_a_token_in_a_description_is_never_a_reason(self) -> None:
        self.enable("demo.service", f"[Unit]\nDescription=job {FAKE_TOKEN}\n[Service]\nExecStart=/bin/true\n")
        plan, _rules = make_plan(self)
        assert plan.files is not None and plan.units is not None
        self.assertEqual(plan.units, [])  # the file has a finding, so it is skipped and not proposed
        self.assertEqual(refusals(plan), [])

    def test_a_long_description_is_clipped(self) -> None:
        self.enable("demo.service", f"[Unit]\nDescription={'x' * 200}\n[Service]\nExecStart=/bin/true\n")
        plan, _rules = make_plan(self)
        assert plan.units is not None
        self.assertEqual(len(plan.units[0].reason), 80)


class CommitTests(UnitCaptureCase):
    def test_commit_appends_to_enable_keeping_comments_and_the_reason_is_the_description(self) -> None:
        self.enable("demo.service")
        self.enable("old.service")
        units = write(self.tmp / "profile/units.toml", 'linger = true\nenable = [\n  "old.service",  # mine\n]\n')
        plan, rules = make_plan(self)
        commit_plan(plan, rules, self.home())
        text = units.read_text(encoding="utf-8")
        self.assertIn('"old.service",  # mine', text)
        self.assertIn('"demo.service",  # Demo job', text)
        self.assertTrue(text.startswith("linger = true\n"))

    def test_commit_creates_units_toml_and_a_second_capture_proposes_nothing(self) -> None:
        self.enable("demo.service")
        plan, rules = make_plan(self)
        commit_plan(plan, rules, self.home())
        units = self.tmp / "profile/units.toml"
        self.assertIn('"demo.service"', units.read_text(encoding="utf-8"))
        again, _rules = make_plan(self)
        assert again.units is not None
        self.assertEqual([(i.name, i.mark) for i in again.units], [("demo.service", "=")])

    def test_the_unit_symlink_survives_so_apply_would_find_it_linked(self) -> None:
        self.enable("demo.service")
        plan, rules = make_plan(self)
        commit_plan(plan, rules, self.home())
        self.assertEqual(
            os.path.realpath(self.home() / USER / "demo.service"),
            os.path.realpath(self.tmp / "profile/files" / USER / "demo.service"),
        )

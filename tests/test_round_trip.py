"""What ``capture --commit`` writes into a profile is exactly what ``apply`` reads back.

The two commands were built separately: capture emits panel keyfiles, machine overlays and
``files/`` entries; apply's settings and files planners consume them. This pins the seam: capture a
fake machine into an empty profile, then plan against a second, empty machine and require the same
keys as ``set-key`` actions and the same files as ``link`` actions.
"""

import unittest

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from pathlib import Path

from tests.test_capture import NOW, SCHEMAS, commit_plan, make_plan
from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.planners import files as files_planner
from ubuntu_tweaks.planners import settings as settings_planner
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend


class CaptureThenApplyTests(TempDirCase):
    def capture(self) -> tuple[Path, dict[str, str], set[str]]:
        """Capture the seeded machine into ``tmp/profile``; return (profile, captured keys, profile files)."""
        home = self.tmp / "home"
        write(home / ".config/mimeapps.list", "[Default Applications]\ntext/plain=x.desktop\n")
        write(home / ".config/monitors.xml", "<monitors/>\n")
        plan, rules = make_plan(self)
        assert plan.settings is not None and plan.files is not None
        commit_plan(plan, rules, home)
        keys = {entry.directory + entry.key: entry.value for entry in plan.settings.entries}
        files = {str(adopted.dest.relative_to(self.tmp / "profile")) for adopted in plan.files.adopted}
        return self.tmp / "profile", keys, files

    def context(self, profile_root: Path) -> Context:
        repo = self.tmp / "repo"
        (repo / "modules").mkdir(parents=True, exist_ok=True)
        return Context(repo, Profile(profile_root, "testbox", "flag"))

    def test_every_captured_key_comes_back_as_a_set_key_action_with_the_same_value(self) -> None:
        profile_root, captured, _files = self.capture()
        self.assertTrue(captured, "the scenario must capture something")

        settings = Plan.build(settings_planner.build(self.context(profile_root), FakeBackend(SCHEMAS)))

        self.assertEqual({action.kind for action in settings.actions}, {"set-key"})
        self.assertEqual({action.target.full: action.entry.text for action in settings.actions}, captured)

    def test_captured_files_become_link_actions_into_the_profile(self) -> None:
        profile_root, _captured, files = self.capture()
        self.assertEqual(files, {"files/.config/mimeapps.list", "machines/testbox/files/.config/monitors.xml"})
        fresh_home = self.tmp / "fresh-home"
        fresh_home.mkdir()

        links = Plan.build(
            files_planner.build(
                self.context(profile_root),
                FakeBackend(SCHEMAS),
                home=fresh_home,
                clock=lambda: NOW,
                state_home=self.tmp / "state",
            )
        )

        self.assertEqual({action.kind for action in links.actions}, {"link"})
        links.execute()
        self.assertEqual(
            (fresh_home / ".config/mimeapps.list").resolve(), (profile_root / "files/.config/mimeapps.list").resolve()
        )
        self.assertEqual(
            (fresh_home / ".config/monitors.xml").resolve(),
            (profile_root / "machines/testbox/files/.config/monitors.xml").resolve(),
        )

    def test_applying_what_was_captured_converges_and_reproduces_the_captured_keys(self) -> None:
        profile_root, captured, _files = self.capture()
        ctx = self.context(profile_root)
        backend = FakeBackend(SCHEMAS)

        Plan.build(settings_planner.build(ctx, backend)).execute()

        self.assertEqual(len(Plan.build(settings_planner.build(ctx, backend))), 0)
        self.assertEqual({key.full for key in backend.list_user_keys()}, set(captured))


class BundlesRoundTripTests(TempDirCase):
    """Hand-written bundles (two shared, one machine overlay, reasons, a header) -> apply -> capture back."""

    FILES = {
        "settings/dev-look.ini": (
            '[ubuntu-tweak]\ndescription = "Look"\n\n[org/gnome/desktop/interface]\n'
            "# dark on the OLED\ncolor-scheme='prefer-dark'\n"
        ),
        "settings/misc/battery.ini": "[org/gnome/desktop/interface]\n# I like to see it\nshow-battery-percentage=true\n",
        "machines/testbox/settings/laptop/power.ini": (
            "# this box only\n[org/gnome/settings-daemon/plugins/power]\n"
            "# the render job must not be suspended\nsleep-inactive-ac-timeout=900\n"
        ),
    }

    def setUp(self) -> None:
        super().setUp()
        self.profile = self.tmp / "profile"
        for relative, text in self.FILES.items():
            write(self.profile / relative, text)
        repo = self.tmp / "repo"
        (repo / "modules").mkdir(parents=True)
        self.ctx = Context(repo, Profile(self.profile, "testbox", "flag"))

    def test_apply_shows_reasons_and_then_capture_finds_nothing_to_change(self) -> None:
        backend = FakeBackend(SCHEMAS)
        plan = Plan.build(settings_planner.build(self.ctx, backend))
        described = {action.target.key: action.describe() for action in plan.actions}  # type: ignore[attr-defined]
        self.assertIn("# dark on the OLED", described["color-scheme"])
        self.assertIn("# the render job must not be suspended", described["sleep-inactive-ac-timeout"])
        self.assertEqual({action.scope for action in plan.actions}, {"dev-look", "misc/battery", "laptop/power"})
        plan.execute()
        self.assertEqual(len(Plan.build(settings_planner.build(self.ctx, backend))), 0)

        captured, rules = make_plan(self, backend)
        assert captured.settings is not None
        by_key = {e.key: (e.bundle, e.target, e.mark, e.reason) for e in captured.settings.entries}
        self.assertEqual(by_key["color-scheme"], ("dev-look", "shared", "=", "dark on the OLED"))
        self.assertEqual(by_key["show-battery-percentage"], ("misc/battery", "shared", "=", "I like to see it"))
        self.assertEqual(
            by_key["sleep-inactive-ac-timeout"],
            ("laptop/power", "machine", "=", "the render job must not be suspended"),
        )
        commit_plan(captured, rules, self.tmp / "home")
        for relative, text in self.FILES.items():
            self.assertEqual((self.profile / relative).read_text(), text, relative)  # byte for byte

    def test_a_changed_live_value_comes_back_into_the_same_bundle_keeping_the_reason(self) -> None:
        backend = FakeBackend(SCHEMAS)
        Plan.build(settings_planner.build(self.ctx, backend)).execute()
        backend.set_user("org.gnome.settings-daemon.plugins.power", None, "sleep-inactive-ac-timeout", "7200")

        captured, rules = make_plan(self, backend)
        commit_plan(captured, rules, self.tmp / "home")

        overlay = (self.profile / "machines/testbox/settings/laptop/power.ini").read_text()
        self.assertEqual(overlay, self.FILES["machines/testbox/settings/laptop/power.ini"].replace("=900", "=7200"))
        self.assertEqual(len(Plan.build(settings_planner.build(self.ctx, backend))), 0)


if __name__ == "__main__":
    unittest.main()

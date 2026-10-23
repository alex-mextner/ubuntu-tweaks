"""The files planner and the backup helper: links, adoption, backup+replace, refusals, extras."""

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.backup import backup
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import RefusedError, UbuntuTweakError
from ubuntu_tweaks.planners import files as planner
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend
from tests.util import TempDirCase, write

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
STAMP = "20260102T030405Z"


class FilesCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.profile_root = self.tmp / "profile"
        self.state = self.tmp / "state"
        (self.tmp / "repo").mkdir()
        self.ctx = Context(self.tmp / "repo", Profile(self.profile_root, "home"))

    def actions(self) -> list:
        return list(planner.build(self.ctx, FakeBackend(), home=self.home, clock=lambda: NOW, state_home=self.state))

    def plan(self) -> Plan:
        return Plan.build(self.actions())

    def shared(self, rel: str, text: str = "profile\n") -> Path:
        return write(self.profile_root / "files" / rel, text)


class LinkTests(FilesCase):
    def test_missing_destination_is_a_link_that_creates_parents(self) -> None:
        source = self.shared(".config/foo/bar.conf")
        plan = self.plan()

        self.assertEqual([a.kind for a in plan.actions], ["link"])
        self.assertEqual(plan.actions[0].describe(), "link ~/.config/foo/bar.conf -> files/.config/foo/bar.conf")
        self.assertFalse((self.home / ".config").exists())  # a preview changes nothing
        plan.execute()
        destination = self.home / ".config/foo/bar.conf"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), str(source))
        self.assertEqual(len(self.plan()), 0)

    def test_only_leaves_are_linked_so_apps_can_add_siblings(self) -> None:
        self.shared(".config/foo/bar.conf")
        self.plan().execute()
        (self.home / ".config/foo/app-made.conf").write_text("x", encoding="utf-8")

        self.assertTrue((self.home / ".config/foo").is_dir())
        self.assertFalse((self.home / ".config/foo").is_symlink())
        self.assertEqual(len(self.plan()), 0)

    def test_machine_overlay_path_wins_over_the_shared_file(self) -> None:
        self.shared(".config/foo.conf", "shared\n")
        overlay = write(self.profile_root / "machines/home/files/.config/foo.conf", "overlay\n")
        plan = self.plan()

        self.assertEqual(len(plan), 1)
        self.assertIn("machines/home/files/.config/foo.conf", plan.actions[0].describe())
        self.assertIn("[machine overlay]", plan.actions[0].describe())
        plan.execute()
        self.assertEqual(os.readlink(self.home / ".config/foo.conf"), str(overlay))


class ReplaceTests(FilesCase):
    def test_identical_real_file_is_adopted_and_still_backed_up(self) -> None:
        self.shared(".config/a.conf", "same\n")
        write(self.home / ".config/a.conf", "same\n")
        plan = self.plan()

        self.assertEqual([a.kind for a in plan.actions], ["link-replace"])
        self.assertIn("adopt ~/.config/a.conf", plan.actions[0].describe())
        self.assertIn("identical content", plan.actions[0].describe())
        plan.execute()
        saved = self.state / "ubuntu-tweaks/backup" / STAMP / (self.home / ".config/a.conf").relative_to("/")
        self.assertEqual(saved.read_text(encoding="utf-8"), "same\n")
        self.assertTrue((self.home / ".config/a.conf").is_symlink())

    def test_different_real_file_is_shown_as_backup_replace_with_a_diff_summary(self) -> None:
        self.shared(".config/a.conf", "one\ntwo\nthree\n")
        write(self.home / ".config/a.conf", "one\nTWO\nthree\nfour\n")
        plan = self.plan()

        text = plan.actions[0].describe()
        self.assertTrue(text.startswith("backup+replace ~/.config/a.conf -> files/.config/a.conf"), text)
        self.assertIn("differs: 19 B on disk, 14 B in profile, +1/-2 lines", text)
        plan.execute()
        saved = self.state / "ubuntu-tweaks/backup" / STAMP / (self.home / ".config/a.conf").relative_to("/")
        self.assertEqual(saved.read_text(encoding="utf-8"), "one\nTWO\nthree\nfour\n")
        self.assertEqual((self.home / ".config/a.conf").read_text(encoding="utf-8"), "one\ntwo\nthree\n")

    def test_binary_file_is_summarised_without_a_diff(self) -> None:
        self.shared("blob", "x")
        (self.home / "blob").write_bytes(b"\xff\xfe\x00")

        self.assertIn("binary or unreadable, not diffed", self.plan().actions[0].describe())

    def test_symlink_to_somewhere_else_is_replaced_after_a_backup(self) -> None:
        self.shared(".config/a.conf")
        elsewhere = write(self.tmp / "elsewhere.conf", "x")
        (self.home / ".config").mkdir()
        (self.home / ".config/a.conf").symlink_to(elsewhere)
        plan = self.plan()

        self.assertEqual([a.kind for a in plan.actions], ["link-replace"])
        self.assertIn(f"now a symlink to {elsewhere}", plan.actions[0].describe())
        plan.execute()
        saved = self.state / "ubuntu-tweaks/backup" / STAMP / (self.home / ".config/a.conf").relative_to("/")
        self.assertTrue(saved.is_symlink())
        self.assertEqual(len(self.plan()), 0)

    def test_an_app_that_rewrote_the_link_into_a_real_file_shows_up_again(self) -> None:
        self.shared(".config/mimeapps.list", "a\n")
        self.plan().execute()
        (self.home / ".config/mimeapps.list").unlink()
        write(self.home / ".config/mimeapps.list", "b\n")

        self.assertEqual([a.kind for a in self.plan().actions], ["link-replace"])

    def test_a_file_where_a_parent_directory_should_be_is_shown_as_refused(self) -> None:
        self.shared(".config/a.conf")
        write(self.home / ".config", "a file")
        plan = self.plan()

        self.assertIn("REFUSED", plan.actions[0].describe())
        self.assertIn("~/.config is not a directory", plan.actions[0].describe())
        with self.assertRaisesRegex(RefusedError, "not a directory"):
            plan.execute()

    def test_a_directory_symlink_into_the_profile_is_not_reported_as_an_orphan(self) -> None:
        self.shared(".config/nvim/init.lua")
        (self.home / ".config").mkdir()
        (self.home / ".config/old-nvim").symlink_to(self.profile_root / "files/.config/nvim")

        self.assertEqual(list(planner.extras(self.ctx, FakeBackend(), home=self.home)), [])

    def test_a_directory_in_the_way_is_refused_not_touched(self) -> None:
        self.shared("thing")
        (self.home / "thing").mkdir()
        (self.home / "thing/keep").write_text("k", encoding="utf-8")
        plan = self.plan()

        self.assertIn("REFUSED", plan.actions[0].describe())
        with self.assertRaisesRegex(RefusedError, "is a directory"):
            plan.execute()
        self.assertEqual((self.home / "thing/keep").read_text(encoding="utf-8"), "k")


class RefusalTests(FilesCase):
    def test_parent_dir_components_are_refused(self) -> None:
        with self.assertRaisesRegex(RefusedError, "outside"):
            planner._destination(self.home, PurePosixPath("../x"))

    def test_a_destination_that_resolves_outside_home_is_refused_before_any_plan(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.home / ".config").symlink_to(outside)
        self.shared(".config/a.conf")

        with self.assertRaisesRegex(RefusedError, "outside"):
            self.actions()
        self.assertEqual(list(outside.iterdir()), [])


class ExtrasTests(FilesCase):
    def extras(self) -> list[str]:
        return list(planner.extras(self.ctx, FakeBackend(), home=self.home))

    def test_links_into_the_profile_without_a_source_are_reported(self) -> None:
        kept = self.shared(".config/kept.conf")
        gone = self.profile_root / "files/.config/gone.conf"  # never existed in this checkout
        (self.home / ".config").mkdir()
        (self.home / ".config/kept.conf").symlink_to(kept)
        (self.home / ".config/gone.conf").symlink_to(gone)
        (self.home / ".config/other.conf").symlink_to(self.tmp / "unrelated")
        (self.home / ".bashrc_orphan").symlink_to(self.profile_root / "files/.bashrc_orphan")

        lines = self.extras()

        self.assertEqual(len(lines), 2)
        self.assertTrue(any(line.startswith("~/.config/gone.conf -> ") for line in lines))
        self.assertTrue(any(line.startswith("~/.bashrc_orphan -> ") for line in lines))
        self.assertTrue(all("no longer has that file" in line for line in lines))

    def test_an_orphan_three_levels_below_config_is_reported_and_nothing_is_removed(self) -> None:
        gone = self.profile_root / "files/.config/a/b/c/gone.conf"
        deep = self.home / ".config/a/b/c"
        deep.mkdir(parents=True)
        (deep / "gone.conf").symlink_to(gone)

        lines = self.extras()

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("~/.config/a/b/c/gone.conf -> "))
        self.assertTrue((deep / "gone.conf").is_symlink())  # reported, never removed

    def test_orphans_below_a_managed_directory_outside_config_are_reported(self) -> None:
        self.shared(".local/share/app/live.txt")
        nested = self.home / ".local/share/app/x/y"
        nested.mkdir(parents=True)
        (nested / "gone").symlink_to(self.profile_root / "files/.local/share/app/x/y/gone")
        elsewhere = self.home / ".local/share/other"
        elsewhere.mkdir(parents=True)
        (elsewhere / "gone").symlink_to(self.profile_root / "files/.local/share/other/gone")

        lines = self.extras()

        self.assertEqual(len(lines), 1)  # ~/.local/share/other was never managed, so it is not walked
        self.assertTrue(lines[0].startswith("~/.local/share/app/x/y/gone -> "))

    def test_caches_and_symlinked_directories_are_not_walked(self) -> None:
        gone = self.profile_root / "files/.config/gone"
        for name in ("Code/Cache/Cache_Data", "app/node_modules/pkg", "real"):
            (self.home / ".config" / name).mkdir(parents=True)
            (self.home / ".config" / name / "gone").symlink_to(gone)
        (self.home / ".config/linked").symlink_to(self.home / ".config/real")

        lines = self.extras()

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("~/.config/real/gone -> "))

    def test_a_clean_home_has_no_extras(self) -> None:
        self.shared(".config/kept.conf")
        self.plan().execute()

        self.assertEqual(self.extras(), [])


class BackupTests(TempDirCase):
    def test_second_backup_in_the_same_second_gets_a_suffix(self) -> None:
        original = write(self.tmp / "orig/f", "1")
        first = backup(original, now=NOW, state_home=self.tmp / "state")
        second = backup(original, now=NOW, state_home=self.tmp / "state")

        self.assertNotEqual(first, second)
        self.assertIn(f"{STAMP}.1", str(second))

    def test_directories_are_copied_with_links_preserved(self) -> None:
        write(self.tmp / "d/f", "1")
        (self.tmp / "d/l").symlink_to("f")
        copy = backup(self.tmp / "d", now=NOW, state_home=self.tmp / "state")

        self.assertEqual((copy / "f").read_text(encoding="utf-8"), "1")
        self.assertTrue((copy / "l").is_symlink())

    def test_missing_original_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            backup(self.tmp / "absent", now=NOW, state_home=self.tmp / "state")

    def test_a_dangling_symlink_is_stored_as_a_symlink(self) -> None:
        (self.tmp / "dangling").symlink_to(self.tmp / "gone")
        copy = backup(self.tmp / "dangling", now=NOW, state_home=self.tmp / "state")

        self.assertTrue(copy.is_symlink())
        self.assertEqual(os.readlink(copy), str(self.tmp / "gone"))

    def test_a_fifo_is_refused_instead_of_blocking_on_it(self) -> None:
        os.mkfifo(self.tmp / "pipe")
        with self.assertRaisesRegex(UbuntuTweakError, "not a regular file"):
            backup(self.tmp / "pipe", now=NOW, state_home=self.tmp / "state")


if __name__ == "__main__":
    unittest.main()

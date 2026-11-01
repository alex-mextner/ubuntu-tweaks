"""``leads_back_to`` and the loop refusal of ``replace_with_symlink``."""

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path

from ubuntu_tweaks.errors import RefusedError
from ubuntu_tweaks.link_loop import leads_back_to
from ubuntu_tweaks.symlink import replace_with_symlink
from tests.util import TempDirCase, write

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class LeadsBackToTests(TempDirCase):
    def test_a_plain_file_does_not_lead_back(self) -> None:
        source = write(self.tmp / "profile/monitors.xml", "<monitors/>")

        self.assertFalse(leads_back_to(source, self.tmp / "home/monitors.xml"))

    def test_the_destination_itself_is_a_loop(self) -> None:
        destination = self.tmp / "home/monitors.xml"

        self.assertTrue(leads_back_to(destination, destination))

    def test_a_direct_link_back_is_a_loop(self) -> None:
        destination = self.tmp / "home/monitors.xml"
        source = self.tmp / "profile/monitors.xml"
        source.parent.mkdir()
        source.symlink_to(destination)

        self.assertTrue(leads_back_to(source, destination))

    def test_a_relative_chain_of_links_back_is_a_loop(self) -> None:
        destination = self.tmp / "home/monitors.xml"
        middle = self.tmp / "profile/middle.xml"
        source = self.tmp / "profile/monitors.xml"
        source.parent.mkdir()
        middle.symlink_to("../home/monitors.xml")
        source.symlink_to("middle.xml")

        self.assertTrue(leads_back_to(source, destination))

    def test_a_chain_that_ends_in_a_real_file_is_fine(self) -> None:
        real = write(self.tmp / "store/monitors.xml")
        source = self.tmp / "profile/monitors.xml"
        source.parent.mkdir()
        source.symlink_to(real)

        self.assertFalse(leads_back_to(source, self.tmp / "home/monitors.xml"))

    def test_a_chain_of_forty_links_back_is_still_a_loop(self) -> None:
        destination = self.tmp / "home/monitors.xml"
        previous = destination
        for n in range(40):
            link = self.tmp / f"chain/{n}"
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(previous)
            previous = link

        self.assertTrue(leads_back_to(previous, destination))

    def test_a_parent_directory_alias_of_the_destination_is_seen(self) -> None:
        real = self.tmp / "config-real"
        real.mkdir()
        alias = self.tmp / ".config"
        alias.symlink_to(real)
        source = self.tmp / "profile/monitors.xml"
        source.parent.mkdir()
        source.symlink_to(real / "monitors.xml")

        self.assertTrue(leads_back_to(source, alias / "monitors.xml"))

    def test_dot_dot_after_a_directory_link_is_followed_through_the_link(self) -> None:
        elsewhere = self.tmp / "elsewhere/inner"
        elsewhere.mkdir(parents=True)
        (self.tmp / "alias").symlink_to(elsewhere)  # alias/.. is elsewhere, not tmp
        source = self.tmp / "profile/monitors.xml"
        source.parent.mkdir()
        source.symlink_to(self.tmp / "alias/../monitors.xml")

        self.assertTrue(leads_back_to(source, self.tmp / "elsewhere/monitors.xml"))
        self.assertFalse(leads_back_to(source, self.tmp / "monitors.xml"))

    def test_a_path_that_goes_through_a_directory_link_at_the_destination_is_a_loop(self) -> None:
        store = self.tmp / "store"
        (store / "child").mkdir(parents=True)
        config = self.tmp / "config"
        config.symlink_to(store)  # the destination is a directory link today
        source = self.tmp / "profile/config"
        source.parent.mkdir()
        source.symlink_to(config / "child")  # and the profile entry lives below it

        self.assertTrue(leads_back_to(source, config))

    def test_a_chain_longer_than_the_kernel_follows_counts_as_a_loop(self) -> None:
        destination = self.tmp / "home/monitors.xml"
        previous = destination
        for n in range(41):
            link = self.tmp / f"chain/{n}"
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(previous)
            previous = link

        self.assertTrue(leads_back_to(previous, destination))

    def test_a_cycle_elsewhere_terminates_and_is_refused_too(self) -> None:
        first, second = self.tmp / "a", self.tmp / "b"
        first.symlink_to(second)
        second.symlink_to(first)

        self.assertTrue(leads_back_to(first, self.tmp / "home/monitors.xml"))


class ReplaceWithSymlinkTests(TempDirCase):
    def test_a_source_that_links_back_is_refused_before_the_destination_is_touched(self) -> None:
        destination = write(self.tmp / "home/monitors.xml", "<monitors/>")
        source = self.tmp / "profile/monitors.xml"
        source.parent.mkdir()
        source.symlink_to(destination)

        with self.assertRaisesRegex(RefusedError, "leads back"):
            replace_with_symlink(destination, source, now=lambda: NOW)

        self.assertFalse(destination.is_symlink())
        self.assertEqual(destination.read_text(), "<monitors/>")

    def test_a_relative_source_is_read_from_the_destination_directory(self) -> None:
        destination = write(self.tmp / "home/monitors.xml", "<monitors/>")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        cwd = os.getcwd()
        os.chdir(elsewhere)  # not dest.parent: the guard must not read "monitors.xml" from here
        try:
            with self.assertRaisesRegex(RefusedError, "leads back"):
                replace_with_symlink(destination, Path("monitors.xml"), now=lambda: NOW)
        finally:
            os.chdir(cwd)

        self.assertFalse(destination.is_symlink())

    def test_a_healthy_source_is_linked_as_before(self) -> None:
        source = write(self.tmp / "profile/monitors.xml", "<monitors/>")
        destination = self.tmp / "home/monitors.xml"

        replace_with_symlink(destination, source, now=lambda: NOW)

        self.assertEqual(os.readlink(destination), str(source))
        self.assertEqual(destination.read_text(), "<monitors/>")


if __name__ == "__main__":
    unittest.main()

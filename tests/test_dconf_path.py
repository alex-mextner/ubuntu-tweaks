"""The dconf directory path predicate shared by the manifest parser and the settings backends."""

import unittest

from ubuntu_tweaks.dconf_path import is_dconf_dir_path


class DconfPathTests(unittest.TestCase):
    def test_accepts_slash_delimited_non_empty_components(self) -> None:
        for good in ("/a/", "/org/gnome/shell/", "/a/custom0/"):
            with self.subTest(path=good):
                self.assertTrue(is_dconf_dir_path(good))

    def test_rejects_everything_else(self) -> None:
        for bad in (
            "",
            "/",
            "//",
            "///",
            "//a/",
            "/a//",
            "///a///",
            "a/b/",
            "/a/b",
            "/a//b/",
            "/a/./b/",
            "/a/../b/",
            "a",
        ):
            with self.subTest(path=bad):
                self.assertFalse(is_dconf_dir_path(bad))

"""``TomlDoc``: comment- and order-preserving edits of packages.toml / system.toml (ticket #29)."""

import tomllib
import unittest

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.toml_edit import TomlDoc

COMMENTED = """\
# notes for myself
apt = ["tmux", "ripgrep"]  # shell tools
snap = [
  "firefox",  # the browser
  "code"
]

[keyboard]
layout = "us"  # keep
"""


class AppendTests(unittest.TestCase):
    def test_new_items_go_after_the_last_one_and_comments_stay(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "p")
        doc.append_to_array((), "apt", ["curl", "tmux"])
        doc.append_to_array((), "snap", ["spotify"])
        text = doc.text()
        self.assertIn('apt = ["tmux", "ripgrep", "curl"]  # shell tools', text)
        self.assertIn('  "firefox",  # the browser\n  "code",\n  "spotify",\n]', text)
        self.assertTrue(text.startswith("# notes for myself\n"))
        self.assertEqual(tomllib.loads(text)["snap"], ["firefox", "code", "spotify"])

    def test_a_missing_array_is_created_before_the_first_table(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "p")
        doc.append_to_array((), "flatpak", ["org.x.Y"])
        self.assertLess(doc.text().index("flatpak"), doc.text().index("[keyboard]"))

    def test_a_long_new_array_is_one_item_per_line(self) -> None:
        doc = TomlDoc.parse("", "p")
        doc.append_to_array((), "apt", [f"p{i}" for i in range(6)])
        self.assertIn('  "p0",\n', doc.text())
        self.assertEqual(tomllib.loads(doc.text())["apt"], [f"p{i}" for i in range(6)])

    def test_nothing_new_changes_nothing(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "p")
        doc.append_to_array((), "apt", ["tmux"])
        self.assertEqual(doc.text(), COMMENTED)

    def test_a_non_array_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            TomlDoc.parse('apt = "tmux"\n', "p").append_to_array((), "apt", ["x"])


class ScalarTests(unittest.TestCase):
    def test_set_string_replaces_in_place_keeping_the_trailing_comment(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "s")
        doc.set_string(("keyboard",), "layout", "us,ru")
        self.assertIn('layout = "us,ru"  # keep', doc.text())

    def test_new_keys_go_to_their_table_or_the_top_level_or_a_new_table(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "s")
        doc.set_string(("keyboard",), "model", "pc105")
        doc.set_string((), "timezone", "Europe/Belgrade")
        doc.set_string(("other", "deep"), "k", "v")
        data = tomllib.loads(doc.text())
        self.assertEqual(data["keyboard"], {"layout": "us", "model": "pc105"})
        self.assertEqual((data["timezone"], data["other"]["deep"]), ("Europe/Belgrade", {"k": "v"}))
        self.assertLess(doc.text().index("timezone"), doc.text().index("[keyboard]"))

    def test_a_new_top_level_key_does_not_steal_the_comment_of_the_first_table(self) -> None:
        doc = TomlDoc.parse('# two layouts\n[keyboard]\nlayout = "us"\n', "s")
        doc.set_string((), "locale", "en")
        self.assertEqual(doc.text(), 'locale = "en"\n\n# two layouts\n[keyboard]\nlayout = "us"\n')
        self.assertEqual(doc.key_reason(("keyboard",), "layout"), "")

    def test_a_comment_above_a_single_line_array_is_not_an_items_reason(self) -> None:
        doc = TomlDoc.parse('# notes for myself\napt = ["tmux", "ripgrep"]\n', "p")
        self.assertEqual(doc.item_reason((), "apt", "tmux"), "")

    def test_an_empty_array_that_only_holds_comments_keeps_them(self) -> None:
        doc = TomlDoc.parse("apt = [\n  # nothing yet, add the editors here\n]\n", "p")
        doc.append_to_array((), "apt", ["vim"])
        self.assertEqual(doc.text(), 'apt = [\n  # nothing yet, add the editors here\n  "vim",\n]\n')
        self.assertEqual(tomllib.loads(doc.text())["apt"], ["vim"])

    def test_insert_into_a_file_without_a_trailing_newline(self) -> None:
        doc = TomlDoc.parse('timezone = "A"', "s")
        doc.set_string((), "locale", "en")
        self.assertEqual(tomllib.loads(doc.text()), {"timezone": "A", "locale": "en"})


class ReasonTests(unittest.TestCase):
    def test_item_reason_reads_trailing_then_above(self) -> None:
        doc = TomlDoc.parse('apt = [\n  # for builds\n  "make",\n  "gcc",  # compiler\n]\n', "p")
        self.assertEqual(doc.item_reason((), "apt", "make"), "for builds")
        self.assertEqual(doc.item_reason((), "apt", "gcc"), "compiler")

    def test_set_item_comment_replaces_adds_and_removes(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "p")
        doc.set_item_comment((), "snap", "firefox", "daily driver")
        doc.set_item_comment((), "snap", "code", "editor")
        self.assertIn('"firefox",  # daily driver', doc.text())
        self.assertIn('"code"  # editor', doc.text())
        doc.set_item_comment((), "snap", "firefox", "")
        self.assertNotIn("daily driver", doc.text())
        self.assertEqual(tomllib.loads(doc.text())["snap"], ["firefox", "code"])

    def test_a_single_line_array_is_spread_to_take_a_reason(self) -> None:
        doc = TomlDoc.parse('apt = ["a", "b"]\n', "p")
        doc.set_item_comment((), "apt", "b", "because")
        self.assertEqual(doc.text(), 'apt = [\n  "a",\n  "b",  # because\n]\n')

    def test_spreading_an_array_that_has_comments_is_refused_so_none_is_lost(self) -> None:
        doc = TomlDoc.parse('apt = ["a", "b"]\nsnap = [\n  "x", "y",  # both\n]\n', "p")
        with self.assertRaises(ConfigError):
            doc.set_item_comment((), "snap", "y", "because")

    def test_key_comment_is_the_block_above_the_key(self) -> None:
        doc = TomlDoc.parse(COMMENTED, "s")
        doc.set_key_comment(("keyboard",), "layout", "two layouts")
        self.assertEqual(doc.key_reason(("keyboard",), "layout"), "two layouts")
        self.assertIn("[keyboard]\n# two layouts\nlayout", doc.text())
        doc.set_key_comment(("keyboard",), "layout", "")
        self.assertEqual(doc.key_reason(("keyboard",), "layout"), "")


class ScannerTests(unittest.TestCase):
    def test_strings_with_hashes_and_brackets_are_not_comments_or_array_ends(self) -> None:
        doc = TomlDoc.parse('apt = ["a#b", "c]d"]  # tail\n', "p")
        self.assertEqual(doc.array((), "apt"), ("a#b", "c]d"))
        doc.append_to_array((), "apt", ["e"])
        self.assertEqual(tomllib.loads(doc.text())["apt"], ["a#b", "c]d", "e"])

    def test_bad_toml_names_the_line(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"p:2:"):
            TomlDoc.parse('a = "x"\nb = "unterminated\n', "p")


if __name__ == "__main__":
    unittest.main()

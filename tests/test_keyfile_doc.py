"""``KeyfileDoc``: parsing with comments and least-text edits. Pure text, no PyGObject."""

import unittest

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.keyfile_doc import KeyfileDoc, comment_text, normalize_section

SAMPLE = """\
# free comment, not attached to anything

# why the theme
[org/gnome/desktop/interface]
# dark on purpose
# (the OLED panel)
gtk-theme='Yaru-dark'
clock-show-seconds = true

[org/gnome/mutter]
edge-tiling=false
"""


class ParseTests(unittest.TestCase):
    def test_entries_carry_the_comment_block_directly_above_them(self) -> None:
        doc = KeyfileDoc.parse(SAMPLE, "f.ini")
        theme = doc.find("org/gnome/desktop/interface", "gtk-theme")
        assert theme is not None
        self.assertEqual(comment_text(doc.reason(theme)), "dark on purpose (the OLED panel)")
        seconds = doc.find("org/gnome/desktop/interface", "clock-show-seconds")
        assert seconds is not None
        self.assertEqual((seconds.value, doc.reason(seconds)), ("true", ()))
        self.assertEqual(comment_text(doc.section_comment("org/gnome/desktop/interface")), "why the theme")

    def test_a_comment_separated_by_a_blank_line_belongs_to_nobody(self) -> None:
        doc = KeyfileDoc.parse("# lonely\n\n[s]\nk=1\n", "f.ini")
        entry = doc.find("s", "k")
        assert entry is not None
        self.assertEqual(doc.reason(entry), ())
        self.assertEqual(doc.section_comment("s"), ())

    def test_slashes_around_a_section_name_do_not_matter(self) -> None:
        doc = KeyfileDoc.parse("[/org/x/]\nk=1\n", "f.ini")
        self.assertIsNotNone(doc.find("org/x", "k"))
        self.assertEqual(normalize_section("/"), "/")

    def test_unchanged_document_round_trips_byte_for_byte(self) -> None:
        self.assertEqual(KeyfileDoc.parse(SAMPLE, "f").text(), SAMPLE)
        self.assertEqual(KeyfileDoc.parse("[a]\nk=1", "f").text(), "[a]\nk=1")

    def test_malformed_input_names_file_and_line(self) -> None:
        for text, line in (("k=1\n", 1), ("[a]\nnot a pair\n", 2), ("[a]\nk=1\nk=2\n", 3), ("[a]\nk=1\n[a]\nk=2\n", 4)):
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, rf"f\.ini:{line}:"):
                KeyfileDoc.parse(text, "f.ini")


class EditTests(unittest.TestCase):
    def test_set_value_rewrites_only_the_value_and_keeps_the_spacing(self) -> None:
        doc = KeyfileDoc.parse(SAMPLE, "f")
        doc.set_value("org/gnome/desktop/interface", "clock-show-seconds", "false")
        doc.set_value("org/gnome/desktop/interface", "gtk-theme", "'Yaru'")
        self.assertEqual(
            doc.text(),
            SAMPLE.replace("clock-show-seconds = true", "clock-show-seconds = false").replace("'Yaru-dark'", "'Yaru'"),
        )

    def test_add_appends_to_the_section_or_creates_it_at_the_end(self) -> None:
        doc = KeyfileDoc.parse(SAMPLE, "f")
        doc.add("org/gnome/mutter", "dynamic-workspaces", "true", ["# why"])
        doc.add("org/new", "k", "1")
        self.assertTrue(
            doc.text().endswith(
                "[org/gnome/mutter]\nedge-tiling=false\n# why\ndynamic-workspaces=true\n\n[org/new]\nk=1\n"
            )
        )

    def test_add_to_an_existing_key_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            KeyfileDoc.parse(SAMPLE, "f").add("org/gnome/mutter", "edge-tiling", "true")

    def test_remove_takes_the_key_with_its_comment_and_reports_both(self) -> None:
        doc = KeyfileDoc.parse(SAMPLE, "f")
        removed = doc.remove("org/gnome/desktop/interface", "gtk-theme")
        self.assertEqual(removed.value, "'Yaru-dark'")
        self.assertEqual(comment_text(removed.comment), "dark on purpose (the OLED panel)")
        self.assertIsNone(doc.find("org/gnome/desktop/interface", "gtk-theme"))
        self.assertIsNotNone(doc.find("org/gnome/desktop/interface", "clock-show-seconds"))

    def test_removing_the_last_key_removes_the_section_and_returns_its_comment(self) -> None:
        doc = KeyfileDoc.parse(SAMPLE, "f")
        doc.remove("org/gnome/desktop/interface", "gtk-theme")
        removed = doc.remove("org/gnome/desktop/interface", "clock-show-seconds")
        self.assertEqual(comment_text(removed.section_comment), "why the theme")
        self.assertEqual(
            doc.text(), "# free comment, not attached to anything\n\n[org/gnome/mutter]\nedge-tiling=false\n"
        )

    def test_add_merges_a_section_comment_without_losing_the_existing_one(self) -> None:
        doc = KeyfileDoc.parse("# mine\n[s]\na=1\n", "f")
        doc.add("s", "b", "2", section_comment=["# theirs"])
        self.assertEqual(doc.text(), "# mine\n# theirs\n[s]\na=1\nb=2\n")

    def test_set_comment_replaces_adds_and_removes_the_block(self) -> None:
        doc = KeyfileDoc.parse(SAMPLE, "f")
        doc.set_comment("org/gnome/desktop/interface", "gtk-theme", "new reason")
        self.assertIn("[org/gnome/desktop/interface]\n# new reason\ngtk-theme=", doc.text())
        self.assertNotIn("OLED", doc.text())
        doc.set_comment("org/gnome/mutter", "edge-tiling", "tile by hand")
        self.assertIn("# tile by hand\nedge-tiling=false", doc.text())
        doc.set_comment("org/gnome/mutter", "edge-tiling", "")
        self.assertNotIn("tile by hand", doc.text())


if __name__ == "__main__":
    unittest.main()

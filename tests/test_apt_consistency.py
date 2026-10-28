"""ubuntu_tweaks.apt_consistency: what a source file declares, and whether apt accepts the whole configured list.

The probe is run with a fake Process (the exact argv and the exit codes apt-get gave), plus one test against the real
apt-get on a staged tree that skips when apt-get is missing.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from ubuntu_tweaks.apt_consistency import (
    INLINE_KEY,
    apt_sources_error,
    declarations,
    normalize_uri,
    probe_command,
)
from ubuntu_tweaks.process import CommandResult, FakeProcess

KEY_A = "/etc/apt/keyrings/a.asc"
KEY_B = "/etc/apt/keyrings/b.asc"
CONFLICT = (
    "E: Conflicting values set for option Signed-By regarding source https://x.test/apt/ s: "
    f"{KEY_A} != {KEY_B}\nE: The list of sources could not be read.\n"
)


class Declarations(unittest.TestCase):
    def test_one_line_entry_reads_signed_by_from_the_option_block(self):
        found = declarations(Path("a.list"), f"deb [arch=amd64 signed-by={KEY_A}] https://x.test/apt/ s main\n")
        self.assertEqual([(d.uri, d.suite, d.signed_by) for d in found], [("https://x.test/apt", "s", KEY_A)])

    def test_one_line_entry_without_signed_by_has_none(self):
        found = declarations(Path("a.list"), "deb https://x.test/apt s main\ndeb-src https://x.test/apt s main\n")
        self.assertEqual([d.signed_by for d in found], [None, None])

    def test_option_block_may_be_spaced_and_commented_lines_are_ignored(self):
        text = (
            f"# deb https://x.test/apt s main\ndeb [ arch=amd64   signed-by={KEY_A} ] https://x.test/apt s main # c\n"
        )
        found = declarations(Path("a.list"), text)
        self.assertEqual([(d.suite, d.signed_by, d.active) for d in found], [("s", KEY_A, True)])

    def test_deb822_stanza_reads_signed_by_and_every_uri_suite_pair(self):
        text = f"Types: deb\nURIs: https://x.test/apt https://y.test/apt/\nSuites: s t\nSigned-By: {KEY_A}\n"
        found = declarations(Path("a.sources"), text)
        self.assertEqual(
            {(d.uri, d.suite, d.signed_by) for d in found},
            {(u, s, KEY_A) for u in ("https://x.test/apt", "https://y.test/apt") for s in "st"},
        )

    def test_inline_key_block_is_its_own_value_and_never_equals_a_path(self):
        text = (
            "Types: deb\nURIs: https://x.test/apt\nSuites: s\nSigned-By:\n"
            " -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n abcdef\n -----END PGP PUBLIC KEY BLOCK-----\n"
        )
        self.assertEqual([d.signed_by for d in declarations(Path("a.sources"), text)], [INLINE_KEY])

    def test_a_stanza_with_enabled_no_is_not_active(self):
        text = f"Types: deb\nURIs: https://x.test/apt\nSuites: s\nEnabled: no\nSigned-By: {KEY_A}\n"
        self.assertEqual([d.active for d in declarations(Path("a.sources"), text)], [False])

    def test_the_declaration_keeps_the_text_to_delete(self):
        text = f"deb https://other.test/apt s main\ndeb [signed-by={KEY_A}] https://x.test/apt s main\n"
        found = [d for d in declarations(Path("a.list"), text) if d.uri == "https://x.test/apt"]
        self.assertEqual(found[0].text, f"deb [signed-by={KEY_A}] https://x.test/apt s main")

    def test_a_deb822_declaration_keeps_the_whole_stanza_including_an_inline_key(self):
        text = (
            "Types: deb\nURIs: https://x.test/apt\nSuites: s\nSigned-By:\n"
            " -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n abcdef\n -----END PGP PUBLIC KEY BLOCK-----\n"
        )
        shown = declarations(Path("a.sources"), text)[0].text
        for needle in ("Types: deb", "Signed-By:", "-----BEGIN PGP PUBLIC KEY BLOCK-----", "abcdef", "-----END PGP"):
            self.assertIn(needle, shown)

    def test_a_stanza_with_several_repositories_is_edited_not_deleted(self):
        text = f"Types: deb\nURIs: https://x.test/apt https://y.test/apt\nSuites: s\nSigned-By: {KEY_A}\n"
        shown = {d.uri: d.text for d in declarations(Path("a.sources"), text)}
        self.assertIn("https://x.test/apt s out of the stanza", shown["https://x.test/apt"])
        self.assertIn("declares other repositories", shown["https://x.test/apt"])
        self.assertNotIn(
            "out of the", declarations(Path("b.sources"), "Types: deb\nURIs: https://z.test/a\nSuites: s\n")[0].text
        )

    def test_signed_by_list_is_normalized_to_commas(self):
        text = f"Types: deb\nURIs: https://x.test/apt\nSuites: s\nSigned-By: {KEY_A}\n {KEY_B}\n"
        self.assertEqual([d.signed_by for d in declarations(Path("a.sources"), text)], [f"{KEY_A},{KEY_B}"])

    def test_normalize_uri_drops_the_trailing_slash(self):
        self.assertEqual(normalize_uri("https://x.test/apt/"), "https://x.test/apt")


class Probe(unittest.TestCase):
    def test_a_conflict_returns_the_apt_error_once_per_line(self):
        process = FakeProcess({probe_command(None): CommandResult(100, "", CONFLICT + CONFLICT)})
        error = apt_sources_error(None, process)
        self.assertEqual(error, CONFLICT.strip())

    def test_a_consistent_tree_returns_none(self):
        process = FakeProcess(
            {probe_command(None): CommandResult(0, "", "W: Target Packages is configured multiple times")}
        )
        self.assertIsNone(apt_sources_error(None, process))

    def test_a_missing_apt_get_cannot_judge(self):
        self.assertIsNone(apt_sources_error(None, FakeProcess()))

    def test_a_failure_without_an_error_line_still_reports_something(self):
        process = FakeProcess({probe_command(None): CommandResult(1, "", "apt-get: something odd")})
        self.assertEqual(apt_sources_error(None, process), "apt-get: something odd")

    def test_a_timed_out_probe_cannot_judge(self):
        process = FakeProcess({probe_command(None): CommandResult(124, "", "apt-get: timed out after 60 s")})
        self.assertIsNone(apt_sources_error(None, process))

    def test_a_silent_failure_reports_the_exit_code(self):
        process = FakeProcess({probe_command(None): CommandResult(100, "", "")})
        self.assertEqual(apt_sources_error(None, process), "apt-get indextargets exited with 100")

    def test_the_command_is_unprivileged_c_locale_and_reads_the_real_tree_by_default(self):
        self.assertEqual(probe_command(None)[:4], ("env", "LC_ALL=C", "apt-get", "indextargets"))
        self.assertNotIn("-o", probe_command(None))

    def test_a_staged_root_points_apt_at_the_staged_files(self):
        command = probe_command(Path("/tmp/stage"))
        self.assertIn("Dir::Etc::sourcelist=/tmp/stage/etc/apt/sources.list", command)
        self.assertIn("Dir::Etc::sourceparts=/tmp/stage/etc/apt/sources.list.d", command)


@unittest.skipUnless(shutil.which("apt-get"), "apt-get is not installed")
class RealApt(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)

    def put(self, name, text):
        (self.root / "etc/apt/sources.list.d" / name).write_text(text)

    def test_a_consistent_staged_tree_is_accepted(self):
        self.put(
            "a.sources", f"Types: deb\nURIs: https://x.test/apt\nSuites: s\nComponents: main\nSigned-By: {KEY_A}\n"
        )
        self.put("b.list", f"deb [signed-by={KEY_A}] https://x.test/apt s main\n")
        self.assertIsNone(apt_sources_error(self.root))

    def test_two_different_signed_by_for_one_repository_are_rejected(self):
        self.put(
            "a.sources", f"Types: deb\nURIs: https://x.test/apt\nSuites: s\nComponents: main\nSigned-By: {KEY_A}\n"
        )
        self.put("b.list", f"deb [signed-by={KEY_B}] https://x.test/apt s main\n")
        error = apt_sources_error(self.root)
        self.assertIn("Conflicting values set for option Signed-By", error or "")
        self.assertEqual((error or "").count("Conflicting values"), 1)


if __name__ == "__main__":
    unittest.main()

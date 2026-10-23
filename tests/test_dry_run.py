"""``--dry-run`` on every command: the explicit spelling of the default preview (spec, *Dry run*)."""

import contextlib
import io
import sys
import unittest
from unittest import mock

from tests.test_cli import DROPPED_COMMAND, run_main
from tests.util import TempDirCase, write
from ubuntu_tweaks import commands
from ubuntu_tweaks.cli import build_parser, discover_commands, main
from ubuntu_tweaks.identity import Location

REQUIRED = {"note": ["target", "reason"], "bundle-move": ["bundle", "key"]}  # positionals with no default
COMMIT_WORD = {"apply": "--commit", "init": "--commit", "update": "--commit", "capture": "--commit"}
COMMIT_WORD |= {"bundle-move": "--commit", "note": "--commit", "doctor": "--yes", "finish": "--yes"}


class DryRunFlagTests(unittest.TestCase):
    def test_every_command_accepts_it_before_and_after_the_command_name(self) -> None:
        names = [module.__name__.rsplit(".", 1)[1].replace("_", "-") for module in discover_commands()]
        self.assertGreaterEqual(len(names), 10)
        for name in names:
            with self.subTest(command=name):
                after = build_parser().parse_args([name, *REQUIRED.get(name, []), "--dry-run"])
                before = build_parser().parse_args(["--dry-run", name, *REQUIRED.get(name, [])])
                self.assertTrue(after.dry_run)
                self.assertTrue(before.dry_run)
                self.assertFalse(build_parser().parse_args([name, *REQUIRED.get(name, [])]).dry_run)

    def test_there_is_no_fix_command_any_more(self) -> None:
        names = [module.__name__.rsplit(".", 1)[1] for module in discover_commands()]
        self.assertNotIn("fix", names)


class DryRunRunTests(TempDirCase):
    def drop_commands(self, **files: str) -> None:
        directory = self.tmp / "commands_drop"
        for name, source in files.items():
            write(directory / f"{name}.py", source)
        commands.__path__.append(str(directory))
        self.addCleanup(commands.__path__.remove, str(directory))
        for name in files:
            self.addCleanup(sys.modules.pop, f"ubuntu_tweaks.commands.{name}", None)

    def test_it_says_so_on_stderr_and_keeps_the_exit_code_of_the_preview(self) -> None:
        self.drop_commands(dropped=DROPPED_COMMAND)
        plain = run_main("dropped")
        dry = run_main("dropped", "--dry-run")
        self.assertEqual((plain[0], dry[0]), (7, 7))
        self.assertEqual(dry[1], plain[1])  # stdout is byte-identical: the same preview
        self.assertNotIn("dry run", plain[2])
        self.assertIn("dry run: nothing was changed", dry[2])
        self.assertNotIn("dry run", dry[1])  # stdout stays clean for --json and friends

    def test_the_commit_word_and_dry_run_exclude_each_other_with_exit_2(self) -> None:
        for command, word in COMMIT_WORD.items():
            with self.subTest(command=command):
                code, out, err = run_main(command, *REQUIRED.get(command, []), "--dry-run", word)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(f"--dry-run and {word} exclude each other", err)

    def test_a_dry_run_does_not_write_the_dotfiles_discovery_cache(self) -> None:
        self.drop_commands(dropped=DROPPED_COMMAND)
        for flags, persist in (([], True), (["--dry-run"], False)):
            with (
                self.subTest(flags=flags),
                mock.patch("ubuntu_tweaks.identity.locate", return_value=Location(self.tmp / "x")) as locate,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                main(["dropped", *flags])
            self.assertIs(locate.call_args.kwargs["persist"], persist)


if __name__ == "__main__":
    unittest.main()

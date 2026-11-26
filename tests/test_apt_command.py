"""The apt command the engine builds (pure: nothing is executed) and the best-effort report of kept conffiles."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ubuntu_tweaks.apt_command import conffile_note, install_command, kept_conffiles, noninteractive_arguments


class InstallCommandTests(unittest.TestCase):
    def test_the_command_line_is_noninteractive_and_keeps_local_conffiles(self) -> None:
        self.assertEqual(
            install_command(["zsh", "7zip"]),
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "NEEDRESTART_MODE=a",
                "NEEDRESTART_SUSPEND=1",
                "APT_LISTCHANGES_FRONTEND=none",
                "apt-get",
                "install",
                "-y",
                "-o",
                "Dpkg::Options::=--force-confdef",
                "-o",
                "Dpkg::Options::=--force-confold",
                "zsh",
                "7zip",
            ],
        )

    def test_the_variables_travel_inside_the_command_so_sudo_cannot_scrub_them(self) -> None:
        command = install_command(["zsh"])

        self.assertEqual(command[0], "env")  # elevated as `sudo -- env ...`: the variables are set after sudo
        self.assertLess(command.index("DEBIAN_FRONTEND=noninteractive"), command.index("apt-get"))

    def test_a_modified_conffile_is_never_replaced_silently(self) -> None:
        command = install_command(["zsh"])

        self.assertIn("Dpkg::Options::=--force-confold", command)
        self.assertNotIn("Dpkg::Options::=--force-confnew", command)


class NoninteractiveArgumentsTests(unittest.TestCase):
    def test_an_install_or_upgrade_gets_the_dpkg_options_after_its_subcommand(self) -> None:
        for subcommand in ("install", "upgrade", "dist-upgrade", "full-upgrade"):
            with self.subTest(subcommand=subcommand):
                self.assertEqual(
                    noninteractive_arguments([subcommand, "-y", "x"]),
                    [
                        subcommand,
                        "-o",
                        "Dpkg::Options::=--force-confdef",
                        "-o",
                        "Dpkg::Options::=--force-confold",
                        "-y",
                        "x",
                    ],
                )

    def test_options_before_the_subcommand_do_not_hide_it(self) -> None:
        for arguments in (["-y", "install", "x"], ["--no-install-recommends", "build-dep", "x"], ["satisfy", "x"]):
            with self.subTest(arguments=arguments):
                self.assertIn("Dpkg::Options::=--force-confold", noninteractive_arguments(arguments))

    def test_an_option_with_its_own_value_does_not_hide_the_subcommand(self) -> None:
        for arguments in (["-o", "Acquire::Retries=3", "install", "x"], ["-t", "noble-backports", "install", "x"]):
            with self.subTest(arguments=arguments):
                result = noninteractive_arguments(arguments)
                self.assertEqual(result[:3], [*arguments[:3]])
                self.assertIn("Dpkg::Options::=--force-confold", result)

    def test_the_end_of_options_marker_is_left_in_front_of_its_operands(self) -> None:
        self.assertEqual(noninteractive_arguments(["--", "install", "x"]), ["--", "install", "x"])

    def test_commands_that_run_no_dpkg_are_left_alone(self) -> None:
        for arguments in (["update"], ["download", "earlyoom"], ["indextargets"], []):
            with self.subTest(arguments=arguments):
                self.assertEqual(noninteractive_arguments(arguments), arguments)


class KeptConffilesTests(unittest.TestCase):
    LOG = (
        "2026-11-26 09:00:00 conffile /etc/old/kept.conf keep\n"
        "2026-11-26 10:00:00 startup packages configure\n"
        "2026-11-26 10:00:05 status half-configured zsh:amd64 5.9\n"
        "2026-11-26 10:00:06 conffile /etc/zsh/zshrc keep\n"
        "2026-11-26 10:00:06 conffile /etc/zsh/zshrc keep\n"
        "2026-11-26 10:00:07 conffile /etc/zsh/zprofile install\n"
        "2026-11-26 10:00:08 conffile /etc/other.conf keep\n"
    )

    def kept(self, text: str | None, since: datetime) -> list[str]:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "dpkg.log"
            if text is not None:
                log.write_text(text, encoding="utf-8")
            return kept_conffiles(since, log)

    def test_only_conffiles_kept_since_the_run_started_are_named_once_in_log_order(self) -> None:
        self.assertEqual(self.kept(self.LOG, datetime(2026, 11, 26, 10, 0, 0)), ["/etc/zsh/zshrc", "/etc/other.conf"])

    def test_the_previous_batch_a_second_earlier_is_not_reported_again(self) -> None:
        self.assertEqual(self.kept(self.LOG, datetime(2026, 11, 26, 10, 0, 9, 300000)), [])

    def test_an_older_run_is_not_reported(self) -> None:
        self.assertEqual(self.kept(self.LOG, datetime(2026, 11, 26, 11, 0, 0)), [])

    def test_a_missing_log_is_no_information(self) -> None:
        self.assertEqual(self.kept(None, datetime(2026, 11, 26, 10, 0, 0)), [])


class ConffileNoteTests(unittest.TestCase):
    def test_the_note_names_the_kept_file(self) -> None:
        note = conffile_note("/etc/zsh/zshrc")

        self.assertTrue(note.startswith("apt: kept your /etc/zsh/zshrc"), note)
        self.assertNotIn("\n", note)


if __name__ == "__main__":
    unittest.main()

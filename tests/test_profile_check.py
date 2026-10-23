"""``ubuntu-tweak coverage --check-profile``: the static gate of a profile's coverage rules (spec, *Coverage*).

No machine is scanned and PyGObject is not needed: every test runs with ``gi`` made unimportable, and the tree on
disk is compared before and after to prove nothing is written.
"""

import argparse
import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks import profile_check
from ubuntu_tweaks.cli import main
from ubuntu_tweaks.commands import coverage as coverage_command
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import ITEM_PREFIXES
from ubuntu_tweaks.errors import ConfigError, ExitCode
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile

UNTRACKED = '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "untracked"\nreason = "later"\nticket = "{ticket}"\n'


class CheckCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.profile = Profile(self.tmp / "profile", "box")
        self.ctx = Context(self.repo, self.profile)

    def module(self, name: str, coverage: str) -> None:
        write(
            self.repo / "modules" / name / "module.toml",
            f'description = "{name}"\n[source]\nkind = "builtin"\n{coverage}',
        )

    def profile_rules(self, text: str) -> None:
        write(self.profile.root / "coverage.toml", text)

    def check(self, *, online: bool = False, process: FakeProcess | None = None) -> profile_check.Report:
        return profile_check.check(self.ctx, online=online, process=process or FakeProcess())

    def problems(self, **kwargs: object) -> list[str]:
        return list(self.check(**kwargs).problems)  # type: ignore[arg-type]


class StaticRuleTests(CheckCase):
    def test_a_profile_with_no_rules_of_its_own_is_clean(self) -> None:
        report = self.check()
        self.assertEqual(report.problems, ())
        self.assertGreater(report.rules, 20)  # the engine's own rules were read

    def test_the_engine_defaults_pass_their_own_gate(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_an_untracked_rule_without_a_ticket_is_a_problem_naming_file_and_entry(self) -> None:
        self.profile_rules('[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "untracked"\nreason = "later"\n')
        (problem,) = self.problems()
        self.assertIn("coverage.toml [[coverage]] #1", problem)
        self.assertIn("an untracked rule needs a 'ticket'", problem)

    def test_an_excluded_or_secret_rule_without_a_reason_is_a_problem(self) -> None:
        self.profile_rules(
            '[[coverage]]\nmatch = ["file:~/.config/a"]\nclass = "excluded"\n'
            '[[coverage]]\nmatch = ["file:~/.config/b"]\nclass = "secret"\n'
        )
        found = self.problems()
        self.assertEqual(len(found), 2, found)
        self.assertIn("an excluded rule needs a 'reason'", found[0])
        self.assertIn("a secret rule needs a 'reason'", found[1])

    def test_every_file_is_reported_not_just_the_first_broken_one(self) -> None:
        self.profile_rules('[[coverage]]\nmatch = ["file:~/.config/a"]\nclass = "excluded"\n')
        write(
            self.profile.overlay_dir / "coverage.toml",
            '[[coverage]]\nmatch = ["file:~/.config/b"]\nclass = "untracked"\nreason = "r"\n',
        )
        found = self.problems()
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("machines/box/coverage.toml" in line for line in found))

    def test_a_malformed_ticket_or_a_ticket_on_another_class_is_a_problem(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="83"))
        self.assertIn("'ticket' must be #N or owner/repo#N", self.problems()[0])
        self.profile_rules('[[coverage]]\nmatch = ["a"]\nclass = "excluded"\nreason = "r"\nticket = "#1"\n')
        self.assertIn("'ticket' is only for an untracked rule", self.problems()[0])

    def test_a_disabled_module_claiming_a_pattern_is_not_a_duplicate_of_an_enabled_one(self) -> None:
        rule = '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "tracked"\n'
        self.module("one", rule)
        write(
            self.repo / "modules/two/module.toml",
            f'description = "two"\nenabled = false\n[source]\nkind = "builtin"\n{rule}',
        )
        self.assertEqual(self.problems(), [])

    def test_the_overlay_of_another_machine_is_checked_whatever_the_hostname(self) -> None:
        write(
            self.profile.root / "machines/laptop/coverage.d/10-x.toml",
            '[[coverage]]\nmatch = ["~/.config/foo"]\nclass = "excluded"\nreason = "r"\n',
        )
        (problem,) = self.problems()
        self.assertIn("machines/laptop/coverage.d/10-x.toml [[coverage]] #1", problem)

    def test_a_profile_rule_that_beats_an_intermediate_override_is_not_redundant(self) -> None:
        self.module("foo", '[[coverage]]\nmatch = ["file:~/.cache"]\nclass = "tracked"\n')
        self.profile_rules('[[coverage]]\nmatch = ["file:~/.cache"]\nclass = "excluded"\nreason = "mine"\n')
        self.assertEqual(self.problems(), [])

    def test_a_directory_named_like_a_drop_in_is_ignored(self) -> None:
        (self.profile.root / "coverage.d/odd.toml").mkdir(parents=True)
        self.assertEqual(self.problems(), [])

    def test_drop_in_files_are_checked_and_named_in_the_problem(self) -> None:
        write(
            self.profile.root / "coverage.d/30-x.toml",
            '[[coverage]]\nmatch = ["file:~/.config/a"]\nclass = "untracked"\nreason = "r"\n',
        )
        (problem,) = self.problems()
        self.assertIn("coverage.d/30-x.toml [[coverage]] #1", problem)

    def test_a_well_formed_ticketed_rule_is_clean(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="octo/profile-repo#83"))
        self.assertEqual(self.problems(), [])

    def test_a_toml_syntax_error_is_a_problem_not_a_traceback(self) -> None:
        self.profile_rules("[[coverage]\nmatch =")
        (problem,) = self.problems()
        self.assertIn("coverage.toml", problem)

    def test_an_unknown_top_level_key_is_a_problem(self) -> None:
        self.profile_rules('colour = "red"\n')
        self.assertIn("unknown top-level key", self.problems()[0])

    def test_a_broken_module_manifest_is_a_problem(self) -> None:
        self.module("vscode", '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "excluded"\n')
        (problem,) = self.problems()
        self.assertIn("needs a 'reason'", problem)

    def test_a_disabled_modules_rules_are_checked_too(self) -> None:
        write(
            self.repo / "modules/vscode/module.toml",
            'description = "v"\nenabled = false\n[source]\nkind = "builtin"\n'
            '[[coverage]]\nmatch = ["nonsense"]\nclass = "tracked"\n',
        )
        self.assertIn("nonsense", self.problems()[0])


class MatchTests(CheckCase):
    def check_match(self, *patterns: str) -> list[str]:
        listed = ", ".join(f'"{pattern}"' for pattern in patterns)
        self.profile_rules(f'[[coverage]]\nmatch = [{listed}]\nclass = "excluded"\nreason = "r"\n')
        return self.problems()

    def test_patterns_of_every_known_scheme_are_fine(self) -> None:
        samples = (
            "dconf:/org/gnome/*",
            "file:~/.config/Code",
            "etc:/etc/default/*",
            "apt:my-*",
            "snap:core*",
            "flatpak:org.*",
            "brew:jq",
            "pipx:ruff",
            "uv:httpie",
            "npm:left-pad",
            "bun:prettier",
            "cargo:ripgrep",
            "unit:foo.service",
            "autostart:x.desktop",
            "extension:mine@me",
            "cron:*",
            "ssh:*",
            "git:*",
        )
        self.assertEqual(self.check_match(*samples), [])

    def test_a_leading_wildcard_or_a_partial_scheme_can_still_match(self) -> None:
        self.assertEqual(self.check_match("*", "*Cache*", "fi*", "file:*", "d*"), [])

    def test_the_prefixes_cover_every_adapter_scheme(self) -> None:
        schemes = (
            "dconf",
            "file",
            "etc",
            "apt",
            "snap",
            "flatpak",
            "brew",
            "pipx",
            "uv",
            "npm",
            "bun",
            "cargo",
            "disk",
        )
        for scheme in schemes:
            self.assertTrue(any(prefix.startswith(scheme + ":") for prefix in ITEM_PREFIXES), scheme)

    def test_patterns_that_can_never_match_any_item_are_flagged(self) -> None:
        for bad in (
            "~/.config/foo",  # no scheme
            "file:/home/me/.config/foo",  # file ids are file:~/...
            "file:.config/foo",
            "etc:etc/default/grub",  # etc ids are etc:/etc/...
            "dconf:org/gnome/x",  # dconf ids are dconf:/org/...
            "deb:curl",  # unknown scheme
            "foo",  # no colon, not a prefix of a scheme
            "APT:curl",  # schemes are lowercase, fnmatchcase
        ):
            found = self.check_match(bad)
            self.assertEqual(len(found), 1, bad)
            self.assertIn("can never match", found[0], bad)
            self.assertIn(bad, found[0])

    def test_the_message_names_the_rule_and_the_right_form(self) -> None:
        (problem,) = self.check_match("~/.config/foo")
        self.assertIn("coverage.toml [[coverage]] #1", problem)
        self.assertIn("file:~/", problem)


class DuplicateTests(CheckCase):
    def test_the_same_pattern_twice_in_one_layer_is_flagged_with_both_places(self) -> None:
        self.profile_rules(
            '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "excluded"\nreason = "a"\n'
            '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "secret"\nreason = "b"\n'
        )
        (problem,) = self.problems()
        self.assertIn("file:~/.config/foo", problem)
        self.assertIn("#1", problem)
        self.assertIn("#2", problem)
        self.assertIn("never applies", problem)

    def test_the_same_pattern_twice_inside_one_match_list_is_flagged(self) -> None:
        self.profile_rules(
            '[[coverage]]\nmatch = ["file:~/.config/foo", "file:~/.config/foo"]\nclass = "excluded"\nreason = "a"\n'
        )
        self.assertEqual(len(self.problems()), 1)

    def test_two_modules_claiming_the_same_pattern_are_flagged(self) -> None:
        rule = '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "tracked"\n'
        self.module("one", rule)
        self.module("two", rule)
        (problem,) = self.problems()
        self.assertIn("module one", problem)
        self.assertIn("module two", problem)

    def test_a_higher_layer_restating_a_lower_one_with_the_same_class_is_redundant(self) -> None:
        self.module("vscode", '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "tracked"\n')
        self.profile_rules('[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "tracked"\n')
        (problem,) = self.problems()
        self.assertIn("redundant", problem)
        self.assertIn("module vscode", problem)

    def test_a_higher_layer_changing_the_class_is_a_real_override_and_is_fine(self) -> None:
        self.module("vscode", '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "tracked"\n')
        self.profile_rules('[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "excluded"\nreason = "mine"\n')
        self.assertEqual(self.problems(), [])

    def test_the_profile_restating_an_engine_rule_of_the_same_class_is_redundant(self) -> None:
        self.profile_rules(
            '[[coverage]]\nmatch = ["file:~/.cache"]\nclass = "excluded"\n'
            'reason = "cache: rebuilt by the tool that owns it"\n'
        )
        (problem,) = self.problems()
        self.assertIn("redundant", problem)
        self.assertIn("engine", problem)

    def test_the_same_class_with_the_real_reason_is_an_override_not_a_repeat(self) -> None:
        self.profile_rules(
            '[[coverage]]\nmatch = ["file:~/.ssh"]\nclass = "secret"\n'
            'reason = "SSH keys; restore: from the keychain, then ssh-add"\n'
        )
        self.assertEqual(self.problems(), [])

    def test_a_ticketed_rule_with_its_own_ticket_overrides_a_lower_one(self) -> None:
        self.module("m", UNTRACKED.format(ticket="octo/mod#3"))
        self.profile_rules(UNTRACKED.format(ticket="#42"))
        self.assertEqual(self.problems(), [])


class OnlineTests(CheckCase):
    def gh(self, *argv: str, code: int = 0, out: str = "", err: str = "") -> dict[tuple[str, ...], CommandResult]:
        return {("gh", "issue", "view", *argv): CommandResult(code, out, err)}

    def test_offline_never_asks_gh_and_says_the_tickets_were_not_verified(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12"))
        process = FakeProcess()
        report = self.check(process=process)
        self.assertEqual(process.queries, [])
        self.assertEqual(report.problems, ())
        self.assertTrue(any("--online" in note for note in report.notes), report.notes)

    def test_an_open_ticket_is_fine(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12"))
        process = FakeProcess(self.gh("12", "--json", "state", out='{"state": "OPEN"}'))
        self.assertEqual(self.check(online=True, process=process).problems, ())
        self.assertEqual(process.queries, [("gh", "issue", "view", "12", "--json", "state")])

    def test_a_ticket_of_another_repository_is_asked_with_repo(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="octo/cat#7"))
        process = FakeProcess(self.gh("7", "--repo", "octo/cat", "--json", "state", out='{"state": "OPEN"}'))
        self.assertEqual(self.check(online=True, process=process).problems, ())

    def test_a_closed_ticket_is_a_problem(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12"))
        process = FakeProcess(self.gh("12", "--json", "state", out='{"state": "CLOSED"}'))
        (problem,) = self.problems(online=True, process=process)
        self.assertIn("#12", problem)
        self.assertIn("closed", problem)

    def test_a_missing_ticket_is_a_problem(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#999"))
        process = FakeProcess(
            self.gh("999", "--json", "state", code=1, err="GraphQL: Could not resolve to an issue or pull request")
        )
        (problem,) = self.problems(online=True, process=process)
        self.assertIn("#999", problem)
        self.assertIn("does not exist", problem)

    def test_gh_failing_for_another_reason_is_a_problem_that_says_so(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12"))
        process = FakeProcess(self.gh("12", "--json", "state", code=4, err="gh: To get started with GitHub CLI"))
        (problem,) = self.problems(online=True, process=process)
        self.assertIn("cannot verify #12", problem)

    def test_without_gh_the_online_check_is_skipped_and_says_so(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12"))
        report = self.check(online=True, process=FakeProcess())  # every command answers 127
        self.assertEqual(report.problems, ())
        self.assertTrue(any("gh is not installed" in note for note in report.notes), report.notes)

    def test_a_ticket_used_by_several_rules_is_asked_once(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12") + UNTRACKED.format(ticket="#12").replace("foo", "bar"))
        process = FakeProcess(self.gh("12", "--json", "state", out='{"state": "OPEN"}'))
        self.check(online=True, process=process)
        self.assertEqual(len(process.queries), 1)


class CommandTests(CheckCase):
    def run_command(self, **overrides: object) -> tuple[int, str]:
        values: dict[str, object] = {
            "json": False,
            "coverage_class": None,
            "propose": False,
            "check_profile": True,
            "online": False,
        }
        values.update(overrides)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(sys.modules, {"gi": None}):
            code = coverage_command.run(self.ctx, argparse.Namespace(**values))
        return code, out.getvalue()

    def test_clean_exits_0_and_says_so_without_scanning_the_machine_or_importing_gi(self) -> None:
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("no problems", out)

    def test_invalid_exits_2_and_lists_every_problem(self) -> None:
        self.profile_rules('[[coverage]]\nmatch = ["~/x"]\nclass = "untracked"\nreason = "r"\n')
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.INVALID_CONFIG)
        self.assertIn("needs a 'ticket'", out)

    def test_nothing_is_written(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#1"))
        before = sorted(str(path) for path in self.tmp.rglob("*"))
        self.run_command()
        self.assertEqual(sorted(str(path) for path in self.tmp.rglob("*")), before)

    def test_json_has_problems_notes_and_the_exit_code(self) -> None:
        self.profile_rules(UNTRACKED.format(ticket="#12"))
        code, out = self.run_command(json=True)
        document = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(document), ["exit_code", "notes", "problems", "profile", "rules", "tickets"])
        self.assertEqual((document["problems"], document["tickets"], document["exit_code"]), ([], ["#12"], 0))

    def test_online_without_check_profile_is_invalid_config(self) -> None:
        with self.assertRaisesRegex(ConfigError, "--online needs --check-profile"):
            self.run_command(check_profile=False, online=True)

    def test_the_parser_registers_both_flags(self) -> None:
        from ubuntu_tweaks.cli import build_parser

        args = build_parser().parse_args(["coverage", "--check-profile", "--online"])
        self.assertTrue(args.check_profile and args.online)

    def test_main_runs_it_end_to_end_under_the_cli_entry(self) -> None:
        write(self.profile.root / "profile.toml", "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(sys.modules, {"gi": None}):
            code = main(["--profile", str(self.profile.root), "--plain", "coverage", "--check-profile"])
        self.assertEqual(code, ExitCode.OK, out.getvalue())


if __name__ == "__main__":
    unittest.main()

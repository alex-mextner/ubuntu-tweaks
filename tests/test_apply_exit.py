"""``apply_exit.explain``: what ``finish`` tells the person when ``apply --commit`` ends with 7, 6 or 3."""

import unittest

from ubuntu_tweaks import apply_exit, ui
from ubuntu_tweaks.finish import ExitReport
from ubuntu_tweaks.journal import Entry, Run

POLKIT = (
    "passwords-app-polkit: sudo -- /x/modules/passwords-app-polkit/passwords-app-polkit install: exited with status 1: "
    "passwords-app-polkit: refusing /home/u/dev/passwords-app/data/polkit/io.github.Passwords.policy: "
    "a DOCTYPE or entity could hide actions from this check"
)
PASSKEY = (
    "firefox-passkey-qr: env UBUNTU_TWEAK_MODULE_CONFIG={} /x/firefox-passkey-qr install: exited with status 1: "
    "ed. / Full log: /home/u/.local/state/passkey-qr/x.log / firefox-passkey-qr: /home/u/passkey-qr --yes install: "
    "exited with status 1: command failed (exit 100): sudo apt-get update…"
)


def flat(report: ExitReport) -> str:
    """The text on one line: the terminal wraps it, a test must not care where."""
    return " ".join(report.text.split())


def rerun(subcommand: str) -> str:
    return f"ubuntu-tweak {subcommand}"


def entry(key: str, status: str, detail: str = "", text: str = "some action") -> Entry:
    kind = key.split("|")[0]
    return Entry(key, kind, text, status, detail)


def journal(*entries: Entry, code: int | None = 7, finished: bool = True) -> Run:
    return Run("1", "apply --commit", 0.0, finished, code, list(entries))


def module(name: str) -> str:
    return f"system-module|{name}|system-module:{name}"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        ui.configure(color="never", plain=True, lang="en")
        self.addCleanup(ui.configure)

    def explain(self, code: int, last: Run | None, *, log: str = "", command: str = "finish-setup", since: float = 0.0):
        return apply_exit.explain(code, last, rerun=rerun, command=command, log=log, since=since)


class PartialTests(Case):
    def partial(self) -> Run:
        return journal(
            *[entry(f"link||l{n}", "done") for n in range(16)],
            entry(module("passwords-app-polkit"), "failed", POLKIT),
            entry(module("firefox-passkey-qr"), "failed", PASSKEY),
            *[entry(f"system-module|m{n}|system-module:m{n}", "manual", "waits") for n in range(6)],
            entry("package||apt: install x", "pending"),
        )

    def test_it_says_how_many_changed_and_failed(self) -> None:
        report = self.explain(7, self.partial())
        assert report is not None
        self.assertIn("16 changed, 2 failed, 0 blocked", flat(report))

    def test_every_failed_item_has_its_module_its_cause_and_the_exact_retry_command(self) -> None:
        report = self.explain(7, self.partial())
        assert report is not None
        self.assertIn(
            "passwords-app-polkit: refusing /home/u/dev/passwords-app/data/polkit/"
            "io.github.Passwords.policy: a DOCTYPE or entity could hide actions from this check",
            flat(report),
        )
        self.assertNotIn("passwords-app-polkit: passwords-app-polkit", flat(report))
        self.assertIn("ubuntu-tweak apply --commit --only passwords-app-polkit", flat(report))
        self.assertIn("firefox-passkey-qr: command failed (exit 100): sudo apt-get update…", flat(report))
        self.assertIn("ubuntu-tweak apply --commit --only firefox-passkey-qr", flat(report))

    def test_the_cause_is_the_text_after_the_last_exit_status_and_never_the_module_config(self) -> None:
        report = self.explain(7, self.partial())
        assert report is not None
        self.assertNotIn("UBUNTU_TWEAK_MODULE_CONFIG", flat(report))
        self.assertNotIn("Full log", flat(report))

    def test_the_waiting_items_are_counted_and_the_remaining_steps_are_pointed_at(self) -> None:
        report = self.explain(7, self.partial())
        assert report is not None
        self.assertIn("6 more wait for you", flat(report))
        self.assertIn("The other steps go on", flat(report))
        self.assertIn("finish-setup", flat(report))

    def test_the_failed_step_detail_points_at_the_list_and_has_no_bare_number(self) -> None:
        report = self.explain(7, self.partial())
        assert report is not None
        self.assertIn("listed above", report.problem)
        self.assertNotIn("7", report.problem)

    def test_a_failure_that_is_not_a_module_run_names_the_action_and_retries_the_whole_apply(self) -> None:
        last = journal(entry("package||apt: install x", "failed", "apt: install x: exited with status 100: E: boom"))
        report = self.explain(7, last)
        assert report is not None
        self.assertIn("E: boom", flat(report))
        self.assertIn("[package] some action", flat(report))
        self.assertIn("retry: ubuntu-tweak apply --commit", flat(report))
        self.assertNotIn("--only", flat(report))

    def test_a_blocked_item_is_listed_with_what_it_waits_for(self) -> None:
        last = journal(entry(module("firefox"), "blocked", "system-module run passwords-app"))
        report = self.explain(7, last)
        assert report is not None
        self.assertIn("0 changed, 0 failed, 1 blocked", flat(report))
        self.assertIn("firefox: waits for system-module run passwords-app", flat(report))

    def test_a_long_cause_is_clipped(self) -> None:
        report = self.explain(7, journal(entry(module("m"), "failed", "m: x: exited with status 1: " + "z" * 600)))
        assert report is not None
        longest = max(len(line) for line in report.text.splitlines())
        self.assertLess(longest, 260)
        self.assertIn("…", flat(report))

    def test_only_the_first_line_of_a_cause_is_shown(self) -> None:
        report = self.explain(7, journal(entry(module("m"), "failed", "first line\nsecond line")))
        assert report is not None
        self.assertIn("first line", flat(report))
        self.assertNotIn("second line", flat(report))

    def test_russian_text(self) -> None:
        ui.configure(color="never", plain=True, lang="ru")
        report = self.explain(7, self.partial())
        assert report is not None
        self.assertIn("изменено 16", flat(report))
        self.assertIn("не удалось 2", flat(report))
        self.assertIn("ubuntu-tweak apply --commit --only passwords-app-polkit", flat(report))
        self.assertNotIn("завершилась с кодом", flat(report) + report.problem)

    def test_the_global_flags_the_user_gave_are_kept_in_the_retry_command(self) -> None:
        report = apply_exit.explain(
            7,
            journal(entry(module("m"), "failed", "m: boom")),
            rerun=lambda sub: f"ubuntu-tweak {sub} --machine box",
            command="finish-setup",
        )
        assert report is not None
        self.assertIn("ubuntu-tweak apply --commit --only m --machine box", flat(report))

    def test_a_journal_of_another_run_is_not_blamed_for_this_exit(self) -> None:
        for stale in (None, journal(code=0), journal(code=7, finished=False)):
            with self.subTest(stale=stale):
                self.assertIsNone(self.explain(7, stale))
                report = self.explain(7, stale, log="/tmp/x.log")
                assert report is not None
                self.assertIn("/tmp/x.log", report.problem)

    def test_escapes_in_a_journal_key_or_kind_are_not_replayed(self) -> None:
        report = self.explain(
            7,
            journal(
                entry("system-module|m\x1b]0;owned\x07x|y", "failed", "boom"),
                Entry("odd\x1b[2J|a|b", "odd\x1b[2J", "t", "failed", "boom"),
            ),
        )
        assert report is not None
        self.assertNotIn("\x1b", report.text)
        self.assertNotIn("\x07", report.text)
        self.assertIn("--only", flat(report))

    def test_a_tool_that_repeats_the_phrase_keeps_its_own_words(self) -> None:
        detail = "m: /x/m install: exited with status 1: helper exited with status 2"
        report = self.explain(7, journal(entry(module("m"), "failed", detail)))
        assert report is not None
        self.assertIn("helper exited with status 2", flat(report))
        self.assertNotIn("no output", flat(report))

    def test_a_journal_with_a_wrong_typed_start_is_not_blamed_and_does_not_crash(self) -> None:
        run = journal(entry(module("m"), "failed", "m: boom"))
        run.started = None  # type: ignore[assignment]
        self.assertIsNone(self.explain(7, run))

    def test_a_command_that_never_ran_does_not_show_the_module_options(self) -> None:
        detail = 'm: env UBUNTU_TWEAK_MODULE_CONFIG={"k":1} /x/m install: No such file or directory'
        report = self.explain(7, journal(entry(module("m"), "failed", detail)))
        assert report is not None
        self.assertNotIn("UBUNTU_TWEAK_MODULE_CONFIG", flat(report))
        self.assertIn("session log has the details", flat(report))

    def test_the_finish_command_is_printed_without_control_characters(self) -> None:
        report = self.explain(7, journal(entry(module("m"), "failed", "m: boom")), command="finish\x1b[2J-setup")
        assert report is not None
        self.assertNotIn("\x1b", report.text)
        self.assertIn("finish[2J-setup", flat(report))

    def test_a_journal_older_than_this_apply_is_not_blamed(self) -> None:
        old = journal(entry(module("m"), "failed", "m: boom"))  # started at 0.0
        self.assertIsNotNone(self.explain(7, old, since=0.0))
        self.assertIsNone(self.explain(7, old, since=100.0))

    def test_a_tool_that_said_nothing_never_shows_the_module_config(self) -> None:
        for detail in (
            "m: env UBUNTU_TWEAK_MODULE_CONFIG={} /x/m install: exited with status 1:",
            "m: env UBUNTU_TWEAK_MODULE_CONFIG={} /x/m install: exited with status 1",
        ):
            with self.subTest(detail=detail):
                report = self.explain(7, journal(entry(module("m"), "failed", detail)))
                assert report is not None
                self.assertNotIn("UBUNTU_TWEAK_MODULE_CONFIG", flat(report))
                self.assertIn("exited with status 1, no output", flat(report))

    def test_terminal_escapes_in_a_journal_line_are_not_replayed(self) -> None:
        hostile = "\x1b]0;owned\x07m: x: exited with status 1: boom\x1b[2J"
        report = self.explain(7, journal(entry(module("m"), "failed", hostile), entry("link||x", "failed", "a\x1bb")))
        assert report is not None
        self.assertNotIn("\x1b", report.text)
        self.assertNotIn("\x07", report.text)
        self.assertIn("boom", flat(report))

    def test_a_module_name_is_quoted_in_the_retry_command(self) -> None:
        report = self.explain(7, journal(entry("system-module|a b;c|x", "failed", "boom")))
        assert report is not None
        self.assertIn("--only 'a b;c'", flat(report))

    def test_a_failed_item_without_detail_is_just_its_name(self) -> None:
        report = self.explain(7, journal(entry(module("m"), "failed", "")))
        assert report is not None
        self.assertIn("m", flat(report))
        self.assertNotIn("m: ", flat(report))


class RootAndDriftTests(Case):
    def test_exit_6_says_root_was_not_obtained_and_which_command_to_run_from_a_normal_terminal(self) -> None:
        report = self.explain(6, None)
        assert report is not None
        self.assertIn("root was not obtained", flat(report))
        self.assertIn("not applied", flat(report))
        self.assertIn("ubuntu-tweak apply --commit", flat(report))
        self.assertNotIn("6", report.problem)

    def test_exit_3_says_the_settings_did_not_converge_and_points_at_status(self) -> None:
        report = self.explain(3, None)
        assert report is not None
        self.assertIn("did not converge", flat(report))
        self.assertIn("ubuntu-tweak apply --commit", flat(report))
        self.assertIn("ubuntu-tweak status", flat(report))

    def test_russian_exits(self) -> None:
        ui.configure(color="never", plain=True, lang="ru")
        for code, word in ((6, "root"), (3, "не сошлись")):
            with self.subTest(code=code):
                report = self.explain(code, None)
                assert report is not None
                self.assertIn(word, flat(report))


class OtherCodeTests(Case):
    def test_another_code_keeps_the_generic_text_and_names_the_session_log(self) -> None:
        report = self.explain(2, None, log="/home/u/.local/state/ubuntu-tweaks/logs/x-finish.log")
        assert report is not None
        self.assertEqual(report.text, "")
        self.assertIn("The step's command exited with 2.", report.problem)
        self.assertIn("/home/u/.local/state/ubuntu-tweaks/logs/x-finish.log", report.problem)

    def test_another_code_without_a_known_log_is_left_to_the_generic_path(self) -> None:
        self.assertIsNone(self.explain(2, None))


if __name__ == "__main__":
    unittest.main()

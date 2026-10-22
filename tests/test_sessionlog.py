"""The session log: the tee at the CLI entry, its text rules, masking, retention and the never-break-the-command rule."""

import contextlib
import io
import os
import signal
import stat
import sys
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from tests.util import TempDirCase, no_dotfiles_discovery
from ubuntu_tweaks import cli, sessionlog, ui
from ubuntu_tweaks.capture.secrets import mask_secrets
from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.errors import RefusedError
from ubuntu_tweaks.process import CommandError, Process

# Shaped like real secrets so the masker fires, assembled here so no scanner flags this file.
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" + "k1L2m3N4o5P6"
JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJ" + "zdWIiOiIxMjM0" + "." + "SflKxwRJSMeKKF2QT4"
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"
BASE64 = "dGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgc2VjcmV0IHZhbHVlIQ" + "1AbCd"


class TerminalTextTests(unittest.TestCase):
    def test_escape_sequences_are_removed(self) -> None:
        raw = "\x1b[1;31mred\x1b[0m \x1b]8;;https://x\x07link\x1b]8;;\x07 \x1b[2K\x1b(Bdone\x07"

        self.assertEqual(sessionlog.strip_terminal_text(raw), "red link done")

    def test_a_carriage_return_redraw_keeps_the_last_state(self) -> None:
        self.assertEqual(sessionlog.strip_terminal_text("10%\r50%\r100%"), "100%")
        self.assertEqual(sessionlog.strip_terminal_text("windows line\r"), "windows line")
        self.assertEqual(sessionlog.strip_terminal_text("long text\r\x1b[Kshort"), "short")


class MaskSecretsTests(unittest.TestCase):
    def test_tokens_jwts_and_base64_runs_become_stars(self) -> None:
        self.assertEqual(mask_secrets(f"token {TOKEN} end"), "token *** end")
        self.assertEqual(mask_secrets(f"Authorization: {JWT}"), "Authorization: ***")
        self.assertEqual(mask_secrets(f"key={BASE64}"), "key=***")

    def test_url_credentials_keep_the_scheme_and_host(self) -> None:
        self.assertEqual(
            mask_secrets("clone https://bob:hunter2@github.com/x/y.git"), "clone https://***@github.com/x/y.git"
        )

    def test_a_pem_block_is_masked_across_lines(self) -> None:
        text = f"before\n{PEM_BEGIN}\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n{PEM_END}\nafter"

        self.assertEqual(mask_secrets(text), "before\n***\n***\n***\nafter")

    def test_what_is_not_a_secret_stays(self) -> None:
        text = "me@example.com 192.168.1.20 4b825dc642cb6eb9a060e54bf8d69288fbee4904 /home/a/Pictures/b/c/d.png"

        self.assertEqual(mask_secrets(text), text)

    def test_the_value_after_a_secret_flag_is_masked_in_argv(self) -> None:
        argv = ["apply", "--token", "abc", "--api-password=def", "--only", "x", "--secret", "--commit"]

        self.assertEqual(
            sessionlog.masked_argv(argv),
            ["apply", "--token", "***", "--api-password=***", "--only", "x", "--secret", "--commit"],
        )

    def test_a_value_that_starts_with_a_dash_is_masked_too(self) -> None:
        argv = ["--password", "-hunter2", "--commit"]

        self.assertEqual(sessionlog.masked_argv(argv), ["--password", "***", "--commit"])


class LogCase(TempDirCase):
    """Runs things inside ``run_logged`` against a log directory under the test's temp dir."""

    def setUp(self) -> None:
        super().setUp()
        self.logs = self.tmp / "state" / "logs"
        patcher = mock.patch.dict(os.environ, {sessionlog.LOG_DIR_ENV: str(self.logs)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(sessionlog.SESSION_ENV, None)

    def logged(
        self, command: Callable[[], int], argv: tuple[str, ...] = ("status",)
    ) -> tuple[int | None, str, str, str]:
        """(code, what the terminal got on stdout, on stderr, the newest log's text); ``code`` is None when it raised."""
        out, err = io.StringIO(), io.StringIO()
        code: int | None = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with contextlib.suppress(BaseException):
                code = sessionlog.run_logged(argv, command)
        return code, out.getvalue(), err.getvalue(), self.text()

    def files(self) -> list[Path]:
        return sorted(p for p in self.logs.glob("*.log") if p.name != sessionlog.LATEST)

    def text(self) -> str:
        latest = self.logs / sessionlog.LATEST
        return latest.read_text(encoding="utf-8") if latest.exists() else ""


class SessionTextTests(LogCase):
    def test_stdout_and_stderr_are_logged_in_order_with_stderr_marked(self) -> None:
        def command() -> int:
            print("first")
            print("second", file=sys.stderr)
            print("third")
            return 3

        code, out, err, log = self.logged(command, ("status", "--plain"))

        self.assertEqual((code, out, err), (3, "first\nthird\n", "second\n"))
        body = log.split("\n")
        self.assertEqual(body[body.index("first") : body.index("first") + 3], ["first", "! second", "third"])

    def test_header_and_footer(self) -> None:
        _, _, _, log = self.logged(lambda: 4, ("apply", "--token", "hunter2", "--commit"))

        lines = log.splitlines()
        self.assertEqual(lines[0], "# session")
        self.assertIn("# argv: ubuntu-tweak apply --token '***' --commit", lines)
        self.assertNotIn("hunter2", log)
        for prefix in ("# started: ", "# host: ", "# cwd: ", "# engine: ", "# python: "):
            self.assertTrue(any(line.startswith(prefix) for line in lines), prefix)
        end = next(line for line in lines if line.startswith("# end: "))
        self.assertTrue(end.endswith(" s, exit 4"), end)

    def test_escapes_and_progress_redraws_are_not_in_the_log_but_reach_the_terminal(self) -> None:
        def command() -> int:
            sys.stdout.write("\x1b[32mok\x1b[0m\n")
            for percent in (10, 50, 100):
                sys.stdout.write(f"\rprogress {percent}%")
            sys.stdout.write("\nlast line without newline")
            return 0

        _, out, _, log = self.logged(command)

        self.assertIn("\x1b[32mok", out)
        self.assertIn("ok\nprogress 100%\nlast line without newline\n# end", log)
        self.assertNotIn("\x1b", log)
        self.assertNotIn("progress 10%", log)

    def test_secrets_are_masked_in_the_log_only(self) -> None:
        def command() -> int:
            print(f"token {TOKEN}")
            print("pull https://bob:hunter2@host.example/x.git", file=sys.stderr)
            return 0

        _, out, err, log = self.logged(command)

        self.assertIn(TOKEN, out)
        self.assertIn("hunter2", err)
        self.assertIn("token ***", log)
        self.assertIn("! pull https://***@host.example/x.git", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("hunter2", log)

    def test_the_footer_carries_what_the_run_learned(self) -> None:
        def command() -> int:
            sessionlog.note("profile", "/home/a/.files/ubuntu")
            return 0

        self.assertIn("# profile: /home/a/.files/ubuntu", self.logged(command)[3])

    def test_a_newline_in_a_noted_value_cannot_forge_footer_lines(self) -> None:
        log = self.logged(lambda: (sessionlog.note("profile", "/x\n# exit 0"), 0)[1])[3]

        self.assertIn("# profile: /x # exit 0", log)

    def test_many_tiny_writes_and_a_long_word_next_to_a_url_stay_fast(self) -> None:
        def command() -> int:
            for _ in range(300000):
                sys.stdout.write(".")
            sys.stdout.write("\n" + "a" * 200000 + " see https://example.org/x\n")
            return 0

        started = time.monotonic()
        log = self.logged(command)[3]

        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("see https://example.org/x", log)

    def test_a_pem_block_is_masked_even_when_the_other_stream_speaks_in_between(self) -> None:
        def command() -> int:
            print(PEM_BEGIN)
            print("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7")
            print(f"{PEM_END} (a certificate footer from another tool)", file=sys.stderr)
            print("c2VjcmV0LWtleS1ib2R5")
            print(PEM_END)
            return 0

        log = self.logged(command)[3]

        self.assertNotIn("MIIEvQ", log)
        self.assertNotIn("c2VjcmV0", log)

    def test_a_pem_begin_that_never_ends_stops_hiding_lines_after_a_bound(self) -> None:
        lines = [PEM_BEGIN, *(f"body {number}" for number in range(300)), "visible again"]

        masked = mask_secrets("\n".join(lines)).splitlines()

        self.assertEqual(masked[1], "***")
        self.assertEqual(masked[-1], "visible again")

    def test_one_endless_line_is_cut_into_pieces_instead_of_recopied_on_every_write(self) -> None:
        def command() -> int:
            for _ in range(5):
                sys.stdout.write("a" * 200000)
            return 0

        log = self.logged(command)[3]

        pieces = [line for line in log.splitlines() if line.startswith("a")]
        self.assertGreater(len(pieces), 1)
        self.assertEqual(sum(len(piece) for piece in pieces), 1000000)

    def test_the_cut_of_an_endless_line_never_splits_a_token(self) -> None:
        def command() -> int:
            sys.stdout.write("b" * 262140 + " " + TOKEN[:20])
            sys.stdout.write(TOKEN[20:] + "\n")
            return 0

        log = self.logged(command)[3]

        self.assertNotIn(TOKEN[:20], log)
        self.assertNotIn(TOKEN[20:], log)

    def test_many_threads_writing_at_once_keep_every_line_whole(self) -> None:
        def command() -> int:
            def spam(number: int) -> None:
                for index in range(200):
                    sys.stdout.write(f"thread {number} line {index} " + "x" * 50 + "\n")

            workers = [threading.Thread(target=spam, args=(number,)) for number in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            return 0

        log = self.logged(command)[3]

        whole = [line for line in log.splitlines() if line.startswith("thread ")]
        self.assertEqual(len(whole), 1600)
        self.assertTrue(all(line.endswith("x" * 50) for line in whole))


class TeeTests(LogCase):
    def test_the_tee_is_the_real_stream_for_every_other_attribute(self) -> None:
        class Terminal(io.StringIO):
            fake_tty = True

        terminal = Terminal()
        seen: list[bool] = []

        def command() -> int:
            seen.extend(
                [
                    sys.stdout is not terminal,
                    sys.stdout.isatty() == terminal.isatty(),
                    ui.rich(sys.stdout) == ui.rich(terminal),
                ]
            )
            seen.append(sys.stdout.encoding == terminal.encoding)
            return 0

        before = sys.stdout
        with contextlib.redirect_stdout(terminal), contextlib.redirect_stderr(io.StringIO()):
            sessionlog.run_logged(("status",), command)

        self.assertEqual(seen, [True, True, True, True])
        self.assertIs(sys.stdout, before)

    def test_the_streams_are_restored_afterwards(self) -> None:
        before = (sys.stdout, sys.stderr)

        self.logged(lambda: 0)

        self.assertEqual((sys.stdout, sys.stderr), before)


class FilesTests(LogCase):
    def test_modes_names_and_the_latest_symlink(self) -> None:
        def command() -> int:
            sessionlog.set_command("status")
            return 0

        self.logged(command)

        (path,) = self.files()
        self.assertRegex(path.name, r"^\d{8}T\d{6}Z-status\.log$")
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        latest = self.logs / "latest.log"
        self.assertTrue(latest.is_symlink())
        self.assertEqual(os.readlink(latest), path.name)

    def test_without_a_command_the_name_says_none_and_a_taken_name_gets_a_number(self) -> None:
        with mock.patch.object(sessionlog, "_timestamp", return_value="20261022T183012Z"):
            self.logged(lambda: 0)
            self.logged(lambda: 0)
            self.logged(lambda: (sessionlog.set_command("apply"), 0)[1])
            self.logged(lambda: (sessionlog.set_command("apply"), 0)[1])

        self.assertEqual(
            [path.name for path in self.files()],
            [
                "20261022T183012Z-apply-2.log",
                "20261022T183012Z-apply.log",
                "20261022T183012Z-none-2.log",
                "20261022T183012Z-none.log",
            ],
        )
        self.assertEqual(os.readlink(self.logs / "latest.log"), "20261022T183012Z-apply-2.log")

    def test_only_the_newest_fifty_logs_are_kept(self) -> None:
        self.logs.mkdir(parents=True)
        for number in range(60):
            (self.logs / f"2020010{1 + number // 30}T0000{number % 30:02d}Z-old.log").write_text("old")
        (self.logs / "notes.txt").write_text("mine")
        (self.logs / "20200101T000000Z-dir.log.d").mkdir()

        self.logged(lambda: 0)

        kept = sorted(p.name for p in self.logs.glob("*.log") if p.name != "latest.log")
        self.assertEqual(len(kept), sessionlog.KEEP_LOGS)
        self.assertNotIn("old", kept[-1])  # the new log sorts last
        self.assertNotIn("20200101T000000Z-old.log", kept)
        self.assertTrue((self.logs / "notes.txt").exists())
        self.assertTrue((self.logs / "20200101T000000Z-dir.log.d").is_dir())
        self.assertEqual(os.readlink(self.logs / "latest.log"), self.files()[-1].name)

    def test_the_directory_comes_from_the_state_home(self) -> None:
        home = Path("/home/someone")

        self.assertEqual(sessionlog.log_dir({}, home), home / ".local/state/ubuntu-tweaks/logs")
        self.assertEqual(sessionlog.log_dir({"XDG_STATE_HOME": "/x/state"}, home), Path("/x/state/ubuntu-tweaks/logs"))
        self.assertEqual(
            sessionlog.log_dir({sessionlog.LOG_DIR_ENV: "/seam", "XDG_STATE_HOME": "/x"}, home), Path("/seam")
        )


class OutcomeTests(LogCase):
    def test_a_usage_error_is_logged_as_none_with_the_usage_text(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            cli.main(["--no-such-flag"])

        self.assertEqual(raised.exception.code, 2)
        (path,) = self.files()
        log = path.read_text(encoding="utf-8")
        self.assertTrue(path.name.endswith("-none.log"))
        self.assertIn("! usage: ubuntu-tweak", log)
        self.assertIn("exit 2", log)
        self.assertIn("usage: ubuntu-tweak", err.getvalue())

    def test_help_is_logged_with_exit_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        log = self.text()
        self.assertIn("\nusage: ubuntu-tweak", log)
        self.assertTrue(any(line.endswith("exit 0") for line in log.splitlines()))

    def test_a_run_of_a_command_is_named_after_it_and_records_the_profile(self) -> None:
        profile = self.tmp / "profile"
        profile.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), no_dotfiles_discovery():
            code = cli.main(["machine", "--profile", str(profile), "--machine", "m1", "--plain"])

        (path,) = self.files()
        log = path.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(path.name.endswith("-machine.log"))
        self.assertIn("machine:  m1 (from --machine)", log)
        self.assertIn(f"# profile: {profile}", log)
        self.assertIn("# machine: m1", log)
        self.assertIn("# argv: ubuntu-tweak machine --profile", log)

    def test_a_handled_error_is_logged_with_its_exit_code(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(cli, "_resolve_profile", side_effect=RefusedError("secret finding")),
        ):
            code = cli.main(["machine"])

        log = self.text()
        self.assertEqual(code, 5)
        self.assertIn("! ", log)
        self.assertIn("secret finding", log)
        self.assertIn("exit 5", log)

    def test_an_unexpected_exception_logs_its_traceback_and_type(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(cli, "_resolve_profile", side_effect=RuntimeError("boom")),
        ):
            code = cli.main(["machine"])

        log = self.text()
        self.assertEqual(code, 1)
        self.assertIn("! Traceback (most recent call last):", log)
        self.assertIn("! RuntimeError: boom", log)
        self.assertIn("# exception: RuntimeError", log)
        self.assertIn("exit 1", log)

    def test_ctrl_c_is_logged_as_interrupted_with_exit_130_and_still_propagates(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(cli, "_resolve_profile", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            cli.main(["machine"])

        log = self.text()
        self.assertIn("# interrupted (Ctrl-C)", log)
        self.assertIn("exit 130", log)

    def test_an_exception_that_escapes_is_logged_and_propagates(self) -> None:
        def command() -> int:
            raise ValueError("escaped")

        code, _, err, log = self.logged(command)

        self.assertIsNone(code)
        self.assertIn("! ValueError: escaped", log)
        self.assertIn("# exception: ValueError", log)
        self.assertEqual(err.count("ValueError: escaped"), 0)  # the interpreter prints it once it propagates

    @unittest.skipUnless(threading.current_thread() is threading.main_thread(), "signal handlers need the main thread")
    def test_sigterm_is_logged_as_terminated_and_the_default_action_is_restored(self) -> None:
        def command() -> int:
            signal.raise_signal(signal.SIGTERM)
            return 0

        code, _, _, log = self.logged(command)

        self.assertIsNone(code)
        self.assertIn("# terminated (SIGTERM)", log)
        self.assertIn("exit 143", log)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class NestedRunTests(LogCase):
    def test_a_run_exports_its_log_to_children_and_takes_it_back(self) -> None:
        seen: list[str] = []

        def command() -> int:
            seen.append(os.environ[sessionlog.SESSION_ENV])
            return 0

        self.logged(command)

        (path,) = self.files()
        self.assertEqual(seen, [str(path)])
        self.assertNotIn(sessionlog.SESSION_ENV, os.environ)

    def test_the_variable_names_the_final_name_after_set_command(self) -> None:
        seen: list[str] = []

        def command() -> int:
            sessionlog.set_command("update")
            seen.append(os.environ[sessionlog.SESSION_ENV])
            return 0

        self.logged(command)

        self.assertEqual(seen, [str(self.files()[0])])

    def test_a_process_that_finds_the_variable_writes_no_log_of_its_own(self) -> None:
        parent = self.tmp / "parent.log"
        parent.write_text("")
        inside: list[object] = []
        with mock.patch.dict(os.environ, {sessionlog.SESSION_ENV: str(parent)}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = sessionlog.run_logged(("apply",), lambda: (inside.append(sys.stdout), 0)[1])

        self.assertEqual((code, inside, self.logs.exists()), (0, [out], False))

    def test_a_variable_naming_an_unwritable_place_does_not_suppress_the_log(self) -> None:
        with mock.patch.dict(os.environ, {sessionlog.SESSION_ENV: str(self.tmp / "gone" / "parent.log")}):
            code, _, _, log = self.logged(lambda: 0)

        self.assertEqual(code, 0)
        self.assertIn("# session", log)

    def test_a_real_child_ubuntu_tweak_logs_into_the_parents_file_only(self) -> None:
        script = "import sys; from ubuntu_tweaks.cli import main; sys.exit(main(['--help']))"

        def command() -> int:
            return Process().execute([sys.executable, "-B", "-c", script], cwd=REPO_ROOT) or 0

        code, out, _, log = self.logged(command)

        self.assertEqual(len(self.files()), 1)
        self.assertIn("usage: ubuntu-tweak", out)
        self.assertIn("\nusage: ubuntu-tweak", log)
        self.assertEqual(code, 0)


class PromptTests(LogCase):
    def run_with_input(self, typed: str, command: Callable[[], int]) -> tuple[str, str]:
        """(what reached stdout, the log) of ``command`` reading ``typed`` from a fake terminal input."""
        with mock.patch.object(sys, "stdin", io.StringIO(typed)):
            _, out, _, log = self.logged(command)
        return out, log

    def test_a_question_and_the_typed_answer_are_one_line(self) -> None:
        answers: list[str] = []

        def command() -> int:
            answers.append(input("Install foo? [y/N] "))
            return 0

        out, log = self.run_with_input("y\n", command)

        self.assertEqual((answers, out), (["y"], "Install foo? [y/N] "))
        self.assertIn("\n> Install foo? [y/N] y\n", log)
        self.assertEqual(log.count("Install foo?"), 1)

    def test_every_read_form_is_logged(self) -> None:
        def command() -> int:
            sys.stdout.write("Name: ")
            sys.stdin.readline()
            list(zip(range(1), sys.stdin))  # iteration
            sys.stdin.read()
            return 0

        _, log = self.run_with_input("alice\nbob\ncarol\n", command)

        self.assertIn("\n> Name: alice\n> bob\n> carol\n", log)

    def test_a_masked_answer_does_not_mask_the_next_ordinary_one(self) -> None:
        answers: list[str] = []

        def command() -> int:
            input("Password: ")
            answers.append(input("Name: "))
            return 0

        _, log = self.run_with_input("hunter2\nalice\n", command)

        self.assertIn("> Password: ***", log)
        self.assertIn("> Name: alice", log)

    def test_a_secret_asked_for_on_the_line_before_the_prompt_is_masked(self) -> None:
        def command() -> int:
            print("Enter your GitHub token:")
            input("> ")
            return 0

        _, log = self.run_with_input("hunter2\n", command)

        self.assertNotIn("hunter2", log)

    def test_a_getpass_style_prompt_on_stderr_is_masked(self) -> None:
        def command() -> int:
            sys.stderr.write("Password: ")
            sys.stdin.readline()
            return 0

        _, log = self.run_with_input("hunter2\n", command)

        self.assertNotIn("hunter2", log)

    def test_characters_read_one_at_a_time_become_one_line(self) -> None:
        def command() -> int:
            sys.stdout.write("Continue? ")
            while sys.stdin.read(1) != "\n":
                pass
            return 0

        _, log = self.run_with_input("yes\n", command)

        self.assertIn("\n> Continue? yes\n", log)
        self.assertNotIn("\n> y\n", log)

    def test_the_answer_to_a_password_or_secret_prompt_is_masked_whatever_it_looks_like(self) -> None:
        _, log = self.run_with_input("hunter2\n", lambda: (input("Wi-Fi password: "), 0)[1])

        self.assertNotIn("hunter2", log)
        self.assertIn("> Wi-Fi password: ***", log)

    def test_an_answer_that_looks_like_a_secret_is_masked(self) -> None:
        _, log = self.run_with_input(TOKEN + "\n", lambda: (input("Token: "), 0)[1])

        self.assertNotIn(TOKEN, log)
        self.assertIn("> Token: ***", log)

    def test_end_of_input_raises_as_before_and_logs_nothing_typed(self) -> None:
        def command() -> int:
            with self.assertRaises(EOFError):
                input("Q? ")
            return 0

        _, log = self.run_with_input("", command)

        self.assertNotIn("\n> ", log)

    def test_the_prompt_is_flushed_to_the_terminal_before_blocking_on_the_answer(self) -> None:
        events: list[str] = []

        class Terminal(io.StringIO):
            def flush(self) -> None:
                events.append("flush")

        class Keyboard(io.StringIO):
            def readline(self, size: int = -1) -> str:
                events.append("read")
                return super().readline(size)

        def command() -> int:
            sys.stdout.write("Name: ")
            sys.stdin.readline()
            return 0

        with mock.patch.object(sys, "stdin", Keyboard("alice\n")), contextlib.redirect_stdout(Terminal()):
            sessionlog.run_logged(("status",), command)

        self.assertEqual(events, ["flush", "read"])

    def test_the_tee_reports_the_terminal_but_has_no_descriptor(self) -> None:
        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        seen: list[bool] = []

        def command() -> int:
            seen.append(sys.stdin.isatty())
            with self.assertRaises(io.UnsupportedOperation):
                sys.stdin.fileno()
            return 0

        with mock.patch.object(sys, "stdin", Terminal("")):
            self.logged(command)

        self.assertEqual(seen, [True])

    def test_stdin_is_restored_and_outside_a_logged_run_nothing_happens(self) -> None:
        before = sys.stdin
        self.logged(lambda: 0)
        sessionlog.note("k", "v")
        sessionlog.set_command("x")

        self.assertIs(sys.stdin, before)


class ChildOutputTests(LogCase):
    def test_what_a_child_prints_reaches_the_log_and_the_failure_still_raises(self) -> None:
        script = "import sys; print('child out'); print('child err', file=sys.stderr); sys.exit(7)"
        raised: list[CommandError] = []

        def command() -> int:
            try:
                Process().execute([sys.executable, "-c", script])
            except CommandError as error:
                raised.append(error)
                return 7
            return 0

        code, out, err, log = self.logged(command)

        self.assertEqual((code, out, err), (7, "child out\n", "child err\n"))
        self.assertIn("\nchild out\n", log)  # the two pipes are read by two threads: no order between streams
        self.assertIn("\n! child err\n", log)
        self.assertEqual([error.returncode for error in raised], [7])
        self.assertIn("exited with status 7", str(raised[0]))


class NeverBreaksTheCommandTests(LogCase):
    def test_a_filesystem_without_hard_links_keeps_the_provisional_name_and_keeps_logging(self) -> None:
        def command() -> int:
            sessionlog.set_command("status")
            print("after the rename that could not happen")
            return 0

        with mock.patch("os.link", side_effect=PermissionError(1, "Operation not permitted")):
            code, _, err, log = self.logged(command)

        (path,) = self.files()
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(path.name.endswith("-none.log"))
        self.assertIn("after the rename that could not happen", log)

    def test_a_filesystem_without_symlinks_still_writes_the_log(self) -> None:
        with mock.patch("pathlib.Path.symlink_to", side_effect=OSError(1, "Operation not permitted")):
            code, _, err, _ = self.logged(lambda: 0)

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.files()), 1)

    def test_a_failing_write_switches_logging_off_with_one_warning(self) -> None:
        def command() -> int:
            for number in range(5):
                print(f"line {number}")
            return 9

        with mock.patch.object(sessionlog._Sink, "_line", side_effect=OSError(28, "No space left on device")):
            code, out, err, _ = self.logged(command)

        self.assertEqual((code, out), (9, "".join(f"line {n}\n" for n in range(5))))
        self.assertEqual(err.count("cannot write the session log"), 1)
        self.assertIn("No space left on device", err)

    def test_a_log_that_cannot_be_created_is_one_warning_and_the_command_runs(self) -> None:
        blocker = self.tmp / "file"
        blocker.write_text("not a directory")
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {sessionlog.LOG_DIR_ENV: str(blocker / "logs")}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = sessionlog.run_logged(("status",), lambda: (print("still runs"), 3)[1])

        self.assertEqual((code, out.getvalue()), (3, "still runs\n"))
        self.assertEqual(err.getvalue().count("cannot create the session log"), 1)


if __name__ == "__main__":
    unittest.main()

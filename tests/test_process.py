"""The real ``Process`` with harmless commands (no root, no machine state), and ``elevate`` with patched lookups."""

import contextlib
import gc
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import warnings
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from ubuntu_tweaks import process as process_module
from ubuntu_tweaks import ui, watchdog
from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess, Process, root_prefix, run, use_root_prefix
from ubuntu_tweaks.watchdog import Stall


class QueryTests(unittest.TestCase):
    def test_success_failure_and_missing_binary(self) -> None:
        process = Process()

        self.assertEqual(process.query(["true"]), CommandResult(0))
        self.assertEqual(process.query(["false"]).returncode, 1)
        missing = process.query(["/nonexistent/ubuntu-tweak-test"])
        self.assertEqual(missing.returncode, 127)
        self.assertIn("command not found", missing.stderr)

    def test_captures_output(self) -> None:
        self.assertEqual(Process().query(["echo", "hi"]).stdout, "hi\n")

    def test_timeout_is_124(self) -> None:
        result = Process().query(["sleep", "5"], timeout=0.1)

        self.assertEqual(result.returncode, 124)
        self.assertFalse(result.ok)

    def test_a_file_that_cannot_be_executed_is_126(self) -> None:
        result = Process().query([__file__])  # a source file without the exec bit

        self.assertEqual(result.returncode, 126)
        self.assertIn("cannot run", result.stderr)


class ExecuteTests(unittest.TestCase):
    def test_non_zero_exit_and_missing_binary_raise(self) -> None:
        process = Process()
        with self.assertRaisesRegex(CommandError, "exited with status 1"):
            process.execute(["false"])
        with self.assertRaisesRegex(CommandError, "command not found"):
            process.execute(["/nonexistent/ubuntu-tweak-test"])

    def test_a_file_that_cannot_be_executed_raises(self) -> None:
        with self.assertRaisesRegex(CommandError, "cannot run"):
            Process().execute([__file__])

    def test_zero_exit_with_input_is_fine(self) -> None:
        Process().execute(["true"], input_text="ignored\n")


class RecordingStream(io.StringIO):
    """A stream that remembers each ``write`` call, to tell chunks from lines."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return super().write(text)


class StreamedOutputTests(unittest.TestCase):
    """What a child prints is copied to ``sys.stdout`` / ``sys.stderr`` (so the session log sees it)."""

    def shown(self, call: Callable[[], object]) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            call()
        return out.getvalue(), err.getvalue()

    def test_execute_copies_stdout_and_stderr_of_the_child(self) -> None:
        script = "import sys; print('to-out'); print('to-err', file=sys.stderr)"

        out, err = self.shown(lambda: Process().execute([sys.executable, "-c", script]))

        self.assertEqual((out, err), ("to-out\n", "to-err\n"))

    def test_last_line_merges_stderr_into_the_stdout_stream_and_env_is_the_whole_environment(self) -> None:
        script = "import os, sys; print('to-out'); print('to-err', file=sys.stderr); print(os.environ.get('ONLY'))"
        last: list[str] = []

        out, err = self.shown(
            lambda: process_module.run_streamed([sys.executable, "-c", script], env={"ONLY": "this"}, last_line=last)
        )

        self.assertEqual((out, err), ("to-out\nto-err\nthis\n", ""))
        self.assertEqual(last, ["this"])

    def test_last_line_is_empty_for_a_silent_child_and_skips_blank_lines(self) -> None:
        silent: list[str] = []
        self.shown(lambda: process_module.run_streamed(["true"], last_line=silent))
        self.assertEqual(silent, [])
        blank: list[str] = []
        self.shown(lambda: process_module.run_streamed(["sh", "-c", "echo kept; echo; echo '   '"], last_line=blank))
        self.assertEqual(blank, ["kept"])

    def test_last_line_masks_what_the_session_log_would_mask(self) -> None:
        last: list[str] = []
        script = "echo '-----BEGIN " + "PRIVATE KEY-----'; echo SYNTHETIC-KEY-FRAGMENT"
        self.shown(lambda: process_module.run_streamed(["sh", "-c", script], last_line=last))
        self.assertEqual(last, ["***"])

    def test_last_line_masks_after_removing_escapes_and_across_chunks(self) -> None:
        token = "ghp_" + "A1b2" * 9
        script = (
            f"import sys, time; t = {token!r}; sys.stdout.write(t[:24] + '\\x1b[31m'); sys.stdout.flush(); time.sleep(0.2); "
            "sys.stdout.write(t[24:] + '\\x1b[0m\\n'); "
            "sys.stdout.write('https://alice:demo-password@example.org/' + 'x' * 1967); sys.stdout.flush(); time.sleep(0.2); "
            "sys.stdout.write('\\n')"
        )
        last: list[str] = []
        out, _ = self.shown(lambda: process_module.run_streamed([sys.executable, "-c", script], last_line=last))
        self.assertIn("alice", out)  # the test child printed it; the reason must not carry the password
        self.assertNotIn("demo-password", last[0])

    def test_last_line_masks_a_token_split_by_an_escape(self) -> None:
        token = "ghp_" + "A1b2" * 9
        script = f"t = {token!r}; print(t[:24] + '\\x1b[31m' + t[24:] + '\\x1b[0m')"
        last: list[str] = []
        self.shown(lambda: process_module.run_streamed([sys.executable, "-c", script], last_line=last))
        self.assertEqual(last, ["***"])

    def test_last_line_does_not_split_a_credential_where_an_endless_line_is_cut(self) -> None:
        script = (
            "import sys, time; sys.stdout.write(' ' * 262144 + 'https://alice:'); sys.stdout.flush(); time.sleep(0.3); "
            "sys.stdout.write('SYNTHETIC-PASSWORD-123@example.org/\\n')"
        )
        last: list[str] = []
        self.shown(lambda: process_module.run_streamed([sys.executable, "-c", script], last_line=last))
        self.assertEqual(last, ["https://***@example.org/"])

    def test_last_line_is_what_the_session_log_shows_even_after_many_erased_lines(self) -> None:
        script = "import sys; sys.stdout.write('-----BEGIN ' + 'PRIVATE KEY-----\\n' + '\\x1b[2K\\n' * 201 + 'SYNTHETIC-KEY-FRAGMENT\\n')"
        last: list[str] = []
        self.shown(lambda: process_module.run_streamed([sys.executable, "-c", script], last_line=last))
        self.assertEqual(last, ["***"])

    def test_a_grandchild_writing_after_the_child_is_gone_does_not_change_the_last_line(self) -> None:
        last: list[str] = []
        with mock.patch.object(process_module, "_DRAIN_GRACE", 0.2):
            self.shown(
                lambda: process_module.run_streamed(
                    ["sh", "-c", "(sleep 1; echo background-status) & echo real-failure"], last_line=last
                )
            )
            time.sleep(1.3)
        self.assertEqual(last, ["real-failure"])

    def test_stderr_tail_still_holds_only_stderr(self) -> None:
        script = "import sys; print('to-out'); print('to-err', file=sys.stderr)"
        tail: list[str] = []
        self.shown(lambda: process_module.run_streamed([sys.executable, "-c", script], stderr_tail=tail))
        self.assertEqual("".join(tail), "to-err\n")

    def test_run_without_capture_copies_output_and_returns_empty(self) -> None:
        returned: list[str] = []

        out, err = self.shown(lambda: returned.append(run(["echo", "hello"])))

        self.assertEqual((returned, out, err), ([""], "hello\n", ""))

    def test_a_prompt_without_newline_is_shown_before_the_child_ends(self) -> None:
        script = "import sys, time; sys.stdout.write('Continue? '); sys.stdout.flush(); time.sleep(0.5); print('done')"
        out = RecordingStream()
        with contextlib.redirect_stdout(out):
            Process().execute([sys.executable, "-c", script])

        self.assertEqual(out.chunks[0], "Continue? ")  # shown on its own, before the rest of the output exists
        self.assertEqual("".join(out.chunks), "Continue? done\n")

    def test_input_text_reaches_the_child(self) -> None:
        out, _ = self.shown(lambda: Process().execute(["cat"], input_text="from the caller\n"))

        self.assertEqual(out, "from the caller\n")

    def test_a_multibyte_character_split_across_reads_is_not_mangled(self) -> None:
        script = "import os, time; data = 'é'.encode(); os.write(1, data[:1]); time.sleep(0.3); os.write(1, data[1:])"

        out, _ = self.shown(lambda: Process().execute([sys.executable, "-c", script]))

        self.assertEqual(out, "é")

    def test_a_failure_carries_the_last_line_the_child_wrote_to_stderr(self) -> None:
        script = "import sys; print('noise'); print('first', file=sys.stderr); print('dns error: could not resolve', file=sys.stderr); sys.exit(2)"
        with self.assertRaises(CommandError) as raised:
            self.shown(lambda: Process().execute([sys.executable, "-c", script]))
        self.assertTrue(str(raised.exception).endswith("exited with status 2: first / dns error: could not resolve"))
        self.assertEqual(raised.exception.returncode, 2)

    def test_a_silent_failure_has_no_detail_suffix(self) -> None:
        with self.assertRaises(CommandError) as raised:
            self.shown(lambda: Process().execute([sys.executable, "-c", "import sys; sys.exit(4)"]))
        self.assertTrue(str(raised.exception).endswith("exited with status 4"))

    def test_the_detail_is_one_clipped_line(self) -> None:
        script = "import sys; print('x' * 5000, file=sys.stderr); sys.exit(1)"
        with self.assertRaises(CommandError) as raised:
            self.shown(lambda: Process().execute([sys.executable, "-c", script]))
        self.assertLess(len(str(raised.exception).splitlines()[0]), 600)
        self.assertEqual(len(str(raised.exception).splitlines()), 1)

    def test_failures_keep_their_messages_and_status(self) -> None:
        with self.assertRaisesRegex(CommandError, "exited with status 3") as raised:
            self.shown(lambda: Process().execute([sys.executable, "-c", "import sys; print('before'); sys.exit(3)"]))
        self.assertEqual(raised.exception.returncode, 3)
        with self.assertRaisesRegex(CommandError, "`false` failed with exit code 1$") as failed:
            run(["false"])
        self.assertEqual(failed.exception.returncode, 1)
        with self.assertRaisesRegex(CommandError, "command not found \\(while running: /nonexistent/x\\)"):
            run(["/nonexistent/x"])

    def test_a_timeout_stops_the_child_and_raises(self) -> None:
        with self.assertRaisesRegex(CommandError, "`sleep 5` timed out after 0.2 s"):
            run(["sleep", "5"], timeout=0.2)

    def test_output_left_by_a_daemon_holding_the_pipe_does_not_hang_the_run(self) -> None:
        with mock.patch.object(process_module, "_DRAIN_GRACE", 0.2):
            started = time.monotonic()
            out, _ = self.shown(lambda: Process().execute(["sh", "-c", "sleep 1 & echo launched"]))

        self.assertEqual(out, "launched\n")
        self.assertLess(time.monotonic() - started, 0.9)

    def test_a_closed_terminal_does_not_stall_the_child(self) -> None:
        class Closed(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError

        started = time.monotonic()
        with contextlib.redirect_stdout(Closed()):
            Process().execute([sys.executable, "-c", "print('x' * 200000)"])

        self.assertLess(time.monotonic() - started, 10)

    def test_a_daemon_holding_stdout_does_not_cut_off_the_tail_of_stderr(self) -> None:
        script = "(sleep 1 >&1 2>/dev/null &) ; sleep 0.3; echo tail-of-stderr >&2"

        with mock.patch.object(process_module, "_DRAIN_GRACE", 0.1):
            _, err = self.shown(lambda: Process().execute(["sh", "-c", script]))

        self.assertEqual(err, "tail-of-stderr\n")

    def test_an_abandoned_pipe_is_drained_without_writing_to_the_terminal_any_more(self) -> None:
        with mock.patch.object(process_module, "_DRAIN_GRACE", 0.1):
            out, _ = self.shown(lambda: Process().execute(["sh", "-c", "(sleep 0.5; echo late) & echo now"]))
            time.sleep(1.0)

        self.assertEqual(out, "now\n")

    def test_ctrl_c_gives_the_child_a_moment_then_stops_it_and_propagates(self) -> None:
        real_wait = subprocess.Popen.wait
        interrupted: list[subprocess.Popen[bytes]] = []

        def wait(process: subprocess.Popen[bytes], timeout: float | None = None) -> int:
            if not interrupted:
                interrupted.append(process)
                raise KeyboardInterrupt
            return real_wait(process, timeout)

        with (
            mock.patch.object(process_module, "_INTERRUPT_GRACE", 0.05),
            mock.patch.object(subprocess.Popen, "wait", wait),
            self.assertRaises(KeyboardInterrupt),
        ):
            Process().execute(["sleep", "5"])

        self.assertIsNotNone(interrupted[0].poll())  # the child did not outlive the interrupt

    def test_a_character_the_terminal_cannot_encode_is_replaced_not_dropped_with_the_rest(self) -> None:
        raw = io.BytesIO()
        terminal = io.TextIOWrapper(raw, encoding="ascii", write_through=True)
        script = "print('caf\u00e9'); print('after')"

        with contextlib.redirect_stdout(terminal):
            Process().execute([sys.executable, "-c", script])

        self.assertEqual(raw.getvalue(), b"caf?\nafter\n")

    def test_a_python_child_is_unbuffered_on_its_pipe(self) -> None:
        out, _ = self.shown(
            lambda: Process().execute([sys.executable, "-c", "import os; print(os.environ['PYTHONUNBUFFERED'])"])
        )

        self.assertEqual(out, "1\n")

    def test_sigterm_to_us_does_not_kill_the_child(self) -> None:
        marker = Path(tempfile.mkdtemp()) / "finished"
        self.addCleanup(shutil.rmtree, marker.parent, ignore_errors=True)

        with (
            warnings.catch_warnings(),  # the child is deliberately still running when its Popen is collected
            mock.patch.object(process_module, "_DRAIN_GRACE", 0.1),
            mock.patch.object(subprocess.Popen, "wait", side_effect=SystemExit(143)),
            self.assertRaises(SystemExit),
        ):
            warnings.simplefilter("ignore", ResourceWarning)
            Process().execute(["sh", "-c", f"sleep 0.5; touch {marker}"])

        time.sleep(1.0)
        gc.collect()
        self.assertTrue(marker.exists())


class ElevateTests(unittest.TestCase):
    def elevate(self, *, euid: int, tools: set[str]) -> list[str]:
        with (
            mock.patch("os.geteuid", return_value=euid),
            mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in tools else None),
        ):
            return Process().elevate(["apt-get", "install"])

    def test_root_runs_as_is(self) -> None:
        self.assertEqual(self.elevate(euid=0, tools=set()), ["apt-get", "install"])

    def test_sudo_is_preferred_then_pkexec(self) -> None:
        self.assertEqual(self.elevate(euid=1000, tools={"sudo", "pkexec"}), ["sudo", "--", "apt-get", "install"])
        self.assertEqual(self.elevate(euid=1000, tools={"pkexec"}), ["pkexec", "apt-get", "install"])

    def test_neither_is_an_error(self) -> None:
        with self.assertRaisesRegex(CommandError, "neither sudo nor pkexec"):
            self.elevate(euid=1000, tools=set())

    def test_an_open_session_prefix_wins_and_root_still_runs_as_is(self) -> None:
        self.addCleanup(use_root_prefix, None)
        use_root_prefix(["sudo", "-A", "--"])

        self.assertEqual(root_prefix(), ("sudo", "-A", "--"))
        self.assertEqual(self.elevate(euid=1000, tools={"sudo", "pkexec"}), ["sudo", "-A", "--", "apt-get", "install"])
        self.assertEqual(self.elevate(euid=0, tools=set()), ["apt-get", "install"])
        use_root_prefix(None)
        self.assertEqual(self.elevate(euid=1000, tools={"sudo"}), ["sudo", "--", "apt-get", "install"])


class FakeProcessTests(unittest.TestCase):
    def test_records_calls_and_answers_127_for_unknown_commands(self) -> None:
        fake = FakeProcess({("a",): CommandResult(0, "out")})

        self.assertEqual(fake.query(["a"]).stdout, "out")
        self.assertEqual(fake.query(["b"]).returncode, 127)
        fake.execute(["c"], input_text="x")
        self.assertEqual(fake.queries, [("a",), ("b",)])
        self.assertEqual(fake.executed, [(("c",), "x")])


SPAWNS_A_GRANDCHILD_THAT_STOPS = textwrap.dedent(
    """
    import os, signal, subprocess, sys
    stopper = "import os, signal; os.kill(os.getpid(), signal.SIGSTOP); print('grandchild went on')"
    child = subprocess.Popen([sys.executable, "-c", stopper])
    print("grandchild", child.pid, flush=True)
    sys.exit(child.wait())
    """
)


class StallWatchdogTest(unittest.TestCase):
    """``run_streamed`` watches the child and everything below it: ticket #126 (a stopped ``apt-get`` behind sudo-rs)."""

    def setUp(self) -> None:
        ui.configure(plain=True, lang="en")
        self.addCleanup(ui.configure)
        self.stalls: list[Stall] = []
        self.out = io.StringIO()

    def stream(self, argv: list[str], *, timeout: float | None = None, **options: object) -> int:
        settings: dict[str, object] = {
            "label": "run earlyoom",
            "stall_seconds": 0.6,
            "poll_seconds": 0.1,
            "on_stall": self.stalls.append,
        }
        settings.update(options)
        with contextlib.redirect_stdout(self.out), process_module.watching(**settings):  # type: ignore[arg-type]
            return process_module.run_streamed(argv, timeout=timeout)

    def test_a_stopped_grandchild_is_continued_by_the_engine_and_the_step_finishes(self) -> None:
        code = self.stream([sys.executable, "-c", SPAWNS_A_GRANDCHILD_THAT_STOPS])

        shown = self.out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("grandchild went on", shown)
        self.assertEqual([(s.kind, s.resumed) for s in self.stalls], [("stopped", True)])
        self.assertIn("run earlyoom", shown)
        self.assertIn("SIGCONT", shown)

    def test_a_grandchild_that_cannot_be_continued_is_reported_with_the_literal_command_and_left_alone(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            self.stream([sys.executable, "-c", SPAWNS_A_GRANDCHILD_THAT_STOPS], timeout=8, resume=lambda _pid: False)

        shown = self.out.getvalue()
        match = re.search(r"grandchild (\d+)", shown)
        self.assertIsNotNone(match, shown)
        pid = int(match.group(1))  # type: ignore[union-attr]
        self.addCleanup(self.reap, pid)
        self.assertEqual(watchdog.read_processes()[pid].state, "T")  # the engine never resumed it itself here
        self.assertEqual(
            [(s.kind, s.pid, s.command) for s in self.stalls][:1], [("stopped", pid, f"sudo kill -CONT {pid}")]
        )
        self.assertIn(f"sudo kill -CONT {pid}", shown)

    @staticmethod
    def reap(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)

    def test_a_quiet_child_is_reported_and_left_to_finish_by_itself(self) -> None:
        code = self.stream([sys.executable, "-c", "import time; time.sleep(2.5); print('slow but fine')"])

        shown = self.out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("slow but fine", shown)
        self.assertEqual([s.kind for s in self.stalls], ["quiet"])
        self.assertIn("run earlyoom", shown)

    def test_output_keeps_a_slow_child_unreported(self) -> None:
        script = "import time\nfor i in range(8):\n    print(i, flush=True)\n    time.sleep(0.2)"

        code = self.stream([sys.executable, "-c", script], stall_seconds=3.0)  # slack for a loaded machine

        self.assertEqual((code, self.stalls), (0, []))

    def test_the_watch_belongs_to_its_block(self) -> None:
        self.assertIsNone(process_module.current_watch())
        with process_module.watching(label="step"):
            watch = process_module.current_watch()
            self.assertIsNotNone(watch)
            self.assertEqual(watch.label, "step")  # type: ignore[union-attr]
        self.assertIsNone(process_module.current_watch())
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(process_module.run_streamed(["true"]), 0)  # no block: the defaults, no label needed

    def test_a_child_that_cannot_start_still_propagates_as_before(self) -> None:
        with self.assertRaises(FileNotFoundError), process_module.watching(label="x"):
            process_module.run_streamed(["/nonexistent/ubuntu-tweak-test"])


class HeartbeatWhileStreaming(unittest.TestCase):
    def tearDown(self) -> None:
        ui.configure()

    def test_a_quiet_step_shows_a_heartbeat_that_is_erased_before_the_next_output(self) -> None:
        ui.configure(color="always", lang="en")
        script = "import time; print('before', flush=True); time.sleep(1.0); print('after', flush=True)"
        out = io.StringIO()

        with (
            contextlib.redirect_stdout(out),
            process_module.watching(label="run earlyoom", stall_seconds=60, poll_seconds=0.1, heartbeat_after=0.3),
        ):
            code = process_module.run_streamed([sys.executable, "-c", script])

        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("before\n", text)  # a loaded machine may draw a heartbeat before the first output
        self.assertIn("run earlyoom", text)
        self.assertRegex(text, r"00:0[0-9]")
        self.assertTrue(text.endswith("\r\x1b[2Kafter\n"), repr(text[-40:]))  # erased right before the output

    def test_no_heartbeat_bytes_without_the_explained_layout(self) -> None:
        ui.configure(plain=True)
        out = io.StringIO()

        with (
            contextlib.redirect_stdout(out),
            process_module.watching(label="x", stall_seconds=60, poll_seconds=0.1, heartbeat_after=0.1),
        ):
            process_module.run_streamed([sys.executable, "-c", "import time; time.sleep(0.6); print('end')"])

        self.assertEqual(out.getvalue(), "end\n")

    def test_a_prompt_without_a_newline_is_not_erased_by_the_heartbeat(self) -> None:
        ui.configure(color="always", lang="en")
        script = "import time; print('Continue? [Y/n] ', end='', flush=True); time.sleep(1.0); print('y', flush=True)"
        out = io.StringIO()

        with (
            contextlib.redirect_stdout(out),
            process_module.watching(label="x", stall_seconds=60, poll_seconds=0.1, heartbeat_after=0.2),
        ):
            process_module.run_streamed([sys.executable, "-c", script])

        text = out.getvalue()
        self.assertTrue(text.endswith("Continue? [Y/n] y\n"), repr(text))  # nothing erased the prompt

    def test_a_terminal_that_stops_reading_cannot_stop_the_timeout(self) -> None:
        ui.configure(color="always", lang="en")
        release = threading.Event()
        self.addCleanup(release.set)

        class Stuck(io.StringIO):
            def isatty(self) -> bool:
                return True

            def write(self, text: str) -> int:
                release.wait(10)  # the terminal was paused with Ctrl-S
                return len(text)

        started = time.monotonic()
        with (
            contextlib.redirect_stdout(Stuck()),
            process_module.watching(label="x", stall_seconds=60, poll_seconds=0.1, heartbeat_after=0.2),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                process_module.run_streamed(["sleep", "30"], timeout=1.5)

        self.assertLess(time.monotonic() - started, 6)

    def test_without_a_block_nothing_is_watched_so_nested_engines_never_report_twice(self) -> None:
        with mock.patch.object(watchdog, "Watchdog") as dog, contextlib.redirect_stdout(io.StringIO()):
            process_module.run_streamed(["true"])

        dog.assert_not_called()

    def test_without_a_block_there_is_no_heartbeat_even_in_the_explained_layout(self) -> None:
        ui.configure(color="always")
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            process_module.run_streamed([sys.executable, "-c", "import time; time.sleep(0.6); print('end')"])

        self.assertEqual(out.getvalue(), "end\n")


class SudoRsBackgroundPtyModel(unittest.TestCase):
    """The cause of ticket #126, reproduced in a sandbox with a model of sudo-rs 0.2.13's monitor."""

    def model(self, stdin_kind: str) -> dict[str, object]:
        script = Path(__file__).with_name("sudo_rs_monitor.py")
        done = subprocess.run([sys.executable, str(script), stdin_kind], capture_output=True, text=True, timeout=30)
        if done.returncode != 0 and "OSError" in done.stderr:
            self.skipTest("no pty can be opened here")
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_grandchild_that_touches_the_pty_is_left_stopped_while_its_parent_runs_on(self) -> None:
        report = self.model("tty")

        self.assertEqual(report["grandchild_state"], "T")
        self.assertFalse(report["grandchild_finished"])
        self.assertNotEqual(report["command_state"], "T")  # the direct child was resumed: sudo sees nothing wrong
        self.assertGreaterEqual(report["bounces"], 1)  # type: ignore[operator]

    def test_a_grandchild_whose_stdin_is_not_a_terminal_never_stops(self) -> None:
        report = self.model("devnull")

        self.assertTrue(report["grandchild_finished"])
        self.assertEqual(report["bounces"], 0)


if __name__ == "__main__":
    unittest.main()

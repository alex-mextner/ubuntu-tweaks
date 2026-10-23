"""The real ``Process`` with harmless commands (no root, no machine state), and ``elevate`` with patched lookups."""

import contextlib
import gc
import io
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from ubuntu_tweaks import process as process_module
from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess, Process, root_prefix, run, use_root_prefix


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
        self.assertTrue(str(raised.exception).endswith("exited with status 2: dns error: could not resolve"))
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


if __name__ == "__main__":
    unittest.main()

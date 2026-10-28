"""The stall watchdog and the heartbeat (spec, *Reconcile and failover*, *Stalled steps*), against fake clocks and
fake process tables; the real ``/proc`` reader and the real SIGCONT are exercised on harmless children."""

import contextlib
import io
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ubuntu_tweaks import ui, watchdog
from ubuntu_tweaks.watchdog import Heartbeat, Proc, Stall, Watchdog


def proc(pid: int, ppid: int, state: str = "S", cpu: int = 0, comm: str = "x") -> Proc:
    return Proc(pid, ppid, state, cpu, comm)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Fixture(unittest.TestCase):
    """sudo (10) -> monitor (11) -> python (12) -> apt-get (13), as seen on the machine that froze."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.table = {
            1: proc(1, 0, comm="systemd"),
            10: proc(10, 1, comm="sudo"),
            11: proc(11, 10, comm="sudo"),
            12: proc(12, 11, comm="python3"),
            13: proc(13, 12, comm="apt-get"),
            99: proc(99, 1, "T", comm="unrelated"),
        }
        self.scans = 0
        self.stalls: list[Stall] = []
        self.resumed: list[int] = []
        self.resume_result = True
        ui.configure(plain=True)
        self.addCleanup(ui.configure)

    def processes(self) -> dict[int, Proc]:
        self.scans += 1
        return dict(self.table)

    def resume(self, pid: int) -> bool:
        self.resumed.append(pid)
        return self.resume_result

    def dog(self, stall_seconds: float = 120.0) -> Watchdog:
        return Watchdog(
            10,
            step="run earlyoom",
            stall_seconds=stall_seconds,
            clock=self.clock,
            processes=self.processes,
            resume=self.resume,
            report=self.stalls.append,
        )

    def run_for(self, dog: Watchdog, seconds: int) -> None:
        for _ in range(seconds):
            self.clock.advance(1)
            dog.poll()


class ProcessTable(unittest.TestCase):
    def test_a_stat_line_with_parentheses_and_spaces_in_the_name_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stat = Path(root, "321", "stat")
            stat.parent.mkdir()
            fields = "T 77 321 321 0 -1 4194560 1 2 3 4 11 22 33 44 20 0 1 0 5 100 200"
            stat.write_text(f"321 (apt-get (x) y) {fields}\n")
            (Path(root) / "self").mkdir()  # not a number: skipped

            table = watchdog.read_processes(Path(root))

        self.assertEqual(table, {321: Proc(321, 77, "T", 11 + 22 + 33 + 44, "apt-get (x) y")})

    def test_a_process_that_vanishes_while_the_table_is_read_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            Path(root, "5").mkdir()  # no stat file

            self.assertEqual(watchdog.read_processes(Path(root)), {})

    def test_the_real_proc_lists_this_process_with_its_parent(self) -> None:
        table = watchdog.read_processes()

        self.assertEqual(table[os.getpid()].ppid, os.getppid())

    def test_the_tree_of_a_pid_is_the_pid_and_all_its_descendants(self) -> None:
        table = {p.pid: p for p in (proc(10, 1), proc(11, 10), proc(12, 11), proc(13, 10), proc(20, 1))}

        self.assertEqual(sorted(p.pid for p in watchdog.tree(table, 10)), [10, 11, 12, 13])

    def test_a_cycle_in_a_corrupt_table_ends(self) -> None:
        table = {1: proc(1, 2), 2: proc(2, 1)}

        self.assertEqual(sorted(p.pid for p in watchdog.tree(table, 1)), [1, 2])


class QuietStalls(Fixture):
    def test_output_that_keeps_coming_never_stalls_and_reads_the_tree_only_every_few_seconds(self) -> None:
        dog = self.dog()

        for _ in range(600):
            self.clock.advance(0.5)
            dog.activity("progress\n")
            dog.poll()

        self.assertEqual(self.stalls, [])
        self.assertLessEqual(self.scans, 300 // int(watchdog.RESCAN_SECONDS) + 1)

    def test_a_stopped_sibling_is_resumed_while_another_child_keeps_printing(self) -> None:
        dog = self.dog()
        self.table[13] = proc(13, 12, "T", comm="apt-get")

        for _ in range(60):
            self.clock.advance(1)
            dog.activity("progress\n")  # the other child prints every second
            dog.poll()

        self.assertEqual(self.resumed, [13])
        self.assertEqual((self.stalls[0].kind, self.stalls[0].resumed), ("stopped", True))

    def test_output_and_the_end_of_the_step_never_wait_for_a_slow_resume(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def slow_resume(pid: int) -> bool:
            entered.set()
            release.wait(5)  # `sudo -n kill -CONT` taking its time
            return True

        self.resume = slow_resume  # type: ignore[method-assign]
        dog = self.dog()
        self.table[13] = proc(13, 12, "T", comm="apt-get")
        self.run_for(dog, 9)
        self.clock.advance(2)
        thread = threading.Thread(target=dog.poll, daemon=True)
        thread.start()
        entered.wait(2)

        started = time.monotonic()
        dog.activity("output\n")  # an output pump takes the lock at once
        dog.finish()
        waited = time.monotonic() - started
        release.set()
        thread.join(2)

        self.assertLess(waited, 1.0)
        self.assertEqual(self.stalls, [])  # the step ended first: the late result is dropped

    def test_a_closed_watchdog_reports_nothing_even_for_a_stopped_process(self) -> None:
        dog = self.dog()
        self.table[13] = proc(13, 12, "T", comm="apt-get")
        self.run_for(dog, 5)

        dog.finish()  # the step ended while a poll was still on its way
        self.run_for(dog, 30)

        self.assertEqual((self.stalls, self.resumed), ([], []))

    def test_a_poll_never_waits_for_a_blocked_terminal_write(self) -> None:
        dog = self.dog()
        held, release = threading.Event(), threading.Event()

        def blocked_write() -> None:
            with dog.lock:  # an output pump stuck in stream.write()
                held.set()
                release.wait(5)

        thread = threading.Thread(target=blocked_write, daemon=True)
        thread.start()
        held.wait(2)
        started = time.monotonic()
        dog.poll()
        dog.finish()
        waited = time.monotonic() - started
        release.set()
        thread.join(2)

        self.assertLess(waited, 2.0)

    def test_no_output_and_no_cpu_for_the_stall_time_is_one_report(self) -> None:
        dog = self.dog(stall_seconds=120)

        self.run_for(dog, 119)
        self.assertEqual(self.stalls, [])
        self.run_for(dog, 2)
        self.run_for(dog, 300)

        self.assertEqual([(s.kind, s.step) for s in self.stalls], [("quiet", "run earlyoom")])
        self.assertGreaterEqual(self.stalls[0].seconds, 120)
        self.assertEqual(self.resumed, [])  # nothing is stopped: nothing is resumed, nothing is killed

    def test_output_rearms_the_report(self) -> None:
        dog = self.dog(stall_seconds=30)

        self.run_for(dog, 31)
        dog.activity()
        self.run_for(dog, 31)

        self.assertEqual([s.kind for s in self.stalls], ["quiet", "quiet"])

    def test_cpu_time_used_by_the_tree_counts_as_progress(self) -> None:
        dog = self.dog(stall_seconds=30)

        for second in range(1, 200):
            self.clock.advance(1)
            self.table[13] = proc(13, 12, cpu=second, comm="apt-get")  # a quiet but busy child
            dog.poll()

        self.assertEqual(self.stalls, [])


class StoppedDescendants(Fixture):
    def stop_apt(self) -> None:
        self.table[13] = proc(13, 12, "T", comm="apt-get")

    def test_a_stopped_grandchild_is_resumed_after_the_grace_and_reported_once_with_the_command(self) -> None:
        dog = self.dog()
        self.stop_apt()

        self.run_for(dog, 9)
        self.assertEqual((self.stalls, self.resumed), ([], []))  # sudo itself bounces a tty stop for a moment
        self.run_for(dog, 2)

        self.assertEqual(self.resumed, [13])
        self.assertEqual(len(self.stalls), 1)
        stall = self.stalls[0]
        self.assertEqual((stall.kind, stall.pid, stall.comm, stall.resumed), ("stopped", 13, "apt-get", True))
        self.assertEqual(stall.command, "sudo kill -CONT 13")
        self.run_for(dog, 5)
        self.assertEqual(len(self.stalls), 1)

    def test_a_process_that_stays_stopped_after_the_resume_is_reported_again_and_not_resumed_again(self) -> None:
        dog = self.dog()
        self.stop_apt()

        self.run_for(dog, 11)
        self.run_for(dog, 11)
        self.run_for(dog, 60)

        self.assertEqual(self.resumed, [13])
        self.assertEqual([(s.kind, s.resumed) for s in self.stalls], [("stopped", True), ("stopped", False)])
        self.assertEqual(self.stalls[1].command, "sudo kill -CONT 13")

    def test_when_nothing_can_resume_it_the_first_report_already_carries_the_command(self) -> None:
        dog = self.dog()
        self.resume_result = False
        self.stop_apt()

        self.run_for(dog, 11)

        self.assertEqual([s.resumed for s in self.stalls], [False])
        self.assertEqual(self.stalls[0].command, "sudo kill -CONT 13")

    def test_a_process_that_continues_before_the_grace_is_not_reported(self) -> None:
        dog = self.dog()
        self.stop_apt()
        self.run_for(dog, 5)
        self.table[13] = proc(13, 12, comm="apt-get")

        self.run_for(dog, 60)

        self.assertEqual((self.stalls, self.resumed), ([], []))

    def test_a_ptrace_stop_and_a_stopped_stranger_are_left_alone(self) -> None:
        dog = self.dog()
        self.table[13] = proc(13, 12, "t", comm="gdb-target")  # 99 is stopped too, but not ours

        self.run_for(dog, 60)

        self.assertEqual(self.resumed, [])
        self.assertEqual([s.kind for s in self.stalls], [])

    def test_the_grace_shrinks_with_a_short_stall_time(self) -> None:
        dog = self.dog(stall_seconds=4)
        self.stop_apt()

        self.run_for(dog, 5)

        self.assertEqual(self.resumed, [13])

    def test_a_stop_report_replaces_the_quiet_report(self) -> None:
        dog = self.dog(stall_seconds=20)
        self.stop_apt()

        self.run_for(dog, 100)

        self.assertEqual({s.kind for s in self.stalls}, {"stopped"})


class Resuming(unittest.TestCase):
    def test_our_own_stopped_child_is_continued(self) -> None:
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.wait)  # cleanups run last-in first-out: kill first, then reap
        self.addCleanup(child.kill)
        os.kill(child.pid, signal.SIGSTOP)
        for _ in range(100):
            if watchdog.read_processes()[child.pid].state == "T":
                break
            time.sleep(0.02)

        self.assertTrue(watchdog.resume_stopped(child.pid))

        for _ in range(100):
            if watchdog.read_processes()[child.pid].state != "T":
                break
            time.sleep(0.02)
        self.assertNotEqual(watchdog.read_processes()[child.pid].state, "T")

    def test_a_process_that_is_gone_needs_nothing(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()

        self.assertTrue(watchdog.resume_stopped(child.pid))

    def test_a_root_process_is_continued_through_the_open_root_session(self) -> None:
        asked: list[list[str]] = []

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            asked.append(list(argv))
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)  # never a password prompt on the wizard's terminal
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch("os.kill", side_effect=PermissionError),
            mock.patch("ubuntu_tweaks.process.root_prefix", return_value=("sudo", "-A", "--")),
            mock.patch("subprocess.run", runner),
        ):
            self.assertTrue(watchdog.resume_stopped(4242))

        self.assertEqual(asked, [["sudo", "-n", "-A", "--", "kill", "-CONT", "4242"]])

    def test_without_a_root_session_or_when_sudo_refuses_the_answer_is_no(self) -> None:
        with mock.patch("os.kill", side_effect=PermissionError):
            with mock.patch("ubuntu_tweaks.process.root_prefix", return_value=None):
                self.assertFalse(watchdog.resume_stopped(4242))
            with (
                mock.patch("ubuntu_tweaks.process.root_prefix", return_value=("sudo", "--")),
                mock.patch(
                    "subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "a password is required")
                ),
            ):
                self.assertFalse(watchdog.resume_stopped(4242))
            with (
                mock.patch("ubuntu_tweaks.process.root_prefix", return_value=("pkexec",)),
                mock.patch("subprocess.run") as run,
            ):
                self.assertFalse(watchdog.resume_stopped(4242))  # a polkit dialog is not ours to pop up
                run.assert_not_called()

    def test_a_hung_sudo_is_not_waited_for_for_ever(self) -> None:
        with (
            mock.patch("os.kill", side_effect=PermissionError),
            mock.patch("ubuntu_tweaks.process.root_prefix", return_value=("sudo", "--")),
            mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sudo", 15)),
        ):
            self.assertFalse(watchdog.resume_stopped(4242))


class Messages(unittest.TestCase):
    def setUp(self) -> None:
        ui.configure(plain=True, lang="en")
        self.addCleanup(ui.configure)

    def test_a_stopped_process_is_named_with_its_pid_and_the_one_line_command(self) -> None:
        text = "\n".join(
            watchdog.render(
                Stall(
                    "stopped",
                    "run earlyoom: sudo -- earlyoom install",
                    12.0,
                    13,
                    "apt-get",
                    False,
                    "sudo kill -CONT 13",
                )
            )
        )

        for expected in ("run earlyoom: sudo -- earlyoom install", "apt-get", "pid 13", "sudo kill -CONT 13", "Ctrl-C"):
            self.assertIn(expected, text)

    def test_a_resumed_process_says_so(self) -> None:
        text = "\n".join(watchdog.render(Stall("stopped", "step", 12.0, 13, "apt-get", True, "sudo kill -CONT 13")))

        self.assertIn("SIGCONT", text)

    def test_a_quiet_step_names_the_time_and_the_ways_to_change_it(self) -> None:
        text = "\n".join(watchdog.render(Stall("quiet", "step", 125.0, None, "", False, "")))

        for expected in ("step", "125", "stall_seconds", watchdog.STALL_ENV, "Ctrl-C"):
            self.assertIn(expected, text)

    def test_the_wizard_language_is_russian_on_request(self) -> None:
        ui.configure(plain=True, lang="ru")

        stopped = "\n".join(watchdog.render(Stall("stopped", "шаг", 12.0, 13, "apt-get", False, "sudo kill -CONT 13")))
        quiet = "\n".join(watchdog.render(Stall("quiet", "шаг", 125.0, None, "", False, "")))

        self.assertIn("остановлен", stopped)
        self.assertIn("sudo kill -CONT 13", stopped)
        self.assertIn("не выводит", quiet)
        self.assertNotIn("looks stuck", stopped + quiet)

    def test_the_journal_detail_is_one_short_line_without_colour(self) -> None:
        stall = Stall("stopped", "step", 12.0, 13, "apt-get", False, "sudo kill -CONT 13")

        detail = stall.detail()

        self.assertNotIn("\n", detail)
        self.assertIn("stalled", detail)
        self.assertIn("sudo kill -CONT 13", detail)
        self.assertEqual(Stall("quiet", "s", 61.0).detail(), "stalled: no output and no CPU for 61 s")


class Report(Fixture):
    def test_the_default_report_prints_the_warning_and_notes_it_in_the_session_log(self) -> None:
        out = io.StringIO()
        dog = Watchdog(
            10,
            step="run earlyoom",
            stall_seconds=30,
            clock=self.clock,
            processes=self.processes,
            resume=self.resume,
            on_stall=self.stalls.append,
        )

        with contextlib.redirect_stdout(out), mock.patch("ubuntu_tweaks.sessionlog.note") as note:
            self.run_for(dog, 31)

        self.assertIn("run earlyoom", out.getvalue())
        note.assert_called_once()
        self.assertEqual(note.call_args.args[0], "stall")
        self.assertIn("run earlyoom", note.call_args.args[1])
        self.assertEqual([s.kind for s in self.stalls], ["quiet"])  # the callback of the journal got it too


class HeartbeatLine(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.stream = io.StringIO()
        ui.configure(color="always")
        self.addCleanup(ui.configure)

    def beat(self) -> Heartbeat:
        return Heartbeat("run earlyoom", self.stream, clock=self.clock, after=3.0)

    def test_a_quiet_step_gets_one_rewritten_line_with_the_elapsed_time(self) -> None:
        beat = self.beat()

        self.clock.advance(2.9)
        beat.tick()
        self.assertEqual(self.stream.getvalue(), "")  # a fast command never flickers
        self.clock.advance(2.2)
        beat.tick()
        self.clock.advance(60)
        beat.tick()

        text = self.stream.getvalue()
        self.assertIn("\r\x1b[2K", text)
        self.assertIn("run earlyoom", text)
        self.assertIn("00:05", text)
        self.assertIn("01:05", text)
        self.assertNotIn("\n", text)

    def test_the_line_is_erased_before_anything_else_is_written(self) -> None:
        beat = self.beat()
        self.clock.advance(5)
        beat.tick()
        self.stream.seek(0)
        self.stream.truncate()

        beat.erase()
        beat.erase()  # nothing left to erase the second time

        self.assertEqual(self.stream.getvalue(), "\r\x1b[2K")

    def test_output_restarts_the_quiet_clock(self) -> None:
        beat = self.beat()
        self.clock.advance(5)
        beat.tick()
        beat.activity()
        self.stream.seek(0)
        self.stream.truncate()
        self.clock.advance(2)
        beat.tick()

        self.assertEqual(self.stream.getvalue(), "")

    def test_a_prompt_or_half_printed_line_is_never_overwritten(self) -> None:
        beat = self.beat()
        beat.activity("Continue? [Y/n] ")

        self.clock.advance(60)
        beat.tick()
        self.assertEqual(self.stream.getvalue(), "")  # the cursor is not in column 0

        beat.activity("y\n")
        self.clock.advance(5)
        beat.tick()
        self.assertIn("run earlyoom", self.stream.getvalue())

    def test_the_line_stops_being_redrawn_after_half_an_hour_of_silence(self) -> None:
        beat = self.beat()
        self.clock.advance(watchdog.HEARTBEAT_MAX_SECONDS + 10)
        beat.tick()

        self.assertEqual(self.stream.getvalue(), "")

    def test_a_progress_line_ended_by_a_carriage_return_is_not_overwritten(self) -> None:
        beat = self.beat()
        beat.activity("Reading package lists... 45%\r")

        self.clock.advance(60)
        beat.tick()

        self.assertEqual(self.stream.getvalue(), "")

    def test_a_plain_stream_never_gets_a_heartbeat(self) -> None:
        ui.configure(plain=True)
        beat = self.beat()

        self.clock.advance(100)
        beat.tick()
        beat.erase()

        self.assertEqual(self.stream.getvalue(), "")

    def test_a_pipe_without_forced_colour_gets_none(self) -> None:
        ui.configure(color="auto")
        beat = self.beat()

        self.clock.advance(100)
        beat.tick()

        self.assertEqual(self.stream.getvalue(), "")

    def test_the_line_fits_one_terminal_row(self) -> None:
        beat = Heartbeat("x" * 500, self.stream, clock=self.clock, after=0)

        self.clock.advance(5)
        beat.tick()

        visible = self.stream.getvalue().replace("\r\x1b[2K", "")
        self.assertLessEqual(ui.visible_len(visible), ui.width())


class Defaults(unittest.TestCase):
    def test_the_stall_time_is_120_seconds_unless_the_environment_says_otherwise(self) -> None:
        self.assertEqual(watchdog.stall_seconds({}), 120.0)
        self.assertEqual(watchdog.stall_seconds({watchdog.STALL_ENV: "300"}), 300.0)

    def test_the_steps_own_value_wins_over_the_environment(self) -> None:
        self.assertEqual(watchdog.stall_seconds({watchdog.STALL_ENV: "300"}, 45), 45.0)

    def test_a_bad_value_falls_back_to_the_default(self) -> None:
        for bad in ("", "abc", "-5", "0", "2", "1e99"):
            with self.subTest(bad=bad):
                self.assertEqual(watchdog.stall_seconds({watchdog.STALL_ENV: bad}), 120.0)


if __name__ == "__main__":
    unittest.main()

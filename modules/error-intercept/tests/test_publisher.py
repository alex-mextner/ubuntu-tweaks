"""The agent-sub publisher: argv, reply parsing, failures kept pending, retry, where agent-sub is found."""

import errno
import json
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from ei_fixtures import Case, FailingPublish, FakePublish

from error_intercept.publisher import (
    LOCAL_PREFIX,
    AgentSubPublisher,
    DurablePublisher,
    PublishError,
    locate_agent_sub,
    tool_path,
)

EVENT = {
    "topic": "errors",
    "kind": "crash",
    "severity": "critical",
    "source": "gnome-shell",
    "key": "gnome-shell|SIGSEGV|f",
    "text": "--starts with dashes\nsecond line",
    "data": {"signal": "SIGSEGV"},
    "frames": ["/tmp/a.png"],
}


class Runner:
    def __init__(
        self, stdout='{"id": "ev-7", "status": "pending", "detail": ""}', returncode=0, stderr="", raises=None
    ):
        self.calls = []
        self.reply = (stdout, returncode, stderr)
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.reply[1], self.reply[0], self.reply[2])


class AgentSubPublisherTest(Case):
    def publisher(self, runner, launcher="/x/agent-sub"):
        return AgentSubPublisher(self.paths, self.env, runner, locate=lambda: launcher)

    def test_argv_uses_equals_forms_so_text_cannot_be_read_as_an_option(self):
        runner = Runner()
        self.assertEqual(self.publisher(runner)(EVENT), "ev-7")
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv[:2], ["/x/agent-sub", "publish"])
        self.assertIn("--text=--starts with dashes\nsecond line", argv)
        self.assertIn("--topic=errors", argv)
        self.assertIn("--key=gnome-shell|SIGSEGV|f", argv)
        self.assertIn("--frame=/tmp/a.png", argv)
        self.assertEqual(json.loads(next(a for a in argv if a.startswith("--data="))[7:]), {"signal": "SIGSEGV"})
        self.assertEqual(argv[-1], "--json")
        self.assertIn(str(self.home / ".local/bin"), kwargs["env"]["PATH"].split(os.pathsep))

    def test_replay_runs_agent_sub_replay(self):
        runner = Runner()
        self.assertEqual(self.publisher(runner).replay("ev-3"), "ev-3")  # the event keeps its id
        self.assertEqual(runner.calls[0][0], ["/x/agent-sub", "replay", "ev-3"])  # replay has no --json

    def test_every_failure_is_a_publish_error(self):
        cases = [
            Runner(returncode=2, stderr="agent-sub: bad\n"),
            Runner(stdout="not json"),
            Runner(stdout="[]"),
            Runner(stdout='{"status": "pending"}'),
            Runner(raises=subprocess.TimeoutExpired("agent-sub", 1)),
            Runner(raises=FileNotFoundError("agent-sub")),
        ]
        for runner in cases:
            with self.assertRaises(PublishError):
                self.publisher(runner)(EVENT)

    def test_a_nul_byte_is_a_permanent_refusal_and_agent_sub_is_never_started(self):
        for field in ("text", "key", "source", "kind"):
            runner = Runner()
            with self.assertRaises(PublishError) as raised:
                self.publisher(runner)({**EVENT, field: "bad\x00byte"})
            self.assertTrue(raised.exception.permanent, field)
            self.assertEqual(runner.calls, [], field)

    def test_an_argument_over_the_kernel_limit_is_a_permanent_refusal(self):
        for text in ("x" * 200_000, "я" * 70_000):  # 140 KB in UTF-8: over what one argument may hold (128 KiB)
            runner = Runner()
            with self.assertRaises(PublishError) as raised:
                self.publisher(runner)({**EVENT, "text": text})
            self.assertTrue(raised.exception.permanent)
            self.assertEqual(runner.calls, [])

    def test_an_event_just_under_the_limit_is_published(self):
        runner = Runner()
        self.assertEqual(self.publisher(runner)({**EVENT, "text": "x" * 90_000}), "ev-7")

    def test_a_value_error_is_permanent_but_the_kernel_refusing_the_environment_is_retried(self):
        for failure, permanent in ((ValueError("embedded null byte"), True), (OSError(errno.E2BIG, "too long"), False)):
            with self.assertRaises(PublishError) as raised:
                self.publisher(Runner(raises=failure))(EVENT)
            self.assertEqual(raised.exception.permanent, permanent, failure)

    def test_output_that_is_not_utf8_is_retried_not_refused(self):
        failure = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")  # raised after the child ran
        with self.assertRaises(PublishError) as raised:
            self.publisher(Runner(raises=failure))(EVENT)
        self.assertFalse(raised.exception.permanent)

    def test_an_unlaunchable_agent_sub_is_still_retried(self):
        with self.assertRaises(PublishError) as raised:
            self.publisher(Runner(raises=FileNotFoundError("agent-sub")))(EVENT)
        self.assertFalse(raised.exception.permanent)

    def test_missing_agent_sub_is_a_publish_error_that_says_what_to_do(self):
        with self.assertRaisesRegex(PublishError, "agent_tools_dir"):
            self.publisher(Runner(), launcher=None)(EVENT)


class DurablePublisherTest(Case):
    def durable(self, inner):
        return DurablePublisher(inner, self.paths, self.audit, self.clock)

    def test_success_passes_the_id_through_and_keeps_nothing(self):
        self.assertEqual(self.durable(FakePublish())(EVENT), "ev-1")
        self.assertFalse(self.paths.pending.exists())

    def test_failure_is_audited_and_kept_for_retry(self):
        local = self.durable(FailingPublish("boom"))(EVENT)
        self.assertTrue(local.startswith(LOCAL_PREFIX))
        (kept,) = self.paths.pending.glob("*.json")
        self.assertEqual(oct(kept.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(kept.read_text())["event"], EVENT)
        (record,) = self.audit.records()
        self.assertEqual((record["decision"], record["event_id"], record["error"]), ("publish-failed", local, "boom"))

    def test_the_kept_event_is_synced_to_disk_before_the_caller_may_archive_the_report(self):
        synced = []
        real_fsync = os.fsync

        def recording_fsync(fd):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
            real_fsync(fd)

        with mock.patch("error_intercept.durable.os.fsync", recording_fsync):
            self.durable(FailingPublish())(EVENT)
        pending = str(self.paths.pending)
        file_at = next(
            i for i, p in enumerate(synced) if p.startswith(pending + "/.tmp-")
        )  # the file, before the rename
        self.assertLess(file_at, synced.index(pending))  # then the directory that holds the new name

    def test_a_refused_event_is_set_aside_and_does_not_block_the_queue(self):
        self.durable(FailingPublish())({**EVENT, "key": "bad"})  # kept for retry
        self.durable(FailingPublish())({**EVENT, "key": "good"})

        class RejectsTheFirst:
            def __init__(self):
                self.events = []

            def __call__(self, event):
                if event["key"] == "bad":
                    raise PublishError("agent-sub exited 2: invalid severity", permanent=True)
                self.events.append(event)
                return "ev-9"

        inner = RejectsTheFirst()
        self.assertEqual(self.durable(inner).retry_pending(), ["ev-9"])
        self.assertEqual([e["key"] for e in inner.events], ["good"])
        self.assertEqual(len(list(self.paths.pending.glob("*.rejected"))), 1)
        self.assertEqual(list(self.paths.pending.glob("*.json")), [])
        self.assertIn("retry-rejected", self.audit_decisions())

    def test_a_refusal_at_publish_time_is_kept_audited_and_never_retried(self):
        class Refuses:
            def __call__(self, event):
                raise PublishError("agent-sub exited 2: bad", permanent=True)

        local = self.durable(Refuses())(EVENT)
        self.assertTrue(local.startswith(LOCAL_PREFIX))
        self.assertEqual(self.durable(FakePublish()).retry_pending(), [])
        self.assertEqual(len(list(self.paths.pending.glob("*.rejected"))), 1)
        self.assertEqual(self.audit.records()[0]["status"], "rejected")

    def test_a_refused_event_does_not_block_the_queue_behind_it(self):
        publisher = self.durable(AgentSubPublisher(self.paths, self.env, Runner(), locate=lambda: "/x/agent-sub"))
        refused = publisher({**EVENT, "text": "bad\x00byte"})
        self.assertTrue(refused.startswith(LOCAL_PREFIX))
        self.assertEqual(publisher({**EVENT, "key": "good"}), "ev-7")
        self.assertEqual(list(self.paths.pending.glob("*.json")), [])
        self.assertEqual(len(list(self.paths.pending.glob("*.rejected"))), 1)

    def test_an_event_without_required_keys_is_refused_permanently(self):
        with self.assertRaises(PublishError) as raised:
            AgentSubPublisher(self.paths, self.env, Runner(), locate=lambda: "/x/agent-sub")({"kind": "crash"})
        self.assertTrue(raised.exception.permanent)

    def test_exit_code_two_is_permanent_and_others_are_not(self):
        for code, permanent in ((2, True), (1, False), (127, False)):
            with self.assertRaises(PublishError) as raised:
                AgentSubPublisher(self.paths, self.env, Runner(returncode=code), locate=lambda: "/x/a")(EVENT)
            self.assertEqual(raised.exception.permanent, permanent)

    def test_retry_publishes_oldest_first_then_forgets_them(self):
        failing = self.durable(FailingPublish())
        first = failing(EVENT)
        self.now += 5
        second = failing({**EVENT, "key": "k2"})
        fake = FakePublish()
        self.assertEqual(self.durable(fake).retry_pending(), ["ev-1", "ev-2"])
        self.assertEqual([e["key"] for e in fake.events], ["gnome-shell|SIGSEGV|f", "k2"])
        self.assertEqual(list(self.paths.pending.glob("*.json")), [])
        self.assertEqual([r["was"] for r in self.audit.records() if r["decision"] == "retried"], [first, second])

    def test_retry_stops_at_the_first_failure_and_keeps_everything(self):
        self.durable(FailingPublish())(EVENT)
        self.assertEqual(self.durable(FailingPublish()).retry_pending(), [])
        self.assertEqual(len(list(self.paths.pending.glob("*.json"))), 1)

    def test_a_damaged_pending_file_is_set_aside_not_lost(self):
        self.paths.pending.mkdir(parents=True)
        (self.paths.pending / "pending-1-x.json").write_text("{broken")
        self.assertEqual(self.durable(FakePublish()).retry_pending(), [])
        self.assertTrue((self.paths.pending / "pending-1-x.bad").exists())
        self.assertIn("pending-unreadable", self.audit_decisions())


class ConcurrentRetryTest(Case):
    def test_two_consumers_publish_a_pending_event_once(self):
        failing = DurablePublisher(FailingPublish(), self.paths, self.audit, self.clock)
        failing(EVENT)
        published = []

        def slow(event):
            published.append(event)
            time.sleep(0.3)
            return f"ev-{len(published)}"

        results = []
        consumers = [DurablePublisher(slow, self.paths, self.audit, self.clock) for _ in range(2)]
        threads = [threading.Thread(target=lambda c=c: results.append(c.retry_pending())) for c in consumers]
        threads[0].start()
        time.sleep(0.1)
        threads[1].start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(published), 1)
        self.assertEqual(sorted(len(r) for r in results), [0, 1])
        self.assertEqual(list(self.paths.pending.glob("*.json")), [])


class LocateTest(Case):
    def make_launcher(self, root):
        launcher = root / "lib/agenttools_subscriptions/agent-sub"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        return str(launcher)

    def test_path_beats_the_config_and_the_config_beats_the_guesses(self):
        on_path = self.home / ".local/bin/agent-sub"
        on_path.parent.mkdir(parents=True)
        on_path.write_text("#!/bin/sh\n")
        on_path.chmod(0o755)
        self.assertEqual(locate_agent_sub(self.paths, self.env), str(on_path))
        on_path.unlink()
        guess = self.make_launcher(self.home / "xp/agent-tools")
        self.assertEqual(locate_agent_sub(self.paths, self.env), guess)
        configured = self.make_launcher(self.root / "elsewhere")
        self.paths.config.mkdir(parents=True)
        self.paths.config_file.write_text(json.dumps({"agent_tools_dir": str(self.root / "elsewhere")}))
        self.assertEqual(locate_agent_sub(self.paths, self.env), configured)

    def test_nothing_found_is_none(self):
        self.assertIsNone(locate_agent_sub(self.paths, self.env))

    def test_tool_path_appends_the_user_directories_once(self):
        value = tool_path({"PATH": "/usr/bin"}, self.home).split(os.pathsep)
        self.assertEqual(value[0], "/usr/bin")
        self.assertIn(str(self.home / ".bun/bin"), value)
        self.assertEqual(len(value), len(set(value)))


if __name__ == "__main__":
    unittest.main()

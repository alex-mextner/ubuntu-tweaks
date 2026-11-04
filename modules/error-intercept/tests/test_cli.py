"""The error-intercept command: ingest and the stop switch, list/replay/status/archive, the lazy journal source."""

import io
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

from ei_fixtures import MODULE, Case, crash_text

from error_intercept import cli
from error_intercept.paths import ORIGINAL_NOTIFIER

SCRIPT = MODULE / "error-intercept"


class FakeAgentSub:
    """The ``agent-sub`` the cli would run: records argv and answers like ``publish --json`` / ``replay --json``."""

    def __init__(self, fail=False, drain_fails=False):
        self.calls = []
        self.fail = fail
        self.drain_fails = drain_fails

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == "drain":  # no --json there either
            if self.drain_fails or "--json" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "agent-sub: down")
            return subprocess.CompletedProcess(argv, 0, "nothing pending\n", "")
        if argv[1] == "replay" and "--json" in argv:  # the real parser rejects it
            return subprocess.CompletedProcess(argv, 2, "", "agent-sub: error: unrecognized arguments: --json")
        if argv[1] == "replay":
            return subprocess.CompletedProcess(argv, 0, f"{argv[2]}: delivered\n", "")
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "agent-sub: down")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"id": f"ev-{len(self.calls)}", "status": "pending", "detail": ""}), ""
        )


class CliCase(Case):
    def setUp(self):
        super().setUp()
        self.runner = FakeAgentSub()
        self.execs = []
        self.launcher = self.home / ".local/bin/agent-sub"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\n")
        self.launcher.chmod(0o755)

    def run_cli(self, *argv, runner=None, **seams):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            env=self.env,
            home=self.home,
            out=out,
            err=err,
            runner=runner or self.runner,
            execv=lambda path, args: self.execs.append((path, args)),
            clock=self.clock,
            sleep=lambda s: None,
            quiet_s=0,
            **seams,
        )
        return code, out.getvalue(), err.getvalue()


class IngestCommandTest(CliCase):
    def test_ingest_publishes_archives_and_exits_zero(self):
        self.write_crash()
        code, out, err = self.run_cli("ingest")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 published, 1 archived", out)
        self.assertEqual(self.crash_files(), [])
        self.assertEqual([c[1] for c in self.runner.calls], ["publish", "drain"])
        self.assertEqual(self.execs, [])

    def test_ingest_drains_agent_sub_after_the_work_and_a_drain_failure_is_not_fatal(self):
        self.write_crash()
        self.run_cli("ingest")
        self.assertEqual([c[1] for c in self.runner.calls], ["publish", "drain"])  # publish first, drain last
        self.assertNotIn("--json", self.runner.calls[-1])
        self.write_crash("_usr_bin_b.1000.crash", text=crash_text(exe="/usr/bin/b", cmdline="b"))
        flaky = FakeAgentSub(drain_fails=True)
        code, _, err = self.run_cli("ingest", runner=flaky)
        self.assertEqual(code, 0)
        self.assertIn("agent-sub drain failed", err)

    def test_dry_run_does_not_drain(self):
        self.write_crash()
        self.run_cli("ingest", "--dry-run")
        self.assertEqual(self.runner.calls, [])

    def test_drain_command(self):
        code, out, _ = self.run_cli("drain")
        self.assertEqual((code, self.runner.calls[0][1]), (0, "drain"))
        code, _, err = self.run_cli("drain", runner=FakeAgentSub(drain_fails=True))
        self.assertEqual(code, 1)
        self.assertIn("agent-sub drain failed", err)

    def test_a_second_run_does_nothing(self):
        self.write_crash()
        self.run_cli("ingest")
        code, out, _ = self.run_cli("ingest")
        self.assertEqual(code, 0)
        self.assertIn("0 published, 0 archived", out)
        self.assertEqual([c[1] for c in self.runner.calls].count("publish"), 1)

    def test_agent_sub_down_still_exits_zero_keeps_the_event_and_the_next_run_retries(self):
        self.write_crash()
        code, _, _ = self.run_cli("ingest", runner=FakeAgentSub(fail=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.crash_files(), [])
        self.assertEqual(len(list(self.paths.pending.glob("*.json"))), 1)
        code, out, _ = self.run_cli("ingest")
        self.assertEqual(code, 0)
        self.assertIn("retried pending event -> ev-1", out)
        self.assertEqual(list(self.paths.pending.glob("*.json")), [])

    def test_disabled_hands_over_to_the_original_dialog_and_touches_nothing(self):
        path = self.write_crash()
        self.run_cli("disable")
        self.assertTrue(self.paths.disabled.exists())
        code, _, _ = self.run_cli("ingest")
        self.assertEqual(self.execs, [(ORIGINAL_NOTIFIER, [ORIGINAL_NOTIFIER])])
        self.assertTrue(path.exists())
        self.assertEqual(self.runner.calls, [])
        self.run_cli("enable")
        self.assertFalse(self.paths.disabled.exists())
        self.execs.clear()
        self.run_cli("ingest")
        self.assertEqual(self.execs, [])
        self.assertFalse(path.exists())

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        path = self.write_crash()
        code, out, _ = self.run_cli("ingest", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would publish", out)
        self.assertIn("would handle: 0 published", out)
        self.assertTrue(path.exists())
        self.assertEqual((self.runner.calls, self.audit.records()), ([], []))

    def test_dry_run_while_disabled_says_so_and_does_not_exec(self):
        self.run_cli("disable")
        code, out, _ = self.run_cli("ingest", "--dry-run")
        self.assertEqual((code, self.execs), (0, []))
        self.assertIn("DISABLED", out)

    def test_crash_dir_option_scans_another_directory(self):
        other = self.root / "other"
        other.mkdir()
        (other / "_usr_bin_x.1000.crash").write_text(crash_text(cmdline="x"))
        os.utime(other / "_usr_bin_x.1000.crash", (self.now - 70, self.now - 60))
        code, out, _ = self.run_cli("ingest", "--crash-dir", str(other))
        self.assertEqual(code, 0)
        self.assertIn("1 published", out)

    def test_a_bug_exits_one_with_a_traceback_not_a_dialog(self):
        with mock.patch.object(cli.apport_source.Ingester, "run", side_effect=RuntimeError("bug")):
            code, _, err = self.run_cli("ingest")
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: bug", err)


class SpoolCommandTest(CliCase):
    def test_ingest_spool_publishes_and_archives(self):
        self.paths.spool.mkdir(parents=True)
        (self.paths.spool / "1.json").write_text(json.dumps({"rule": "r", "wm_class": "App", "action": "intercept"}))
        code, out, _ = self.run_cli("ingest-spool")
        self.assertEqual(code, 0)
        self.assertIn("1.json", out)
        self.assertEqual([c[1] for c in self.runner.calls], ["publish", "drain"])
        self.assertEqual(list(self.paths.spool.iterdir()), [])


class ListReplayStatusTest(CliCase):
    def setUp(self):
        super().setUp()
        self.write_crash()
        self.run_cli("ingest")

    def test_list_shows_the_audited_event_text_and_json(self):
        _, out, _ = self.run_cli("list")
        self.assertIn("ev-1", out)
        self.assertIn("gnome-shell|SIGSEGV|mtk_anonymous_file_open_fd|nested", out)
        _, raw, _ = self.run_cli("list", "--json")
        self.assertEqual(json.loads(raw)[0]["event_id"], "ev-1")

    def test_replay_of_a_published_event_uses_agent_sub_replay(self):
        code, out, _ = self.run_cli("replay", "ev-1")
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.calls[-1][1:3], ["replay", "ev-1"])
        self.assertIn("replayed ev-1", out)

    def test_replay_of_an_event_that_never_reached_agent_sub_publishes_it_again(self):
        broken = FakeAgentSub(fail=True)
        self.write_crash("_usr_bin_other.1000.crash", text=crash_text(exe="/usr/bin/other", cmdline="other"))
        self.run_cli("ingest", runner=broken)
        local = next(r["event_id"] for r in self.audit.records() if str(r.get("event_id", "")).startswith("pending-"))
        code, _, _ = self.run_cli("replay", local)
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.calls[-1][1], "publish")
        self.assertTrue(any("--key=other|SIGSEGV|" in arg for arg in self.runner.calls[-1]))

    def test_replay_of_an_unknown_id_is_exit_one(self):
        code, _, err = self.run_cli("replay", "ev-nope")
        self.assertEqual(code, 1)
        self.assertIn("no audited event", err)

    def test_status_reports_switch_agent_sub_archive_and_last_event(self):
        _, out, _ = self.run_cli("status")
        self.assertIn("interception: on", out)
        self.assertIn(str(self.launcher), out)
        self.assertIn("archive: 1 file(s)", out)
        self.assertIn("last ev-1", out)
        _, raw, _ = self.run_cli("status", "--json")
        facts = json.loads(raw)
        self.assertEqual((facts["enabled"], facts["archived"], facts["pending_retry"]), (True, 1, 0))

    def test_status_shows_the_switch_when_off(self):
        self.run_cli("disable")
        _, out, _ = self.run_cli("status")
        self.assertIn("OFF", out)

    def test_status_reports_a_bad_rules_file_and_the_default_off(self):
        self.paths.config.mkdir(parents=True, exist_ok=True)
        (self.paths.config / "intercept-rules.json").write_text('{"version": 1, "nested_test_shell": {"exempt": 1}}')
        _, out, _ = self.run_cli("status")
        self.assertIn("routed like any crash", out)
        self.assertIn("rules problem: user rules: nested_test_shell.exempt must be true or false", out)
        _, raw, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(raw)["nested_test_shell_exempt"], False)

    def test_archive_list(self):
        _, out, _ = self.run_cli("archive", "list")
        self.assertIn("_usr_bin_gnome-shell.1000.crash.", out)
        _, raw, _ = self.run_cli("archive", "list", "--json")
        self.assertEqual(len(json.loads(raw)), 1)


class JournalCommandTest(CliCase):
    def test_journal_runs_the_source_with_a_publisher_that_honours_the_switch(self):
        seen = []
        fake = types.ModuleType("error_intercept.journal_source")
        fake.main = lambda publish, argv=None: seen.append((publish, argv)) or 0
        with mock.patch.dict(sys.modules, {"error_intercept.journal_source": fake}):
            code, _, _ = self.run_cli("journal", "--dry-run", "--no-kernel")
        self.assertEqual(code, 0)
        self.assertEqual(seen[0][1], ["--dry-run", "--no-kernel"])
        event = {
            "topic": "errors",
            "kind": "js-error",
            "severity": "warning",
            "source": "gnome-shell",
            "key": "k",
            "text": "t",
            "data": {},
            "frames": [],
        }
        self.assertEqual(seen[0][0](event), "ev-1")
        self.run_cli("disable")
        self.assertEqual(seen[0][0](event), "disabled")
        self.assertEqual(len(self.runner.calls), 1)

    def test_journal_without_the_source_module_is_a_clear_exit_two(self):
        with mock.patch.dict(sys.modules, {"error_intercept.journal_source": None}):
            code, _, err = self.run_cli("journal")
        self.assertEqual(code, 2)
        self.assertIn("journal source", err)


class ScriptTest(Case):
    def test_the_executable_runs_from_any_directory_and_its_help_lists_the_commands(self):
        env = {**os.environ, **self.env}
        done = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, cwd="/", env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        for command in (
            "ingest",
            "ingest-spool",
            "journal",
            "list",
            "replay",
            "status",
            "enable",
            "disable",
            "archive",
        ):
            self.assertIn(command, done.stdout)

    def test_the_executable_dry_runs_against_a_temp_crash_dir(self):
        self.write_crash()
        env = {**os.environ, **self.env}
        done = subprocess.run([str(SCRIPT), "ingest", "--dry-run"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("would publish", done.stdout)
        self.assertEqual(len(self.crash_files()), 1)


if __name__ == "__main__":
    unittest.main()

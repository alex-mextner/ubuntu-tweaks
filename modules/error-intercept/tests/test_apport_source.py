"""The crash-directory scan: publish, archive, backlog, root-owned reports, failures, idempotence."""

import gzip
import json
import os
import unittest
from unittest import mock

from ei_fixtures import FAKE_TOKEN, USER_HOME, Case, FailingPublish, FakePublish, crash_text

from error_intercept import apport_source as apport_module
from error_intercept.apport_source import BACKLOG_S, Ingester, apport_can_list, metadata_event
from error_intercept.publisher import LOCAL_PREFIX, DurablePublisher


class IngestCase(Case):
    def ingester(self, publish, **kwargs):
        kwargs.setdefault("quiet_s", 0)
        return Ingester(self.paths, publish, self.audit, clock=self.clock, sleep=lambda s: None, **kwargs)

    def listed(self):
        return [p.name for p in self.crash.glob("*.crash") if apport_can_list(p, os.geteuid())]


class PublishAndArchiveTest(IngestCase):
    def test_report_is_published_sanitized_archived_and_no_longer_listed_by_apport(self):
        path = self.write_crash()
        (self.crash / "_usr_bin_gnome-shell.1000.upload").write_text("")
        self.assertEqual(self.listed(), [path.name])  # apport-checkreports would show the dialog for it
        publish = FakePublish()
        outcome = self.ingester(publish).run()
        self.assertEqual((outcome.published, outcome.archived), (1, 1))
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.crash_files(), [])  # report and .upload stamp are gone
        (event,) = publish.events
        self.assertEqual(
            (event["topic"], event["kind"], event["severity"], event["source"]),
            ("errors", "crash", "info", "gnome-shell"),
        )
        self.assertEqual(event["key"], "gnome-shell|SIGSEGV|mtk_anonymous_file_open_fd|nested")
        self.assertIn("nested-test-shell", event["data"]["tags"])
        blob = json.dumps(event)
        for secret in (FAKE_TOKEN, "hunter2", "ProcEnviron", "ProcMaps", USER_HOME, "Too many levels"):
            self.assertNotIn(secret, blob)
        self.assertLessEqual(len(event["text"]), 2048)
        self.assertLessEqual(len(json.dumps(event["data"])), 8192)
        (archived,) = list(self.paths.archive.glob("*.gz"))
        self.assertEqual(oct(archived.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.paths.archive.stat().st_mode & 0o777), "0o700")
        self.assertEqual(gzip.decompress(archived.read_bytes()).decode(), crash_text())
        self.assertEqual(self.audit_decisions(), ["published", "archived"])
        self.assertEqual(self.audit.records()[0]["event_id"], "ev-1")

    def test_event_names_the_archive_it_will_live_in(self):
        self.write_crash()
        publish = FakePublish()
        self.ingester(publish).run()
        (archived,) = list(self.paths.archive.glob("*.gz"))
        self.assertTrue(publish.events[0]["data"]["archive"].endswith(archived.name))
        self.assertTrue(publish.events[0]["data"]["archive"].startswith("~/"))

    def test_a_claim_left_by_a_crashed_run_is_restored_and_ingested(self):
        path = self.write_crash()
        (self.crash / ".ei-claims").mkdir()
        os.rename(path, self.crash / ".ei-claims" / path.name)  # the previous run died after claiming it
        publish = FakePublish()
        outcome = self.ingester(publish).run()
        self.assertEqual((outcome.published, outcome.archived, len(publish.events)), (1, 1, 1))
        self.assertEqual(self.crash_files(), [])
        self.assertIn("claim-recovered", self.audit_decisions())

    def test_a_report_that_vanishes_during_the_scan_is_skipped(self):
        path = self.write_crash()
        real_wait = apport_module.wait_until_written

        def vanishing(report, quiet_s, clock, sleep):
            report.unlink()
            real_wait(report, quiet_s, clock, sleep)

        publish = FakePublish()
        with mock.patch.object(apport_module, "wait_until_written", vanishing):
            outcome = self.ingester(publish).run()
        self.assertEqual((publish.events, outcome.archived), ([], 0))
        self.assertFalse(path.exists())

    def test_rerun_is_idempotent(self):
        self.write_crash()
        publish = FakePublish()
        self.ingester(publish).run()
        again = self.ingester(publish).run()
        self.assertEqual((len(publish.events), again.published, again.archived), (1, 0, 0))

    def test_reading_does_not_mark_the_report_seen(self):
        path = self.write_crash()
        before = path.stat()
        self.ingester(FakePublish(), dry_run=True).run()
        after = path.stat()
        self.assertEqual((before.st_atime_ns, before.st_mtime_ns), (after.st_atime_ns, after.st_mtime_ns))

    def test_dry_run_changes_nothing_and_publishes_nothing(self):
        path = self.write_crash()
        publish = FakePublish()
        outcome = self.ingester(publish, dry_run=True).run()
        self.assertTrue(path.exists())
        self.assertEqual((publish.events, list(self.paths.state.glob("*"))), ([], []))
        self.assertTrue(any("would publish" in line for line in outcome.lines))
        self.assertTrue(any("would archive" in line for line in outcome.lines))

    def test_a_report_still_being_written_is_waited_for(self):
        path = self.write_crash(age_s=0.0)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.now += 1.0

        Ingester(self.paths, FakePublish(), self.audit, clock=self.clock, sleep=sleep, quiet_s=3.0).run()
        self.assertEqual(len(sleeps), 3)
        self.assertFalse(path.exists())

    def test_unparsable_report_is_archived_with_a_metadata_event(self):
        self.write_crash("_usr_bin_app.1000.crash", text="not an apport report\n")
        publish = FakePublish()
        self.ingester(publish).run()
        (event,) = publish.events
        self.assertEqual(event["key"], "app|unknown|unparsable")
        self.assertIn("Parsing failed", event["text"])
        self.assertEqual(self.audit_decisions(), ["parse-failed", "archived"])
        self.assertEqual(len(list(self.paths.archive.glob("*.gz"))), 1)


class BacklogTest(IngestCase):
    def test_unseen_recent_is_published_old_and_seen_are_archived_silently(self):
        self.write_crash("_usr_bin_a.1000.crash", age_s=3600)
        self.write_crash("_usr_bin_b.1000.crash", age_s=BACKLOG_S + 60)
        self.write_crash("_usr_bin_c.1000.crash", age_s=3600, seen=True)
        publish = FakePublish()
        outcome = self.ingester(publish).run()
        self.assertEqual([e["source"] for e in publish.events], ["gnome-shell"])
        self.assertEqual(outcome.archived, 3)
        self.assertEqual(self.crash_files(), [])
        self.assertEqual(self.audit_decisions().count("backlog-archived"), 2)
        reasons = {
            r.get("report"): r.get("reason") for r in self.audit.records() if r["decision"] == "backlog-archived"
        }
        self.assertEqual(
            reasons, {"_usr_bin_b.1000.crash": "older than 24 h", "_usr_bin_c.1000.crash": "already seen in apport"}
        )


class RootOwnedTest(IngestCase):
    def test_a_report_of_another_owner_gets_a_metadata_event_once_and_stays(self):
        path = self.write_crash("_usr_bin_sshd.0.crash")
        publish = FakePublish()
        foreign = os.geteuid() + 1  # the file is "owned by somebody else" from the ingester's point of view
        first = self.ingester(publish, uid=foreign).run()
        second = self.ingester(publish, uid=foreign).run()
        self.assertTrue(path.exists())
        self.assertEqual((first.metadata_only, second.metadata_only, len(publish.events)), (1, 0, 1))
        event = publish.events[0]
        self.assertEqual(
            (event["severity"], event["source"], event["data"]["metadata_only"]), ("warning", "sshd", True)
        )
        self.assertNotIn("CoreDump", json.dumps(event))
        self.assertEqual(self.audit_decisions(), ["metadata-only"])

    @unittest.skipIf(os.geteuid() == 0, "root can read everything")
    def test_an_unreadable_report_of_the_user_is_metadata_only_and_not_touched(self):
        path = self.write_crash("_usr_bin_locked.1000.crash", mode=0o000)
        publish = FakePublish()
        self.ingester(publish).run()
        self.assertTrue(path.exists())
        self.assertEqual([e["key"] for e in publish.events], ["locked|unknown|root-owned"])
        path.chmod(0o600)

    def test_an_old_unreadable_report_is_remembered_without_an_event(self):
        self.write_crash("_usr_bin_old.0.crash", age_s=BACKLOG_S + 1)
        publish = FakePublish()
        self.ingester(publish, uid=os.geteuid() + 1).run()
        self.assertEqual((publish.events, self.audit_decisions()), ([], ["backlog-left-in-place"]))

    def test_metadata_event_reads_program_owner_and_time_from_the_file(self):
        event = metadata_event("_usr_lib_x86_64-linux-gnu_foo_barbaz.0.crash", self.now, 0, self.home)
        self.assertEqual((event["source"], event["data"]["report_owner"]), ("barbaz", "0"))


class FailureTest(IngestCase):
    def test_publish_failure_keeps_the_event_pending_audits_it_and_still_archives_the_report(self):
        path = self.write_crash()
        failing = FailingPublish("agent-sub not found")
        outcome = self.ingester(DurablePublisher(failing, self.paths, self.audit, self.clock)).run()
        self.assertFalse(path.exists())  # apport can never show it
        self.assertEqual(outcome.archived, 1)
        (pending,) = list(self.paths.pending.glob("*.json"))
        kept = json.loads(pending.read_text())
        self.assertTrue(kept["id"].startswith(LOCAL_PREFIX))
        self.assertEqual(kept["event"]["key"], "gnome-shell|SIGSEGV|mtk_anonymous_file_open_fd|nested")
        self.assertEqual(kept["error"], "agent-sub not found")
        self.assertEqual(self.audit_decisions(), ["publish-failed", "published", "archived"])
        self.assertEqual(self.audit.records()[1]["status"], "pending-local")

    def test_a_failed_archive_leaves_the_report_and_says_so(self):
        path = self.write_crash()
        self.paths.state.mkdir(parents=True)
        self.paths.archive.write_text("a file where the archive directory should be")
        outcome = self.ingester(FakePublish()).run()
        self.assertTrue(path.exists())
        self.assertIn("archive-failed", self.audit_decisions())
        self.assertTrue(any("archive failed" in line for line in outcome.lines))

    def test_a_missing_crash_directory_is_not_an_error(self):
        self.crash.rmdir()
        outcome = self.ingester(FakePublish()).run()
        self.assertTrue(any("not readable" in line for line in outcome.lines))


if __name__ == "__main__":
    unittest.main()

"""The audit log under hostile conditions: file names that are not UTF-8, a write that dies half way."""

import errno
import json
import os
import unittest
from unittest import mock

from ei_fixtures import Case, FakePublish

from error_intercept.apport_source import Ingester
from error_intercept.audit import Audit


class AuditTest(Case):
    def test_a_torn_write_costs_one_line_and_the_next_append_starts_clean(self):
        real_write = os.write
        calls = []

        def dying_write(fd, data):
            calls.append(1)
            if len(calls) == 1:
                return real_write(fd, bytes(data)[:12])  # a short write ...
            raise OSError(errno.ENOSPC, "No space left on device")  # ... then the disk is full

        self.audit.append(decision="before")
        with mock.patch("error_intercept.audit.os.write", dying_write):
            with self.assertRaises(OSError):
                self.audit.append(decision="torn", event_id="ev-torn")
        self.audit.append(decision="after", event_id="ev-after")
        self.assertEqual(self.audit_decisions(), ["before", "after"])
        self.assertEqual(self.audit.find("ev-after")["decision"], "after")

    def test_a_short_write_that_succeeds_in_pieces_is_completed(self):
        real_write = os.write

        def stingy_write(fd, data):
            return real_write(fd, bytes(data)[:5])

        with mock.patch("error_intercept.audit.os.write", stingy_write):
            self.audit.append(decision="whole", event_id="ev-1", text="x" * 50)
        self.assertEqual(self.audit.find("ev-1")["text"], "x" * 50)

    def test_non_ascii_text_round_trips(self):
        Audit(self.paths.audit).append(decision="ru", event_id="ev-ru", text="ошибка")
        self.assertEqual(self.audit.find("ev-ru")["text"], "ошибка")


class HostileFileNameTest(Case):
    def make_foreign_report(self, raw_name):
        fd = os.open(os.path.join(os.fsencode(self.crash), raw_name), os.O_WRONLY | os.O_CREAT, 0o640)
        os.write(fd, b"ProblemType: Crash\n")
        os.close(fd)
        mtime = self.now - 60
        os.utime(os.path.join(os.fsencode(self.crash), raw_name), (mtime - 5, mtime))

    def ingester(self, publish, **kwargs):
        return Ingester(self.paths, publish, self.audit, clock=self.clock, sleep=lambda s: None, quiet_s=0, **kwargs)

    def test_a_report_named_with_an_invalid_utf8_byte_is_reported_once_and_never_aborts_the_scan(self):
        self.make_foreign_report(b"bad\xff.1001.crash")
        self.write_crash("_usr_bin_ok.1000.crash")
        publish = FakePublish()
        foreign = os.geteuid() + 1
        first = self.ingester(publish, uid=foreign).run()
        second = self.ingester(publish, uid=foreign).run()
        self.assertEqual((first.metadata_only, second.metadata_only), (2, 0))
        reported = [r["report"] for r in self.audit.records() if r.get("decision") == "metadata-only"]
        self.assertIn("bad\\udcff.1001.crash", reported)
        for event in publish.events:
            event["text"].encode("utf-8")  # no lone surrogate may reach agent-sub
            json.dumps(event, ensure_ascii=False).encode("utf-8")

    def test_an_owned_report_with_such_a_name_is_archived_under_it(self):
        self.make_foreign_report(b"bad\xff.1000.crash")
        outcome = self.ingester(FakePublish()).run()
        self.assertEqual(outcome.archived, 1)
        self.assertEqual(self.crash_files(), [])


if __name__ == "__main__":
    unittest.main()

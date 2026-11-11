"""The shell extension's spool: publish each file, then archive it."""

import gzip
import json
import threading
import time
import unittest
from unittest import mock

from ei_fixtures import Case, FakePublish

from error_intercept import spool as spool_module
from error_intercept.spool import MAX_FILE_BYTES, event_from_spool, ingest_spool

RECORD = {
    "v": 1,
    "ts": 1_800_000_000,
    "source": "shell-extension",
    "kind": "window",
    "severity": "warning",
    "key": "apport-crash|Apport-gtk",
    "rule": "apport-crash",
    "action": "intercept",
    "wm_class": "Apport-gtk",
    "app_id": "apport-gtk",
    "title": "Sorry, Ubuntu 26.04 has experienced an internal error. /home/alice/x",
    "pid": 4321,
}


class SpoolTest(Case):
    def drop(self, name, record=None, raw=None):
        self.paths.spool.mkdir(parents=True, exist_ok=True)
        path = self.paths.spool / name
        path.write_text(raw if raw is not None else json.dumps(record or RECORD))
        return path

    def test_a_file_becomes_an_event_and_is_archived(self):
        self.drop("1.json")
        publish = FakePublish()
        ingest_spool(self.paths, publish, self.audit)
        (event,) = publish.events
        self.assertEqual((event["kind"], event["severity"], event["source"]), ("window", "warning", "Apport-gtk"))
        self.assertEqual(event["key"], "apport-crash|Apport-gtk")
        self.assertIn("hidden", event["text"])
        self.assertNotIn("/home/alice", json.dumps(event))
        self.assertEqual(event["data"]["pid"], 4321)
        self.assertEqual(list(self.paths.spool.iterdir()), [])
        (archived,) = self.paths.spool_archive.glob("*.gz")
        self.assertEqual(json.loads(gzip.decompress(archived.read_bytes())), RECORD)
        self.assertEqual(self.audit_decisions(), ["spool-published"])

    def test_observe_says_left_visible(self):
        event = event_from_spool({**RECORD, "action": "observe"}, self.home)
        self.assertIn("left visible", event["text"])

    def test_unknown_severity_and_action_fall_back_safely(self):
        event = event_from_spool({"severity": "apocalyptic", "action": "explode", "wm_class": "X"}, self.home)
        self.assertEqual(event["severity"], "warning")
        self.assertIn("left visible", event["text"])
        self.assertEqual(event["key"], "unknown-rule|X")

    def test_a_lone_surrogate_in_a_title_cannot_make_the_event_unsendable(self):
        # JSON \\ud83d (half of an emoji) decodes to a str that cannot be encoded as UTF-8
        self.drop("1.json", raw='{"rule": "r", "wm_class": "App\\ud83d", "title": "broken \\ud83d title"}')
        publish = FakePublish()
        ingest_spool(self.paths, publish, self.audit)
        (event,) = publish.events
        json.dumps(event, ensure_ascii=False).encode("utf-8")
        self.assertIn("broken", event["text"])

    def test_a_symlink_in_the_spool_is_not_followed(self):
        target = self.root / "secret.json"
        target.write_text(json.dumps({"rule": "stolen", "wm_class": "X"}))
        self.paths.spool.mkdir(parents=True, exist_ok=True)
        (self.paths.spool / "1.json").symlink_to(target)
        publish = FakePublish()
        ingest_spool(self.paths, publish, self.audit)
        self.assertEqual(publish.events, [])
        self.assertTrue(target.exists())

    def test_files_being_written_are_skipped(self):
        self.drop(".1.json.tmp")
        self.drop("2.json.tmp")
        self.drop(".hidden.json")
        publish = FakePublish()
        ingest_spool(self.paths, publish, self.audit)
        self.assertEqual(publish.events, [])
        self.assertEqual(len(list(self.paths.spool.iterdir())), 3)

    def test_malformed_files_are_archived_with_a_reason_and_never_retried(self):
        self.drop("a.json", raw="{nope")
        self.drop("b.json", raw="[1]")
        self.drop("c.json", raw=" " * (MAX_FILE_BYTES + 1))
        publish = FakePublish()
        ingest_spool(self.paths, publish, self.audit)
        self.assertEqual(publish.events, [])
        self.assertEqual(list(self.paths.spool.iterdir()), [])
        self.assertEqual(len(list(self.paths.spool_archive.glob("*.gz"))), 3)
        reasons = [r["reason"] for r in self.audit.records()]
        self.assertEqual(reasons, ["not JSON", "not a JSON object", f"larger than {MAX_FILE_BYTES} bytes"])

    def test_odd_json_values_and_absurd_nesting_do_not_block_the_records_behind_them(self):
        self.drop("a.json", raw=json.dumps({"action": [], "severity": {}, "wm_class": "X"}))  # unhashable values
        self.drop("b.json", raw="[" * 12_000)  # nesting deeper than the JSON parser's recursion limit
        self.drop("c.json")
        publish = FakePublish()
        ingest_spool(self.paths, publish, self.audit)
        self.assertEqual([e["source"] for e in publish.events], ["X", "Apport-gtk"])
        self.assertEqual(list(self.paths.spool.glob("*.json")), [])
        self.assertEqual(
            [r["reason"] for r in self.audit.records() if r["decision"] == "spool-malformed"], ["not JSON"]
        )

    def test_a_record_the_converter_chokes_on_is_archived_as_malformed_and_the_next_is_published(self):
        self.drop("a.json", record={**RECORD, "key": "bad"})
        self.drop("b.json", record={**RECORD, "key": "good"})
        real = spool_module.event_from_spool

        def chokes_on_the_first(record, home):
            if record["key"] == "bad":
                raise TypeError("surprise")
            return real(record, home)

        publish = FakePublish()
        with mock.patch.object(spool_module, "event_from_spool", chokes_on_the_first):
            ingest_spool(self.paths, publish, self.audit)
        self.assertEqual([e["key"] for e in publish.events], ["good"])
        self.assertEqual(list(self.paths.spool.glob("*.json")), [])
        self.assertEqual(len(list(self.paths.spool_archive.glob("*.gz"))), 2)
        (bad,) = [r for r in self.audit.records() if r["decision"] == "spool-malformed"]
        self.assertIn("TypeError", bad["reason"])

    def test_an_archive_failure_of_one_file_quarantines_it_and_does_not_block_the_next(self):
        self.drop("a.json", record={**RECORD, "key": "first"})
        self.drop("b.json", record={**RECORD, "key": "second"})
        real = spool_module.store
        calls = []

        def fails_once(path, destination, mtime, target=None):
            calls.append(path.name)
            if len(calls) == 1:
                raise OSError("ENOSPC")
            return real(path, destination, mtime, target)

        publish = FakePublish()
        with mock.patch.object(spool_module, "store", fails_once):
            ingest_spool(self.paths, publish, self.audit)
        self.assertEqual([e["key"] for e in publish.events], ["first", "second"])
        self.assertEqual(sorted(p.name for p in self.paths.spool.iterdir()), ["a.json.bad"])
        self.assertIn("spool-quarantined", self.audit_decisions())
        again = FakePublish()
        ingest_spool(self.paths, again, self.audit)
        self.assertEqual(again.events, [])  # a quarantined file is not retried

    def test_disabled_leaves_the_spool_alone(self):
        self.drop("1.json")
        self.paths.disabled.parent.mkdir(parents=True, exist_ok=True)
        self.paths.disabled.write_text("1\n")
        publish = FakePublish()
        lines = ingest_spool(self.paths, publish, self.audit)
        self.assertEqual((publish.events, len(list(self.paths.spool.iterdir()))), ([], 1))
        self.assertTrue(lines[0].startswith("DISABLED"))

    def test_overlapping_consumers_publish_each_file_once(self):
        self.drop("1.json")
        published = []

        def slow(event):
            published.append(event)
            time.sleep(0.3)
            return f"ev-{len(published)}"

        runs = [threading.Thread(target=ingest_spool, args=(self.paths, slow, self.audit)) for _ in range(2)]
        runs[0].start()
        time.sleep(0.1)
        runs[1].start()
        for run in runs:
            run.join()
        self.assertEqual(len(published), 1)
        self.assertEqual(list(self.paths.spool.glob("*.json")), [])

    def test_a_file_that_vanishes_under_the_consumer_is_skipped(self):
        path = self.drop("1.json")
        publish = FakePublish()

        def vanishing(event):
            path.unlink()  # the other consumer archived it between our read and our archive
            return publish(event)

        ingest_spool(self.paths, vanishing, self.audit)
        self.assertEqual(len(publish.events), 1)

    def test_dry_run_publishes_and_moves_nothing(self):
        self.drop("1.json")
        publish = FakePublish()
        lines = ingest_spool(self.paths, publish, self.audit, dry_run=True)
        self.assertEqual((publish.events, len(list(self.paths.spool.iterdir()))), ([], 1))
        self.assertTrue(any("would publish" in line for line in lines))


if __name__ == "__main__":
    unittest.main()

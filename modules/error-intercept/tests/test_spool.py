"""The shell extension's spool: publish each file, then archive it."""

import gzip
import json
import threading
import time
import unittest

from ei_fixtures import Case, FakePublish

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
    "title": "Sorry, Ubuntu 26.04 has experienced an internal error. /home/ultra/x",
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
        self.assertNotIn("/home/ultra", json.dumps(event))
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

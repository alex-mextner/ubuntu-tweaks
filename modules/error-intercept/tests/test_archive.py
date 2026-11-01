"""Archiving a report: claim, compress, sync, only then remove; and what a failure at each step leaves behind."""

import gzip
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from ei_fixtures import Case

from error_intercept import archive
from error_intercept.archive import CLAIM_DIR, archive_file, archive_name, free_archive_path, recover_claims


class ArchiveTest(Case):
    def setUp(self):
        super().setUp()
        self.dest = self.root / "archive"

    def reports(self):
        return [n for n in os.listdir(self.crash) if n != CLAIM_DIR]

    def report(self, name="_usr_bin_app.1000.crash", text="ProblemType: Crash\nold\n"):
        path = self.crash / name
        path.write_text(text)
        return path

    def test_archive_holds_the_content_and_the_source_is_gone(self):
        path = self.report()
        target = archive_file(path, self.dest, 1_800_000_000.0)
        self.assertEqual(gzip.decompress(target.read_bytes()).decode(), "ProblemType: Crash\nold\n")
        self.assertEqual(self.reports(), [])
        self.assertEqual(os.listdir(self.dest), [target.name])  # no temporary file left behind

    def test_a_report_replaced_while_compressing_is_not_deleted(self):
        path = self.report()
        real_copy = shutil.copyfileobj

        def copy_then_apport_writes_a_new_report(source, destination, length=0):
            real_copy(source, destination, length)
            path.write_text("ProblemType: Crash\nnew report of the same program\n")  # apport reuses the name

        with mock.patch.object(archive.shutil, "copyfileobj", copy_then_apport_writes_a_new_report):
            target = archive_file(path, self.dest, 1_800_000_000.0)
        self.assertIn("old", gzip.decompress(target.read_bytes()).decode())
        self.assertEqual(path.read_text(), "ProblemType: Crash\nnew report of the same program\n")
        self.assertEqual(self.reports(), [path.name])
        self.assertEqual(os.listdir(self.crash / CLAIM_DIR), [])  # and no claim is left lying around

    def test_a_failed_directory_sync_puts_the_report_back_untouched(self):
        path = self.report()
        with mock.patch.object(archive, "fsync_dir", side_effect=OSError("EIO")):
            with self.assertRaises(OSError):
                archive_file(path, self.dest, 1_800_000_000.0)
        self.assertEqual(path.read_text(), "ProblemType: Crash\nold\n")
        self.assertEqual(self.reports(), [path.name])
        self.assertEqual([n for n in os.listdir(self.dest) if n.startswith(".")], [])

    def test_a_failed_compression_puts_the_report_back_and_leaves_no_temporary_file(self):
        path = self.report()
        with mock.patch.object(archive.shutil, "copyfileobj", side_effect=OSError("ENOSPC")):
            with self.assertRaises(OSError):
                archive_file(path, self.dest, 1_800_000_000.0)
        self.assertEqual(self.reports(), [path.name])
        self.assertEqual(os.listdir(self.dest), [])

    def test_long_names_stay_under_the_file_system_limit(self):
        for length in (200, 231, 240, 250, 251, 254, 255):
            name = "_" + "x" * (length - len(".1000.crash") - 1) + ".1000.crash"
            self.assertEqual(len(name), length)
            path = self.report(name)
            target = archive_file(path, self.dest, 1_800_000_000.0)
            self.assertLessEqual(len(os.fsencode(target.name)), 255, length)
        self.assertEqual(self.reports(), [])

    def test_long_names_are_bounded_and_distinct(self):
        a = archive_name(Path("_" + "a" * 240 + ".1000.crash"), 0)
        b = archive_name(Path("_" + "a" * 241 + ".1000.crash"), 0)
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(os.fsencode(a)), 255)

    def test_an_interrupted_run_is_recovered_at_the_next_scan(self):
        path = self.report()
        (self.crash / CLAIM_DIR).mkdir()
        os.rename(path, self.crash / CLAIM_DIR / path.name)  # what a crashed archive_file leaves
        restored = recover_claims(self.crash, os.geteuid())
        self.assertEqual(restored, [path])
        self.assertEqual(self.reports(), [path.name])

    def test_recovery_never_overwrites_a_new_report_it_restores_beside_it(self):
        path = self.report()
        (self.crash / CLAIM_DIR).mkdir()
        (self.crash / CLAIM_DIR / path.name).write_text("old claimed")
        restored = recover_claims(self.crash, os.geteuid())
        self.assertEqual([p.name for p in restored], ["_usr_bin_app.1000.r1.crash"])
        self.assertEqual(path.read_text(), "ProblemType: Crash\nold\n")
        self.assertEqual(restored[0].read_text(), "old claimed")
        self.assertEqual(sorted(self.reports()), ["_usr_bin_app.1000.crash", "_usr_bin_app.1000.r1.crash"])

    def test_a_failed_compression_after_apport_made_a_new_report_keeps_both_generations(self):
        path = self.report()

        def apport_writes_then_disk_fails(source, destination, length=0):
            path.write_text("ProblemType: Crash\nnew\n")
            raise OSError("ENOSPC")

        with mock.patch.object(archive.shutil, "copyfileobj", apport_writes_then_disk_fails):
            with self.assertRaises(OSError):
                archive_file(path, self.dest, 1_800_000_000.0)
        self.assertEqual(sorted(self.reports()), ["_usr_bin_app.1000.crash", "_usr_bin_app.1000.r1.crash"])
        self.assertEqual(os.listdir(self.crash / CLAIM_DIR), [])
        target = archive_file(path, self.dest, 1_800_000_000.0)  # and the next attempt is not blocked
        self.assertIn("new", gzip.decompress(target.read_bytes()).decode())

    def test_a_leftover_claim_does_not_block_the_next_archive_of_the_same_name(self):
        path = self.report()
        (self.crash / CLAIM_DIR).mkdir()
        (self.crash / CLAIM_DIR / path.name).write_text("leftover")
        with self.assertRaises(FileExistsError):
            archive_file(path, self.dest, 1_800_000_000.0)
        self.assertTrue(path.exists())
        recover_claims(self.crash, os.geteuid())  # what the next scan does first
        archive_file(path, self.dest, 1_800_000_000.0)
        self.assertEqual(sorted(self.reports()), ["_usr_bin_app.1000.r1.crash"])

    def test_two_reports_of_the_same_name_and_second_never_overwrite_each_other(self):
        first = archive_file(self.report(text="first"), self.dest, 1_800_000_000.1)
        second = archive_file(self.report(text="second"), self.dest, 1_800_000_000.9)
        self.assertNotEqual(first, second)
        self.assertEqual(gzip.decompress(first.read_bytes()), b"first")
        self.assertEqual(gzip.decompress(second.read_bytes()), b"second")

    def test_free_archive_path_names_the_file_the_next_archive_will_create(self):
        path = self.report()
        planned = free_archive_path(self.dest, path, 1_800_000_000.0)
        self.dest.mkdir()
        self.assertEqual(archive_file(path, self.dest, 1_800_000_000.0, planned), planned)


if __name__ == "__main__":
    unittest.main()

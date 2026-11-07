"""Archiving a report: claim, compress, sync, only then remove; and what a failure at each step leaves behind."""

import gzip
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from ei_fixtures import Case

from error_intercept import archive
from error_intercept.archive import (
    CLAIM_PREFIX,
    ClaimDirError,
    archive_name,
    claim,
    claim_dir,
    free_archive_path,
    recover_claims,
    release,
    store,
)

UID = os.geteuid()


def archive_file(source, archive_dir, mtime, target=None):
    """What the ingester does with one report: claim it, ``store`` it, put it back when that fails."""
    claimed = claim(source, UID)
    try:
        return store(claimed, archive_dir, mtime, target)
    except BaseException:
        release(claimed, source)
        raise


class ArchiveTest(Case):
    def setUp(self):
        super().setUp()
        self.dest = self.root / "archive"

    def claims(self):
        return self.crash / f"{CLAIM_PREFIX}{UID}"

    def reports(self):
        return [n for n in os.listdir(self.crash) if not n.startswith(CLAIM_PREFIX)]

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
        self.assertEqual(os.listdir(self.claims()), [])  # and no claim is left lying around

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
        os.rename(path, claim_dir(self.crash, UID) / path.name)  # what a crashed run leaves
        restored = recover_claims(self.crash, UID)
        self.assertEqual(restored, [path])
        self.assertEqual(self.reports(), [path.name])

    def test_recovery_never_overwrites_a_new_report_it_restores_beside_it(self):
        path = self.report()
        (claim_dir(self.crash, UID) / path.name).write_text("old claimed")
        restored = recover_claims(self.crash, UID)
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
        self.assertEqual(os.listdir(self.claims()), [])
        target = archive_file(path, self.dest, 1_800_000_000.0)  # and the next attempt is not blocked
        self.assertIn("new", gzip.decompress(target.read_bytes()).decode())

    def test_release_never_replaces_a_report_that_appears_under_the_free_name(self):
        path = self.report()
        claimed = claim(path, UID)
        real_link = os.link

        def apport_writes_first(source, target, **kwargs):
            if not path.exists():
                path.write_text("ProblemType: Crash\nnew\n")  # between the check and the link
            return real_link(source, target, **kwargs)

        with mock.patch.object(archive.os, "link", apport_writes_first):
            target = release(claimed, path)
        self.assertEqual(path.read_text(), "ProblemType: Crash\nnew\n")
        self.assertEqual(target.name, "_usr_bin_app.1000.r1.crash")

    def test_a_release_that_died_after_linking_is_completed_not_duplicated(self):
        path = self.report()
        claimed = claim(path, UID)
        os.link(claimed, path)  # the state a kill between link and unlink leaves
        self.assertEqual(release(claimed, path), path)
        self.assertEqual(self.reports(), [path.name])
        self.assertEqual(os.listdir(self.claims()), [])

    def test_a_leftover_claim_does_not_block_the_next_archive_of_the_same_name(self):
        path = self.report()
        (claim_dir(self.crash, UID) / path.name).write_text("leftover")
        target = archive_file(path, self.dest, 1_800_000_000.0)  # the claim of the report takes the next free name
        self.assertEqual(gzip.decompress(target.read_bytes()).decode(), "ProblemType: Crash\nold\n")
        self.assertEqual(self.reports(), [])
        restored = recover_claims(self.crash, UID)  # and the earlier run's claim is still restored, not lost
        self.assertEqual([p.read_text() for p in restored], ["leftover"])

    def test_claims_live_in_a_private_directory_of_the_user(self):
        claimed = claim(self.report(), UID)
        self.assertEqual(claimed.parent, self.claims())
        self.assertEqual(oct(self.claims().stat().st_mode & 0o777), "0o700")
        self.assertEqual(self.reports(), [])
        release(claimed, self.crash / claimed.name)
        self.assertEqual(self.reports(), ["_usr_bin_app.1000.crash"])

    def test_a_claim_directory_that_is_a_symlink_is_refused_and_the_report_stays(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.claims().symlink_to(elsewhere)
        path = self.report()
        with self.assertRaises(ClaimDirError):
            claim(path, UID)
        self.assertEqual(self.reports(), [path.name])
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(recover_claims(self.crash, UID), [])

    def test_a_claim_directory_open_to_others_is_refused(self):
        self.claims().mkdir()
        self.claims().chmod(0o777)
        with self.assertRaises(ClaimDirError):
            claim(self.report(), UID)

    def test_a_claim_directory_of_another_owner_is_refused(self):
        fake = UID + 1
        (self.crash / f"{CLAIM_PREFIX}{fake}").mkdir(
            mode=0o700
        )  # created by us, so not owned by the uid it is named for
        with self.assertRaises(ClaimDirError):
            claim(self.report(), fake)
        self.assertEqual(recover_claims(self.crash, fake), [])

    def test_restored_names_stay_within_the_file_system_limit(self):
        name = "_" + "x" * (255 - len(".crash") - 1) + ".crash"
        self.assertEqual(len(name), 255)
        path = self.report(name)
        claimed = claim(path, UID)
        self.report(name)  # apport wrote a new report under the same name
        target = release(claimed, path)
        self.assertLessEqual(len(os.fsencode(target.name)), 255)
        self.assertTrue(target.name.endswith(".crash"))
        self.assertEqual(len(self.reports()), 2)

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

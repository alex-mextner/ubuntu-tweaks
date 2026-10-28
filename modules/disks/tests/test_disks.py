"""disks module against a fake root and a stateful fake machine (no real fstab, systemd, apt, git or mount)."""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks import disks as lib  # noqa: E402
from ubuntu_tweaks.errors import ConfigError  # noqa: E402
from ubuntu_tweaks.module_script import (  # noqa: E402
    APT_ENVIRONMENT,
    EXIT_DRIFT,
    EXIT_NEEDS_MANUAL,
    EXIT_OK,
    EXIT_USAGE,
    Account,
    ModuleError,
    NeedsManual,
    main,
)
from ubuntu_tweaks.process import CommandError, CommandResult, Process  # noqa: E402


def load_script():
    path = str(MODULE / "disks")
    loader = importlib.machinery.SourceFileLoader("disks_script", path)
    spec = importlib.util.spec_from_loader("disks_script", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


disks = load_script()

APFS_UUID = "fcbf521d-af6b-463d-9959-d25f686b3379"
NTFS_UUID = "0C64C2AC64C2983A"
STOCK_FSTAB = "# /etc/fstab: static file system information.\nUUID=dd8ff67b / ext4 errors=remount-ro 0 1\nUUID=DCB1-9A1E /boot/efi vfat umask=0077 0 1\n"
APFS_ENTRY = (
    f"/dev/disk/by-uuid/{APFS_UUID} /mnt/mac-backups fuse.apfs-fuse "
    "ro,allow_other,uid=1000,gid=1000,nosuid,nodev,nofail,x-systemd.automount,x-systemd.device-timeout=10s 0 0"
)
APFSUTIL_PLAIN = (
    "Volume 0 E7613EA4-185F-471C-B090-972113929391\n---\nName: backups\nFileVault:          No\nSnapshots:\n"
)
APFSUTIL_VAULT = APFSUTIL_PLAIN.replace("FileVault:          No", "FileVault:          Yes")
MOUNTS = {"mounts": {"mac-backups": {"uuid": APFS_UUID, "fstype": "apfs"}}}


class FakeMachine(Process):
    """Answers the queries the module makes from a small state, and applies the mutations it executes."""

    def __init__(
        self, root, *, attached=True, packages=(), kernel_apfs=None, verify_errors=False, broken_before=False,
        apfsutil=APFSUTIL_PLAIN,
    ):  # fmt: skip
        self.root = root
        self.attached = attached
        self.packages = set(packages)
        self.kernel_apfs = kernel_apfs
        self.verify_errors = verify_errors
        self.broken_before = broken_before
        self.apfsutil = apfsutil
        self.modprobe_ok = True
        self.sig_key = "AA:BB:CC"  # the signer's key id in `modinfo -F sig_key`; "" is an unsigned module
        self.enrolled_keys = ["AA:BB:CC"]  # what `mokutil --list-enrolled` lists
        self.git_head = lib.PINNED_APFS_FUSE[1]
        self.symlink_binary = False
        self.clone_parent_modes = []
        self.mokutil_ok = True
        self.enrolled_format = "[key {n}]\n  X509v3 Subject Key Identifier:\n    {key}\n"
        self.fail_stop = False
        self.fail_start = None
        self.loaded = set()
        self.active = set()
        self.mounted = {}  # mount point -> (fstype, options)
        self.executed = []
        self.apt_runs = []  # (stdin text, the noninteractive variables the child would see) per apt-get call
        self.queried = []

    def set_lockdown(self, mode):
        """The kernel's lockdown file as the running kernel prints it, ``mode`` in brackets."""
        path = self.root / "sys/kernel/security/lockdown"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            " ".join(f"[{name}]" if name == mode else name for name in ("none", "integrity", "confidentiality")) + "\n"
        )

    def load_apfs(self):
        path = self.root / "proc/filesystems"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("nodev\tsysfs\nnodev\ttmpfs\n\text4\n\tapfs\n")

    # the units the generator would make from the managed block
    def _units(self):
        text = (self.root / "etc/fstab").read_text() if (self.root / "etc/fstab").exists() else ""
        units = set()
        for line in lib.managed_lines(text) or []:
            target = lib.fstab_unescape(line.split()[1])
            units.add(lib.unit_name(target, "mount"))
            if "x-systemd.automount" in line:
                units.add(lib.unit_name(target, "automount"))
        return units

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        self.queried.append(tuple(argv))
        if argv[0] == "dpkg-query":
            ok = argv[-1] in self.packages
            return CommandResult(0 if ok else 1, "install ok installed" if ok else "", "")
        if argv[:3] == ["modinfo", "-F", "version"]:
            return (
                CommandResult(0, f"v{self.kernel_apfs}\n", "")
                if self.kernel_apfs
                else CommandResult(1, "", "not found")
            )
        if argv[:4] == ["modinfo", "-F", "sig_key", "apfs"]:
            return CommandResult(0, f"{self.sig_key}\n" if self.sig_key else "", "")
        if argv[:2] == ["mokutil", "--list-enrolled"]:
            if not self.mokutil_ok:
                return CommandResult(1, "", "EFI variables are not supported on this system")
            body = "".join(self.enrolled_format.format(key=key, n=n) for n, key in enumerate(self.enrolled_keys))
            return CommandResult(0, body, "")
        if argv[:1] == ["git"] and argv[-2:] == ["rev-parse", "HEAD"]:
            return CommandResult(0, f"{self.git_head}\n", "")
        if argv[:2] == ["systemctl", "show"]:
            return CommandResult(0, "loaded\n" if argv[-1] in self.loaded else "not-found\n", "")
        if argv[:2] == ["systemctl", "is-active"]:
            on = argv[2] in self.active
            return CommandResult(0 if on else 3, "active\n" if on else "inactive\n", "")
        if argv[0] == "findmnt" and "--verify" in argv:
            ours = lib.MARK_BEGIN in Path(argv[-1]).read_text()
            errors = (1 if self.broken_before else 0) + (1 if self.verify_errors and ours else 0)
            return CommandResult(1 if errors else 0, f"0 parse errors, {errors} errors, 2 warnings\n", "")
        if argv[0] == "findmnt":
            point = argv[-1]
            lines = []
            if point in self.mounted:
                if f"{lib.unit_name(point, 'automount')}" in self.active:
                    lines.append("autofs rw,relatime")
                fstype, options = self.mounted[point]
                lines.append(f"{fstype} {options}")
            elif lib.unit_name(point, "automount") in self.active:
                lines.append("autofs rw,relatime")
            return CommandResult(0 if lines else 1, "\n".join(lines), "")
        if argv[0] == "uname":
            return CommandResult(0, "7.0.0-38-generic\n", "")
        if argv[0] == "modprobe":
            if self.modprobe_ok:
                self.load_apfs()
            return CommandResult(
                0 if self.modprobe_ok else 1, "", "" if self.modprobe_ok else "Key was rejected by service"
            )
        if argv[0].endswith("apfsutil"):
            return CommandResult(0, self.apfsutil, "")
        return CommandResult(127, "", f"{argv[0]}: not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        self.executed.append(tuple(argv))
        if argv[0] == "apt-get":
            self.apt_runs.append((input_text, {name: os.environ.get(name) for name in APT_ENVIRONMENT}))
        if argv[0] == "apt-get":
            self.packages.update(a for a in argv[3:] if not a.startswith("-"))
            if "apfs-dkms" in argv:
                self.kernel_apfs = "0.3.18"
        elif argv[:2] == ["git", "clone"]:
            self.clone_parent_modes.append(os.stat(Path(argv[-1]).parent).st_mode & 0o777)
            Path(argv[-1]).mkdir(parents=True)
        elif argv[:2] == ["cmake", "--build"]:
            build = Path(argv[2])
            build.mkdir(parents=True, exist_ok=True)
            if self.symlink_binary:
                (build / "apfs-fuse").symlink_to("/etc/hostname")
            else:
                (build / "apfs-fuse").write_bytes(b"#!/bin/sh\n")
            (build / "apfsutil").write_bytes(b"#!/bin/sh\n")
        elif argv[:2] == ["systemctl", "daemon-reload"]:
            self.loaded = self._units()
        elif argv[:2] == ["systemctl", "stop"]:
            if self.fail_stop:
                raise CommandError(f"{' '.join(argv)}: exited with status 1")
            self.active.discard(argv[2])
        elif argv[:2] == ["systemctl", "start"]:
            unit = argv[2]
            if unit.endswith(".mount") and self.fail_start:
                raise CommandError(f"{' '.join(argv)}: exited with status 1")
            self.active.add(unit)
            if unit.endswith(".mount"):
                for line in lib.managed_lines((self.root / "etc/fstab").read_text()) or []:
                    fields = line.split()
                    if lib.unit_name(lib.fstab_unescape(fields[1]), "mount") == unit:
                        self.mounted[lib.fstab_unescape(fields[1])] = ("fuse", fields[3])


class DisksCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="disks-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "etc").mkdir()
        (self.root / "etc/fstab").write_text(STOCK_FSTAB)
        self.account = Account("alice", Path("/home/alice"), 1000)  # a fixed non-root uid, whoever runs the tests
        self.chowned = []

    def attach(self, uuid=APFS_UUID, kind="by-uuid"):
        link = self.root / f"dev/disk/{kind}/{uuid}"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("")

    def machine(self, **kwargs):
        machine = FakeMachine(self.root, **kwargs)
        if kwargs.get("attached", True):
            self.attach()
        return machine

    def chown(self, descriptor, uid, gid):
        """The seam of ``fchown``: remembers which directory (by its path through /proc) was handed to whom."""
        self.chowned.append((Path(os.readlink(f"/proc/self/fd/{descriptor}")), uid, gid))

    def module(self, machine, options=None, *, root=True, trusted_uid=None):
        return disks.Disks(
            machine,
            self.root,
            MOUNTS if options is None else options,
            self.account,
            is_root=root,
            owner_ids=lambda name: (1000, 1000),
            chown=self.chown,
            # the files of the fake root belong to whoever runs the tests
            trusted_uid=os.getuid() if trusted_uid is None else trusted_uid,
        )

    def fstab(self):
        return (self.root / "etc/fstab").read_text()

    def facts(self, module):
        return {finding.subject: finding for finding in module.findings()}


class FindingsTest(DisksCase):
    def test_nothing_declared_and_no_block_plans_nothing(self):
        module = self.module(self.machine(), {})
        self.assertEqual(module.findings(), [])
        self.assertEqual(module.plan(), [])

    def test_a_fresh_machine_misses_the_driver_entry_mount_point_and_units(self):
        machine = self.machine()
        facts = self.facts(self.module(machine))
        self.assertFalse(facts["apfs-fuse 66b86bd built and installed in /usr/local/bin/apfs-fuse"].ok)
        self.assertFalse(facts["mac-backups: /etc/fstab entry"].ok)
        self.assertEqual(facts["mac-backups: /etc/fstab entry"].detail, APFS_ENTRY)
        self.assertFalse(facts["mac-backups: mount point /mnt/mac-backups"].ok)
        self.assertFalse(facts["mac-backups: systemd knows mnt-mac\\x2dbackups.mount"].ok)
        self.assertFalse(facts["mac-backups: mnt-mac\\x2dbackups.automount active"].ok)
        self.assertIn("attached", facts[f"mac-backups: device /dev/disk/by-uuid/{APFS_UUID}"].detail)

    def test_an_unplugged_disk_is_not_drift_by_itself(self):
        machine = self.machine(attached=False)
        facts = self.facts(self.module(machine))
        device = facts[f"mac-backups: device /dev/disk/by-uuid/{APFS_UUID}"]
        self.assertTrue(device.ok)
        self.assertIn("not attached", device.detail)

    def test_plan_names_every_step_and_the_exact_entry(self):
        lines = self.module(self.machine()).plan()
        self.assertTrue(lines[0].startswith("apt-get install git cmake"))
        self.assertIn("mkdir /mnt/mac-backups", lines)
        self.assertIn(f"  {APFS_ENTRY}", lines)
        self.assertIn("systemctl daemon-reload; systemctl start mnt-mac\\x2dbackups.automount", lines)

    def test_a_foreign_fstab_line_for_the_same_device_is_a_manual_fact(self):
        (self.root / "etc/fstab").write_text(STOCK_FSTAB + f"UUID={APFS_UUID} /media/old apfs ro 0 0\n")
        facts = self.facts(self.module(self.machine()))
        finding = facts["mac-backups: no other /etc/fstab line uses its device or mount point"]
        self.assertFalse(finding.ok)
        self.assertTrue(finding.manual)
        self.assertIn("line 4", finding.detail)

    def test_mounted_read_write_while_read_only_is_declared_is_a_manual_fact(self):
        machine = self.machine()
        self.module(machine).apply()
        machine.mounted["/mnt/mac-backups"] = ("fuse", "rw,relatime")
        finding = self.facts(self.module(machine))["mac-backups: mounted read-only (fuse)"]
        self.assertFalse(finding.ok)
        self.assertTrue(finding.manual)


class ApplyTest(DisksCase):
    def test_apply_builds_the_driver_writes_the_block_and_mounts_the_disk(self):
        machine = self.machine()
        module = self.module(machine)
        module.apply()
        text = self.fstab()
        self.assertTrue(text.startswith(STOCK_FSTAB))  # the rest of the file stays as it was
        self.assertIn(APFS_ENTRY + "\n", text)
        self.assertIn(lib.MARK_BEGIN, text)
        self.assertEqual((self.root / "etc/fstab.pre-ubuntu-tweaks").read_text(), STOCK_FSTAB)
        self.assertTrue((self.root / "mnt/mac-backups").is_dir())
        self.assertEqual(
            (self.root / "usr/local/share/ubuntu-tweaks/apfs-fuse.commit").read_text().strip(),
            lib.PINNED_APFS_FUSE[1] + " root-build",
        )
        self.assertTrue(os.access(self.root / "usr/local/bin/apfs-fuse", os.X_OK))
        self.assertEqual(machine.executed[0][:3], ("apt-get", "install", "-y"))
        self.assertIn(
            ("git", "-C", machine.executed[2][2], "checkout", "--detach", lib.PINNED_APFS_FUSE[1]), machine.executed
        )
        self.assertIn(("systemctl", "daemon-reload"), machine.executed)
        self.assertIn(("systemctl", "start", "mnt-mac\\x2dbackups.automount"), machine.executed)
        self.assertIn(("systemctl", "start", "mnt-mac\\x2dbackups.mount"), machine.executed)
        self.assertEqual([f for f in module.findings() if not f.ok], [])

    def test_the_source_is_fetched_and_built_as_root_in_a_private_directory_never_in_a_user_tree(self):
        machine = self.machine()
        module = self.module(machine)
        module.apply()
        self.assertFalse(any(call[0] == "runuser" for call in machine.executed))
        self.assertEqual(machine.clone_parent_modes, [0o700])  # a fresh directory only root can enter
        self.assertEqual(self.chowned, [])  # and nothing was handed to the desktop user
        self.assertEqual((self.root / "usr/local/bin/apfs-fuse").read_bytes(), b"#!/bin/sh\n")

    def test_a_checkout_that_is_not_the_pinned_commit_is_never_installed(self):
        machine = self.machine()
        machine.git_head = "0" * 40
        with self.assertRaises(ModuleError) as caught:
            self.module(machine).apply()
        self.assertIn(lib.PINNED_APFS_FUSE[1][:7], str(caught.exception))
        self.assertFalse((self.root / "usr/local/bin/apfs-fuse").exists())
        self.assertFalse((self.root / "usr/local/share/ubuntu-tweaks/apfs-fuse.commit").exists())

    def test_a_build_result_that_is_a_symlink_is_never_copied(self):
        machine = self.machine()
        machine.symlink_binary = True
        with self.assertRaises(ModuleError):
            self.module(machine).apply()
        self.assertFalse((self.root / "usr/local/bin/apfs-fuse").exists())

    def test_a_second_apply_changes_nothing(self):
        machine = self.machine()
        self.module(machine).apply()
        before = self.fstab()
        machine.executed.clear()
        module = self.module(machine)
        module.apply()
        self.assertEqual(self.fstab(), before)
        self.assertFalse(any(call[0] in ("apt-get", "git", "cmake") for call in machine.executed))
        self.assertEqual(module.plan(), [])

    def test_a_detached_disk_gets_its_entry_and_automount_but_no_mount_attempt(self):
        machine = self.machine(attached=False)
        module = self.module(machine)
        module.apply()
        self.assertIn(APFS_ENTRY, self.fstab())
        self.assertIn(("systemctl", "start", "mnt-mac\\x2dbackups.automount"), machine.executed)
        self.assertNotIn(("systemctl", "start", "mnt-mac\\x2dbackups.mount"), machine.executed)
        self.assertEqual([f for f in module.findings() if not f.ok], [])

    def test_findmnt_rejecting_the_new_fstab_leaves_the_file_alone(self):
        machine = self.machine(verify_errors=True)
        module = self.module(machine)
        with self.assertRaises(ModuleError) as caught:
            module.apply()
        self.assertIn("nothing was written", str(caught.exception))
        self.assertEqual(self.fstab(), STOCK_FSTAB)
        self.assertFalse((self.root / "etc/fstab.pre-ubuntu-tweaks").exists())

    def test_errors_the_fstab_already_had_do_not_block_our_block(self):
        machine = self.machine(broken_before=True)
        self.module(machine).apply()
        self.assertIn(APFS_ENTRY, self.fstab())

    def test_a_failing_mount_is_reported_with_the_journal_command_after_the_config_is_in_place(self):
        machine = self.machine()
        machine.fail_start = True
        with self.assertRaises(ModuleError) as caught:
            self.module(machine).apply()
        self.assertIn("journalctl -b -u mnt-mac\\x2dbackups.mount", str(caught.exception))
        self.assertIn(APFS_ENTRY, self.fstab())

    def test_a_filevault_volume_is_a_manual_step_not_a_failed_mount(self):
        machine = self.machine(apfsutil=APFSUTIL_VAULT)
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine).apply()
        self.assertIn("FileVault", str(caught.exception))
        self.assertNotIn(("systemctl", "start", "mnt-mac\\x2dbackups.mount"), machine.executed)
        self.assertIn(APFS_ENTRY, self.fstab())  # the entry is right; nofail keeps the boot clean

    def test_a_stale_entry_is_replaced_and_an_empty_declaration_removes_the_block(self):
        machine = self.machine(attached=False)
        self.module(machine).apply()
        two = {"mounts": {"mac-backups": MOUNTS["mounts"]["mac-backups"], "win": {"uuid": NTFS_UUID, "fstype": "ntfs"}}}
        self.assertEqual(self.facts(self.module(machine, two))["win: /etc/fstab entry"].ok, False)
        self.module(machine, two).apply()
        self.assertIn(
            f"/dev/disk/by-uuid/{NTFS_UUID} /mnt/win ntfs3 ro,uid=1000,gid=1000,umask=022,nosuid,nodev,nofail",
            self.fstab(),
        )
        only_ntfs = {"mounts": {"win": two["mounts"]["win"]}}
        stale = [f for f in self.module(machine, only_ntfs).findings() if not f.ok and "declared" in f.subject]
        self.assertEqual(len(stale), 1)
        self.module(machine, only_ntfs).apply()
        self.assertNotIn(APFS_UUID, self.fstab())
        self.module(machine, {}).apply()
        self.assertEqual(self.fstab(), STOCK_FSTAB)

    def test_apply_needs_root(self):
        with self.assertRaises(ModuleError):
            self.module(self.machine(), root=False).apply()

    def test_a_declared_owner_name_is_looked_up_for_uid_and_gid(self):
        machine = self.machine()
        options = {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "owner": "bob"}}}
        module = disks.Disks(
            machine,
            self.root,
            options,
            self.account,
            is_root=True,
            owner_ids=lambda name: (2000, 2001) if name == "bob" else (1, 1),
        )
        module.apply()
        self.assertIn("uid=2000,gid=2001", self.fstab())

    def test_a_removed_declaration_stops_its_automount_unit_and_never_unmounts(self):
        machine = self.machine(attached=False)  # nothing mounted behind the automount units
        two = {"mounts": {"mac-backups": MOUNTS["mounts"]["mac-backups"], "win": {"uuid": NTFS_UUID, "fstype": "ntfs"}}}
        self.module(machine, two).apply()
        self.assertLessEqual({"mnt-mac\\x2dbackups.automount", "mnt-win.automount"}, machine.active)
        self.module(machine, {"mounts": {"win": two["mounts"]["win"]}}).apply()
        self.assertIn(("systemctl", "stop", "mnt-mac\\x2dbackups.automount"), machine.executed)
        self.assertNotIn("mnt-mac\\x2dbackups.automount", machine.active)
        self.assertIn("mnt-win.automount", machine.active)
        self.module(machine, {}).apply()
        self.assertIn(("systemctl", "stop", "mnt-win.automount"), machine.executed)
        self.assertEqual(machine.active & {"mnt-win.automount", "mnt-mac\\x2dbackups.automount"}, set())
        self.assertFalse(
            any(call[:2] == ("systemctl", "stop") and call[2].endswith(".mount") for call in machine.executed)
        )
        self.assertFalse(any(call[0] == "umount" for call in machine.executed))

    def test_a_moved_mount_point_stops_the_automount_of_the_old_one(self):
        machine = self.machine(attached=False)
        self.module(machine).apply()
        moved = {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "mountpoint": "/mnt/backups-here"}}}
        self.module(machine, moved).apply()
        self.assertIn(("systemctl", "stop", "mnt-mac\\x2dbackups.automount"), machine.executed)
        self.assertNotIn(("systemctl", "stop", "mnt-backups\\x2dhere.automount"), machine.executed)
        self.assertIn("mnt-backups\\x2dhere.automount", machine.active)

    def test_a_second_apply_stops_nothing(self):
        machine = self.machine()
        self.module(machine).apply()
        machine.executed.clear()
        self.module(machine).apply()
        self.assertFalse(any(call[:2] == ("systemctl", "stop") for call in machine.executed))

    def test_a_removed_disk_that_is_still_mounted_is_a_manual_step_and_nothing_is_stopped_or_changed(self):
        machine = self.machine()
        self.module(machine).apply()  # attached: the filesystem is mounted behind the automount unit
        self.assertIn("/mnt/mac-backups", machine.mounted)
        before = self.fstab()
        machine.executed.clear()
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine, {}).apply()
        self.assertIn("/mnt/mac-backups is still mounted", str(caught.exception))
        self.assertIn("umount /mnt/mac-backups", str(caught.exception))
        self.assertEqual(machine.executed, [])
        self.assertEqual(self.fstab(), before)
        self.assertIn("mnt-mac\\x2dbackups.automount", machine.active)
        machine.mounted.clear()  # the user unmounted it
        self.module(machine, {}).apply()
        self.assertNotIn("mnt-mac\\x2dbackups.automount", machine.active)
        self.assertEqual(self.fstab(), STOCK_FSTAB)

    def test_plan_names_the_automount_unit_it_will_stop(self):
        machine = self.machine(attached=False)
        self.module(machine).apply()
        module = self.module(machine, {})
        self.assertEqual(module.plan()[0], "remove the ubuntu-tweaks disks block from /etc/fstab")
        self.assertTrue(any(line.startswith("systemctl stop mnt-mac\\x2dbackups.automount") for line in module.plan()))

    def test_plan_says_needs_manual_for_what_install_refuses(self):
        machine = self.machine()
        self.module(machine).apply()  # mounted
        plan = self.module(machine, {}).plan()
        self.assertTrue(any(line.startswith("needs manual: /mnt/mac-backups is still mounted") for line in plan))
        self.assertFalse(any(line.startswith("systemctl stop") for line in plan))
        home_missing = {"mounts": {"data": {"uuid": "1111-1111", "fstype": "ext4", "mountpoint": "~/data"}}}
        plan = self.module(self.machine(attached=False), home_missing).plan()
        self.assertTrue(
            any(line.startswith("needs manual: data: the home directory /home/alice does not exist") for line in plan)
        )
        self.assertFalse(any(line.startswith("mkdir") for line in plan))

    def test_a_mounted_disk_whose_mount_point_moved_is_refused_with_the_real_reason(self):
        machine = self.machine()
        self.module(machine).apply()
        moved = {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "mountpoint": "/mnt/elsewhere"}}}
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine, moved).apply()
        self.assertIn("declaration removed, mount point moved or automount switched off", str(caught.exception))

    def test_a_disk_that_gets_mounted_while_the_driver_builds_is_not_detached_by_the_stop(self):
        machine = self.machine(attached=False)
        self.module(machine).apply()
        module = self.module(machine, {})
        answers = iter([False, True])  # the first look sees nothing mounted, the look before the stop sees a mount
        module._mounted_filesystem = lambda point: next(answers)
        machine.executed.clear()
        before = self.fstab()
        with self.assertRaises(NeedsManual) as caught:
            module.apply()
        self.assertIn("the fstab was not changed", str(caught.exception))
        self.assertFalse(any(call[:2] == ("systemctl", "stop") for call in machine.executed))
        self.assertEqual(self.fstab(), before)

    def test_an_existing_mount_point_is_never_opened_because_that_would_mount_it(self):
        machine = self.machine(attached=False)
        self.module(machine).apply()  # creates /mnt/mac-backups
        opened = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", spy):
            self.module(machine).apply()
        self.assertNotIn("mac-backups", opened)

    def test_a_new_mount_point_under_mnt_is_not_opened_either(self):
        opened = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", spy):
            self.module(self.machine(attached=False)).apply()
        self.assertNotIn("mac-backups", opened)

    def test_a_home_directory_that_is_not_there_is_never_created_by_root(self):
        options = {
            "mounts": {
                "data": {"uuid": "11111111-2222-3333-4444-555555555555", "fstype": "ext4", "mountpoint": "~/data"}
            }
        }
        machine = self.machine(attached=False)
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine, options).apply()
        self.assertIn("the home directory /home/alice does not exist", str(caught.exception))
        self.assertFalse((self.root / "home").exists())
        self.assertEqual(machine.executed, [])

    def test_a_reordered_or_duplicated_block_is_drift_that_check_can_see(self):
        machine = self.machine(attached=False)
        two = {"mounts": {"a": {"uuid": "1111-1111", "fstype": "ext4"}, "b": {"uuid": "2222-2222", "fstype": "ext4"}}}
        self.module(machine, two).apply()
        lines = self.fstab().splitlines()
        first = next(i for i, line in enumerate(lines) if "1111-1111" in line and not line.startswith("#"))
        second = next(i for i, line in enumerate(lines) if "2222-2222" in line and not line.startswith("#"))
        lines[first], lines[second] = lines[second], lines[first]
        (self.root / "etc/fstab").write_text("\n".join(lines) + "\n")
        module = self.module(machine, two)
        fact = self.facts(module)[disks.BLOCK_FACT]
        self.assertFalse(fact.ok)
        self.assertIn("order", fact.detail)
        self.assertTrue(any(line.startswith("write the ubuntu-tweaks block") for line in module.plan()))
        module.apply()
        self.assertEqual([f for f in self.module(machine, two).findings() if not f.ok], [])

    def test_a_changed_description_is_drift_and_is_written(self):
        machine = self.machine()
        self.module(machine).apply()
        described = {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "description": "MacBook backups"}}}
        module = self.module(machine, described)
        block = self.facts(module)[disks.BLOCK_FACT]
        self.assertFalse(block.ok)
        self.assertIn("MacBook backups", block.detail)
        self.assertTrue(any("MacBook backups" in line for line in module.plan()))
        module.apply()
        self.assertIn("# mac-backups: MacBook backups (apfs, fuse, read-only)\n", self.fstab())
        self.assertEqual([f for f in self.module(machine, described).findings() if not f.ok], [])
        machine.executed.clear()
        self.module(machine, described).apply()
        self.assertFalse(any(call[:2] == ("systemctl", "daemon-reload") for call in machine.executed))

    def test_a_hand_edited_comment_inside_the_block_is_drift_too(self):
        machine = self.machine()
        self.module(machine).apply()
        edited = self.fstab().replace("# mac-backups: apfs, fuse, read-only", "# mac-backups: my own words")
        self.assertNotEqual(edited, self.fstab())
        (self.root / "etc/fstab").write_text(edited)
        module = self.module(machine)
        self.assertFalse(self.facts(module)[disks.BLOCK_FACT].ok)
        module.apply()
        self.assertIn("# mac-backups: apfs, fuse, read-only", self.fstab())

    def test_changed_options_are_written(self):
        machine = self.machine()
        self.module(machine).apply()
        noexec = {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "options": ["noexec"]}}}
        module = self.module(machine, noexec)
        self.assertFalse(self.facts(module)["mac-backups: /etc/fstab entry"].ok)
        module.apply()
        self.assertIn("nosuid,nodev,noexec,nofail", self.fstab())

    def test_unplugging_the_disk_after_install_is_not_drift(self):
        machine = self.machine()
        self.module(machine).apply()
        (self.root / f"dev/disk/by-uuid/{APFS_UUID}").unlink()
        module = self.module(machine)
        self.assertEqual([f for f in module.findings() if not f.ok], [])
        self.assertEqual(module.plan(), [])
        entry = next(line for line in self.fstab().splitlines() if APFS_UUID in line and not line.startswith("#"))
        options = entry.split()[3].split(",")
        self.assertIn("nofail", options)
        self.assertIn("x-systemd.automount", options)

    def test_directories_made_in_the_users_home_belong_to_the_user_without_following_links(self):
        (self.root / "home/alice").mkdir(parents=True)
        home = (self.root / "home/alice").resolve()
        options = {
            "mounts": {
                "data": {"uuid": "11111111-2222-3333-4444-555555555555", "fstype": "ext4", "mountpoint": "~/a/data"}
            }
        }
        self.module(self.machine(attached=False), options).apply()
        self.assertEqual(self.chowned, [(home / "a", 1000, -1), (home / "a/data", 1000, -1)])
        self.assertTrue((home / "a/data").is_dir())

    def test_a_mount_point_under_mnt_stays_roots(self):
        self.module(self.machine()).apply()
        self.assertEqual(self.chowned, [])

    def home_data(self, **extra):
        return {
            "mounts": {
                "data": {
                    "uuid": "11111111-2222-3333-4444-555555555555",
                    "fstype": "ext4",
                    "mountpoint": "~/a/data",
                    **extra,
                }
            }
        }

    def test_a_symlink_on_the_mount_point_path_is_a_manual_step_before_anything_is_changed(self):
        home = self.root / "home/alice"
        home.mkdir(parents=True)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for link in (home / "a", home / "a/data"):
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(elsewhere)
            machine = self.machine(attached=False)
            with self.assertRaises(NeedsManual, msg=str(link)) as caught:
                self.module(machine, self.home_data()).apply()
            self.assertIn("symbolic link", str(caught.exception))
            self.assertIn("nothing was changed", str(caught.exception))
            self.assertEqual(machine.executed, [])
            self.assertEqual(self.chowned, [])
            self.assertEqual(self.fstab(), STOCK_FSTAB)
            self.assertEqual(list(elsewhere.iterdir()), [])
            link.unlink()

    def test_a_link_on_the_second_disk_stops_the_run_before_the_first_disk_is_touched_and_before_any_driver(self):
        home = self.root / "home/alice"
        home.mkdir(parents=True)
        (home / "b").symlink_to(self.root)
        options = {
            "mounts": {
                "good": {"uuid": "11111111-2222-3333-4444-555555555555", "fstype": "ext4", "mountpoint": "~/a/good"},
                "linked": {"uuid": APFS_UUID, "fstype": "apfs", "mountpoint": "~/b/mac"},
            }
        }
        machine = self.machine()
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine, options).apply()
        self.assertIn("linked: mount point /home/alice/b/mac", str(caught.exception))
        self.assertEqual(machine.executed, [])  # no apt-get, no build
        self.assertFalse((home / "a").exists())
        self.assertFalse((self.root / "usr/local/bin/apfs-fuse").exists())

    def test_a_link_swapped_in_after_the_check_is_not_followed(self):
        home = self.root / "home/alice"
        home.mkdir(parents=True)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (home / "a").symlink_to(elsewhere)
        module = self.module(self.machine(attached=False), self.home_data())
        module._mountpoint_problem = lambda spec: None  # the check saw a directory; the link came after it
        with self.assertRaises(ModuleError) as caught:
            module._make_mountpoint(module._specs[0])
        self.assertIn("not a real directory", str(caught.exception))
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(self.chowned, [])

    def test_a_symlinked_mount_point_is_a_finding_and_a_plan_line(self):
        machine = self.machine()
        self.module(machine).apply()
        mount_point = self.root / "mnt/mac-backups"
        mount_point.rmdir()
        (self.root / "mnt/elsewhere").mkdir()
        mount_point.symlink_to(self.root / "mnt/elsewhere")
        module = self.module(machine)
        bad = [f for f in module.findings() if not f.ok]
        self.assertEqual([f.subject for f in bad], ["mac-backups: mount point /mnt/mac-backups"])
        self.assertIn("symbolic link", bad[0].detail)
        self.assertTrue(bad[0].manual)
        self.assertTrue(any(line.startswith("needs manual: mac-backups:") for line in module.plan()))

    def test_a_failed_stop_leaves_the_old_block_so_the_next_apply_stops_the_unit(self):
        machine = self.machine(attached=False)
        two = {"mounts": {"mac-backups": MOUNTS["mounts"]["mac-backups"], "win": {"uuid": NTFS_UUID, "fstype": "ntfs"}}}
        self.module(machine, two).apply()
        only_ntfs = {"mounts": {"win": two["mounts"]["win"]}}
        machine.fail_stop = True
        with self.assertRaises(CommandError):
            self.module(machine, only_ntfs).apply()
        self.assertIn(APFS_UUID, self.fstab())  # the block still names the unit that is still running
        self.assertIn("mnt-mac\\x2dbackups.automount", machine.active)
        machine.fail_stop = False
        self.module(machine, only_ntfs).apply()
        self.assertNotIn("mnt-mac\\x2dbackups.automount", machine.active)
        self.assertNotIn(APFS_UUID, self.fstab())

    def test_a_binary_not_owned_by_the_trusted_user_is_not_the_installed_driver(self):
        machine = self.machine()
        self.module(machine).apply()
        self.assertEqual([f for f in self.module(machine).findings() if not f.ok], [])
        elsewhere = self.module(machine, trusted_uid=os.getuid() + 1)  # as if the binary belonged to somebody else
        bad = [f.subject for f in elsewhere.findings() if not f.ok]
        self.assertEqual(bad, ["apfs-fuse 66b86bd built and installed in /usr/local/bin/apfs-fuse"])

    def test_a_binary_an_older_version_built_as_the_user_is_rebuilt_by_root(self):
        machine = self.machine()
        self.module(machine).apply()
        stamp = self.root / "usr/local/share/ubuntu-tweaks/apfs-fuse.commit"
        stamp.write_text(lib.PINNED_APFS_FUSE[1] + "\n")  # the old stamp: the commit alone
        self.assertFalse(self.module(machine)._apfs_fuse_ok())
        machine.executed.clear()
        self.module(machine).apply()
        self.assertTrue(any(call[:2] == ("cmake", "--build") for call in machine.executed))
        self.assertTrue(self.module(machine)._apfs_fuse_ok())

    def test_plan_stops_at_the_manual_lines_install_would_stop_at(self):
        home = self.root / "home/alice"
        home.mkdir(parents=True)
        (home / "b").symlink_to(self.root)
        options = {
            "mounts": {
                "good": {"uuid": "1111-1111", "fstype": "ext4", "mountpoint": "~/a/good"},
                "linked": {"uuid": "2222-2222", "fstype": "ext4", "mountpoint": "~/b/x"},
            }
        }
        plan = self.module(self.machine(attached=False), options).plan()
        self.assertTrue(plan[0].startswith("needs manual: linked:"))
        self.assertFalse(any(line.startswith(("mkdir", "write", "systemctl")) for line in plan))

    def test_a_group_writable_binary_is_not_the_installed_driver(self):
        machine = self.machine()
        self.module(machine).apply()
        (self.root / "usr/local/bin/apfs-fuse").chmod(0o775)
        self.assertFalse(self.module(machine)._apfs_fuse_ok())


class KernelDriverTest(DisksCase):
    def options(self, **extra):
        return {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "driver": "kernel", **extra}}}

    def test_the_kernel_driver_installs_ubuntus_dkms_package_and_mounts_apfs_read_only(self):
        machine = self.machine()
        self.module(machine, self.options()).apply()
        self.assertIn(("apt-get", "install", "-y", "apfs-dkms", "linux-headers-7.0.0-38-generic"), machine.executed)
        self.assertIn(
            f"/dev/disk/by-uuid/{APFS_UUID} /mnt/mac-backups apfs ro,uid=1000,gid=1000,nosuid,nodev,nofail",
            self.fstab(),
        )
        self.assertFalse(any(call[0] == "git" for call in machine.executed))

    def test_read_write_asks_for_the_experimental_write_support_by_name(self):
        machine = self.machine(kernel_apfs="0.3.18")
        self.module(machine, self.options(read_only=False)).apply()
        self.assertIn("rw,uid=1000,gid=1000,readwrite,nosuid,nodev,nofail", self.fstab())

    def test_secure_boot_refusing_the_module_is_a_manual_step_with_the_exact_remedy(self):
        machine = self.machine()
        machine.modprobe_ok = False
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine, self.options()).apply()
        self.assertIn("mokutil --import", str(caught.exception))
        self.assertIn('driver = "fuse"', str(caught.exception))

    def test_auto_picks_the_kernel_module_only_when_the_kernel_already_has_it(self):
        with_module = self.module(self.machine(kernel_apfs="0.3.18"))
        self.assertIn(" apfs ", with_module._entries()[0][2])
        without = self.module(self.machine())
        self.assertIn(" fuse.apfs-fuse ", without._entries()[0][2])

    def test_auto_with_read_write_wants_the_kernel_module(self):
        module = self.module(
            self.machine(), {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "read_only": False}}}
        )
        self.assertIn(" apfs rw,", module._entries()[0][2])

    def test_a_module_that_is_present_but_unsigned_under_lockdown_is_not_a_working_driver(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.sig_key = ""
        machine.set_lockdown("integrity")
        self.assertIn(" fuse.apfs-fuse ", self.module(machine)._entries()[0][2])  # auto stays on FUSE
        found = self.facts(self.module(machine, self.options()))
        kernel = found["package apfs-dkms: the apfs kernel module for the running kernel"]
        self.assertFalse(kernel.ok)
        self.assertTrue(kernel.manual)
        self.assertIn("unsigned", kernel.detail)

    def test_a_module_signed_by_a_key_that_is_not_enrolled_is_not_a_working_driver_under_lockdown(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.set_lockdown("integrity")
        machine.enrolled_keys = ["11:22:33"]
        self.assertIn(" fuse.apfs-fuse ", self.module(machine)._entries()[0][2])
        machine.enrolled_keys = ["aa:bb:cc"]  # the same key id as AA:BB:CC: case does not matter
        self.assertIn(" apfs ", self.module(machine)._entries()[0][2])

    def test_key_ids_and_serials_compare_whatever_their_spelling(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.set_lockdown("integrity")
        machine.sig_key = "00:3B:0A"  # a serial with a leading zero byte
        machine.enrolled_keys = ["3b:0a", "99:99"]
        self.assertIn(" apfs ", self.module(machine)._entries()[0][2])
        machine.sig_key = "0A"  # a short serial: mokutil prints it as "10 (0xa)"
        machine.enrolled_format = "Serial Number:\n    10 (0xa)\n"
        machine.enrolled_keys = [""]
        self.assertIn(" apfs ", self.module(machine)._entries()[0][2])
        machine.sig_key = "0B"
        self.assertIn(" fuse.apfs-fuse ", self.module(machine)._entries()[0][2])

    def test_a_key_that_cannot_be_checked_is_not_a_working_driver(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.set_lockdown("integrity")
        machine.mokutil_ok = False
        self.assertIn(
            " fuse.apfs-fuse ", self.module(machine)._entries()[0][2]
        )  # auto does not trust what it cannot verify
        kernel = self.facts(self.module(machine, self.options()))[
            "package apfs-dkms: the apfs kernel module for the running kernel"
        ]
        self.assertFalse(kernel.ok)
        self.assertTrue(kernel.manual)
        self.assertIn("cannot say whether it is enrolled", kernel.detail)

    def test_the_kernel_state_is_asked_once_per_run(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.set_lockdown("integrity")
        module = self.module(machine)
        module._entries()
        module._entries()
        module.findings()
        self.assertEqual(sum(1 for call in machine.queried if call[:2] == ("mokutil", "--list-enrolled")), 1)

    def test_only_the_key_id_and_serial_fields_of_an_enrolled_certificate_count(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.set_lockdown("integrity")
        machine.sig_key = "11:22:33"
        # the same bytes as the module's key, but as the issuer's Authority Key Identifier and inside a modulus
        machine.enrolled_format = (
            "[key 1]\n  Modulus:\n    11:22:33:44\n  X509v3 Authority Key Identifier:\n    keyid:11:22:33\n"
            "  X509v3 Subject Key Identifier:\n    99:88:77\n"
        )
        machine.enrolled_keys = [""]
        self.assertIn(" fuse.apfs-fuse ", self.module(machine)._entries()[0][2])
        machine.enrolled_format = "[key 1]\n  Serial Number:\n    11:22:33\n"
        self.assertIn(" apfs ", self.module(machine)._entries()[0][2])

    def test_plan_says_what_a_blocked_module_needs_not_an_install_it_will_not_do(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.sig_key = ""
        machine.set_lockdown("integrity")
        plan = self.module(machine, self.options()).plan()
        self.assertTrue(any(line.startswith("needs manual: the apfs module is unsigned") for line in plan))
        self.assertFalse(any("apt-get install apfs-dkms" in line for line in plan))
        missing = self.module(self.machine(), self.options()).plan()
        self.assertTrue(any(line.startswith("apt-get install apfs-dkms") for line in missing))

    def test_plan_lists_modprobe_for_an_installed_module_that_is_not_loaded(self):
        machine = self.machine(kernel_apfs="0.3.18")  # present, not registered in /proc/filesystems
        self.assertIn("modprobe apfs  # installed, not loaded yet", self.module(machine, self.options()).plan())

    def test_a_loaded_module_works_whatever_its_signature_says(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.sig_key = ""
        machine.set_lockdown("confidentiality")
        machine.load_apfs()
        self.assertIn(" apfs ", self.module(machine)._entries()[0][2])

    def test_without_lockdown_an_unsigned_module_loads(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.sig_key = ""
        machine.set_lockdown("none")
        self.assertIn(" apfs ", self.module(machine)._entries()[0][2])

    def test_a_blocked_module_stays_a_manual_step_on_the_second_apply(self):
        machine = self.machine()
        machine.modprobe_ok = False
        with self.assertRaises(NeedsManual):
            self.module(machine, self.options()).apply()  # installs the DKMS package, the kernel refuses it
        machine.executed.clear()
        machine.queried.clear()
        with self.assertRaises(NeedsManual) as caught:
            self.module(machine, self.options()).apply()
        self.assertIn("mokutil --import", str(caught.exception))
        self.assertIn(("modprobe", "apfs"), machine.queried)
        self.assertFalse(any(call[0] == "apt-get" for call in machine.executed))
        self.assertFalse(any(call[:2] == ("systemctl", "start") for call in machine.executed))
        self.assertEqual(self.fstab(), STOCK_FSTAB)  # no entry for a driver that cannot load

    def test_a_module_already_loaded_is_not_loaded_again(self):
        machine = self.machine(kernel_apfs="0.3.18")
        machine.load_apfs()
        self.module(machine, self.options()).apply()
        self.assertNotIn(("modprobe", "apfs"), machine.queried)
        self.assertFalse(any(call[0] == "apt-get" for call in machine.executed))


class GeneratedFstabTest(DisksCase):
    """The real ``findmnt --verify`` (util-linux), not the fake: the generated lines must parse and add no error."""

    OPTIONS = {
        "mounts": {
            "mac-backups": MOUNTS["mounts"]["mac-backups"],
            "win": {"label": "Windows 11", "fstype": "ntfs", "description": "Windows partition"},
            "data": {"uuid": "11111111-2222-3333-4444-555555555555", "fstype": "ext4", "options": ["noatime"]},
            "usb": {"uuid": "ABCD-1234", "fstype": "vfat", "automount": False, "idle_timeout": "5min"},
        }
    }

    @unittest.skipUnless(shutil.which("findmnt"), "util-linux findmnt is not installed")
    def test_findmnt_verify_finds_no_parse_error_and_no_error_about_an_unplugged_disk(self):
        module = self.module(self.machine(attached=False), self.OPTIONS)
        block = lib.fstab_block(module._entries())
        tab = self.root / "fstab.candidate"
        tab.write_text(block)
        result = subprocess.run(
            ["findmnt", "--verify", "--tab-file", str(tab)], capture_output=True, text=True, check=False
        )
        report = result.stdout + result.stderr
        self.assertIn("0 parse errors", report)
        errors = [line.strip() for line in report.splitlines() if "[E]" in line]
        # none of the disks is attached here; the only thing a fresh machine lacks is the mount point directory
        self.assertTrue(all("target: No such file or directory" in line for line in errors), msg=report)
        self.assertFalse([line for line in report.splitlines() if "[E]" in line and "source" in line], msg=report)


class OtherFilesystemsTest(DisksCase):
    def test_ntfs_with_ntfs_3g_installs_the_package_and_uses_its_helper(self):
        machine = self.machine(attached=False)
        options = {
            "mounts": {"win": {"label": "Windows 11", "fstype": "ntfs", "driver": "ntfs-3g", "read_only": False}}
        }
        self.module(machine, options).apply()
        self.assertIn(("apt-get", "install", "-y", "ntfs-3g"), machine.executed)
        self.assertIn(
            "/dev/disk/by-label/Windows\\134x2011 /mnt/win ntfs-3g rw,uid=1000,gid=1000,umask=022,nosuid,nodev,nofail,x-systemd.automount",
            self.fstab(),
        )

    def test_apt_runs_without_a_terminal_and_noninteractively(self):
        # ticket #126: a grandchild of sudo-rs that touches the pty is stopped for ever; stdin must be at EOF
        machine = self.machine(attached=False)
        options = {
            "mounts": {"win": {"label": "Windows 11", "fstype": "ntfs", "driver": "ntfs-3g", "read_only": False}}
        }

        self.module(machine, options).apply()

        self.assertEqual(machine.apt_runs, [("", APT_ENVIRONMENT)])

    def test_ext4_needs_no_package_and_no_owner(self):
        machine = self.machine(attached=False)
        (self.root / "home/alice").mkdir(parents=True)
        options = {
            "mounts": {
                "data": {
                    "uuid": "11111111-2222-3333-4444-555555555555",
                    "fstype": "ext4",
                    "mountpoint": "~/data",
                    "options": ["noatime"],
                }
            }
        }
        module = self.module(machine, options)
        module.apply()
        self.assertFalse(any(call[0] in ("apt-get", "git") for call in machine.executed))
        self.assertIn(
            "/dev/disk/by-uuid/11111111-2222-3333-4444-555555555555 /home/alice/data ext4 rw,nosuid,nodev,noatime,nofail",
            self.fstab(),
        )
        self.assertTrue((self.root / "home/alice/data").is_dir())

    def test_without_automount_a_present_disk_is_mounted_directly(self):
        machine = self.machine()
        options = {"mounts": {"data": {"uuid": APFS_UUID, "fstype": "ext4", "automount": False}}}
        module = self.module(machine, options)
        module.apply()
        self.assertNotIn("x-systemd.automount", self.fstab())
        self.assertIn(("systemctl", "start", "mnt-data.mount"), machine.executed)
        self.assertNotIn(("systemctl", "start", "mnt-data.automount"), machine.executed)


class BadDeclarationsTest(DisksCase):
    def test_apfs_fuse_cannot_write(self):
        options = {"mounts": {"m": {"uuid": APFS_UUID, "fstype": "apfs", "driver": "fuse", "read_only": False}}}
        with self.assertRaises(ConfigError) as caught:
            self.module(self.machine(), options).findings()
        self.assertIn("apfs-fuse cannot write", str(caught.exception))

    def test_unknown_option_and_bad_filesystem_name_the_key(self):
        with self.assertRaises(ConfigError) as caught:
            self.module(self.machine(), {"mounts": {"m": {"uuid": "x", "fstype": "zfs"}}})
        self.assertIn("[modules.disks.mounts.m]", str(caught.exception))
        with self.assertRaises(ConfigError):
            self.module(self.machine(), {"bogus": 1})


class CliTest(DisksCase):
    def run_main(self, module, command):
        out, err = io.StringIO(), io.StringIO()
        return main(module, [command], out=out, err=err), out.getvalue(), err.getvalue()

    def test_check_reports_drift_then_ok_after_install(self):
        machine = self.machine()
        module = self.module(machine)
        code, out, _ = self.run_main(module, "check")
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("missing mac-backups: /etc/fstab entry", out)
        code, _, _ = self.run_main(module, "install")
        self.assertEqual(code, EXIT_OK)
        code, out, _ = self.run_main(self.module(machine), "check")
        self.assertEqual((code, out), (EXIT_OK, ""))

    def test_install_reports_exit_10_for_a_manual_step(self):
        machine = self.machine(apfsutil=APFSUTIL_VAULT)
        code, out, _ = self.run_main(self.module(machine), "install")
        self.assertEqual(code, EXIT_NEEDS_MANUAL)
        self.assertTrue(out.startswith("needs manual:"))

    def test_usage_error(self):
        code, _, err = self.run_main(self.module(self.machine()), "frobnicate")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage: disks", err)


if __name__ == "__main__":
    unittest.main()

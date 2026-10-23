"""disks module against a fake root and a stateful fake machine (no real fstab, systemd, apt, git or mount)."""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks import disks as lib  # noqa: E402
from ubuntu_tweaks.errors import ConfigError  # noqa: E402
from ubuntu_tweaks.module_script import (  # noqa: E402
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
    "ro,allow_other,uid=1000,gid=1000,nofail,x-systemd.automount,x-systemd.device-timeout=10s 0 0"
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
        self.fail_start = None
        self.loaded = set()
        self.active = set()
        self.mounted = {}  # mount point -> (fstype, options)
        self.executed = []
        self.queried = []

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
            return CommandResult(
                0 if self.modprobe_ok else 1, "", "" if self.modprobe_ok else "Key was rejected by service"
            )
        if argv[0].endswith("apfsutil"):
            return CommandResult(0, self.apfsutil, "")
        return CommandResult(127, "", f"{argv[0]}: not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        if argv[:3] == ["runuser", "-u", "alice"]:
            argv = argv[4:]
        self.executed.append(tuple(argv))
        if argv[0] == "apt-get":
            self.packages.update(a for a in argv[3:] if not a.startswith("-"))
            if "apfs-dkms" in argv:
                self.kernel_apfs = "0.3.18"
        elif argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        elif argv[:2] == ["cmake", "--build"]:
            build = Path(argv[2])
            build.mkdir(parents=True, exist_ok=True)
            (build / "apfs-fuse").write_bytes(b"#!/bin/sh\n")
            (build / "apfsutil").write_bytes(b"#!/bin/sh\n")
        elif argv[:2] == ["systemctl", "daemon-reload"]:
            self.loaded = self._units()
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
        self.account = Account("alice", Path("/home/alice"), os.getuid())

    def attach(self, uuid=APFS_UUID, kind="by-uuid"):
        link = self.root / f"dev/disk/{kind}/{uuid}"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("")

    def machine(self, **kwargs):
        machine = FakeMachine(self.root, **kwargs)
        if kwargs.get("attached", True):
            self.attach()
        return machine

    def module(self, machine, options=None, *, root=True):
        return disks.Disks(
            machine,
            self.root,
            MOUNTS if options is None else options,
            self.account,
            is_root=root,
            owner_ids=lambda name: (1000, 1000),
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
            (self.root / "usr/local/share/ubuntu-tweaks/apfs-fuse.commit").read_text().strip(), lib.PINNED_APFS_FUSE[1]
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

    def test_the_source_is_built_as_the_desktop_user_and_only_installed_as_root(self):
        machine = self.machine()
        module = self.module(machine)
        seen = []
        real = machine.execute

        def spy(argv, **kwargs):
            seen.append(list(argv))
            return real(argv, **kwargs)

        machine.execute = spy
        module.apply()
        compiled = [a for a in seen if a[:1] == ["runuser"] and "cmake" in a]
        self.assertEqual(len(compiled), 2)
        user_level = [a for a in seen if a[0] not in ("apt-get", "systemctl")]
        self.assertTrue(user_level and all(a[:4] == ["runuser", "-u", "alice", "--"] for a in user_level))

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
        machine = self.machine()
        self.module(machine).apply()
        two = {"mounts": {"mac-backups": MOUNTS["mounts"]["mac-backups"], "win": {"uuid": NTFS_UUID, "fstype": "ntfs"}}}
        self.assertEqual(self.facts(self.module(machine, two))["win: /etc/fstab entry"].ok, False)
        self.module(machine, two).apply()
        self.assertIn(
            f"/dev/disk/by-uuid/{NTFS_UUID} /mnt/win ntfs3 ro,uid=1000,gid=1000,umask=022,nofail", self.fstab()
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


class KernelDriverTest(DisksCase):
    def options(self, **extra):
        return {"mounts": {"mac-backups": {**MOUNTS["mounts"]["mac-backups"], "driver": "kernel", **extra}}}

    def test_the_kernel_driver_installs_ubuntus_dkms_package_and_mounts_apfs_read_only(self):
        machine = self.machine()
        self.module(machine, self.options()).apply()
        self.assertIn(("apt-get", "install", "-y", "apfs-dkms", "linux-headers-7.0.0-38-generic"), machine.executed)
        self.assertIn(f"/dev/disk/by-uuid/{APFS_UUID} /mnt/mac-backups apfs ro,uid=1000,gid=1000,nofail", self.fstab())
        self.assertFalse(any(call[0] == "git" for call in machine.executed))

    def test_read_write_asks_for_the_experimental_write_support_by_name(self):
        machine = self.machine(kernel_apfs="0.3.18")
        self.module(machine, self.options(read_only=False)).apply()
        self.assertIn("rw,uid=1000,gid=1000,readwrite,nofail", self.fstab())

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


class OtherFilesystemsTest(DisksCase):
    def test_ntfs_with_ntfs_3g_installs_the_package_and_uses_its_helper(self):
        machine = self.machine(attached=False)
        options = {
            "mounts": {"win": {"label": "Windows 11", "fstype": "ntfs", "driver": "ntfs-3g", "read_only": False}}
        }
        self.module(machine, options).apply()
        self.assertIn(("apt-get", "install", "-y", "ntfs-3g"), machine.executed)
        self.assertIn(
            "/dev/disk/by-label/Windows\\134x2011 /mnt/win ntfs-3g rw,uid=1000,gid=1000,umask=022,nofail,x-systemd.automount",
            self.fstab(),
        )

    def test_ext4_needs_no_package_and_no_owner(self):
        machine = self.machine(attached=False)
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
            "/dev/disk/by-uuid/11111111-2222-3333-4444-555555555555 /home/alice/data ext4 rw,noatime,nofail",
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

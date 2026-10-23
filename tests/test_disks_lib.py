"""``ubuntu_tweaks.disks``: declaration parsing, fstab text and systemd unit names (pure, no machine)."""

import unittest

from ubuntu_tweaks import disks
from ubuntu_tweaks.errors import ConfigError

HOME = "/home/alice"
UUID = "fcbf521d-af6b-463d-9959-d25f686b3379"


def one(**table):
    return disks.parse_disks({"mounts": {"m": table}}, home=HOME)[0]


class ParseTest(unittest.TestCase):
    def test_defaults_follow_the_filesystem(self):
        apfs = one(uuid=UUID, fstype="apfs")
        self.assertEqual((apfs.mountpoint, apfs.read_only, apfs.driver, apfs.automount), ("/mnt/m", True, "auto", True))
        self.assertEqual(apfs.source, f"/dev/disk/by-uuid/{UUID}")
        self.assertFalse(one(uuid=UUID, fstype="ext4").read_only)
        self.assertTrue(one(uuid=UUID, fstype="ntfs").read_only)
        self.assertEqual(one(uuid=UUID, fstype="ntfs").driver, "ntfs3")

    def test_home_is_expanded_and_system_directories_refused(self):
        self.assertEqual(one(uuid=UUID, fstype="ext4", mountpoint="~/Mac").mountpoint, "/home/alice/Mac")
        for bad in ("/", "/etc/x", "/usr", "~", "relative", "/mnt/../etc", "/mnt/a b", "/proc/x"):
            with self.assertRaises(ConfigError, msg=bad):
                one(uuid=UUID, fstype="ext4", mountpoint=bad)

    def test_exactly_one_selector_and_a_known_filesystem(self):
        for table in (
            {"fstype": "ext4"},
            {"uuid": UUID, "label": "x", "fstype": "ext4"},
            {"uuid": UUID},
            {"uuid": UUID, "fstype": "zfs"},
            {"uuid": UUID, "fstype": "ext4", "driver": "fuse"},
            {"uuid": UUID, "fstype": "ext4", "owner": "bob"},
            {"uuid": UUID, "fstype": "ext4", "volume": 1},
            {"uuid": UUID, "fstype": "ext4", "bogus": 1},
            {"uuid": UUID, "fstype": "ext4", "read_only": "yes"},
            {"uuid": UUID, "fstype": "ext4", "device_timeout": "soon"},
        ):
            with self.assertRaises(ConfigError, msg=str(table)):
                one(**table)

    def test_reserved_and_systemd_options_have_their_own_keys(self):
        for bad in ("ro", "rw", "nofail", "uid=1", "x-systemd.automount", "has space", "a,b"):
            with self.assertRaises(ConfigError, msg=bad):
                one(uuid=UUID, fstype="ext4", options=[bad])
        self.assertEqual(one(uuid=UUID, fstype="ext4", options=["noatime", "discard"]).options, ("noatime", "discard"))

    def test_two_disks_may_not_share_a_mount_point_or_a_device(self):
        same_point = {
            "a": {"uuid": "1", "fstype": "ext4", "mountpoint": "/mnt/x"},
            "b": {"uuid": "2", "fstype": "ext4", "mountpoint": "/mnt/x"},
        }
        same_device = {"a": {"uuid": "1", "fstype": "ext4"}, "b": {"uuid": "1", "fstype": "ext4"}}
        for mounts in (same_point, same_device):
            with self.assertRaises(ConfigError):
                disks.parse_disks({"mounts": mounts}, home=HOME)

    def test_unknown_top_level_option(self):
        with self.assertRaises(ConfigError):
            disks.parse_disks({"mount": {}}, home=HOME)
        self.assertEqual(disks.parse_disks({}, home=HOME), [])


class EntryTest(unittest.TestCase):
    def test_apfs_fuse_entry(self):
        spec = one(uuid=UUID, fstype="apfs")
        entry = disks.fstab_entry(spec, "fuse", (1000, 1000))
        self.assertEqual(
            entry,
            f"/dev/disk/by-uuid/{UUID} /mnt/m fuse.apfs-fuse "
            "ro,allow_other,uid=1000,gid=1000,nofail,x-systemd.automount,x-systemd.device-timeout=10s 0 0",
        )

    def test_apfs_volume_and_idle_timeout(self):
        spec = one(uuid=UUID, fstype="apfs", volume=2, idle_timeout="5min", device_timeout="30s")
        entry = disks.fstab_entry(spec, "kernel", (1, 2))
        self.assertIn(
            " apfs ro,uid=1,gid=2,vol=2,nofail,x-systemd.automount,x-systemd.device-timeout=30s,x-systemd.idle-timeout=5min ",
            entry,
        )

    def test_apfs_fuse_refuses_read_write(self):
        spec = one(uuid=UUID, fstype="apfs", read_only=False)
        with self.assertRaises(ConfigError):
            disks.fstab_entry(spec, "fuse", (1, 1))
        self.assertIn("rw,uid=1,gid=1,readwrite,nofail", disks.fstab_entry(spec, "kernel", (1, 1)))

    def test_a_label_is_udev_encoded_then_fstab_escaped(self):
        self.assertEqual(disks.udev_encode("Windows 11"), "Windows\\x2011")
        self.assertEqual(disks.fstab_escape("a b\\c"), "a\\040b\\134c")
        spec = one(label="Windows 11", fstype="ntfs")
        self.assertEqual(spec.source, "/dev/disk/by-label/Windows\\x2011")
        self.assertTrue(
            disks.fstab_entry(spec, "ntfs3", (1000, 1000)).startswith(
                "/dev/disk/by-label/Windows\\134x2011 /mnt/m ntfs3 ro,"
            )
        )

    def test_without_automount_the_systemd_options_shrink(self):
        entry = disks.fstab_entry(one(uuid=UUID, fstype="ext4", automount=False), "kernel", None)
        self.assertEqual(entry, f"/dev/disk/by-uuid/{UUID} /mnt/m ext4 rw,nofail,x-systemd.device-timeout=10s 0 0")

    def test_owner_mapping_needs_a_known_owner(self):
        with self.assertRaises(ConfigError):
            disks.fstab_entry(one(uuid=UUID, fstype="exfat"), "kernel", None)

    def test_resolve_driver(self):
        apfs = one(uuid=UUID, fstype="apfs")
        self.assertEqual(disks.resolve_driver(apfs, kernel_apfs=False), "fuse")
        self.assertEqual(disks.resolve_driver(apfs, kernel_apfs=True), "kernel")
        self.assertEqual(disks.resolve_driver(one(uuid=UUID, fstype="apfs", driver="fuse"), kernel_apfs=True), "fuse")
        self.assertEqual(disks.resolve_driver(one(uuid=UUID, fstype="ntfs"), kernel_apfs=True), "ntfs3")


class UnitNameTest(unittest.TestCase):
    def test_matches_systemd_escape(self):
        self.assertEqual(disks.unit_name("/mnt/mac-backups", "mount"), "mnt-mac\\x2dbackups.mount")
        self.assertEqual(disks.unit_name("/mnt/mac-backups", "automount"), "mnt-mac\\x2dbackups.automount")
        self.assertEqual(disks.unit_name("/home/alice/Mac_disk.1", "mount"), "home-alice-Mac_disk.1.mount")
        self.assertEqual(disks.unit_name("/mnt/.hidden", "mount"), "mnt-.hidden.mount")
        self.assertEqual(disks.unit_name("/", "mount"), "-.mount")


class FstabBlockTest(unittest.TestCase):
    TEXT = "UUID=a / ext4 defaults 0 1\n"

    def block(self, *uuids):
        entries = []
        for number, uuid in enumerate(uuids):
            spec = disks.parse_disks({"mounts": {f"d{number}": {"uuid": uuid, "fstype": "ext4"}}}, home=HOME)[0]
            entries.append((spec, "kernel", disks.fstab_entry(spec, "kernel", None)))
        return disks.fstab_block(entries)

    def test_block_is_appended_replaced_and_removed_without_touching_the_rest(self):
        added = disks.merge_fstab(self.TEXT, self.block("u1"))
        self.assertTrue(added.startswith(self.TEXT + "\n# BEGIN ubuntu-tweaks disks"))
        self.assertEqual(disks.merge_fstab(added, self.block("u1")), added)  # idempotent
        replaced = disks.merge_fstab(added, self.block("u2"))
        self.assertIn("/dev/disk/by-uuid/u2", replaced)
        self.assertNotIn("/dev/disk/by-uuid/u1", replaced)
        self.assertEqual(disks.merge_fstab(replaced, ""), self.TEXT)

    def test_a_block_in_the_middle_keeps_the_lines_after_it(self):
        added = disks.merge_fstab(self.TEXT, self.block("u1")) + "UUID=z /z ext4 defaults 0 2\n"
        self.assertEqual(disks.managed_lines(added), [line for line in added.splitlines() if "by-uuid/u1" in line])
        self.assertTrue(disks.merge_fstab(added, "").endswith("UUID=z /z ext4 defaults 0 2\n"))

    def test_no_block_means_none(self):
        self.assertIsNone(disks.managed_lines(self.TEXT))
        self.assertEqual(disks.merge_fstab("", ""), "")

    def test_an_unterminated_block_is_refused_not_guessed(self):
        with self.assertRaises(ConfigError):
            disks.managed_lines("# BEGIN ubuntu-tweaks disks\nfoo\n")

    def test_foreign_references_ignore_the_managed_block(self):
        spec = one(uuid=UUID, fstype="ext4")
        block = disks.fstab_block([(spec, "kernel", disks.fstab_entry(spec, "kernel", None))])
        text = self.TEXT + f"UUID={UUID} /media/x ext4 defaults 0 2\n/dev/sdz1 /mnt/m ext4 defaults 0 2\n" + block
        self.assertEqual(disks.foreign_references(text, spec), [2, 3])
        self.assertEqual(disks.foreign_references(self.TEXT + block, spec), [])


if __name__ == "__main__":
    unittest.main()

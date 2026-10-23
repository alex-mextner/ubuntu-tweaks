"""``ubuntu_tweaks.disks``: declaration parsing, fstab text and systemd unit names (pure, no machine)."""

import unittest

from ubuntu_tweaks import disks
from ubuntu_tweaks.errors import ConfigError

HOME = "/home/alice"
USER = "alice"
UUID = "fcbf521d-af6b-463d-9959-d25f686b3379"


def one(**table):
    return disks.parse_disks({"mounts": {"m": table}}, home=HOME, user=USER)[0]


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

    def test_a_mount_point_is_under_mnt_media_user_or_the_users_home_and_nowhere_else(self):
        for good in ("/mnt/m", "/mnt/disks/m", "/media/alice/m", "/home/alice/data", "~/a/b"):
            self.assertEqual(one(uuid=UUID, fstype="ext4", mountpoint=good).mountpoint, good.replace("~", HOME))
        for bad in (
            "/mnt", "/mnt/", "/media", "/media/alice", "/media/bob/m", "/home", "/home/alice", "/home/bob/m",
            "/var/lib/m", "/var", "/root/m", "/etc/m", "/boot/m", "/boot/efi", "/usr/m", "/opt/m", "/srv/m", "/tmp/m",
        ):  # fmt: skip
            with self.assertRaises(ConfigError, msg=bad):
                one(uuid=UUID, fstype="ext4", mountpoint=bad)

    def test_a_home_that_is_a_system_directory_gives_no_mount_points(self):
        for home, user in (("/root", "root"), ("/var/lib/svc", "svc"), ("/", "x")):
            with self.assertRaises(ConfigError, msg=home):
                disks.parse_disks(
                    {"mounts": {"m": {"uuid": UUID, "fstype": "ext4", "mountpoint": "~/x"}}}, home=home, user=user
                )

    UNSAFE = ("\n", "\r", "\t", "\x00", "\x0b", "\x1c", "\x1f", "\x7f", "\x85", "\u2028", "\u2029", "\u202e")

    def test_no_value_may_carry_a_control_character_into_fstab(self):
        # every string value of a declaration, each with the table that is otherwise valid
        values = {
            "description": {"fstype": "ntfs", "uuid": UUID},
            "owner": {"fstype": "ntfs", "uuid": UUID},
            "device_timeout": {"fstype": "ntfs", "uuid": UUID},
            "mountpoint": {"fstype": "ntfs", "uuid": UUID},
            "label": {"fstype": "ntfs"},
            "uuid": {"fstype": "ntfs"},
        }
        for char in self.UNSAFE:
            for key, table in values.items():
                with self.assertRaises(ConfigError, msg=f"{key} {char!r}"):
                    one(**{**table, key: f"x{char}y"})

    def test_a_description_cannot_start_a_second_fstab_line(self):
        with self.assertRaises(ConfigError):
            one(uuid=UUID, fstype="ext4", description="Mac\n/dev/sda1 / ext4 defaults 0 0")

    def test_options_reject_control_characters_through_their_own_pattern(self):
        for option in ("noatime\n/dev/sda1", "a\tb", "x\x00", "nosuid\u2028"):
            with self.assertRaises(ConfigError, msg=repr(option)):
                one(uuid=UUID, fstype="ext4", options=[option])

    def test_the_mount_point_rule_has_one_home_in_mountpoint_anchor(self):
        self.assertEqual(disks.mountpoint_anchor("/mnt/m", HOME, USER), "/mnt")
        self.assertEqual(disks.mountpoint_anchor("/media/alice/m/x", HOME, USER), "/media/alice")
        self.assertEqual(disks.mountpoint_anchor("/home/alice/a/b", HOME, USER), HOME)
        self.assertEqual(disks.mountpoint_anchor("/home/alice/a/b", HOME + "/", USER), HOME)
        for path in ("/mnt", "/media/bob/m", "/home/bob/m", "/var/x", "/home/alice"):
            self.assertIsNone(disks.mountpoint_anchor(path, HOME, USER), msg=path)
        self.assertIsNone(disks.mountpoint_anchor("/root/x", "/root", "root"))  # a system directory is no home
        self.assertIsNone(
            disks.mountpoint_anchor("/home/bob/x", "/home", "alice")
        )  # /home is not a home: not everybody's
        self.assertIsNone(disks.mountpoint_anchor("/x/y", "/x", "alice"))

    def test_a_home_below_mnt_is_not_mounted_over_and_nested_mount_points_are_refused(self):
        self.assertIsNone(disks.mountpoint_anchor("/mnt/home/alice", "/mnt/home/alice", "alice"))
        self.assertIsNone(disks.mountpoint_anchor("/mnt/home", "/mnt/home/alice", "alice"))
        self.assertEqual(disks.mountpoint_anchor("/mnt/home/alice/d", "/mnt/home/alice", "alice"), "/mnt/home/alice")
        nested = {
            "a": {"uuid": "1", "fstype": "ext4", "mountpoint": "/mnt/a"},
            "b": {"uuid": "2", "fstype": "ext4", "mountpoint": "/mnt/a/b"},
        }
        with self.assertRaises(ConfigError):
            disks.parse_disks({"mounts": nested}, home=HOME, user=USER)
        siblings = {"a": {"uuid": "1", "fstype": "ext4"}, "a-x": {"uuid": "2", "fstype": "ext4"}}
        self.assertEqual(len(disks.parse_disks({"mounts": siblings}, home=HOME, user=USER)), 2)

    def test_a_description_with_text_and_hash_marks_is_one_comment_line(self):
        spec = one(uuid=UUID, fstype="ext4", description="Бэкап #1 (USB) # END ubuntu-tweaks disks")
        block = disks.fstab_block([(spec, "kernel", disks.fstab_entry(spec, "kernel", None))])
        self.assertEqual(len(block.splitlines()), 4)
        self.assertEqual(disks.managed_lines(block), [disks.fstab_entry(spec, "kernel", None)])

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
        self.assertEqual(
            one(uuid=UUID, fstype="ext4", options=["suid", "dev", "noexec"]).options, ("suid", "dev", "noexec")
        )

    def test_two_disks_may_not_share_a_mount_point_or_a_device(self):
        same_point = {
            "a": {"uuid": "1", "fstype": "ext4", "mountpoint": "/mnt/x"},
            "b": {"uuid": "2", "fstype": "ext4", "mountpoint": "/mnt/x"},
        }
        same_device = {"a": {"uuid": "1", "fstype": "ext4"}, "b": {"uuid": "1", "fstype": "ext4"}}
        for mounts in (same_point, same_device):
            with self.assertRaises(ConfigError):
                disks.parse_disks({"mounts": mounts}, home=HOME, user=USER)

    def test_unknown_top_level_option(self):
        with self.assertRaises(ConfigError):
            disks.parse_disks({"mount": {}}, home=HOME, user=USER)
        self.assertEqual(disks.parse_disks({}, home=HOME, user=USER), [])


class EntryTest(unittest.TestCase):
    def test_apfs_fuse_entry(self):
        spec = one(uuid=UUID, fstype="apfs")
        entry = disks.fstab_entry(spec, "fuse", (1000, 1000))
        self.assertEqual(
            entry,
            f"/dev/disk/by-uuid/{UUID} /mnt/m fuse.apfs-fuse "
            "ro,allow_other,uid=1000,gid=1000,nosuid,nodev,nofail,x-systemd.automount,x-systemd.device-timeout=10s 0 0",
        )

    def test_apfs_volume_and_idle_timeout(self):
        spec = one(uuid=UUID, fstype="apfs", volume=2, idle_timeout="5min", device_timeout="30s")
        entry = disks.fstab_entry(spec, "kernel", (1, 2))
        self.assertIn(
            " apfs ro,uid=1,gid=2,vol=2,nosuid,nodev,nofail,x-systemd.automount,x-systemd.device-timeout=30s,x-systemd.idle-timeout=5min ",
            entry,
        )

    def test_apfs_fuse_refuses_read_write(self):
        spec = one(uuid=UUID, fstype="apfs", read_only=False)
        with self.assertRaises(ConfigError):
            disks.fstab_entry(spec, "fuse", (1, 1))
        self.assertIn("rw,uid=1,gid=1,readwrite,nosuid,nodev,nofail", disks.fstab_entry(spec, "kernel", (1, 1)))

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
        self.assertEqual(
            entry, f"/dev/disk/by-uuid/{UUID} /mnt/m ext4 rw,nosuid,nodev,nofail,x-systemd.device-timeout=10s 0 0"
        )

    def test_every_entry_is_nosuid_nodev_and_noexec_stays_the_users_choice(self):
        for fstype, driver in (
            ("ext4", "kernel"),
            ("vfat", "kernel"),
            ("ntfs", "ntfs3"),
            ("ntfs", "ntfs-3g"),
            ("iso9660", "kernel"),
        ):
            entry = disks.fstab_entry(one(uuid=UUID, fstype=fstype), driver, (1, 1))
            options = entry.split()[3].split(",")
            self.assertIn("nosuid", options, msg=fstype)
            self.assertIn("nodev", options, msg=fstype)
            self.assertNotIn("noexec", options, msg=fstype)
        noexec = disks.fstab_entry(one(uuid=UUID, fstype="ext4", options=["noexec"]), "kernel", None)
        self.assertIn("nosuid,nodev,noexec,nofail", noexec)

    def test_suid_and_dev_are_an_explicit_opt_out_and_nosuid_is_not_doubled(self):
        spec = one(uuid=UUID, fstype="ext4", options=["suid", "dev"])
        options = disks.fstab_entry(spec, "kernel", None).split()[3].split(",")
        self.assertNotIn("nosuid", options)
        self.assertNotIn("nodev", options)
        spec = one(uuid=UUID, fstype="ext4", options=["nosuid", "nodev"])
        self.assertEqual(disks.fstab_entry(spec, "kernel", None).split()[3].split(",").count("nosuid"), 1)
        spec = one(uuid=UUID, fstype="ext4", options=["suid"])
        options = disks.fstab_entry(spec, "kernel", None).split()[3].split(",")
        self.assertEqual(("nosuid" in options, "nodev" in options), (False, True))

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


class PinTest(unittest.TestCase):
    def test_the_apfs_fuse_pin_is_a_full_commit_hash(self):
        # the module checks out this value and builds it as root: only a full SHA names exactly one tree
        commit = disks.PINNED_APFS_FUSE[1]
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertTrue(disks.PINNED_APFS_FUSE[0].startswith("https://"))


class FstabBlockTest(unittest.TestCase):
    TEXT = "UUID=a / ext4 defaults 0 1\n"

    def block(self, *uuids):
        entries = []
        for number, uuid in enumerate(uuids):
            spec = disks.parse_disks(
                {"mounts": {f"d{number}": {"uuid": uuid, "fstype": "ext4"}}}, home=HOME, user=USER
            )[0]
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

    def test_the_whole_block_is_comparable_not_only_its_entry_lines(self):
        spec = one(uuid=UUID, fstype="ext4", description="old")
        entry = disks.fstab_entry(spec, "kernel", None)
        old = disks.fstab_block([(spec, "kernel", entry)])
        new = disks.fstab_block([(one(uuid=UUID, fstype="ext4", description="new"), "kernel", entry)])
        self.assertEqual(disks.managed_lines(old), disks.managed_lines(new))  # the entry lines alone cannot tell
        self.assertEqual(disks.managed_block(self.TEXT + old), old)
        self.assertNotEqual(disks.managed_block(self.TEXT + old), new)
        self.assertEqual(disks.managed_block(self.TEXT + old + "UUID=z /z ext4 defaults 0 2\n"), old)
        self.assertIsNone(disks.managed_block(self.TEXT))

    def test_block_changes_name_what_to_write_and_what_to_remove_and_ignore_the_markers(self):
        spec = one(uuid=UUID, fstype="ext4", description="a")
        entry = disks.fstab_entry(spec, "kernel", None)
        old = disks.fstab_block([(spec, "kernel", entry)])
        other = one(uuid=UUID, fstype="ext4", description="b")
        new = disks.fstab_block([(other, "kernel", entry)])
        self.assertEqual(disks.block_changes(new, old), (["# m: b (ext4, read-write)"], ["# m: a (ext4, read-write)"]))
        self.assertEqual(disks.block_changes(old, old), ([], []))
        self.assertEqual(disks.block_changes(old, None), (old.splitlines()[1:-1], []))
        self.assertEqual(disks.block_changes(old, old.replace("generated", "written")), ([], []))

    def test_automount_points_map_each_unit_to_its_mount_point(self):
        lines = [
            "/dev/disk/by-uuid/a /mnt/mac-backups fuse.apfs-fuse ro,nofail,x-systemd.automount 0 0",
            "/dev/disk/by-uuid/b /mnt/data ext4 rw,nofail 0 0",
            "/dev/disk/by-uuid/c /mnt/with\\040space ext4 rw,x-systemd.automount 0 0",
            "short line",
        ]
        self.assertEqual(
            disks.automount_points(lines),
            {"mnt-mac\\x2dbackups.automount": "/mnt/mac-backups", "mnt-with\\x20space.automount": "/mnt/with space"},
        )

    def test_block_drift_says_what_differs_in_one_line(self):
        spec = one(uuid=UUID, fstype="ext4", description="a")
        entry = disks.fstab_entry(spec, "kernel", None)
        old = disks.fstab_block([(spec, "kernel", entry)])
        new = disks.fstab_block([(one(uuid=UUID, fstype="ext4", description="b"), "kernel", entry)])
        self.assertEqual(disks.block_drift(new, old), "expected: # m: b (ext4, read-write)")
        self.assertEqual(disks.block_drift(old, old + "# extra\n"), "unexpected: # extra")
        self.assertIn("order", disks.block_drift(old, old))


if __name__ == "__main__":
    unittest.main()

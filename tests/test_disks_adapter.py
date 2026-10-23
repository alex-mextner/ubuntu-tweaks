"""The ``disk`` coverage adapter against a scratch machine: a fake ``lsblk`` and a scratch ``/etc/fstab``."""

import json
import re
import tomllib
import unittest

try:
    import gi  # noqa: F401
except ImportError as error:  # the engine's tests run under the interpreter that has PyGObject
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from tests.util import TempDirCase, write
from ubuntu_tweaks import disks as lib
from ubuntu_tweaks.adapters import disks
from ubuntu_tweaks.adapters._system import System, use
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Item, Layer, Rule, Rules, classify
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile

MAC_UUID = "fcbf521d-af6b-463d-9959-d25f686b3379"
WINDOWS_UUID = "0C64C2AC64C2983A"
ROOT_UUID = "dd8ff67b-27dc-4179-a3ef-35c1363d9ee6"
CARD_UUID = "1234-ABCD"


def node(name, kind, **fields):
    base = {
        "name": name, "path": f"/dev/{name}", "type": kind, "uuid": None, "partuuid": None, "label": None,
        "partlabel": None, "parttypename": None, "fstype": None, "mountpoints": [], "size": "1G", "tran": None,
        "hotplug": False, "pkname": None,
    }  # fmt: skip
    return {**base, **fields}


def lsblk(*extra):
    """The machine this module was written on, plus whatever ``extra`` adds, as ``lsblk --json`` prints it."""
    devices = [
        node("loop0", "loop", fstype="squashfs", mountpoints=["/snap/bare/5"], size="4K"),
        node("loop1", "loop", fstype="squashfs", mountpoints=["/snap/core24/1643"], size="66.8M"),
        node(
            "sda", "disk", tran="usb", hotplug=True, size="931.5G",
            children=[
                node(
                    "sda1", "part", uuid="67E3-17ED", label="EFI", partlabel="EFI System Partition",
                    parttypename="EFI System", fstype="vfat", size="200M", hotplug=True, pkname="sda",
                ),
                node(
                    "sda2", "part", uuid=MAC_UUID, partuuid="ef1d7f6b-712f-4bd8-bc2a-fb8891e13509",
                    parttypename="Apple APFS", fstype="apfs", size="931.3G", hotplug=True, pkname="sda",
                ),
            ],
        ),
        node(
            "nvme0n1", "disk", tran="nvme", size="476.9G",
            children=[
                node(
                    "nvme0n1p1", "part", uuid="DCB1-9A1E", parttypename="EFI System", fstype="vfat", size="200M",
                    mountpoints=["/boot/efi"], tran="nvme", pkname="nvme0n1",
                ),
                node(
                    "nvme0n1p2", "part", partlabel="Microsoft reserved partition", parttypename="Microsoft reserved",
                    size="16M", tran="nvme", pkname="nvme0n1",
                ),
                node(
                    "nvme0n1p3", "part", uuid=WINDOWS_UUID, partuuid="9ac11f37-b383-4ff1-967c-df987ac5d5da",
                    label="Windows 11", partlabel="Basic data partition", parttypename="Microsoft basic data",
                    fstype="ntfs", size="92.7G", tran="nvme", pkname="nvme0n1",
                ),
                node(
                    "nvme0n1p4", "part", uuid=ROOT_UUID, parttypename="Linux filesystem", fstype="ext4", size="384G",
                    mountpoints=["/"], tran="nvme", pkname="nvme0n1",
                ),
                node(
                    "nvme0n1p5", "part", uuid="5b1c0000-0000-4000-8000-000000000001", fstype="swap", size="8G",
                    mountpoints=["[SWAP]"], tran="nvme", pkname="nvme0n1",
                ),
            ],
        ),
        node("sdb", "disk", children=[node("sdb1", "part", uuid="a1b2c3d4-0000-4000-8000-000000000002", fstype="crypto_LUKS", pkname="sdb")]),
        node(
            "sdc", "disk", tran="usb", hotplug=True,
            children=[node("sdc1", "part", uuid=CARD_UUID, partuuid="c4a6d0e1-7f20-4b8a-9d3e-1f2a3b4c5d6e", label="Camera Card", fstype="exfat", size="64G", hotplug=True, pkname="sdc")],
        ),
        *extra,
    ]  # fmt: skip
    return json.dumps({"blockdevices": devices})


FSTAB = f"""# /etc/fstab
/dev/disk/by-uuid/{ROOT_UUID} / ext4 defaults 0 1
/dev/disk/by-uuid/DCB1-9A1E /boot/efi vfat defaults 0 1
/swap2.img\tnone\tswap\tsw\t0\t0
tmpfs /tmp tmpfs defaults 0 0
nas:/export /mnt/nas nfs defaults 0 0
UUID={CARD_UUID} /media/camera exfat defaults,nofail 0 0

# BEGIN ubuntu-tweaks disks (generated)
# mac-backups: MacBook backups (apfs, fuse, read-only)
/dev/disk/by-uuid/{MAC_UUID} /mnt/mac-backups fuse.apfs-fuse ro,allow_other,nosuid,nodev,nofail 0 0
# END ubuntu-tweaks disks
"""
MACHINE_TOML = f"""[modules.disks.mounts.mac-backups]
uuid = "{MAC_UUID}"
fstype = "apfs"
"""


class DiskCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.rootfs = self.tmp / "rootfs"
        self.repo = self.tmp / "repo"
        self.profile_root = self.tmp / "profile"
        for directory in (self.home, self.rootfs, self.repo, self.profile_root):
            directory.mkdir()
        self.ctx = Context(self.repo, Profile(self.profile_root, "box"))

    def machine(self, output: str | None = None, fstab: str | None = FSTAB) -> FakeProcess:
        responses = {} if output is None else {disks.LSBLK: CommandResult(0, output)}
        self.process = FakeProcess(responses)
        if fstab is not None:
            write(self.rootfs / "etc/fstab", fstab)
        manager = use(System(self.process, self.home, self.rootfs))
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return self.process

    def declare(self, text: str = MACHINE_TOML) -> None:
        write(self.profile_root / "machines/box/machine.toml", text)

    def found(self) -> dict[str, Item]:
        return {item.id: item for item in disks.discover(self.ctx)}

    def classified(self, rules: Rules | None = None) -> dict[str, tuple[Coverage, str, str]]:
        rules = rules or Rules()
        owned = disks.owned(self.ctx)
        result = {}
        for item in disks.discover(self.ctx):
            finding = disks.classify(self.ctx, item, rules) or classify(item, rules, owned)
            result[item.id] = (finding.coverage, finding.reason, finding.owner)
        return result


class DiscoverTests(DiskCase):
    def test_every_filesystem_swap_file_and_unmanaged_mount_is_an_item_with_its_kind(self) -> None:
        self.machine(lsblk())
        kinds = {item.id: item.kind for item in disks.discover(self.ctx)}
        self.assertEqual(
            kinds,
            {
                "disk:loops": "loop",
                "disk:uuid:67E3-17ED": "efi",
                f"disk:uuid:{MAC_UUID}": "filesystem",
                "disk:uuid:DCB1-9A1E": "system",
                f"disk:uuid:{WINDOWS_UUID}": "filesystem",
                f"disk:uuid:{ROOT_UUID}": "system",
                "disk:uuid:5b1c0000-0000-4000-8000-000000000001": "swap",
                "disk:uuid:a1b2c3d4-0000-4000-8000-000000000002": "filesystem",
                f"disk:uuid:{CARD_UUID}": "filesystem",
                "disk:fstab:/swap2.img": "swap",
                "disk:fstab:/tmp": "pseudo",
                "disk:fstab:/mnt/nas": "network",
            },
        )

    def test_details_say_what_the_disk_is_where_it_is_and_which_fstab_line_names_it(self) -> None:
        self.machine(lsblk())
        found = self.found()
        self.assertEqual(
            found[f"disk:uuid:{WINDOWS_UUID}"].detail, "ntfs, 92.7G, label 'Windows 11', on nvme0n1p3, not mounted"
        )
        self.assertEqual(found[f"disk:uuid:{MAC_UUID}"].detail, "apfs, 931.3G, on sda2, not mounted, removable")
        self.assertEqual(
            found[f"disk:uuid:{ROOT_UUID}"].detail, "ext4, 384G, on nvme0n1p4, mounted at /, /etc/fstab line 2"
        )
        self.assertIn("/etc/fstab line 7", found[f"disk:uuid:{CARD_UUID}"].detail)
        self.assertEqual(found["disk:loops"].detail, "2 loop devices with a filesystem (snap images)")
        self.assertEqual(found["disk:fstab:/swap2.img"].detail, "swap /swap2.img -> none, /etc/fstab line 4")

    def test_the_lines_of_the_managed_block_are_not_items(self) -> None:
        self.machine(lsblk())
        self.assertFalse([item for item in disks.discover(self.ctx) if "mac-backups" in item.id])

    def test_a_filesystem_without_a_uuid_is_named_by_label_then_by_device(self) -> None:
        extra = [
            node("sdd1", "part", label="Scratch", fstype="ext4", pkname="sdd"),
            node("sde1", "part", fstype="ext4", pkname="sde"),
        ]
        self.machine(lsblk(*extra), fstab="")
        ids = set(self.found())
        self.assertIn("disk:label:Scratch", ids)
        self.assertIn("disk:dev:/dev/sde1", ids)

    def test_a_device_the_fstab_names_by_label_or_link_is_folded_into_its_item(self) -> None:
        fstab = f"LABEL=Camera\\040Card /media/c exfat nofail 0 0\n/dev/disk/by-uuid/{WINDOWS_UUID} /mnt/w ntfs3 ro 0 0\n/dev/sdb1 /mnt/b ext4 defaults 0 0\n"
        self.machine(lsblk(), fstab=fstab)
        found = self.found()
        self.assertIn("/etc/fstab line 1", found[f"disk:uuid:{CARD_UUID}"].detail)
        self.assertIn("/etc/fstab line 2", found[f"disk:uuid:{WINDOWS_UUID}"].detail)
        self.assertIn("/etc/fstab line 3", found["disk:uuid:a1b2c3d4-0000-4000-8000-000000000002"].detail)
        self.assertFalse([item for item in found if item.startswith("disk:fstab:")])

    def test_without_lsblk_one_item_says_so_and_nothing_raises(self) -> None:
        self.machine(None)
        items = list(disks.discover(self.ctx))
        self.assertEqual([(item.id, item.kind) for item in items], [("disk:unreadable", "disk")])
        self.assertIn("lsblk", items[0].detail)

    def test_unparseable_lsblk_output_is_the_same_unreadable_item(self) -> None:
        for output in ("not json", "[]", '{"blockdevices": "x"}', "{}"):
            with self.subTest(output=output):
                self.machine(output)
                self.assertEqual([item.id for item in disks.discover(self.ctx)], ["disk:unreadable"])

    def test_a_missing_fstab_is_not_an_error(self) -> None:
        self.machine(lsblk(), fstab=None)
        self.assertNotIn("disk:fstab:/tmp", self.found())

    def test_discovery_only_queries_lsblk_and_never_changes_anything(self) -> None:
        process = self.machine(lsblk())
        list(disks.discover(self.ctx))
        disks.owned(self.ctx)
        self.assertEqual(set(process.queries), {disks.LSBLK})
        self.assertEqual(process.executed, [])
        self.assertEqual(sorted(path.name for path in self.rootfs.rglob("*") if path.is_file()), ["fstab"])


class ClassifyTests(DiskCase):
    def test_system_swap_loop_pseudo_and_foreign_efi_are_excluded_each_with_its_reason(self) -> None:
        self.machine(lsblk())
        got = self.classified()
        self.assertEqual(got["disk:loops"][:2], (Coverage.EXCLUDED, "read-only loop images (snap packages)"))
        self.assertEqual(got["disk:fstab:/swap2.img"][:2], (Coverage.EXCLUDED, "swap space"))
        self.assertEqual(got["disk:uuid:5b1c0000-0000-4000-8000-000000000001"][:2], (Coverage.EXCLUDED, "swap space"))
        self.assertEqual(got["disk:fstab:/tmp"][:2], (Coverage.EXCLUDED, "kernel pseudo or temporary filesystem"))
        self.assertEqual(got["disk:uuid:67E3-17ED"][0], Coverage.EXCLUDED)
        self.assertIn("EFI System Partition", got["disk:uuid:67E3-17ED"][1])
        self.assertEqual(got[f"disk:uuid:{ROOT_UUID}"][0], Coverage.EXCLUDED)
        self.assertIn("mounted at /,", got[f"disk:uuid:{ROOT_UUID}"][1])
        self.assertIn("mounted at /boot/efi", got["disk:uuid:DCB1-9A1E"][1])

    def test_data_disks_and_network_mounts_are_untracked_until_declared(self) -> None:
        self.machine(lsblk())
        got = self.classified()
        for item_id in (
            f"disk:uuid:{WINDOWS_UUID}", f"disk:uuid:{CARD_UUID}", "disk:uuid:a1b2c3d4-0000-4000-8000-000000000002",
            "disk:fstab:/mnt/nas",
        ):  # fmt: skip
            self.assertEqual(got[item_id], (Coverage.UNTRACKED, "", ""), msg=item_id)

    def test_a_declared_disk_is_tracked_and_names_its_declaration(self) -> None:
        self.machine(lsblk())
        self.declare()
        self.assertEqual(
            self.classified()[f"disk:uuid:{MAC_UUID}"], (Coverage.TRACKED, "", "[modules.disks.mounts.mac-backups]")
        )

    def test_a_rule_of_the_profile_has_the_last_word_over_the_adapter_and_the_engine_does_not(self) -> None:
        self.machine(lsblk())
        mine = Rule(
            (f"disk:uuid:{WINDOWS_UUID}", "disk:loops"),
            Coverage.EXCLUDED,
            "dual boot, never mounted here",
            Layer.PROFILE,
        )
        got = self.classified(Rules((mine,)))
        self.assertEqual(got[f"disk:uuid:{WINDOWS_UUID}"][:2], (Coverage.EXCLUDED, "dual boot, never mounted here"))
        self.assertEqual(got["disk:loops"][1], "dual boot, never mounted here")
        engine = Rule(("disk:loops",), Coverage.TRACKED, "engine default", Layer.ENGINE)
        self.assertEqual(
            self.classified(Rules((engine,)))["disk:loops"][:2],
            (Coverage.EXCLUDED, "read-only loop images (snap packages)"),
        )

    def test_the_unreadable_item_is_left_to_the_generic_rules(self) -> None:
        self.machine(None)
        self.assertEqual(self.classified()["disk:unreadable"][0], Coverage.UNTRACKED)


class OwnedTests(DiskCase):
    def test_a_declaration_by_uuid_label_or_partuuid_owns_the_plugged_in_device(self) -> None:
        self.machine(lsblk())
        self.declare(
            f"""[modules.disks.mounts.mac]
uuid = "{MAC_UUID.upper()}"
fstype = "apfs"

[modules.disks.mounts.windows]
label = "Windows 11"
fstype = "ntfs"

[modules.disks.mounts.card]
partuuid = "c4a6d0e1-7f20-4b8a-9d3e-1f2a3b4c5d6e"
fstype = "exfat"
"""
        )
        self.assertEqual(
            disks.owned(self.ctx),
            {
                f"disk:uuid:{MAC_UUID}": "[modules.disks.mounts.mac]",
                f"disk:uuid:{WINDOWS_UUID}": "[modules.disks.mounts.windows]",
                f"disk:uuid:{CARD_UUID}": "[modules.disks.mounts.card]",
            },
        )

    def test_the_shared_profile_declares_too_and_an_unplugged_disk_owns_nothing(self) -> None:
        self.machine(lsblk())
        write(
            self.profile_root / "profile.toml",
            MACHINE_TOML + '\n[modules.disks.mounts.gone]\nuuid = "0000-0000"\nfstype = "ext4"\n',
        )
        self.assertEqual(disks.owned(self.ctx), {f"disk:uuid:{MAC_UUID}": "[modules.disks.mounts.mac-backups]"})

    def test_nothing_declared_asks_nothing_of_the_machine(self) -> None:
        process = self.machine(lsblk())
        self.assertEqual(disks.owned(self.ctx), {})
        self.assertEqual(process.queries, [])

    def test_a_malformed_declaration_is_skipped_not_raised(self) -> None:
        self.machine(lsblk())
        self.declare("[modules.disks.mounts]\nbroken = 1\n[modules.disks.mounts.ok]\nuuid = 5\n")
        self.assertEqual(disks.owned(self.ctx), {})


class CaptureTests(DiskCase):
    def propose(self, *ids: str) -> list:
        self.machine(lsblk())
        wanted = [item for item in disks.discover(self.ctx) if item.id in ids]
        return list(disks.capture(self.ctx, wanted))

    def test_it_proposes_a_declaration_for_each_untracked_disk_the_module_can_mount(self) -> None:
        everything = [item.id for item in self.found_all()]
        proposals = self.propose(*everything)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal.title, "machines/box/machine.toml")
        self.assertEqual(
            [item.id for item in proposal.items],
            [f"disk:uuid:{MAC_UUID}", f"disk:uuid:{WINDOWS_UUID}", f"disk:uuid:{CARD_UUID}"],
        )
        self.assertNotIn("crypto_LUKS", proposal.text)  # a container the module cannot open
        self.assertNotIn(ROOT_UUID, proposal.text)  # the system's own is not a proposal

    def found_all(self) -> list[Item]:
        self.machine(lsblk())
        return [item for item in disks.discover(self.ctx) if item.kind == "filesystem"]

    def test_the_apfs_disk_is_proposed_read_only_and_the_text_is_a_valid_declaration(self) -> None:
        proposal = self.propose(f"disk:uuid:{MAC_UUID}")[0]
        table = tomllib.loads(proposal.text)["modules"]["disks"]
        self.assertEqual(
            table["mounts"]["apfs-fcbf521d"],
            {
                "uuid": MAC_UUID,
                "fstype": "apfs",
                "read_only": True,
                "description": "apfs (931.3G on sda2, USB)",
            },
        )
        spec = lib.parse_disks(table, home="/home/alice", user="alice")[0]
        self.assertTrue(spec.read_only)
        self.assertEqual(spec.mountpoint, "/mnt/apfs-fcbf521d")

    def test_a_filesystem_linux_writes_safely_gets_no_read_only_line(self) -> None:
        text = self.propose(f"disk:uuid:{CARD_UUID}")[0].text
        table = tomllib.loads(text)["modules"]["disks"]["mounts"]["camera-card"]
        self.assertEqual(
            table, {"uuid": CARD_UUID, "fstype": "exfat", "description": "Camera Card (exfat, 64G on sdc1, USB)"}
        )

    def test_the_windows_partition_is_read_only_commented_out_and_never_forced(self) -> None:
        text = self.propose(f"disk:uuid:{WINDOWS_UUID}")[0].text
        self.assertEqual(tomllib.loads(text), {})  # writing it into the profile enables nothing
        self.assertIn("proposal only", text)
        self.assertIn("never mounted by force", text)
        self.assertIn("# read_only = true", text)
        self.assertNotIn("force", text.replace("never mounted by force", ""))
        uncommented = "\n".join(line[2:] for line in text.splitlines() if re.match(r"# (\[|\w+ = )", line))
        table = tomllib.loads(uncommented)["modules"]["disks"]
        spec = lib.parse_disks(table, home="/home/alice", user="alice")[0]
        self.assertEqual((spec.fstype, spec.read_only, spec.driver, spec.name), ("ntfs", True, "ntfs3", "windows-11"))

    def test_names_are_unique_valid_and_never_a_declared_one(self) -> None:
        extra = [
            node(
                "sdd1",
                "part",
                uuid="aaaa0000-0000-4000-8000-000000000003",
                label="Windows 11",
                fstype="ext4",
                pkname="sdd",
            ),
            node("sde1", "part", uuid="bbbb0000-0000-4000-8000-000000000004", label="!!!", fstype="ext4", pkname="sde"),
        ]
        self.machine(lsblk(*extra))
        self.declare('[modules.disks.mounts.windows-11]\nuuid = "x"\nfstype = "ext4"\n')
        wanted = [item for item in disks.discover(self.ctx) if item.kind == "filesystem"]
        text = next(iter(disks.capture(self.ctx, wanted))).text
        names = re.findall(r"^(?:# )?\[modules\.disks\.mounts\.([^\]]+)\]", text, re.MULTILINE)
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("windows-11", names)
        self.assertIn("windows-11-2", names)
        self.assertTrue(all(lib._NAME.fullmatch(name) for name in names), msg=names)

    def test_nothing_to_propose_is_no_proposal(self) -> None:
        self.assertEqual(self.propose(f"disk:uuid:{ROOT_UUID}", "disk:loops", "disk:fstab:/mnt/nas"), [])
        self.assertEqual(self.propose("disk:uuid:a1b2c3d4-0000-4000-8000-000000000002"), [])

    def test_without_lsblk_nothing_is_proposed(self) -> None:
        self.machine(None)
        self.assertEqual(list(disks.capture(self.ctx, [Item("disk", "disk:unreadable", "disk")])), [])


class DiscoveryRegistrationTests(unittest.TestCase):
    def test_the_adapter_is_found_by_dropping_the_file(self) -> None:
        from ubuntu_tweaks.adapters import discover_adapters

        self.assertIn("disk", [module.NAME for module in discover_adapters()])


if __name__ == "__main__":
    unittest.main()

"""tailnet-mounts: peer and disk discovery, unit rendering, reconcile and the module script, against a fake machine.

Fixtures are the recorded `tailscale status --json` of the owner's tailnet and the `lsblk` output of odroidn2 and
ubuntu-pc. Nothing here touches the real /etc, systemd, ssh or the network.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent
sys.path.insert(0, str(MODULE.parents[1]))
sys.path.insert(0, str(MODULE))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import tailnet_mounts_lib as lib  # noqa: E402
from fake_host import FakeHost, remote_output, status_text  # noqa: E402

from ubuntu_tweaks.errors import ConfigError  # noqa: E402
from ubuntu_tweaks.module_script import (  # noqa: E402
    EXIT_DRIFT,
    EXIT_NEEDS_MANUAL,
    EXIT_OK,
    Account,
    ModuleError,
    NeedsManual,
    main,
)


def load_script():
    loader = importlib.machinery.SourceFileLoader("tailnet_mounts_script", str(MODULE / "tailnet-mounts"))
    spec = importlib.util.spec_from_loader("tailnet_mounts_script", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


script = load_script()

BACKUPS = "ubuntu-pc:/run/media/ultra/backups"
SECOND = "cc32a44a-ace1-43ae-8bcc-607e0f3de823"
DAY = 86400.0


def make_config(**changes):
    options = {"exclude_hosts": [], "idle_timeout": 300, "home": True, **changes}
    return lib.parse_config(options, user="ultra", uid=os.getuid(), gid=os.getgid(), home="/home/ultra")


def shares_of(host, name, config=None):
    return {(share.name, share.path) for share in lib.parse_shares(remote_output(name), config or make_config())}


class Staged(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        (self.root / "etc/systemd/system").mkdir(parents=True)
        self.clock = 1_000_000.0
        self.host = FakeHost()
        self.config = make_config()

    def now(self):
        return self.clock

    def syncer(self, config=None):
        return lib.Syncer(self.root, config or self.config, self.runner, now=self.now)

    def runner(self, argv, timeout):
        return self.host.query(argv, timeout=timeout)

    def unit(self, name):
        return (self.root / "etc/systemd/system" / name).read_text()

    def unit_names(self):
        return sorted(path.name for path in (self.root / "etc/systemd/system").iterdir() if path.is_file())

    def snapshot(self):
        base = self.root / "etc/systemd/system"
        return {path.name: (path.read_text(), path.stat().st_mtime_ns) for path in base.iterdir() if path.is_file()}


class Discovery(Staged):
    def test_status_keeps_only_peers_of_the_linux_family_and_names_them_by_hostname(self):
        tailnet = lib.parse_status(status_text())
        self.assertEqual(tailnet.self_host, "home")
        by_host = {peer.host: peer for peer in tailnet.peers}
        self.assertEqual(by_host["odroidn2"].address, "odroidn2")
        self.assertTrue(by_host["odroidn2"].online)
        self.assertFalse(by_host["do-edge"].online)
        self.assertEqual(by_host["ubuntu-pc"].os, "linux")
        self.assertNotIn("home", by_host)

    def test_a_hostname_that_is_not_a_dns_label_is_addressed_by_its_tailscale_ip(self):
        tailnet = lib.parse_status(status_text())
        odd = next(peer for peer in tailnet.peers if peer.os == "android")
        self.assertEqual(odd.address, "100.109.42.116")
        self.assertEqual(odd.host, "pixel-7")

    def test_a_stopped_tailscale_is_an_error_not_an_empty_tailnet(self):
        with self.assertRaises(lib.SyncError):
            lib.parse_status(status_text(backend="Stopped"))
        with self.assertRaises(lib.SyncError):
            lib.parse_status("not json")

    def test_real_odroidn2_exposes_its_data_disks_and_home_and_nothing_else(self):
        self.assertEqual(
            shares_of("odroidn2", "odroidn2"),
            {("storage", "/mnt/storage"), ("wd500", "/mnt/wd500"), ("home", "/home/ultra")},
        )

    def test_real_ubuntu_pc_exposes_its_udisks_disks_and_home(self):
        self.assertEqual(
            shares_of("ubuntu-pc", "ubuntu-pc"),
            {("backups", "/run/media/ultra/backups"), (SECOND, f"/run/media/ultra/{SECOND}"), ("home", "/home/ultra")},
        )

    def test_home_can_be_switched_off(self):
        found = lib.parse_shares(remote_output("odroidn2"), make_config(home=False))
        self.assertEqual({share.name for share in found}, {"storage", "wd500"})

    def test_colliding_basenames_get_a_deterministic_suffix_by_sorted_path(self):
        payload = {
            "blockdevices": [
                {"name": "a", "type": "part", "fstype": "ext4", "mountpoints": ["/media/x/data", "/mnt/data"]},
                {"name": "b", "type": "part", "fstype": "ext4", "mountpoints": ["/mnt/home"]},
            ]
        }
        output = json.dumps(payload) + "\nHOME=/home/ultra\n"
        found = {(share.name, share.path) for share in lib.parse_shares(output, self.config)}
        self.assertEqual(
            found,
            {("data", "/media/x/data"), ("data-2", "/mnt/data"), ("home", "/home/ultra"), ("home-2", "/mnt/home")},
        )

    def test_swap_squashfs_and_network_filesystems_are_not_shared(self):
        payload = {
            "blockdevices": [
                {"name": "a", "type": "loop", "fstype": "squashfs", "mountpoints": ["/mnt/snap"]},
                {"name": "b", "type": "part", "fstype": "swap", "mountpoints": ["/mnt/swap"]},
                {"name": "c", "type": "part", "fstype": "LVM2_member", "mountpoints": ["/mnt/lvm"]},
                {
                    "name": "d",
                    "type": "part",
                    "fstype": "ext4",
                    "mountpoints": ["/mnt/tailnet/other/disk", "/boot/efi"],
                },
                {"name": "e", "type": "part", "fstype": "ext4", "mountpoints": [None]},
            ]
        }
        output = json.dumps(payload) + "\nHOME=/home/ultra\n"
        self.assertEqual({share.name for share in lib.parse_shares(output, self.config)}, {"home"})

    def test_an_old_lsblk_with_a_single_mountpoint_column_is_understood(self):
        payload = {"blockdevices": [{"name": "a", "type": "part", "fstype": "ext4", "mountpoint": "/mnt/old"}]}
        output = json.dumps(payload) + "\nHOME=/home/ultra\n"
        self.assertIn("old", {share.name for share in lib.parse_shares(output, self.config)})

    def test_nested_children_are_walked(self):
        payload = {
            "blockdevices": [
                {
                    "name": "a",
                    "type": "disk",
                    "fstype": None,
                    "children": [{"name": "a1", "type": "part", "fstype": "ext4", "mountpoints": ["/mnt/deep"]}],
                }
            ]
        }
        output = json.dumps(payload) + "\nHOME=/home/ultra\n"
        self.assertIn("deep", {share.name for share in lib.parse_shares(output, self.config)})

    def test_malformed_remote_output_is_a_discovery_error(self):
        for output in ("", "{}", "garbage\nHOME=/home/ultra\n", '{"blockdevices": 5}\nHOME=/h\n'):
            with self.subTest(output=output), self.assertRaises(lib.DiscoveryError):
                lib.parse_shares(output, self.config)

    def test_hostile_remote_payload_never_reaches_a_unit_file(self):
        hostile = [
            "/mnt/evil\nExecStart=/bin/rm -rf /",
            "/mnt/../etc",
            "/mnt/a%h%n",
            "/mnt/$(id)",
            "/mnt/`id`",
            "/media/x;reboot",
            '/mnt/quote"s',
            "/mnt/back\\slash",
            "/mnt/comma,uid=0",
            "mnt/relative",
            "/mnt/" + "a" * 300,
            "/mnt",
            "/mnt/",
            "/mnt/tailnet",
            "/mnt/-option",
        ]
        payload = {
            "blockdevices": [
                {"name": "x\nEvil=1", "type": "part", "fstype": "ext4", "label": "[Service]", "mountpoints": hostile}
            ]
        }
        output = json.dumps(payload) + "\nHOME=/home/ultra\nExecStart=/bin/true\n"
        found = lib.parse_shares(output, self.config)
        self.assertEqual({share.name for share in found} - {"home", "option"}, set(), found)
        for share in found:
            for text in (
                lib.render_mount("evil", "evil", share, self.config),
                lib.render_automount("evil", share, self.config),
            ):
                lines = [line for line in text.splitlines() if line and not line.startswith(("#", "["))]
                self.assertTrue(all("=" in line for line in lines), text)
                self.assertNotIn("ExecStart", text)
                self.assertNotIn("Evil", text)

    def test_a_hostile_home_is_ignored(self):
        output = json.dumps({"blockdevices": []}) + "\nHOME=/home/u\nExecStart=/bin/true\n"
        self.assertEqual(lib.parse_shares(output, self.config), [lib.Share("home", "/home/u")])
        output = json.dumps({"blockdevices": []}) + "\nHOME=/h%n\n"
        self.assertEqual(lib.parse_shares(output, self.config), [])

    def test_peer_and_disk_names_are_reduced_to_a_safe_alphabet(self):
        self.assertEqual(lib.clean_name("Ultra Win!"), "ultra-win")
        self.assertEqual(lib.clean_name("../.."), "disk")
        self.assertEqual(lib.clean_name("My Disk 2"), "my-disk-2")


class Rendering(Staged):
    def test_mount_and_automount_units_match_the_golden_text(self):
        share = lib.Share("storage", "/mnt/storage")
        self.assertEqual(
            lib.render_mount("odroidn2", "odroidn2", share, make_config()),
            "# managed by ubuntu-tweaks tailnet-mounts\n"
            "[Unit]\n"
            "Description=sshfs odroidn2:/mnt/storage (tailnet-mounts)\n"
            "After=network-online.target tailscaled.service\n"
            "Wants=network-online.target\n"
            "\n"
            "[Mount]\n"
            "What=ultra@odroidn2:/mnt/storage\n"
            "Where=/mnt/tailnet/odroidn2/storage\n"
            "Type=fuse.sshfs\n"
            "Options=_netdev,nofail,allow_other,default_permissions,"
            f"uid={os.getuid()},gid={os.getgid()},reconnect,"
            "ServerAliveInterval=15,ServerAliveCountMax=3,ConnectTimeout=8,BatchMode=yes,"
            "StrictHostKeyChecking=accept-new,UserKnownHostsFile=/var/lib/tailnet-mounts/known_hosts,"
            "IdentityFile=/home/ultra/.ssh/id_ed25519,compression=no,cache=yes,auto_cache\n"
            "TimeoutSec=25\n",
        )
        self.assertEqual(
            lib.render_automount("odroidn2", share, make_config()),
            "# managed by ubuntu-tweaks tailnet-mounts\n"
            "[Unit]\n"
            "Description=sshfs odroidn2:/mnt/storage on first access (tailnet-mounts)\n"
            "\n"
            "[Automount]\n"
            "Where=/mnt/tailnet/odroidn2/storage\n"
            "TimeoutIdleSec=300\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n",
        )

    def test_a_path_with_a_space_is_kept_in_what_and_a_dns_name_is_not_needed_for_an_ip(self):
        text = lib.render_mount("pixel-7", "100.109.42.116", lib.Share("my-disk", "/mnt/my disk"), make_config())
        self.assertIn("What=ultra@100.109.42.116:/mnt/my disk\n", text)

    def test_unit_stems_use_the_systemd_path_escape(self):
        self.assertEqual(lib.unit_stem("odroidn2", "storage"), "mnt-tailnet-odroidn2-storage")
        self.assertEqual(lib.unit_stem("ubuntu-pc", "a-b"), "mnt-tailnet-ubuntu\\x2dpc-a\\x2db")

    def test_idle_timeout_is_a_setting(self):
        text = lib.render_automount("h", lib.Share("d", "/mnt/d"), make_config(idle_timeout=60))
        self.assertIn("TimeoutIdleSec=60\n", text)

    def test_config_rejects_unknown_and_wrongly_typed_options(self):
        for bad in ({"nope": 1}, {"idle_timeout": "5"}, {"idle_timeout": 1}, {"exclude_hosts": "x"}, {"home": "yes"},
                    {"identity_file": "relative"}, {"identity_file": "/a b/key"}, {"identity_file": "/a,b/key"},
                    {"roots": ["/etc"]}, {"roots": "/mnt"}, {"roots": ["/mnt/../etc"]}):  # fmt: skip
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                lib.parse_config(bad, user="ultra", uid=1000, gid=1000, home="/home/ultra")

    def test_config_round_trips_through_json(self):
        config = make_config(exclude_hosts=["clvm"], idle_timeout=120)
        self.assertEqual(lib.Config.from_json(config.to_json()), config)


class Reconcile(Staged):
    def test_first_sync_writes_units_for_every_disk_of_every_online_linux_peer(self):
        report = self.syncer().sync()
        def stem(host, disk):
            return "mnt-tailnet-" + host.replace("-", "\\x2d") + "-" + disk.replace("-", "\\x2d")

        stems = {
            stem(host, disk)
            for host, disks in {
                "odroidn2": ("storage", "wd500", "home"),
                "ubuntu-pc": ("backups", SECOND, "home"),
                "clvm": ("home",),
                "bananapi-m4": ("home",),
            }.items()
            for disk in disks
        }
        expected = sorted(f"{stem}.{kind}" for stem in stems for kind in ("mount", "automount"))
        self.assertEqual(self.unit_names(), expected)
        self.assertEqual(
            self.host.enabled, {f"{stem}.automount" for stem in stems}, "only the automount units are enabled"
        )
        self.assertEqual(self.host.active, self.host.enabled, "and started; the .mount units stay lazy")
        self.assertTrue(any("Permission denied" in line for line in report.warnings), report.warnings)
        self.assertFalse(any("odroidn2" in line for line in report.warnings))

    def test_ssh_uses_a_private_known_hosts_file_batch_mode_and_the_users_key(self):
        self.syncer().sync()
        call = self.host.ssh_calls()[0]
        joined = " ".join(call)
        self.assertIn("-F /dev/null", joined)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("ConnectTimeout=8", joined)
        self.assertIn("StrictHostKeyChecking=accept-new", joined)
        self.assertIn("UserKnownHostsFile=/var/lib/tailnet-mounts/known_hosts", joined)
        self.assertIn("-i /home/ultra/.ssh/id_ed25519", joined)
        self.assertTrue(any(word.startswith("ultra@") for word in call))

    def test_only_online_linux_peers_are_asked(self):
        self.syncer().sync()
        asked = {next(word for word in call if "@" in word).split("@")[1] for call in self.host.ssh_calls()}
        self.assertEqual(asked, {"bananapi-m4", "clvm", "odroidn2", "ubuntu-hyper", "ubuntu-pc"})

    def test_mount_points_are_private_and_plain_directories(self):
        self.syncer().sync()
        base = self.root / "mnt/tailnet"
        self.assertEqual(base.stat().st_mode & 0o777, 0o700)
        self.assertEqual(base.stat().st_uid, os.getuid())
        self.assertTrue((base / "odroidn2/storage").is_dir())
        self.assertFalse((base / "odroidn2").is_symlink())

    def test_a_symlinked_mount_root_is_refused(self):
        (self.root / "mnt").mkdir()
        (self.tmp / "elsewhere").mkdir()
        (self.root / "mnt/tailnet").symlink_to(self.tmp / "elsewhere")
        with self.assertRaises(lib.SyncError):
            self.syncer().sync()
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])

    def test_second_sync_changes_nothing(self):
        self.syncer().sync()
        before = self.snapshot()
        self.host.log.clear()
        self.clock += 300
        report = self.syncer().sync()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.host.mutations(), [])
        self.assertEqual(report.changed, 0)

    def test_units_of_an_offline_host_are_kept(self):
        self.syncer().sync()
        before = self.snapshot()
        self.host.status = status_text(online={"odroidn2": False})
        self.host.log.clear()
        self.clock += 300
        self.syncer().sync()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.host.mutations(), [])

    def test_units_of_a_host_whose_ssh_fails_are_kept(self):
        self.syncer().sync()
        before = self.snapshot()
        del self.host.remotes["odroidn2"]
        self.clock += 300
        report = self.syncer().sync()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(any("odroidn2" in line for line in report.warnings))

    def test_a_vanished_disk_of_a_reachable_host_is_removed_with_its_mount_and_directory(self):
        self.syncer().sync()
        point = self.root / "mnt/tailnet/odroidn2/wd500"
        self.host.mounted.add("/mnt/tailnet/odroidn2/wd500")
        self.host.remotes["odroidn2"] = remote_output("odroidn2").replace("/mnt/wd500", "/var/lib/gone")
        self.host.log.clear()
        self.clock += 300
        self.syncer().sync()
        names = self.unit_names()
        self.assertNotIn("mnt-tailnet-odroidn2-wd500.mount", names)
        self.assertNotIn("mnt-tailnet-odroidn2-wd500.automount", names)
        self.assertIn("mnt-tailnet-odroidn2-storage.mount", names)
        stop = [argv for argv in self.host.log if argv[:2] == ["systemctl", "stop"]]
        self.assertTrue(any("mnt-tailnet-odroidn2-wd500.automount" in argv for argv in stop), stop)
        self.assertIn(["fusermount3", "-uz", "/mnt/tailnet/odroidn2/wd500"], self.host.log)
        self.assertNotIn("mnt-tailnet-odroidn2-wd500.automount", self.host.enabled)
        self.assertFalse(point.exists())

    def test_units_not_marked_as_managed_are_never_touched(self):
        foreign = self.root / "etc/systemd/system/mnt-tailnet-odroidn2-mine.mount"
        foreign.write_text("[Mount]\nWhat=x\nWhere=/mnt/tailnet/odroidn2/mine\n")
        other = self.root / "etc/systemd/system/mnt-data.mount"
        other.write_text("# managed by ubuntu-tweaks tailnet-mounts\n[Mount]\nWhere=/mnt/data\n")
        self.syncer().sync()
        self.assertEqual(foreign.read_text(), "[Mount]\nWhat=x\nWhere=/mnt/tailnet/odroidn2/mine\n")
        self.assertTrue(other.exists(), "a unit that does not live under the tailnet root is not ours, header or not")

    def test_a_reachable_host_with_no_disks_loses_its_units(self):
        self.syncer().sync()
        self.host.remotes["clvm"] = '{"blockdevices": []}\nHOME=/home/ultra\n'
        self.host.remotes["odroidn2"] = '{"blockdevices": []}\nHOME=/home/ultra\n'
        self.syncer(make_config(home=False)).sync()
        self.assertFalse([name for name in self.unit_names() if "odroidn2" in name])

    def test_an_excluded_host_is_dropped_at_once(self):
        self.syncer().sync()
        self.syncer(make_config(exclude_hosts=["odroidn2"])).sync()
        self.assertFalse([name for name in self.unit_names() if "odroidn2" in name])
        self.assertTrue([name for name in self.unit_names() if "ubuntu" in name])

    def test_a_peer_gone_from_the_tailnet_keeps_its_units_for_thirty_days(self):
        self.syncer().sync()
        self.host.status = status_text(drop={"odroidn2"})
        self.clock += 29 * DAY
        self.syncer().sync()
        self.assertTrue([name for name in self.unit_names() if "odroidn2" in name])
        self.clock += 2 * DAY
        self.syncer().sync()
        self.assertFalse([name for name in self.unit_names() if "odroidn2" in name])

    def test_a_peer_that_reappears_restarts_its_thirty_days(self):
        self.syncer().sync()
        self.host.status = status_text(drop={"odroidn2"})
        self.clock += 20 * DAY
        self.syncer().sync()
        self.host.status = status_text()
        self.clock += 20 * DAY
        self.syncer().sync()
        self.host.status = status_text(drop={"odroidn2"})
        self.clock += 20 * DAY
        self.syncer().sync()
        self.assertTrue([name for name in self.unit_names() if "odroidn2" in name])

    def test_a_broken_tailscale_changes_nothing_and_fails_the_sync(self):
        self.syncer().sync()
        before = self.snapshot()
        self.host.tailscale_down = True
        with self.assertRaises(lib.SyncError):
            self.syncer().sync()
        self.assertEqual(self.snapshot(), before)
        self.host.tailscale_down = False
        self.host.status = status_text(backend="NeedsLogin")
        with self.assertRaises(lib.SyncError):
            self.syncer().sync()
        self.assertEqual(self.snapshot(), before)

    def test_a_edited_managed_unit_is_rewritten(self):
        self.syncer().sync()
        path = self.root / "etc/systemd/system/mnt-tailnet-odroidn2-storage.automount"
        path.write_text(path.read_text().replace("TimeoutIdleSec=300", "TimeoutIdleSec=1"))
        self.host.log.clear()
        self.syncer().sync()
        self.assertIn("TimeoutIdleSec=300", path.read_text())
        self.assertIn(["systemctl", "daemon-reload"], self.host.log)

    def test_state_records_peers_shares_and_warnings_for_check_and_status(self):
        self.syncer().sync()
        state = lib.read_state(self.root)
        self.assertEqual(
            {share["name"] for share in state["peers"]["odroidn2"]["shares"]}, {"storage", "wd500", "home"}
        )
        self.assertIn("Permission denied", state["peers"]["ubuntu-hyper"]["error"])
        self.assertEqual(state["peers"]["odroidn2"]["error"], "")


def account(name="ultra"):
    return Account(name, Path("/home") / name, os.getuid())


class ModuleScript(Staged):
    def setUp(self):
        super().setUp()
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/bin/fusermount3").write_text("")
        (self.root / "home/ultra/.ssh").mkdir(parents=True)
        (self.root / "home/ultra/.ssh/id_ed25519").write_text("key")

    def module(self, *, root=True, options=None, host=None):
        self.host = host or self.host
        return script.TailnetMounts(
            self.root,
            options or {},
            account(),
            self.host,
            is_root=root,
            gid=os.getgid(),
        )

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.module().apply()
        self.host.sync_result = "success"

    def facts(self, module=None):
        return {finding.subject: finding for finding in (module or self.module()).findings()}

    def unmet(self, module=None):
        return [finding.line() for finding in (module or self.module()).findings() if not finding.ok]

    def test_nothing_is_satisfied_on_a_fresh_machine(self):
        self.host.packages.clear()
        text = "\n".join(self.unmet())
        self.assertIn("package sshfs", text)
        self.assertIn("tailnet-mounts-sync.timer", text)

    def test_install_makes_check_clean_and_a_second_install_writes_nothing(self):
        self.install()
        self.assertEqual(self.unmet(), [])
        before = {path: path.stat().st_mtime_ns for path in self.root.rglob("*") if path.is_file()}
        self.host.log.clear()
        self.install()
        after = {path: path.stat().st_mtime_ns for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.host.mutations(), [])

    def test_install_puts_a_self_contained_root_owned_copy_in_usr_local_lib(self):
        self.install()
        lib_dir = self.root / "usr/local/lib/tailnet-mounts"
        for name in (
            "tailnet_mounts_lib.py",
            "tailnet-mounts-sync",
            "ubuntu_tweaks/disks.py",
            "ubuntu_tweaks/errors.py",
        ):
            self.assertTrue((lib_dir / name).is_file(), name)
        self.assertTrue(os.access(lib_dir / "tailnet-mounts-sync", os.X_OK))
        done = subprocess.run(
            [sys.executable, "-B", "-I", "-c", f"import sys; sys.path.insert(0, {str(lib_dir)!r}); import tailnet_mounts_lib"],
            capture_output=True, text=True, env={"PATH": os.environ["PATH"]},
        )  # fmt: skip
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads((self.root / "etc/tailnet-mounts/config.json").read_text())["user"], "ultra")

    def test_install_enables_the_timer_and_installs_sshfs_when_it_is_missing(self):
        self.host.packages.clear()
        self.install()
        self.assertIn(["apt-get", "install", "-y", "sshfs"], self.host.log)
        self.assertIn("tailnet-mounts-sync.timer", self.host.enabled)
        timer = (self.root / "etc/systemd/system/tailnet-mounts-sync.timer").read_text()
        self.assertIn("OnBootSec=1min", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)
        service = (self.root / "etc/systemd/system/tailnet-mounts-sync.service").read_text()
        self.assertIn("After=tailscaled.service network-online.target", service)
        self.assertIn("Wants=tailscaled.service network-online.target", service)

    def test_unreachable_peers_are_warnings_in_status_and_not_drift(self):
        self.install()
        module = self.module()
        self.assertEqual(self.unmet(module), [])
        warnings = [f.line() for f in module.findings() if "ubuntu-hyper" in f.subject]
        self.assertTrue(warnings and "Permission denied" in warnings[0], warnings)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(main(module, ["check"], out=out, err=err), EXIT_OK)
        out = io.StringIO()
        main(module, ["status"], out=out, err=io.StringIO())
        self.assertIn("ubuntu-hyper", out.getvalue())

    def test_a_missing_unit_a_stopped_automount_and_a_failed_sync_are_drift(self):
        self.install()
        (self.root / "etc/systemd/system/mnt-tailnet-odroidn2-storage.mount").unlink()
        self.host.active.discard("mnt-tailnet-odroidn2-wd500.automount")
        self.host.sync_result = "exit-code"
        text = "\n".join(self.unmet())
        self.assertIn("mnt-tailnet-odroidn2-storage.mount", text)
        self.assertIn("mnt-tailnet-odroidn2-wd500.automount", text)
        self.assertIn("last sync", text)

    def test_a_hand_edited_unit_is_drift(self):
        self.install()
        path = self.root / "etc/systemd/system/mnt-tailnet-odroidn2-storage.mount"
        path.write_text(path.read_text().replace("allow_other", "allow_root"))
        self.assertTrue(any("mnt-tailnet-odroidn2-storage.mount" in line for line in self.unmet()))

    def test_a_hung_mount_is_a_finding_and_check_does_not_wait_for_it(self):
        self.install()
        point = "/mnt/tailnet/odroidn2/storage"
        self.host.mounted.add(point)
        self.host.hung.add(point)
        self.assertTrue(any("hung" in line and point in line for line in self.unmet()))
        stats = [argv for argv in self.host.log if argv[:1] == ["timeout"]]
        self.assertTrue(stats and stats[-1][:2] == ["timeout", "5"], stats)

    def test_check_never_touches_an_idle_automount_point(self):
        self.install()
        self.host.log.clear()
        self.unmet()
        self.assertEqual(
            [argv for argv in self.host.log if argv[:1] == ["timeout"]], [], "an idle automount is not stat-ed"
        )

    def test_install_needs_root_and_a_missing_sshfs_without_root_needs_manual(self):
        self.host.packages.clear()
        with self.assertRaises(NeedsManual):
            self.module(root=False).apply()
        self.host.packages.add("sshfs")
        with self.assertRaises(ModuleError):
            self.module(root=False).apply()

    def test_a_missing_identity_key_is_a_manual_finding(self):
        (self.root / "home/ultra/.ssh/id_ed25519").unlink()
        finding = next(f for f in self.module().findings() if "id_ed25519" in f.subject)
        self.assertFalse(finding.ok)
        self.assertTrue(finding.manual)

    def test_plan_previews_the_units_without_writing_or_changing_anything(self):
        self.host.packages.clear()
        before = sorted(path for path in self.root.rglob("*"))
        self.host.log.clear()
        lines = self.module(root=False).plan()
        self.assertEqual(sorted(self.root.rglob("*")), before)
        self.assertEqual(self.host.mutations(), [])
        text = "\n".join(lines)
        self.assertIn("apt-get install sshfs", text)
        self.assertIn("ultra@odroidn2:/mnt/storage -> /mnt/tailnet/odroidn2/storage", text)
        self.assertIn(f"ultra@ubuntu-pc:/run/media/ultra/{SECOND} -> /mnt/tailnet/ubuntu-pc/{SECOND}", text)
        self.assertIn("ultra@odroidn2:/home/ultra -> /mnt/tailnet/odroidn2/home", text)
        self.assertIn("ubuntu-hyper", text)
        self.assertIn("Permission denied", text)
        ssh = " ".join(self.host.ssh_calls()[0])
        self.assertIn("UserKnownHostsFile=/dev/null", ssh, "a user preview cannot write the root-owned known_hosts")

    def test_plan_after_install_has_nothing_to_do(self):
        self.install()
        lines = self.module(root=False).plan()
        self.assertFalse(
            [line for line in lines if line.startswith(("write", "remove", "apt-get", "systemctl", "mkdir"))], lines
        )

    def test_exit_codes_through_the_dispatcher(self):
        self.host.packages.clear()
        out = io.StringIO()
        self.assertEqual(main(self.module(), ["check"], out=out, err=io.StringIO()), EXIT_DRIFT)
        self.install()
        self.assertEqual(main(self.module(), ["check"], out=io.StringIO(), err=io.StringIO()), EXIT_OK)
        self.host.packages.clear()
        self.assertEqual(
            main(self.module(root=False), ["install"], out=io.StringIO(), err=io.StringIO()), EXIT_NEEDS_MANUAL
        )


if __name__ == "__main__":
    unittest.main()

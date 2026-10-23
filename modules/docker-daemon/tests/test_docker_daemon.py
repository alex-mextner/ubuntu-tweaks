"""docker-daemon against a staged root (DOCKER_DAEMON_ROOT) with a fake systemctl and dockerd on PATH."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "docker-daemon"

FAKE_SYSTEMCTL = """\
#!/usr/bin/python3 -B
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("systemctl " + " ".join(args) + "\\n")
if args[:2] == ["is-active", "docker"]:
    sys.exit(0 if os.environ.get("FAKE_DOCKER_ACTIVE") == "1" else 3)
if args[0] == "show":
    print("yes" if os.environ.get("FAKE_NEED_RELOAD") == "1" else "no")
    sys.exit(0)
if args[0] in ("reload", "daemon-reload") and os.environ.get("FAKE_SYSTEMCTL_FAIL") == args[0]:
    sys.stderr.write("Failed to " + args[0] + "\\n")
    sys.exit(1)
"""

FAKE_DOCKERD = """\
#!/usr/bin/python3 -B
import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("dockerd " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_DOCKERD_REJECT"):
    sys.stderr.write("unable to configure the Docker daemon with file: unknown option\\n")
    sys.exit(1)
print("configuration OK")
"""

LAPTOP = {"memory_high": "6G", "memory_max": "8G", "memory_swap_max": "1G"}
PC = {"memory_high": "24G", "memory_max": "32G", "memory_swap_max": "4G"}


class Staged(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.bin / "systemctl").chmod(0o755)
        self.log = self.tmp / "log"
        self.log.write_text("")
        self.env_extra = {}
        (self.root / "etc/docker").mkdir(parents=True)
        self.install_dockerd()

    def install_dockerd(self):
        path = self.root / "usr/bin/dockerd"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE_DOCKERD)
        path.chmod(0o755)

    def install_nvidia(self):
        path = self.root / "usr/bin/nvidia-container-runtime"
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    def daemon(self):
        return json.loads((self.root / "etc/docker/daemon.json").read_text())

    def run_script(self, command, **options):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "DOCKER_DAEMON_ROOT": str(self.root),
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(options),
            "FAKE_LOG": str(self.log),
            **self.env_extra,
        }
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True, timeout=60
        )

    def calls(self, prefix=""):
        return [line for line in self.log.read_text().splitlines() if line.startswith(prefix)]

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class DaemonJson(Staged):
    def test_creates_the_file_and_the_slice_and_reloads_only_an_active_docker(self):
        self.install_nvidia()
        self.env_extra = {"FAKE_DOCKER_ACTIVE": "1"}
        result = self.run_script("install", **PC)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.daemon(),
            {
                "cgroup-parent": "docker-limited.slice",
                "runtimes": {"nvidia": {"path": "nvidia-container-runtime", "args": []}},
            },
        )
        slice_text = (self.root / "etc/systemd/system/docker-limited.slice").read_text()
        self.assertIn("[Slice]\nMemoryAccounting=yes\nMemoryHigh=24G\nMemoryMax=32G\nMemorySwapMax=4G\n", slice_text)
        self.assertIn("systemctl daemon-reload", self.calls())
        self.assertIn("systemctl reload docker", self.calls())
        self.assertNotIn("systemctl restart docker", self.calls())
        self.assertIn("new containers", result.stdout)
        self.assertEqual(self.run_script("check", **PC).returncode, 0)

    def test_inactive_docker_is_not_reloaded(self):
        result = self.run_script("install", **PC)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("systemctl reload"), [])
        self.assertIn("not active", result.stdout)
        self.assertTrue((self.root / "etc/docker/daemon.json").is_file())

    def test_foreign_keys_survive_and_nested_tables_merge(self):
        self.install_nvidia()
        (self.root / "etc/docker/daemon.json").write_text(
            json.dumps(
                {
                    "log-driver": "journald",
                    "registry-mirrors": ["https://mirror.example.test"],
                    "runtimes": {"mine": {"path": "/opt/mine"}},
                    "cgroup-parent": "old.slice",
                }
            )
        )
        self.assertEqual(self.run_script("install", **PC).returncode, 0)
        document = self.daemon()
        self.assertEqual(document["log-driver"], "journald")
        self.assertEqual(document["registry-mirrors"], ["https://mirror.example.test"])
        self.assertEqual(document["runtimes"]["mine"], {"path": "/opt/mine"})
        self.assertIn("nvidia", document["runtimes"])
        self.assertEqual(document["cgroup-parent"], "docker-limited.slice")
        backups = list((self.root / "var/backups/ubuntu-tweaks/docker-daemon").glob("*/daemon.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())["cgroup-parent"], "old.slice")

    def test_daemon_option_keys_are_merged_too(self):
        result = self.run_script("install", daemon={"log-opts": {"max-size": "10m"}, "live-restore": True})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.daemon(), {"log-opts": {"max-size": "10m"}, "live-restore": True})
        self.assertFalse((self.root / "etc/systemd/system").exists())

    def test_second_run_changes_nothing_and_does_not_touch_docker(self):
        self.env_extra = {"FAKE_DOCKER_ACTIVE": "1"}
        self.run_script("install", **PC)
        before = {p: (self.root / p).stat().st_mtime_ns for p in self.tree()}
        self.log.write_text("")
        again = self.run_script("install", **PC)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("nothing to change", again.stdout)
        self.assertEqual({p: (self.root / p).stat().st_mtime_ns for p in self.tree()}, before)
        self.assertEqual(self.calls("systemctl reload"), [])
        self.assertEqual(self.calls("systemctl daemon-reload"), [])

    def test_a_formatting_only_difference_is_not_drift(self):
        (self.root / "etc/docker/daemon.json").write_text('{"live-restore":true}')
        self.assertEqual(self.run_script("check", daemon={"live-restore": True}).returncode, 0)

    def test_invalid_json_is_a_manual_step_and_is_never_overwritten(self):
        path = self.root / "etc/docker/daemon.json"
        path.write_text('{"broken": ')
        checked = self.run_script("check", **PC)
        self.assertEqual(checked.returncode, 3)
        self.assertIn("not valid JSON", checked.stdout)
        self.assertIn("not valid JSON", self.run_script("plan", **PC).stdout)
        installed = self.run_script("install", **PC)
        self.assertEqual(installed.returncode, 10)
        self.assertIn("not valid JSON", installed.stdout)
        self.assertEqual(path.read_text(), '{"broken": ')
        self.assertFalse((self.root / "etc/systemd/system").exists())

    def test_a_json_array_is_the_same_manual_step(self):
        (self.root / "etc/docker/daemon.json").write_text("[1]")
        self.assertEqual(self.run_script("install", **PC).returncode, 10)

    def test_dockerd_validation_failure_writes_nothing_not_even_the_slice(self):
        self.env_extra = {"FAKE_DOCKERD_REJECT": "1"}
        result = self.run_script("install", daemon={"nonsense": 1}, **PC)
        self.assertEqual(result.returncode, 1)
        self.assertIn("rejects", result.stderr)
        self.assertEqual(self.tree(), ["usr/bin/dockerd"])  # the fake dockerd only
        self.assertEqual(self.calls("systemctl daemon-reload"), [])

    def test_a_runtime_the_profile_spells_out_wins_over_the_default_entry(self):
        self.install_nvidia()
        custom = {"path": "/opt/nvidia/runtime", "args": ["--debug"]}
        self.assertEqual(self.run_script("install", daemon={"runtimes": {"nvidia": custom}}).returncode, 0)
        self.assertEqual(self.daemon()["runtimes"]["nvidia"], custom)
        self.assertEqual(self.run_script("check", daemon={"runtimes": {"nvidia": custom}}).returncode, 0)

    def test_a_failed_reload_is_an_error(self):
        self.env_extra = {"FAKE_DOCKER_ACTIVE": "1", "FAKE_SYSTEMCTL_FAIL": "reload"}
        result = self.run_script("install", daemon={"live-restore": True})
        self.assertEqual(result.returncode, 1)
        self.assertIn("reload docker", result.stderr)


class Nvidia(Staged):
    def test_auto_skips_the_runtime_when_the_binary_is_missing_and_says_so(self):
        self.assertEqual(self.run_script("install", **PC).returncode, 0)
        self.assertNotIn("runtimes", self.daemon())
        status = self.run_script("status", **PC)
        self.assertIn("nvidia runtime skipped", status.stdout)
        self.assertEqual(status.returncode, 0)

    def test_auto_adds_it_once_the_toolkit_is_installed(self):
        self.run_script("install", **PC)
        self.install_nvidia()
        self.assertEqual(self.run_script("check", **PC).returncode, 3)
        self.assertEqual(self.run_script("install", **PC).returncode, 0)
        self.assertIn("nvidia", self.daemon()["runtimes"])

    def test_true_without_the_binary_needs_manual(self):
        result = self.run_script("install", nvidia_runtime=True)
        self.assertEqual(result.returncode, 10)
        self.assertIn("nvidia-container-toolkit", result.stdout)
        self.assertEqual(self.run_script("check", nvidia_runtime=True).returncode, 3)

    def test_true_without_the_binary_plans_a_manual_step_and_no_write(self):
        plan = self.run_script("plan", nvidia_runtime=True, **PC)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("needs manual", plan.stdout)
        self.assertNotIn("+++", plan.stdout)
        self.assertNotIn("daemon-reload", plan.stdout)

    def test_false_never_adds_it(self):
        self.install_nvidia()
        self.assertEqual(self.run_script("install", nvidia_runtime=False, **PC).returncode, 0)
        self.assertNotIn("runtimes", self.daemon())


class Slice(Staged):
    def test_a_foreign_slice_file_is_backed_up_and_replaced(self):
        path = self.root / "etc/systemd/system/docker-limited.slice"
        path.parent.mkdir(parents=True)
        path.write_text("[Slice]\nMemoryMax=8G\n")
        self.assertEqual(self.run_script("install", **PC).returncode, 0)
        self.assertIn("MemoryMax=32G", path.read_text())
        backups = list((self.root / "var/backups/ubuntu-tweaks/docker-daemon").glob("*/docker-limited.slice"))
        self.assertEqual(backups[0].read_text(), "[Slice]\nMemoryMax=8G\n")

    def test_a_changed_limit_rewrites_the_slice_but_not_daemon_json(self):
        self.run_script("install", **PC)
        stamp = (self.root / "etc/docker/daemon.json").stat().st_mtime_ns
        self.env_extra = {"FAKE_DOCKER_ACTIVE": "1"}
        self.log.write_text("")
        self.assertEqual(self.run_script("install", **{**PC, "memory_max": "40G"}).returncode, 0)
        self.assertIn("MemoryMax=40G", (self.root / "etc/systemd/system/docker-limited.slice").read_text())
        self.assertEqual((self.root / "etc/docker/daemon.json").stat().st_mtime_ns, stamp)
        self.assertEqual(self.calls("systemctl reload"), [])
        self.assertIn("systemctl daemon-reload", self.calls())

    def test_a_unit_systemd_has_not_loaded_is_drift_planned_and_gets_a_daemon_reload(self):
        self.run_script("install", **PC)
        self.env_extra = {"FAKE_NEED_RELOAD": "1"}
        self.assertEqual(self.run_script("check", **PC).returncode, 3)
        plan = self.run_script("plan", **PC)
        self.assertIn("systemctl daemon-reload", plan.stdout)
        self.assertNotIn("nothing to change", plan.stdout)
        self.log.write_text("")
        # nothing differs on disk: install must still reload systemd when it says so
        self.assertEqual(self.run_script("install", **PC).returncode, 0)
        self.assertIn("systemctl daemon-reload", self.calls())

    def test_custom_slice_name(self):
        result = self.run_script("install", slice="containers", memory_max="16G")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.daemon()["cgroup-parent"], "containers.slice")
        self.assertTrue((self.root / "etc/systemd/system/containers.slice").is_file())

    def test_laptop_values_are_accepted_as_they_were(self):
        self.assertEqual(self.run_script("install", **LAPTOP).returncode, 0)
        self.assertIn("MemoryHigh=6G", (self.root / "etc/systemd/system/docker-limited.slice").read_text())


class Preconditions(Staged):
    def test_docker_missing_is_needs_manual_and_changes_nothing(self):
        (self.root / "usr/bin/dockerd").unlink()
        before = self.tree()
        result = self.run_script("install", **PC)
        self.assertEqual(result.returncode, 10)
        self.assertIn("docker is not installed", result.stdout)
        self.assertEqual(self.tree(), before)
        check = self.run_script("check", **PC)
        self.assertEqual(check.returncode, 3)
        self.assertIn("manual", check.stdout)

    def test_options_are_validated(self):
        for bad in (
            {"surprise": 1},
            {"daemon": []},
            {"nvidia_runtime": "maybe"},
            {"memory_max": 8},
            {"memory_max": "eight gigs"},
            {"memory_high": "9G", "memory_max": "8G"},
            {"slice": "Bad.slice", "memory_max": "8G"},
            {"memory_max": "8G", "daemon": {"cgroup-parent": "other.slice"}},
        ):
            result = self.run_script("check", **bad)
            self.assertEqual(result.returncode, 1, bad)
            self.assertIn("docker-daemon:", result.stderr)

    def test_check_and_plan_are_read_only(self):
        before = self.tree()
        self.run_script("check", **PC)
        plan = self.run_script("plan", **PC)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls("systemctl reload"), [])
        self.assertEqual(self.calls("systemctl daemon-reload"), [])


class Plan(Staged):
    def test_plan_shows_the_exact_diffs_and_the_new_containers_note(self):
        self.env_extra = {"FAKE_DOCKER_ACTIVE": "1"}
        plan = self.run_script("plan", **PC)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("+MemoryMax=32G", plan.stdout)
        self.assertIn('+    "cgroup-parent": "docker-limited.slice"', plan.stdout)
        self.assertIn("systemctl daemon-reload", plan.stdout)
        self.assertIn("systemctl reload docker", plan.stdout)
        self.assertIn("new containers", plan.stdout)

    def test_dry_run_flag_and_converged_plan(self):
        self.run_script("install", **PC)
        plan = self.run_script("--dry-run", **PC)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("nothing to change", plan.stdout)

    def test_a_replaced_file_is_announced_with_its_backup(self):
        (self.root / "etc/docker/daemon.json").write_text('{"live-restore": false}')
        plan = self.run_script("plan", daemon={"live-restore": True})
        self.assertIn("back up /etc/docker/daemon.json", plan.stdout)
        self.assertIn('-{"live-restore": false}', plan.stdout)
        self.assertIn('+    "live-restore": true', plan.stdout)


if __name__ == "__main__":
    unittest.main()

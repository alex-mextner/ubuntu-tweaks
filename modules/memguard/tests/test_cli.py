"""`memguard swap-drain`: the decision/result it prints and its exit codes, against a fake
`systemctl` that runs swap-drain on fixtures the way swap-drain.service would."""
import os
import subprocess
import sys
import textwrap
import time

from test_swap_drain import MODULE, Case, ms

MEMGUARD = os.path.join(MODULE, "memguard")


class SwapDrainCommand(Case):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        fake = os.path.join(self.bin, "systemctl")
        with open(fake, "w") as f:
            f.write(textwrap.dedent("""\
                #!/bin/bash
                echo "$*" >>"$FAKE_LOG"
                if [ -n "$FAKE_RC" ]; then echo "Interactive authentication required." >&2; exit "$FAKE_RC"; fi
                nohup $FAKE_UNIT >>"$FAKE_LOG" 2>&1 &
                """))
        os.chmod(fake, 0o755)
        self.fake_log = self.path("systemctl.log")

    def memguard(self, *args, unit=(), rc=None):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": self.fake_log,
               "FAKE_UNIT": " ".join(self.drain_argv(*unit))}
        if rc:
            env["FAKE_RC"] = str(rc)
        return subprocess.run([sys.executable, "-B", MEMGUARD, "swap-drain", *args, "--state-dir", self.state_dir],
                              capture_output=True, text=True, timeout=60, env=env)

    def systemctl_calls(self):
        if not os.path.exists(self.fake_log):
            return []
        with open(self.fake_log) as f:
            return [line.strip() for line in f if line.startswith("start")]

    def test_prints_the_skip_reason(self):
        self.fixtures(avail_mb=19384, used_mb=1507)
        r = self.memguard()
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("skip min_used: SwapUsed 1507M < 4G", r.stdout)
        self.assertEqual(self.systemctl_calls(), ["start --no-block --no-ask-password swap-drain.service"])

    def test_wait_prints_the_result(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        stub = self.stubs("sleep 0.5; printf 'Filename Type Size Used Priority\\n' >\"$SWAPS\"")
        r = self.memguard("--wait", unit=stub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("draining", r.stdout)
        self.assertRegex(r.stdout, r"drain done in \d+ s: swap used 15869M")

    def test_without_wait_reports_the_running_drain(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        stub = self.stubs("sleep 2; printf 'Filename Type Size Used Priority\\n' >\"$SWAPS\"")
        r = self.memguard(unit=stub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("draining", r.stdout)
        self.assertIn("journalctl -u swap-drain -f", r.stdout)
        deadline = time.time() + 20                     # let the background drain finish cleanly
        while self.state().get("last", {}).get("result") != "done" and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.state()["last"]["result"], "done")

    def test_running_drain_is_reported_without_starting_another(self):
        started = time.time() - 120
        ms.write_state({"state": "draining", "pid": os.getpid(), "started": started, "updated": time.time(),
                        "device": "/swap_new.img", "left_kb": 4096 * 1024}, self.state_dir)
        r = self.memguard()
        self.assertEqual(r.returncode, 11)
        self.assertIn(f"drain already in progress (started {time.strftime('%H:%M', time.localtime(started))}, "
                      "4096M left on /swap_new.img)", r.stdout)
        self.assertEqual(self.systemctl_calls(), [])

    def test_deciding_drain_lock_held_is_reported_without_starting_another(self):
        fd = ms.try_lock(os.path.join(self.state_dir, ms.LOCK_NAME), create=True)
        self.addCleanup(os.close, fd)
        r = self.memguard()
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        self.assertIn("drain already in progress (lock held)", r.stdout)
        self.assertEqual(self.systemctl_calls(), [])

    def test_polkit_refusal_is_an_error_naming_the_rule(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        r = self.memguard(rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Interactive authentication required", r.stderr)
        self.assertIn("50-memguard-swap-drain.rules", r.stderr)

    def test_dry_run_decides_locally(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        r = self.memguard("--dry-run", "--config", self.path("memguard.toml"), "--meminfo", self.path("meminfo"),
                          "--psi", self.path("psi"), "--swaps", self.path("swaps"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dry-run: would drain /swap_new.img 15869M", r.stdout)
        self.assertEqual(self.systemctl_calls(), [])

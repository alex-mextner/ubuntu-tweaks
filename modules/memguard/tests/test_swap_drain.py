"""swap-drain: fixture-driven decisions (--dry-run) and the watchdog with stub swapoff/swapon.

Run: python3 -B -m unittest discover -s modules/memguard/tests
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODULE)
import memguard_swap as ms  # noqa: E402

SWAP_DRAIN = os.path.join(MODULE, "swap-drain")
MEM_TOTAL_KB = 47971212          # home, 2026-10-14
SWAP_KB = 16777212               # /swap_new.img


def meminfo(avail_mb, swap_used_mb, swap_total_kb=SWAP_KB, swap_free_kb=None):
    free = swap_total_kb - swap_used_mb * 1024 if swap_free_kb is None else swap_free_kb
    return textwrap.dedent(f"""\
        MemTotal:       {MEM_TOTAL_KB} kB
        MemFree:         1000000 kB
        MemAvailable:   {avail_mb * 1024} kB
        SwapCached:       148684 kB
        SwapTotal:      {swap_total_kb} kB
        SwapFree:       {free} kB
        """)


def psi(some60=0.0, full10=0.0):
    return (f"some avg10=0.00 avg60={some60:.2f} avg300=0.00 total=131459281\n"
            f"full avg10={full10:.2f} avg60=0.00 avg300=0.00 total=109314498\n")


def swaps(used_mb, present=True):
    head = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
    return head + (f"/swap_new.img                           file\t\t{SWAP_KB}\t{used_mb * 1024}\t\t-1\n"
                   if present else "")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.state_dir = os.path.join(self.dir, "run")
        os.makedirs(self.state_dir)
        self.config(heavy_gate="", heavy_locks=[])
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.dir, name)

    def put(self, name, text):
        with open(self.path(name), "w") as f:
            f.write(text)
        return self.path(name)

    def config(self, **swap):
        lines = ["[swap]"] + [f"{k} = {json.dumps(v)}" for k, v in swap.items()]
        self.put("memguard.toml", "\n".join(lines) + "\n")

    def fixtures(self, avail_mb, used_mb, some60=0.0, full10=0.0):
        self.put("meminfo", meminfo(avail_mb, used_mb))
        self.put("psi", psi(some60, full10))
        self.put("swaps", swaps(used_mb))

    def drain_argv(self, *extra):
        return [sys.executable, "-B", SWAP_DRAIN, "--config", self.path("memguard.toml"),
                "--meminfo", self.path("meminfo"), "--psi", self.path("psi"), "--swaps", self.path("swaps"),
                "--state-dir", self.state_dir, "--cgroup-root", self.dir, *extra]

    def run_drain(self, *extra, dry=True, timeout=30):
        argv = self.drain_argv(*extra)
        if dry:
            argv.append("--dry-run")
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def state(self):
        return ms.read_state(self.state_dir)

    def stubs(self, swapoff_body):
        log = self.path("calls.log")
        env = f'SWAPS="{self.path("swaps")}"; MEMINFO="{self.path("meminfo")}"; LOG="{log}"; ' \
              f'STATE="{os.path.join(self.state_dir, "swap-drain.json")}"'
        off = self.put("swapoff", f"#!/bin/bash\n{env}\necho \"swapoff $*\" >>\"$LOG\"\n{swapoff_body}\n")
        on = self.put("swapon", f"#!/bin/bash\n{env}\necho \"swapon $*\" >>\"$LOG\"\n"
                                f"grep -q swap_new \"$SWAPS\" || printf '%s\\n' "
                                f"'/swap_new.img file {SWAP_KB} 0 -1' >>\"$SWAPS\"\n")
        os.chmod(off, 0o755)
        os.chmod(on, 0o755)
        return ["--swapoff", off, "--swapon", on, "--tick", "0.1", "--fall-gb-per-min", "0"]

    def calls(self):
        with open(self.path("calls.log")) as f:
            return f.read().splitlines()


class DryRunDecisions(Case):
    def test_incident_1927_state_drains(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        r = self.run_drain()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would drain /swap_new.img 15869M", r.stdout)

    def test_1827_state_skips_min_used(self):
        self.fixtures(avail_mb=19384, used_mb=1507)
        r = self.run_drain()
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip min_used", r.stdout)

    def test_low_free_pct_drains_below_min_used(self):
        # 3 GB used of a 4 GB swap: under drain_min_used_gb but SwapFree 25 % < 50 %
        self.put("meminfo", meminfo(30000, 3072, swap_total_kb=4 * 1024 * 1024))
        self.put("psi", psi())
        self.put("swaps", "Filename Type Size Used Priority\n/dev/zram0 partition 4194304 3145728 100\n")
        r = self.run_drain()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("would drain /dev/zram0", r.stdout)

    def test_insufficient_headroom_skips(self):
        self.fixtures(avail_mb=20 * 1024, used_mb=15 * 1024)
        r = self.run_drain()
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip headroom", r.stdout)

    def test_psi_high_skips(self):
        self.fixtures(avail_mb=27974, used_mb=15869, full10=2.0)
        r = self.run_drain()
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip psi", r.stdout)

    def test_falling_memavailable_skips(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        r = self.run_drain("--fall-gb-per-min", "3")
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip fall", r.stdout)

    def test_cooldown_skips(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        now = 1_800_000_000
        ms.write_state({"state": "idle", "last_drain_finished": now - 600}, self.state_dir)
        r = self.run_drain("--now", str(now))
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip cooldown: last drain finished 10 min ago", r.stdout)
        r = self.run_drain("--now", str(now + 1200))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_lock_held_reports_in_progress(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        started = time.time() - 60
        ms.write_state({"state": "draining", "pid": os.getpid(), "started": started, "updated": time.time(),
                        "device": "/swap_new.img", "left_kb": 9000 * 1024}, self.state_dir)
        fd = ms.try_lock(os.path.join(self.state_dir, "swap-drain.lock"), create=True)
        self.addCleanup(os.close, fd)
        hhmm = time.strftime("%H:%M", time.localtime(started))
        for dry in (True, False):
            r = self.run_drain(dry=dry)
            self.assertEqual(r.returncode, 11, r.stdout)
            self.assertIn(f"drain already in progress (started {hhmm}, 9000M left on /swap_new.img)", r.stdout)

    def test_heavy_lock_held_skips(self):
        lock = self.put("heavy.lock", "")
        self.config(heavy_gate="", heavy_locks=[lock, self.path("absent.lock")])
        self.fixtures(avail_mb=27974, used_mb=15869)
        self.assertEqual(self.run_drain().returncode, 0)        # free (and a missing file is fine)
        fd = os.open(lock, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                           # what `flock lock <cmd>` takes
        r = self.run_drain()
        self.assertEqual(r.returncode, 10)
        self.assertIn(f"skip heavy-lock: {lock} is held by a heavy run", r.stdout)
        self.assertFalse(os.path.exists(self.path("absent.lock")), "heavy locks must never be created")

    def fake_gate(self, runners):
        # one file per variant: a rewrite in the same second would be served from a stale .pyc
        return self.put(f"gate{runners}.py", textwrap.dedent(f"""\
            from collections import namedtuple
            R = namedtuple("R", "pid label")
            def scan_live_runners():
                return [R(1000 + i, "pytest") for i in range({runners})]
            def _max_live():
                return 2
            """))

    def test_heavy_runners_at_limit_skip(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        self.config(heavy_gate=self.fake_gate(2), heavy_locks=[])
        r = self.run_drain()
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip heavy-run: 2 heavy runner(s) live, limit 2: pid 1000 pytest; pid 1001 pytest", r.stdout)
        self.config(heavy_gate=self.fake_gate(1), heavy_locks=[])
        self.assertEqual(self.run_drain().returncode, 0)

    def test_heavy_gate_via_hook_json(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        hook = self.put("hook.json", json.dumps({"cmd": self.fake_gate(3)}))
        self.config(heavy_gate="auto", heavy_hook=hook, heavy_locks=[])
        r = self.run_drain()
        self.assertIn("skip heavy-run: 3 heavy runner(s) live", r.stdout)

    def test_unavailable_heavy_scan_skips(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        self.config(heavy_gate="auto", heavy_hook=self.path("moved-away.json"), heavy_locks=[])
        r = self.run_drain()
        self.assertEqual(r.returncode, 10)
        self.assertIn("skip heavy-run: live-runner scan unavailable (FileNotFoundError", r.stdout)
        broken = self.put("broken_gate.py", "raise SystemExit(3)\n")
        self.config(heavy_gate=broken, heavy_locks=[])
        self.assertIn("live-runner scan unavailable (rc=3", self.run_drain().stdout)

    def test_dry_run_executes_and_writes_nothing(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        self.run_drain("--swapoff", "/bin/false")
        self.assertEqual(os.listdir(self.state_dir), [])


class Watchdog(Case):
    """Non-dry runs: stub swapoff/swapon edit the /proc/swaps fixture like the kernel would."""

    def test_drain_done_device_back_on(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        stub = self.stubs(textwrap.dedent(f"""\
            sleep 0.4; cp "$STATE" "{self.path('mid-state.json')}"
            printf 'Filename Type Size Used Priority\\n' >"$SWAPS"
            exit 0"""))
        r = self.run_drain(*stub, dry=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), ["swapoff /swap_new.img", "swapon /swap_new.img"])
        with open(self.path("mid-state.json")) as f:
            mid = json.load(f)
        self.assertEqual((mid["state"], mid["device"], mid["size_kb"]), ("draining", "/swap_new.img", SWAP_KB))
        self.assertEqual(mid["swap_total_after_kb"], SWAP_KB)
        self.assertEqual(mid["swap_free_after_kb"], SWAP_KB)
        self.assertIn("left_kb", mid)
        self.assertIsInstance(mid["pid"], int)
        st = self.state()
        self.assertEqual((st["state"], st["last"]["result"]), ("idle", "done"))
        self.assertIn("drain start:", r.stdout)
        self.assertIn("top swap holders:", r.stdout)
        self.assertRegex(r.stdout, r"drain done in \d+s \(/swap_new.img\): swap used 15869M .* -> ")

    def test_watchdog_aborts_on_low_avail_device_stays_on(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        # the swap-in eats RAM: avail falls under the 6 GB floor; the kernel would then stop on
        # the pending SIGTERM and reinsert the device (it stays in /proc/swaps)
        low = self.put("meminfo-low", meminfo(4000, 9000))
        stub = self.stubs(textwrap.dedent(f"""\
            trap 'echo TERM >>"$LOG"; exit 255' TERM
            cp "{low}" "$MEMINFO"
            for i in $(seq 100); do sleep 0.1; done
            exit 0"""))
        r = self.run_drain(*stub, dry=False)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertEqual(self.calls(), ["swapoff /swap_new.img", "TERM"])     # no swapon: it never went off
        self.assertIn("abort /swap_new.img: MemAvailable 4000M < floor 6144M; SIGTERM to swapoff", r.stdout)
        self.assertIn("device stayed on with its pages", r.stdout)
        with open(self.path("swaps")) as f:
            self.assertIn("/swap_new.img", f.read())
        st = self.state()
        self.assertEqual((st["state"], st["last"]["result"]), ("idle", "aborted"))
        self.assertIn("last_drain_finished", st)                 # an abort starts the cooldown too

    def test_watchdog_aborts_on_psi(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        high = self.put("psi-high", psi(full10=7.5))
        stub = self.stubs(textwrap.dedent(f"""\
            trap 'echo TERM >>"$LOG"; exit 255' TERM
            cp "{high}" "{self.path('psi')}"
            for i in $(seq 100); do sleep 0.1; done"""))
        r = self.run_drain(*stub, dry=False)
        self.assertEqual(r.returncode, 12, r.stdout)
        self.assertIn("PSI full avg10 7.5 > 5", r.stdout)

    def test_sigterm_to_drain_aborts_swapoff(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        stub = self.stubs(textwrap.dedent(f"""\
            trap 'echo TERM >>"$LOG"; exit 255' TERM
            touch "{self.path('started')}"
            for i in $(seq 100); do sleep 0.1; done"""))
        argv = [sys.executable, "-B", SWAP_DRAIN, "--config", self.path("memguard.toml"),
                "--meminfo", self.path("meminfo"), "--psi", self.path("psi"), "--swaps", self.path("swaps"),
                "--state-dir", self.state_dir, "--cgroup-root", self.dir, *stub]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        deadline = time.time() + 10
        while not os.path.exists(self.path("started")) and time.time() < deadline:
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)                          # systemctl stop swap-drain
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 12, out)
        self.assertIn("stop requested (SIGTERM)", out)
        self.assertIn("TERM", self.calls())

    def test_swapoff_error_is_not_done(self):
        self.fixtures(avail_mb=27974, used_mb=15869)
        stub = self.stubs("echo 'swapoff: /swap_new.img: swapoff failed: Invalid argument'; exit 255")
        r = self.run_drain(*stub, dry=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR swapoff /swap_new.img rc=255: swapoff: /swap_new.img: swapoff failed", r.stdout)
        self.assertEqual(self.state()["last"]["result"], "error")


class Config(unittest.TestCase):
    def test_shipped_toml_equals_defaults(self):
        with open(os.path.join(MODULE, "memguard.toml"), "rb") as f:
            self.assertEqual(tomllib.load(f)["swap"], ms.DEFAULTS)

    def test_bad_keys_are_errors(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write("[swap]\ndrain_headroom_gb = \"8\"\n")
            f.flush()
            with self.assertRaisesRegex(ValueError, "wrong type"):
                ms.load_config(f.name)
            f.seek(0)
            f.truncate()
            f.write("[swap]\ndrain_headrom_gb = 8\n")
            f.flush()
            with self.assertRaisesRegex(ValueError, "unknown"):
                ms.load_config(f.name)

    def test_missing_file_is_defaults(self):
        self.assertEqual(ms.load_config("/nonexistent/memguard.toml"), ms.DEFAULTS)


class Trigger(unittest.TestCase):
    def snap(self, avail_mb=27974, used_mb=15869, full10=0.0):
        return ms.Snapshot(MEM_TOTAL_KB, avail_mb * 1024, SWAP_KB, SWAP_KB - used_mb * 1024,
                           ms.parse_psi(psi(full10=full10)), ms.parse_swaps(swaps(used_mb)))

    def test_fires_after_stable_period_then_waits_retry(self):
        t = ms.DrainTrigger(ms.DEFAULTS)
        wall = 1_800_000_000
        self.assertIsNone(t.update(0, wall, self.snap(), 0.0, {}, False))
        self.assertIsNone(t.update(299, wall, self.snap(), 0.0, {}, False))
        self.assertIn("conditions held 300 s", t.update(300, wall, self.snap(), 0.0, {}, False))
        self.assertIsNone(t.update(302, wall, self.snap(), 0.0, {}, False))        # retry interval
        self.assertIsNotNone(t.update(300 + 600, wall, self.snap(), 0.0, {}, False))

    def test_spike_resets_the_clock(self):
        t = ms.DrainTrigger(ms.DEFAULTS)
        wall = 1_800_000_000
        t.update(0, wall, self.snap(), 0.0, {}, False)
        t.update(200, wall, self.snap(full10=3.0), 0.0, {}, False)           # pressure spike
        self.assertIsNone(t.update(300, wall, self.snap(), 0.0, {}, False))
        self.assertIsNone(t.update(499, wall, self.snap(), 0.0, {}, False))
        self.assertIsNotNone(t.update(600, wall, self.snap(), 0.0, {}, False))

    def test_never_while_draining_falling_or_cooling_down(self):
        wall = 1_800_000_000
        for kwargs in ({"draining": True}, {"fall": 2.0},
                       {"state": {"last_drain_finished": wall - 300}}):
            t = ms.DrainTrigger(ms.DEFAULTS)
            for now in (0, 400, 800):
                self.assertIsNone(t.update(now, wall, self.snap(), kwargs.get("fall", 0.0),
                                           kwargs.get("state", {}), kwargs.get("draining", False)), kwargs)

    def test_off_switch(self):
        t = ms.DrainTrigger({**ms.DEFAULTS, "trigger": False})
        for now in (0, 400, 800):
            self.assertIsNone(t.update(now, 0, self.snap(), 0.0, {}, False))


class StateContract(unittest.TestCase):
    def test_active_drain_ignores_dead_or_stale(self):
        now = time.time()
        live = {"state": "draining", "pid": os.getpid(), "updated": now}
        self.assertIs(ms.active_drain(live, now), live)
        self.assertIsNone(ms.active_drain({**live, "updated": now - 31}, now))
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.assertIsNone(ms.active_drain({**live, "pid": dead.pid}, now))
        self.assertIsNone(ms.active_drain({**live, "state": "idle"}, now))

    def test_effective_swap(self):
        drain = {"swap_total_after_kb": SWAP_KB, "swap_free_after_kb": SWAP_KB}
        self.assertEqual(ms.effective_swap_kb(1463940, 0, drain), (SWAP_KB, SWAP_KB))
        self.assertEqual(ms.effective_swap_kb(1463940, 0, None), (1463940, 0))


if __name__ == "__main__":
    unittest.main()

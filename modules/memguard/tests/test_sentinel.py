"""mem-sentinel with a running swap-drain: no (d) on the kernel's mid-swapoff 0 % SwapFree, the
swap-in left out of the fall rate, the drain trigger, and the systemd watchdog ping."""
import importlib.machinery
import importlib.util
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MODULE)
import memguard_swap as ms  # noqa: E402

GB = 1024 ** 3
KB = 1024
MEM_TOTAL = 47971212 * KB
SWAP = 16777212 * KB


def load_sentinel(log_path):
    os.environ.update(MEM_SENTINEL_LOG=log_path, MEM_SENTINEL_DRY_RUN="1", MEM_SENTINEL_HEARTBEAT_S="0")
    loader = importlib.machinery.SourceFileLoader("mem_sentinel", os.path.join(MODULE, "mem-sentinel"))
    spec = importlib.util.spec_from_loader("mem_sentinel", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class SentinelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.mod = load_sentinel(os.path.join(self.dir, "sentinel.log"))
        self.mod.PROC_SWAPS = os.path.join(self.dir, "swaps")
        self.mod.PROC_PSI = os.path.join(self.dir, "psi")
        self.write("psi", "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
        self.mi = {}
        self.clock = [1000.0]
        patches = [
            mock.patch.object(ms, "CONFIG_PATH", os.path.join(self.dir, "absent.toml")),
            mock.patch.object(self.mod.syslog, "syslog", lambda *a: None),   # keep tests out of the journal
            mock.patch.object(self.mod, "meminfo", lambda: dict(self.mi)),
            mock.patch.object(self.mod, "psi_full_avg10", lambda: 0.0),
            mock.patch.object(self.mod, "docker_bytes", lambda: 0),
            mock.patch.object(self.mod.time, "monotonic", lambda: self.clock[0]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.set_mem(avail=27.3 * GB, swap_total=SWAP, swap_free=SWAP)
        self.sentinel = self.mod.Sentinel(state_dir=os.path.join(self.dir, "run"))
        self.addCleanup(self.sentinel.log.fh.close)
        self.steers = []
        self.sentinel.steer = lambda key, s, growth, avail, fall, docker, procs, why, now: self.steers.extend(why)
        session = {"bytes": 8 * GB, "pids": [], "steer_pid": 433553, "label": "omp-433553.scope"}
        self.sentinel.sample = lambda now: ({}, {"omp-433553.scope": session})
        self.started = []
        self.sentinel.start_drain = self.started.append

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def set_mem(self, avail, swap_total, swap_free):
        self.mi = {"MemTotal": MEM_TOTAL, "MemAvailable": int(avail), "SwapTotal": int(swap_total),
                   "SwapFree": int(swap_free)}

    def set_swaps(self, used_kb, size_kb=SWAP // KB):
        self.write("swaps", "Filename Type Size Used Priority\n"
                            f"/swap_new.img file {size_kb} {int(used_kb)} -1\n")

    def drain_state(self, left_kb):
        now = time.time()
        ms.write_state({"state": "draining", "pid": os.getpid(), "started": now - 60, "updated": now,
                        "device": "/swap_new.img", "size_kb": SWAP // KB, "left_kb": int(left_kb),
                        "swap_total_after_kb": SWAP // KB, "swap_free_after_kb": SWAP // KB},
                       os.path.join(self.dir, "run"))


class DrainAwareness(SentinelCase):
    def test_mid_swapoff_zero_swapfree_does_not_fire_d(self):
        # /proc/meminfo at 19:27:50 on 2026-10-14: SwapTotal = pages left, SwapFree 0
        self.set_mem(avail=27.3 * GB, swap_total=1463940 * KB, swap_free=0)
        self.set_swaps(1463940)
        self.drain_state(1463940)
        self.sentinel.tick()
        self.assertEqual([w for w in self.steers if w.startswith("(d)")], [])

    def test_same_reading_without_a_drain_fires_d(self):
        self.set_mem(avail=27.3 * GB, swap_total=1463940 * KB, swap_free=0)
        self.set_swaps(1463940)
        self.sentinel.tick()
        self.assertTrue(any(w.startswith("(d) SwapFree 0%") for w in self.steers), self.steers)

    def test_dead_drain_state_is_ignored(self):
        self.set_mem(avail=27.3 * GB, swap_total=1463940 * KB, swap_free=0)
        self.set_swaps(1463940)
        self.drain_state(1463940)
        state = ms.read_state(os.path.join(self.dir, "run"))
        state["updated"] = time.time() - 120             # the drain crashed long ago
        ms.write_state(state, os.path.join(self.dir, "run"))
        self.sentinel.tick()
        self.assertTrue(any(w.startswith("(d)") for w in self.steers))

    def run_swap_in(self, draining, leak_gb_per_tick=0.0):
        """30 s of pages coming back at 6 GB/min: avail falls, the device's "left" falls equally;
        a leak lowers avail on top of that."""
        avail, left = 27.3 * GB, 9 * GB
        for _ in range(16):
            self.set_mem(avail=avail, swap_total=left if draining else SWAP,
                         swap_free=0 if draining else SWAP - left)
            self.set_swaps(left / KB)
            if draining:
                self.drain_state(left / KB)
            self.sentinel.tick()
            self.clock[0] += 2
            avail -= (0.2 + leak_gb_per_tick) * GB
            left -= 0.2 * GB
        return self.sentinel.fall_rate(self.clock[0] - 2) / GB

    def test_swap_in_is_left_out_of_the_fall_rate(self):
        self.assertLess(abs(self.run_swap_in(draining=True)), 0.1)

    def test_a_leak_during_the_drain_still_counts(self):
        self.assertAlmostEqual(self.run_swap_in(draining=True, leak_gb_per_tick=0.1), 3.0, delta=0.2)

    def test_without_a_drain_the_same_fall_counts(self):
        self.assertAlmostEqual(self.run_swap_in(draining=False), 6.0, delta=0.3)


class TriggerInLoop(SentinelCase):
    def test_fires_after_the_stable_period(self):
        self.sentinel.swap.trigger = ms.DrainTrigger({**ms.DEFAULTS, "trigger_stable_s": 10})
        self.set_mem(avail=27974 * 1024 * KB, swap_total=SWAP, swap_free=SWAP - 15869 * 1024 * KB)
        self.set_swaps(15869 * 1024)
        for _ in range(4):
            self.sentinel.tick()
            self.clock[0] += 4
        self.assertEqual(len(self.started), 1, self.started)
        self.assertIn("would drain /swap_new.img 15869M", self.started[0])

    def test_no_fire_with_too_little_headroom(self):
        self.sentinel.swap.trigger = ms.DrainTrigger({**ms.DEFAULTS, "trigger_stable_s": 0})
        self.set_mem(avail=20 * GB, swap_total=SWAP, swap_free=SWAP - 15 * GB)
        self.set_swaps(15 * GB / KB)
        self.sentinel.tick()
        self.assertEqual(self.started, [])


class Watchdog(unittest.TestCase):
    def test_sd_notify_reaches_the_socket(self):
        with tempfile.TemporaryDirectory() as d:
            mod = load_sentinel(os.path.join(d, "sentinel.log"))
            path = os.path.join(d, "notify")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(path)
            self.addCleanup(server.close)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                notify = mod.SdNotify()
            self.addCleanup(notify.sock.close)
            notify("READY=1")
            notify("WATCHDOG=1")
            server.settimeout(2)
            self.assertEqual([server.recv(64), server.recv(64)], [b"READY=1", b"WATCHDOG=1"])

    def test_sd_notify_is_a_noop_outside_systemd(self):
        with tempfile.TemporaryDirectory() as d:
            mod = load_sentinel(os.path.join(d, "sentinel.log"))
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NOTIFY_SOCKET", None)
                mod.SdNotify()("WATCHDOG=1")


if __name__ == "__main__":
    unittest.main()

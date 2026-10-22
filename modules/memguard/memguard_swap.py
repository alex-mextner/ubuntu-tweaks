"""memguard swap hygiene: config, readers, the drain decision and the drain state file.

One definition shared by `swap-drain` (the root oneshot that drains, started by
swap-drain.timer or the `memguard` CLI) and the `memguard` CLI, so thresholds and the
state-file contract cannot drift apart. Readers of the state file (a heavy-operation gate;
the memguard daemon later) use `active_drain` and `effective_swap_kb`. Stdlib only.

State file contract (`/run/memguard/swap-drain.json`, written atomically by the lock holder):
  state                "draining" while a swapoff runs, otherwise "idle"
  pid, started         the swap-drain process and its start (epoch s)
  updated              refreshed every watchdog tick (~1 s) while draining
  device, size_kb      the device being drained and its full size
  left_kb              pages of that device still in swap
  swap_total_after_kb  SwapTotal / SwapFree once the device is back on and empty: what a
  swap_free_after_kb   gate should judge instead of /proc/meminfo while draining
  last_decision        {at, action, reason, detail} of the latest run
  last                 {result, device, duration_s, finished, detail} of the latest drain
  last_drain_finished  epoch s of the latest drain that ran (done or aborted): the cooldown
A reader trusts "draining" only when `active_drain()` agrees (pid alive, updated recently).
"""

import errno
import fcntl
import json
import os
import stat
import time
import tomllib
from dataclasses import dataclass, field

MB_KB = 1024  # kB per MiB
GB_KB = 1024 * 1024  # kB per GiB

CONFIG_PATH = "/etc/memguard/memguard.toml"
STATE_DIR = "/run/memguard"
# The drain's lock lives in the root-owned 0755 state dir, not sticky /run/lock: there any user
# could pre-create the file (root's O_CREAT then fails under fs.protected_regular) and disable
# every drain.
LOCK_NAME = "swap-drain.lock"
STATE_NAME = "swap-drain.json"
STALE_S = 30  # a "draining" state not refreshed for this long is a dead drain

# Every [swap] key with its default; memguard.toml documents the same values (a test keeps
# the two equal).
DEFAULTS = {
    # drain conditions (all must hold)
    "drain_min_used_gb": 4.0,  # drain when SwapUsed >= this ...
    "drain_below_free_pct": 50.0,  # ... or SwapFree below this % of SwapTotal
    "drain_headroom_gb": 8.0,  # MemAvailable - SwapUsed(device) must stay >= the larger of
    "drain_headroom_pct": 20.0,  # these two (GB, % of MemTotal)
    "psi_some_avg60_max": 5.0,  # /proc/pressure/memory some avg60 must be below
    "psi_full_avg10_max": 1.0,  # ... and full avg10 below
    "fall_gb_per_min_max": 1.0,  # MemAvailable must not be falling faster than this
    "fall_window_s": 15,  # swap-drain samples MemAvailable this long before deciding
    "drain_cooldown_min": 20,  # since the last drain finished (done or aborted)
    "heavy_locks": [],  # shared non-blocking flock, held for the drain
    "heavy_gate": "",  # heavy-op gate script for the live-runner scan; "auto" = the "cmd" of
    "heavy_hook": "",  # this JSON file; "" = scan off
    "heavy_max_live": 0,  # skip at this many live heavy runners; 0 = the gate's limit
    "user": "",  # desktop user: runs the heavy scan, owns the polkit rule; install.sh fills it in
    # watchdog: SIGTERM to swapoff when any of these trips
    "abort_avail_gb": 6.0,  # MemAvailable below the larger of these two
    "abort_avail_pct": 12.0,
    "abort_psi_full_avg10": 5.0,
    "abort_after_min": 15,  # a drain running longer than this is aborted
}


def load_config(path=None):
    """[swap] of memguard.toml over DEFAULTS. A missing file means defaults; a key of the
    wrong type is an error (a typo must not silently disable a safety threshold)."""
    path = path or CONFIG_PATH
    cfg = dict(DEFAULTS)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return cfg
    swap = data.get("swap", {})
    if not isinstance(swap, dict):
        raise ValueError(f"{path}: [swap] must be a table, got {type(swap).__name__}")
    for key, value in swap.items():
        if key not in DEFAULTS:
            raise ValueError(f"{path}: unknown [swap] key {key!r}")
        default = DEFAULTS[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif isinstance(default, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ValueError(f"{path}: [swap] {key} = {value!r} has the wrong type")
        cfg[key] = value
    return cfg


# ----------------------------------------------------------------------------- readers
@dataclass
class SwapDevice:
    name: str
    size_kb: int
    used_kb: int
    prio: int


@dataclass
class Snapshot:
    mem_total_kb: int
    avail_kb: int
    swap_total_kb: int
    swap_free_kb: int
    psi: dict = field(default_factory=dict)  # {"some": {"avg10":..}, "full": {...}}
    devices: list = field(default_factory=list)

    @property
    def swap_used_kb(self):
        return max(self.swap_total_kb - self.swap_free_kb, 0)

    def psi_val(self, kind, avg):
        return self.psi.get(kind, {}).get(avg, 0.0)


def parse_meminfo(text):
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0])
    return out


def parse_psi(text):
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        kind, *pairs = line.split()
        out[kind] = {k: float(v) for k, _, v in (p.partition("=") for p in pairs) if k.startswith("avg")}
    return out


def parse_swaps(text):
    devices = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            name = parts[0].replace("\\040", " ")
            devices.append(SwapDevice(name, int(parts[2]), int(parts[3]), int(parts[4])))
    return devices


def read_text(path):
    with open(path) as f:
        return f.read()


@dataclass
class Sources:
    meminfo: str = "/proc/meminfo"
    psi: str = "/proc/pressure/memory"
    swaps: str = "/proc/swaps"

    def snapshot(self):
        mi = parse_meminfo(read_text(self.meminfo))
        try:
            psi = parse_psi(read_text(self.psi))
        except OSError:
            psi = {}
        return Snapshot(
            mi.get("MemTotal", 0),
            mi.get("MemAvailable", 0),
            mi.get("SwapTotal", 0),
            mi.get("SwapFree", 0),
            psi,
            parse_swaps(read_text(self.swaps)),
        )


# ----------------------------------------------------------------------------- decision
def headroom_kb(cfg, mem_total_kb):
    return max(cfg["drain_headroom_gb"] * GB_KB, cfg["drain_headroom_pct"] / 100 * mem_total_kb)


def abort_floor_kb(cfg, mem_total_kb):
    return max(cfg["abort_avail_gb"] * GB_KB, cfg["abort_avail_pct"] / 100 * mem_total_kb)


def mb(kb):
    return f"{kb / MB_KB:.0f}M"


def describe(snap):
    free_pct = snap.swap_free_kb * 100 / snap.swap_total_kb if snap.swap_total_kb else 100.0
    return (
        f"swap used {mb(snap.swap_used_kb)} free {free_pct:.0f}%, avail {mb(snap.avail_kb)}, "
        f"psi some60 {snap.psi_val('some', 'avg60'):.1f} full10 {snap.psi_val('full', 'avg10'):.1f}"
    )


def drainable(snap, cfg):
    """Devices whose pages RAM can take now (most used first), or a (reason, detail) skip."""
    used = snap.swap_used_kb
    free_pct = snap.swap_free_kb * 100 / snap.swap_total_kb if snap.swap_total_kb else 100.0
    if used < cfg["drain_min_used_gb"] * GB_KB and free_pct >= cfg["drain_below_free_pct"]:
        return None, (
            "min_used",
            f"SwapUsed {mb(used)} < {cfg['drain_min_used_gb']:g}G and SwapFree "
            f"{free_pct:.0f}% >= {cfg['drain_below_free_pct']:g}%",
        )
    need = headroom_kb(cfg, snap.mem_total_kb)
    devices = sorted((d for d in snap.devices if d.used_kb > 0), key=lambda d: d.used_kb, reverse=True)
    fits = [d for d in devices if snap.avail_kb - d.used_kb >= need]
    if not fits:
        biggest = devices[0].used_kb if devices else used
        return None, (
            "headroom",
            f"avail {mb(snap.avail_kb)} - swap {mb(biggest)} = {mb(snap.avail_kb - biggest)} < headroom {mb(need)}",
        )
    return fits, None


def check_pressure(snap, cfg, fall_gb_per_min):
    some60, full10 = snap.psi_val("some", "avg60"), snap.psi_val("full", "avg10")
    if some60 >= cfg["psi_some_avg60_max"] or full10 >= cfg["psi_full_avg10_max"]:
        return (
            "psi",
            f"PSI some avg60 {some60:.1f} (max {cfg['psi_some_avg60_max']:g}), "
            f"full avg10 {full10:.1f} (max {cfg['psi_full_avg10_max']:g})",
        )
    if fall_gb_per_min > cfg["fall_gb_per_min_max"]:
        return ("fall", f"MemAvailable falling {fall_gb_per_min:.1f} G/min > {cfg['fall_gb_per_min_max']:g}")
    return None


def check_cooldown(state, cfg, now):
    last = (state or {}).get("last_drain_finished")
    if last is not None and now - last < cfg["drain_cooldown_min"] * 60:
        return ("cooldown", f"last drain finished {(now - last) / 60:.0f} min ago < {cfg['drain_cooldown_min']:g} min")
    return None


def memory_decision(snap, cfg, fall_gb_per_min, state, now):
    """Cheap, I/O-free part of the decision: (devices, None) to drain or (None, (reason, detail))."""
    skip = check_cooldown(state, cfg, now)
    if skip:
        return None, skip
    devices, skip = drainable(snap, cfg)
    if skip:
        return None, skip
    skip = check_pressure(snap, cfg, fall_gb_per_min)
    if skip:
        return None, skip
    return devices, None


def watchdog_trip(snap, cfg, elapsed_s):
    """Why a running drain must stop now, or None."""
    floor = abort_floor_kb(cfg, snap.mem_total_kb)
    if snap.avail_kb < floor:
        return f"MemAvailable {mb(snap.avail_kb)} < floor {mb(floor)}"
    full10 = snap.psi_val("full", "avg10")
    if full10 > cfg["abort_psi_full_avg10"]:
        return f"PSI full avg10 {full10:.1f} > {cfg['abort_psi_full_avg10']:g}"
    if elapsed_s > cfg["abort_after_min"] * 60:
        return f"running {elapsed_s / 60:.0f} min > {cfg['abort_after_min']:g} min"
    return None


# ----------------------------------------------------------------------------- state file
def state_path(state_dir=STATE_DIR):
    return os.path.join(state_dir, STATE_NAME)


def read_state(state_dir=STATE_DIR):
    try:
        with open(state_path(state_dir)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state, state_dir=STATE_DIR):
    os.makedirs(state_dir, mode=0o755, exist_ok=True)
    path = state_path(state_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def active_drain(state, wall=None):
    """The state dict if a drain is really running, else None (crashed drains are ignored)."""
    wall = time.time() if wall is None else wall
    if not state or state.get("state") != "draining":
        return None
    if not pid_alive(state.get("pid")) or wall - state.get("updated", 0) > STALE_S:
        return None
    return state


def effective_swap_kb(swap_total_kb, swap_free_kb, drain):
    """(total, free) to judge: during a drain the kernel shows SwapFree 0 and SwapTotal = pages
    left, so use the totals the device will have once it is back on."""
    if drain and "swap_total_after_kb" in drain and "swap_free_after_kb" in drain:
        return drain["swap_total_after_kb"], drain["swap_free_after_kb"]
    return swap_total_kb, swap_free_kb


def try_lock(path, shared=False, create=False):
    """Non-blocking flock; the open fd on success, None when held elsewhere. Heavy locks are
    user files in sticky /tmp: never created (fs.protected_regular would then lock the owner
    out of O_CREAT opens), never followed through a symlink, never opened blocking (a FIFO), and
    must be regular files."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd

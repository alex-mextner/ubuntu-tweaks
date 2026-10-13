# Spec: memguard — macOS-like memory-pressure guard, hang capture, swap hygiene

Source of truth for the `memguard`, `hang-capture`, `apple-hid-battery` and `machines/*` modules. Every user
request is recorded verbatim under *Requests*; *Requirements* is what they mean for the
implementation. Change this file in the same commit as any behavior change. The module
model (`modules/<name>/module.toml`, actions with `check()`/`run()`, `apply --commit`,
`status`, `doctor`, `~/.config/ubuntu-tweaks/config.toml`) is defined in
`docs/specs/ubuntu-tweak-cli.md`; this spec only adds what a system-level module needs.

## Requests (verbatim, in order)

1. «все что ты настроил надо чтобы было распределено между rig и ubuntu-tweaks … чтобы в
   будущем ничего не потерялось при переносе на новую систему … rig больше про агентов и
   разработку, а твики про систему в целом, про приведение ее в адекватное состояние и
   похожее на макос. в частности надо сделать чтобы как на маке паузились аппки и
   показывалось окошко где можно что-то закрыть или продолжить. или мб сможешь найти еще
   более удачные идеи и реализации. и пусть оно умеет будет расширяемым, чтобы rig в него
   мог агентов закинуть которые много ресурсов жрут например.»
2. «например omp-supervisor и omp-steer кажется должно быть в agent-tools»
3. «важно чтобы все что мы настраиваем в систему сохранялось в ~/xp/ubuntu-tweaks/»

### Decisions approved by the owner (2026-10-11)

1. At critical pressure: freeze the biggest offenders (`cgroup.freeze`), show a macOS-style
   dialog (Quit / Resume per item, Resume all), auto-resume with a countdown once memory
   recovers (> 30 % free).
2. The omp cgroup wrapper lives in rig (`harness_wrappers` gets a `cgroup:` option), NOT
   here.
3. Spec first, then plan, then code.

## Why

Two thrash incidents on a 48 GB laptop (MSI Pulse GL66), 2026-10-10 15:xx and 2026-10-11
13:34–13:54, evidence in `OPEN-ISSUES.md` #6/#7 of the hand-rolled fixes (see *Migration*):

- Agent sessions ran concurrent full test suites, three `review diff` fan-outs (up to 12 fat
  agent CLIs) and pytest with testcontainers (6 docker containers). 26 GB vanished in 2 min
  (MemAvailable 28.5 → 2.3 GB, 13:34:40 → 13:36:32).
- Swap (16 GB) had been 100 % full for 13 h from the previous day's spike. Linux never swaps
  pages back in on its own, so the spike had zero swap headroom.
- The kernel OOM killer never fired: reclaim kept "making progress" by evicting file pages
  (binaries, `node_modules`, fixtures), MemAvailable floored at ~1.9 GB, every process waited
  on disk. That is thrash, not OOM — 18 min of it, kernel alive, manual reset.
- systemd-oomd watched exactly one cgroup, `user@1000.service`, at 50 % pressure for 20 s,
  and swap not at all (`-.slice` ships `ManagedOOMSwap=auto` = off). Every session and
  everything it spawned lived in one cgroup (`app.slice/ptyxis-spawn-*.scope`), so the only
  possible kill was "all sessions + the terminal". oomd itself was starved of CPU/IO once the
  thrash began and never completed its window.
- Earlier (10-07..10-09) 9 hard hangs from a RAM-module/BIOS issue left no trace: nothing
  was armed (no lockup → panic, no kdump, no hardware watchdog, journald syncing every 35 s).

macOS handles the first case with "Your system has run out of application memory": apps are
paused and the user picks what to quit. That is the behavior requested here.

## Requirements

### Scope and non-goals

- This repo makes the *system* behave: attribute memory to cgroups, warn, freeze, show the
  dialog, kill as a last resort, capture hangs, keep swap usable. It is public and generic.
- No agent knowledge lives here. Nothing in this repo knows what `omp`, `omp-steer`,
  `tg-ctl` or a "session" is. Agent-specific behavior arrives through the policy extension
  point (`policy.d`) and is owned by rig; agent CLIs are owned by agent-tools (see *Cross-repo
  split*).
- Hardware-specific fixes are not tweaks; they are a per-machine profile (`machines/`).
- Not covered: the kernel panic/GPU investigation itself (OPEN-ISSUES #2, #5), shutdown
  debugging (#4).

### Architecture

Five layers, each useful alone; any one would have shortened the 10-11 incident.

| Layer | Component | Owner |
|---|---|---|
| L1 admission | pre-command hooks refuse heavy local runs | agent-tools |
| L2 containment | per-session scopes with `MemoryHigh/Max`, `docker-limited.slice` | rig (sessions), here (docker) |
| L3 steer/freeze | `memguard` daemon: warn → freeze → dialog → auto-resume | here |
| L4 kill | systemd-oomd swap/pressure kill, earlyoom | here |
| L5 forensics | hang-capture: lockup → panic → kdump, watchdog, probe, after-hang | here |

#### The `memguard` daemon

`/usr/local/sbin/memguard`, python3 stdlib only, root, unit `memguard.service`
(`Nice=-10`, `OOMScoreAdjust=-900`, `MemoryMin=256M`, `LimitMEMLOCK=infinity` with
`mlockall`, `CPUWeight=500`, `IOWeight=500`, `Restart=always`): it must keep sampling while
everything else thrashes. 2 s tick; one pass over `/proc` plus `memory.current` of every
accounted cgroup; `/proc/meminfo` and `/proc/pressure/memory` each tick. The baseline is
today's `mem-sentinel.py`; its heuristics and defaults carry over unchanged, renamed from
`MEM_SENTINEL_*` env vars to `/etc/memguard/memguard.toml`:

```toml
[daemon]
interval_s = 2
ring_s = 60                   # per-cgroup history window
heartbeat_s = 30              # status line cadence in the log (0 = off)
log = "/var/log/memguard/memguard.log"

[warn]                        # heuristics a–e of mem-sentinel, per class
low_avail_pct = 25            # (a) avail < 25 % and the largest class holds
top_share_pct = 30            #     ≥ 30 % of used memory or ≥ 8 GB
top_abs_gb = 8
growth_gb = 4                 # (b) class grew ≥ 4 GB within growth_window_s
growth_window_s = 30
fall_gb_per_min = 6           # (c) MemAvailable falling ≥ 6 GB/min → fastest-growing class
swapfree_pct = 30             # (d) SwapFree < 30 % → largest class
class_max_gb = 16             # (e) one class ≥ 16 GB
notify_cooldown_s = 180       # per class and per notify target

[critical]                    # heuristic f
avail_pct = 10                # avail < 10 % (~1 min above earlyoom's 4 %)
psi_full_avg10 = 20           # or PSI memory full avg10 > 20 % (thrash has begun)
freeze_until_avail_pct = 30   # freeze biggest classes until avail ≥ 30 %
resume_countdown_s = 30       # auto-resume countdown once recovered; dialog can cancel
cooldown_s = 180              # broadcast / notify rate limit at critical
```

`~/.config/ubuntu-tweaks/config.toml` `[modules.memguard.config]` overrides these keys; the
install step renders the merged file. `memguard-dialog` reads the same file for display.

#### Units of accounting

A **unit** is a cgroup under `/sys/fs/cgroup`; memory is `memory.current`, membership is
`cgroup.procs`. Enumerated each tick, deepest leaf wins, every pid counted once:

- user session: `user.slice/user-<uid>.slice/user@<uid>.service/app.slice/app-*.scope`
  (GNOME launches every app in one: `app-gnome-<app-id>-<pid>.scope`, flatpak
  `app-flatpak-<app-id>-<pid>.scope`, snap `snap.<name>.*.scope`) and any scope under a slice
  a policy class matches (e.g. rig's `omp.slice/omp-*.scope`);
- system: `system.slice/docker-*.scope` (and `docker-limited.slice/*`), `system.slice/*.service`
  only when a class names it;
- legacy fallback: a pid named by a class (`comm`/`cmdline` regex) that sits in no matched
  scope is accounted as a process tree (pid + descendants not already inside a matched
  scope). This is mem-sentinel's "legacy omp pid" path and stays for sessions started
  outside a scope. Freezing a tree is `SIGSTOP`/`SIGCONT` on every pid, which is why scopes
  are preferred (`cgroup.freeze` is atomic and children cannot escape).

#### Policy model

Policy is TOML in `/etc/memguard/policy.d/*.toml` (shipped by this module) and
`~/.config/memguard/policy.d/*.toml` (per user; rig drops `50-agents.toml` here). Files
merge in lexical order across both directories, the user directory after the system one;
a `[[class]]` with the same `name` replaces the earlier one (later/user wins). Parsed with
`tomllib`; the daemon re-reads on `SIGHUP` and on inotify of both directories.

```toml
[[class]]
name = "desktop-app"
match = { cgroup = "user.slice/*/app.slice/app-*.scope" }     # glob on the cgroup path
display = { resolver = "desktop-app" }                         # icon + name from .desktop
[[class.actions]]
stage = "warn";     do = "notify"                              # desktop notification
[[class.actions]]
stage = "critical"; do = "freeze"

[[class]]
name = "docker"
match = { cgroup = "system.slice/docker-*.scope" }
display = { resolver = "exec", exec = "/usr/local/libexec/memguard/docker-name" }
[[class.actions]]
stage = "critical"; do = "freeze"

[[class]]
name = "protected"
match = { comm = "^(gnome-shell|pipewire|wireplumber|sshd|tailscaled|systemd.*|dbus-.*|Xwayland|ptyxis|memguard.*)$" }
protect = true                                                 # never frozen or killed
```

- `match`: any of `cgroup` (glob, relative to `/sys/fs/cgroup`), `comm` (regex on
  `/proc/<pid>/comm`), `cmdline` (regex on the joined cmdline), `uid`. All given keys must
  match. First matching class in merged order wins for a unit; `protect = true` classes are
  evaluated first and remove their pids and cgroups from every other class.
- `display`: how the dialog names a unit. Builtin `desktop-app` resolves the app id from the
  scope name (`app-gnome-<id>-…`, `app-flatpak-<id>-…`, `snap.<name>`) to its `.desktop` name
  and icon; builtin `cgroup-name` shows the scope name; `exec` runs the command with the
  unit JSON (below) on stdin and expects `{"name": …, "detail": …, "icon": …}` on stdout
  (2 s timeout, result cached per unit for 15 s, as mem-sentinel caches `omp-live status`).
- `[[class.actions]]`: ordered by stage `warn | high | critical`; `do = notify | freeze |
  kill`. `warn` fires on heuristics a–e for that unit; `high` fires when the unit alone
  exceeds `class_max_gb` *and* avail < `low_avail_pct`; `critical` is machine-wide (f).
  `notify` takes `exec = "<cmd>"` (default: the desktop notification via the dialog
  service). The JSON on stdin is the extension point:

  ```json
  {"stage": "warn", "class": "agents", "unit": "omp.slice/omp-4242-1760181234.scope",
   "cgroup": "/sys/fs/cgroup/user.slice/…/omp-4242-1760181234.scope",
   "pids": [4242, 4310], "rss": 9123456789, "growth_30s": 4521000000,
   "top": [{"pid": 4310, "comm": "node", "rss": 3100000000}],
   "machine": {"total": 51539607552, "avail": 9000000000, "fall_per_min": 6800000000,
               "swap_free_pct": 12, "psi_full_avg10": 4.0}, "reasons": ["(b) grew 4.2 GB in 30 s"]}
  ```

  Rate-limited by `notify_cooldown_s` per unit; a repeat carries `"nth": 2`. Executed as
  the unit's uid (`runuser -u <user>` with the user's `PATH`/`HOME`, as mem-sentinel does),
  off-thread, 40 s timeout, result logged.
- `kill` sends `SIGKILL` to the cgroup (`cgroup.kill`) or the tree. No shipped policy uses
  it; it exists so a user policy can make a class fail fast instead of freezing.

How rig plugs in (owned by rig, documented here as the reference use of the extension
point): `~/.config/memguard/policy.d/50-agents.toml` matches
`cgroup = "user.slice/*/omp.slice/omp-*.scope"`, `display.exec = "tg-ctl omp-live status
--memguard"` (name "omp session: CRYPT", detail = cwd/title), `warn → notify.exec =
"omp-steer --memguard"` (delivers mem-sentinel's steer text into the owning session),
`critical → freeze`. The daemon never sees those names.

#### Stages

| Stage | Trigger (defaults above) | Action |
|---|---|---|
| warn | a–e on a unit | the unit's `warn` actions (notify); status line in the log |
| high | unit ≥ `class_max_gb` and avail < `low_avail_pct` | the unit's `high` actions; default policy: notify again with `nth` |
| critical | avail < 10 % or PSI full avg10 > 20 | freeze non-protected units largest-first until projected avail ≥ 30 % (frozen units' `memory.current` is subtracted from "used" — freezing does not free memory, it stops the growth and the thrash); open the dialog; `logger -p user.crit`; GNOME notification if no dialog |
| recovered | avail ≥ 30 % for one full tick while units are frozen | start the countdown (30 s); on expiry `cgroup.freeze = 0` largest-last; a new critical cancels the countdown |

Freeze = `echo 1 > <cgroup>/cgroup.freeze`; trees = `SIGSTOP` all pids. The daemon keeps
the frozen set in `/run/memguard/frozen.json` and re-applies it on restart, so a daemon or
dialog crash never silently thaws a hog. Freezes and thaws are logged with unit, class,
bytes and reason, and emitted on the socket.

Kill layers below the daemon, unchanged from `mem-guard.sh`:

- **systemd-oomd**: `/etc/systemd/system/-.slice.d/90-memguard.conf` `ManagedOOMSwap=kill`;
  `/etc/systemd/oomd.conf.d/90-memguard.conf` `SwapUsedLimit=80%`,
  `DefaultMemoryPressureDurationSec=10s`. `ManagedOOMMemoryPressure=kill` is set per slice by
  whoever owns the slice (rig's `omp.slice` at 40 %); the policy may list slices under
  `[oomd] pressure_kill = ["…"]` and the install step writes the drop-ins.
- **docker-limited.slice**: `MemoryHigh=6G`, `MemoryMax=8G`, `MemorySwapMax=1G`;
  `/etc/docker/daemon.json` `cgroup-parent` merged in (other keys kept); docker is never
  restarted by the installer (it would stop containers) — `status` reports the pending restart.
- **earlyoom** (`/etc/default/earlyoom`): `-m 4 -s 10 -r 60 -n`, `--prefer`
  `(^|/)(pytest|python3?|node|bun|cc1plus|chrome|chromium|WebKitWebProcess)$`, `--avoid`
  the protected comm list. It acts on MemAvailable alone, i.e. in the thrash case where
  pressure-based oomd stalls.

### Dialog

`memguard-dialog`: user systemd service (`~/.config/systemd/user/memguard-dialog.service`,
`WantedBy=graphical-session.target`), python3 + `python3-gi`, GTK4 + libadwaita so it
matches GNOME. It talks to the daemon over the unix socket `/run/memguard/sock` (mode
`0660`, group `memguard`; the install step creates the group and adds the installing user).
Protocol: newline-delimited JSON; daemon → client events `state` (full snapshot: stage,
machine, units with class, display, bytes, frozen flag, countdown remaining), `frozen`,
`thawed`, `countdown`; client → daemon requests `status`, `resume <unit>`, `resume-all`,
`kill <unit>`, `cancel-countdown`, `test`.

The window (like macOS "Your system has run out of application memory"):

```
  Your system has run out of memory
  Memory was recovered by pausing the largest apps. Quit what you don't need, or
  resume them once memory is free.

  [icon] omp session: CRYPT      — ~/xp/sme-archiving-gc      9.1 GB   [Quit] [Resume]
  [icon] Docker: fake-gcs         — container 3f2a…            2.4 GB   [Quit] [Resume]
  [icon] Chrome                   — 37 windows                 5.8 GB   [Quit] [Resume]

  Free memory  [████████░░░░░░░░░░]  7.9 GB of 48 GB          auto-resume in 27 s
                                                     [Cancel countdown]  [Resume all]
```

- Rows are the frozen units largest first, name and detail from the class `display`.
  `Quit` = `kill <unit>` (confirmation only for `protect`-less classes whose display says it
  has unsaved state: not in v1 — one click, as macOS). `Resume` thaws that unit even while
  avail < 30 %; the daemon may refreeze it on the next critical tick (logged, row returns).
- The footer bar is live from `state` events; the countdown shows once recovered; `Cancel
  countdown` keeps everything frozen until the user resumes by hand.
- The dialog is `Adw.Window`, modal-less, always on top via the shell's urgency hint,
  reopens on the next `frozen` event, closes itself when the frozen set is empty.
- No dialog running (no session, tty login): the daemon sends a GNOME notification
  (`notify-send -u critical`, desktop env borrowed from a user process as mem-sentinel does)
  with the names and "run `memguard resume`"; frozen units stay frozen until the countdown.
- Frozen scopes remain frozen across dialog restarts (the daemon owns the state, the dialog
  only renders it). Every freeze, thaw, quit and countdown event is logged in
  `/var/log/memguard/memguard.log` and the journal.

### CLI

`memguard` (user, `/usr/local/bin/memguard`, same socket, no root):

| Command | What it does |
|---|---|
| `status` | machine line (avail, swap free, PSI, fall rate) + units ranked by bytes with class and stage |
| `frozen` | frozen units with age and countdown |
| `resume <unit>` / `resume all` | thaw |
| `kill <unit>` | `SIGKILL` the cgroup/tree |
| `test` | asks the daemon to run the dialog path on a synthetic unit (a 64 MB scope the daemon creates, freezes and lists) — no memory pressure needed |

Exit codes: 0 ok, 1 daemon unreachable, 2 unknown unit.

### Capture and forensics (`hang-capture`)

Baseline: today's `diag.sh`, `capture.sh`, `hang-probe.sh`, `after-hang.sh`, the two sysctl
files. Installed by `modules/hang-capture/install.sh` (root, idempotent):

- `/etc/sysctl.d/90-lockup-capture.conf`: `kernel.hardlockup_panic=1`,
  `kernel.softlockup_panic=1`, `kernel.hung_task_panic=0` (a > 120 s D-state is routine
  during USB writeback), `kernel.panic=60`; `/etc/sysctl.d/91-hang-evidence.conf`:
  `kernel.unknown_nmi_panic=1`, `panic_on_io_nmi=1`, `panic_on_unrecovered_nmi=1`,
  `panic_on_rcu_stall=1`, `kernel.sysrq=1`, `vm.dirty_expire_centisecs=200`,
  `vm.dirty_writeback_centisecs=100`.
- kdump: `doctor` requires `kdump-tools` and `kexec_crash_loaded=1`; a missing `crashkernel=`
  is reported, not fixed (bootloader edit stays manual).
- iTCO hardware watchdog: Ubuntu denylists `iTCO_wdt`, so `modules-load.d` is ignored; the
  explicit unit `/etc/systemd/system/hw-watchdog.service` (`modprobe iTCO_wdt`,
  `Before=sysinit.target`) plus `/etc/systemd/system.conf.d/90-hw-watchdog.conf`
  `RuntimeWatchdogSec=120s`, `RebootWatchdogSec=5min`. `status` reports
  `/sys/class/watchdog/watchdog0/bootstatus` (32 = the last reset came from the watchdog).
- journald: `/etc/systemd/journald.conf.d/90-hang.conf` `Storage=persistent`,
  `SyncIntervalSec=1s`, `RateLimitIntervalSec=30s`, `RateLimitBurst=100000`.
- `/etc/sysctl.d/90-dirty-writeback.conf`: `vm.dirty_background_bytes=67108864`,
  `vm.dirty_bytes=536870912` (byte limits scale with the slowest disk, not RAM; 12 GB of
  dirty pages for a USB 2.0 disk stalled the box on 10-07).
- RAS: `rasdaemon`, `nvme-cli`, `smartmontools`, `memtest86+`, `msr-tools`; `igen6_edac` /
  `ie31200_edac` loaded when present.
- Sampler: `/usr/local/sbin/hang-probe` + `hang-probe.service` (`Nice=-5`,
  `Restart=always`), one fdatasync'd line every 3 s into `/var/log/hang-probe/probe.log`
  (load, avail, swapfree, top-3 RSS, temps, fans, watts, SCI/GPE/NMI/MCE deltas, C-state usage,
  PCIe/USB runtime PM, i915 PSR/DC, top-3 CPU, last kernel lines) plus `turbostat.log` and
  `udev.log`; never touches the NVIDIA GPU (waking it from D3cold is a suspected trigger).
  logrotate daily ×7.
- `/usr/local/sbin/after-hang`: boots, watchdog bootstatus, `/var/crash`, pstore, last
  kernel/journal lines of the previous boot, lockup/panic/MCE/Xid grep, last probe samples,
  RAS errors. `ubuntu-tweak status` prints its one-line summary when the previous boot ended
  without a clean shutdown.

### Swap hygiene

- `/etc/sysctl.d/92-memguard.conf`: `vm.swappiness=10`, `vm.vfs_cache_pressure=100`.
- `/usr/local/sbin/swap-drain` + `swap-drain.timer` (`OnBootSec=15min`,
  `OnUnitActiveSec=1h`): when SwapUsed > 4 GB and MemAvailable > SwapUsed + 8 GB,
  `swapoff -a && swapon -a`; otherwise logs a skip. Values are keys under
  `[swap] drain_min_used_gb = 4`, `drain_headroom_gb = 8` in `memguard.toml`.

### Per-machine profile: `machines/msi-pulse-gl66/`

Requirements of this laptop, each a file in the profile and a check in its `status.sh`:

- **Secure Boot = Disabled** (machine requirement). 2026-10-11 ~19:05 the BIOS update
  E1581IMS.308 → .313 succeeded, but "Load Optimized Defaults" re-enabled Secure Boot →
  kernel lockdown → every unsigned out-of-tree module stopped loading: nvidia 595.99.02 DKMS
  (no display, GDM down), `livepatch_hid_battery_offset.ko`, apfs DKMS. `doctor` checks
  `mokutil --sb-state` reports disabled and that every module under
  `/lib/modules/$(uname -r)/updates/**` appears in `lsmod`; `BIOS.md` and `doctor` both warn:
  after any BIOS "load defaults", re-check Secure Boot before rebooting into the desktop.
- **`nvidia-runtime-pm.modprobe.conf`** (`NVreg_DynamicPowerManagement=0x00`) is the HDMI
  fix of 2026-10-09 17:01 (sessions "🖥️ HDMI" / "🖥️ DISPL"): HDMI-A-1 is wired to the RTX
  3060 and the GSP fails `GFW_BOOT` on runtime-D3 exit, so the GPU must never enter D3.
  Needs `update-initramfs -u -k all` after install; `status.sh` checks the initramfs carries
  the file (`lsinitramfs | grep`).
- **Kernel pin to 7.0.0-15** (`kernel-15.sh`) is the current state, not an experiment: apt
  hold on the `-15` image/headers/modules, `GRUB_DEFAULT` set to the `-15` menu id,
  `GRUB_TIMEOUT=0` + `GRUB_TIMEOUT_STYLE=hidden`. `status.sh` checks `uname -r`, the holds
  and `/etc/default/grub`.
- **`BIOS.md`**: .313 installed 2026-10-11, the update procedure (USB FAT32, M-Flash), the
  RAM memtest result and one-stick plan, and the Secure Boot warning above.
- **Inventory, origin unknown**: system files found on disk with no session trail, absorbed
  into `machines/msi-pulse-gl66/inventory/` verbatim; `status.sh` checks presence only (no
  `cmp`, no install): `/etc/modules-load.d/apfs.conf` + DKMS `linux-apfs-rw` 0.3.18,
  `/etc/modules-load.d/msr.conf`, `/etc/sysctl.d/60-inotify.conf`,
  `/etc/sysctl.d/60-multihome-arp.conf`, `/etc/systemd/logind.conf.d/lid-switch.conf`,
  `/etc/systemd/system/ha-compose.service`, `/etc/systemd/system/gnome-shell-leak-guard.service`
  (+ `.timer`, script `/usr/local/sbin/gnome-shell-leak-guard`),
  `/etc/default/grub.d/kdump-tools.cfg`. `90-usb-stability.rules` is *not* on disk: it was
  never applied.

### `apple-hid-battery` — Magic Keyboard / Trackpad battery livepatch

Apple Magic Keyboard Touch ID (`004C:0321`, `hid_apple`) and Magic Trackpad 2 (`004C:0265`,
`hid_magicmouse`) over Bluetooth report 100 % / 0 % battery because
`hidinput_query_battery_capacity()` in `hid-input.c` reads `buf[1]`, while Apple's report
`0x90` is `[id, flags, charge]`. Upstream fix d07644524b65 (7.3) is absent in Ubuntu
7.0.0-15/-38. Source session "🍎 MAGIC" 2026-10-09T23:51Z, transcript
`~/.omp/agent/sessions/-tmp/2026-10-09T23-51-08-062Z_01a12313-e21e-7499-8c19-02a237636fc2.jsonl`;
the C source and Makefile recovered from it are in the repo verbatim.

- Fix: kernel livepatch (klp object `hid`) replacing that one function; it locates the
  battery field in the report and reads `buf[1 + report_offset / 8]`, scaled by
  `battery_min/max`. Nothing else in the kernel changes; no ABI change.
- Build: never locally — `build.sh` runs on a Runpod pod via `runpod-run` (installs
  `linux-headers-<k>` for every installed kernel, `make -C <headers> M=$PWD modules`,
  `strip --strip-debug`); `--local` builds with the host headers for the running kernel.
- Install (`install.sh`, root): `.ko` → `/lib/modules/<k>/updates/livepatch_hid_battery_offset.ko`
  for each kernel, `depmod -a`, `/etc/modules-load.d/livepatch-hid-battery-offset.conf`.
  Rollback: `echo 0 > /sys/kernel/livepatch/livepatch_hid_battery_offset/enabled`, then
  `rmmod`. Unsigned → requires Secure Boot disabled (above).
- `status.sh`: module in `lsmod` and `/sys/kernel/livepatch/*/enabled` = 1 → 0; present but
  disabled → 3.
- Self-remove: on a running kernel ≥ 7.3 (upstream fix present) the module is a no-op —
  `install.sh` removes the `.ko` and the modules-load entry, `status.sh` exits 0.

### Principle: every system change lands here the same day

Any change an agent session makes to this machine's system — a file under `/etc`, a module
in `/lib/modules`, a GRUB or BIOS setting — must be committed to this repo the same day, as
a module or a `machines/` profile entry, together with the session's transcript path.
Sessions working from `/tmp` are why three fixes (HDMI runtime PM, the HID livepatch, the
kernel pin) were lost and had to be recovered from transcripts. `ubuntu-tweak doctor` is
the check: a file on disk with a `# installed by` header and no module is drift.

### Module layout

Modules of kind `system` are root install steps, not GNOME settings. They extend the module
model of `ubuntu-tweak-cli.md` with one source kind and one action:

```toml
# modules/memguard/module.toml
description = "macOS-like memory-pressure guard: warn, freeze, dialog, kill"
[source]
kind = "system"                # install.sh run as root, idempotent, prints what it changed
[system]
install = "install.sh"         # ubuntu-tweak apply --commit runs: sudo install.sh
status = "status.sh"           # exit 0 = installed and active, 3 = drift (used by check())
[deps]
apt = ["earlyoom", "python3-gi", "gir1.2-adw-1", "systemd-oomd"]
```

The `system` action's `check()` runs `status.sh`; `run()` runs `sudo install.sh`. Until the
CLI lands, the top-level `install.sh --system <module>` runs the same script (`install.sh`
today only links extensions; the flag is additive).

| Module | Files → target |
|---|---|
| `modules/memguard/` | `memguard.py` → `/usr/local/sbin/memguard`; `memguard.service` → `/etc/systemd/system/`; `memguard.toml` → `/etc/memguard/memguard.toml`; `policy.d/10-desktop.toml`, `20-docker.toml`, `00-protected.toml` → `/etc/memguard/policy.d/`; `memguard-dialog.py` → `/usr/local/libexec/memguard/dialog`; `memguard-dialog.service` → `/etc/systemd/user/`; `memguard-cli.py` → `/usr/local/bin/memguard`; `docker-name` → `/usr/local/libexec/memguard/`; `earlyoom.default` → `/etc/default/earlyoom`; `oomd/-.slice.conf` → `/etc/systemd/system/-.slice.d/90-memguard.conf`; `oomd/oomd.conf` → `/etc/systemd/oomd.conf.d/90-memguard.conf`; `docker-limited.slice` → `/etc/systemd/system/`; `92-memguard.sysctl.conf` → `/etc/sysctl.d/92-memguard.conf`; `swap-drain{,.service,.timer}` → `/usr/local/sbin/`, `/etc/systemd/system/`; `logrotate` → `/etc/logrotate.d/memguard` |
| `modules/hang-capture/` | `90-lockup-capture.conf`, `90-dirty-writeback.conf`, `91-hang-evidence.conf` → `/etc/sysctl.d/`; `hw-watchdog.service` → `/etc/systemd/system/`; `90-hw-watchdog.conf` → `/etc/systemd/system.conf.d/`; `90-hang.conf` → `/etc/systemd/journald.conf.d/`; `hang-probe.sh` → `/usr/local/sbin/hang-probe`; `hang-probe.service` → `/etc/systemd/system/`; `after-hang.sh` → `/usr/local/sbin/after-hang`; `logrotate` → `/etc/logrotate.d/hang-probe` |
| `modules/apple-hid-battery/` | `livepatch_hid_battery_offset.c`, `Makefile` (source, in repo); `build.sh` → `.ko` per kernel; `install.sh` → `/lib/modules/<k>/updates/livepatch_hid_battery_offset.ko`, `/etc/modules-load.d/livepatch-hid-battery-offset.conf`; `status.sh` |
| `machines/msi-pulse-gl66/` | per-machine profile, applied only when `dmidecode -s system-product-name` matches `profile.toml` `match`: `90-usb-stability.rules` → `/etc/udev/rules.d/` (keep hubs, JMS578 bridge, both xHCI awake; never applied so far); `usb-storage-quirks.modprobe.conf`, `nvidia-runtime-pm.modprobe.conf` → `/etc/modprobe.d/` + `update-initramfs -u -k all`; `kernel-15.sh` (applied, current state: pins 7.0.0-15); `BIOS.md`; `inventory/` (see *Per-machine profile*) |

Every installed file carries a header `# installed by ubuntu-tweaks/<module>`; `status.sh`
compares each with `cmp -s` (drift = modified) and checks units active/enabled.

### Cross-repo split

| Piece | Repo | Form |
|---|---|---|
| memguard daemon, policy format, dialog, CLI, oomd/earlyoom/docker-limited, swap hygiene | ubuntu-tweaks | `modules/memguard/` |
| hang capture, probe, after-hang | ubuntu-tweaks | `modules/hang-capture/` |
| USB/NVIDIA/BIOS/kernel-pin for this laptop | ubuntu-tweaks | `machines/msi-pulse-gl66/` |
| Apple Magic Keyboard / Trackpad battery livepatch | ubuntu-tweaks | `modules/apple-hid-battery/` |
| omp cgroup wrapper: `harness_wrappers` `cgroup:` option — scope `omp-<N>` in `omp.slice`, `MemoryHigh=12G`, `MemoryMax=20G`, `MemorySwapMax=2G`, `REVIEW_MAX_CONCURRENCY=2` env (`~/.config/environment.d/50-omp.conf`), `PI_INSTALL_DIR` redirect, in-scope re-exec guard, throwaway-scope probe | rig | replaces `~/.local/bin/omp` |
| user unit `omp.slice` (`MemoryAccounting=yes`, `ManagedOOMMemoryPressure=kill`, `ManagedOOMMemoryPressureLimit=40%`) | rig | `~/.config/systemd/user/omp.slice` on `rig apply` |
| `~/.config/memguard/policy.d/50-agents.toml` (match `omp.slice/omp-*.scope`, display via `tg-ctl omp-live status`, warn → `omp-steer`, critical → freeze) | rig | dropped on `rig apply` |
| `omp-steer`, `who-ran`, `omp-supervisor` (`watch.sh`), `WATCHDOG.md`, hooks `runpod-offload-gate` and `heavy-op-memory-gate` (thresholds relative to MemTotal, machine-wide heavy-runner count) | agent-tools | CLIs and hooks |

### Testing

- Unit (`python3 -m unittest`, TDD): policy merge (two dirs, later/user wins by `name`),
  class matching (cgroup glob, comm/cmdline regex, uid, protect precedence, first-match),
  heuristics a–f on recorded `meminfo`/`memory.current` sequences, the stage machine
  (warn → critical → freeze set largest-first until projected 30 % → recovered → countdown
  → thaw; critical during countdown cancels it), countdown cancel/resume over the socket
  protocol with a fake daemon, display resolvers with a fake `.desktop` dir.
- Integration (`dbus-run-session`, scratch `HOME`, a test slice `memguard-test.slice`):
  `memguard.toml` with `critical.avail_pct` and `freeze_until_avail_pct` set so that a
  synthetic scope allocating 3 GB (`python3 -c 'bytearray(3<<30); sleep'` under
  `systemd-run --scope --slice memguard-test.slice`) trips critical on a machine with
  normal free memory — never a real OOM. Asserts: the scope's `cgroup.freeze` reads 1, the
  dialog service receives `frozen`, `memguard frozen` lists it, `memguard resume all`
  thaws it, `memguard test` round-trips. `status.sh` exits 0 after `install.sh`, a second
  `install.sh` reports nothing changed.
- Manual checklist: start a real 20 GB allocator in a terminal scope; the dialog appears
  with the terminal's app name and size, `Quit` ends it, free-memory bar recovers,
  countdown runs when another frozen unit remains; `after-hang` after a forced
  `echo c > /proc/sysrq-trigger` finds the vmcore.

### Migration from the hand-rolled files

Once `ubuntu-tweak apply --commit --only memguard,hang-capture` (or
`install.sh --system …`) succeeds on this machine:

- removed from `~/.cache/omp-supervisor/root-fixes/`: `mem-guard.sh`, `mem-sentinel.py`,
  `mem-sentinel.service`, `install-sentinel.sh`, `hang-probe.sh`, `diag.sh`, `capture.sh`,
  `after-hang.sh`, `90-lockup-capture.sysctl.conf`, `90-dirty-writeback.sysctl.conf`,
  `90-usb-stability.rules` (never applied: absent from `/etc/udev/rules.d/`, stays an
  experiment in the profile), `usb-storage-quirks.modprobe.conf`,
  `nvidia-runtime-pm.modprobe.conf`, `kernel-15.sh` (all absorbed above);
- `OPEN-ISSUES.md` moves to `machines/msi-pulse-gl66/OPEN-ISSUES.md`; `apply.sh` and
  `shutdown-debug.sh` stay there until OPEN-ISSUES #4 is closed;
- `~/.local/bin/omp`, `~/.config/systemd/user/omp.slice`,
  `~/.config/environment.d/50-omp.conf` are replaced by rig's wrapper and units;
- on disk: `mem-sentinel.service` disabled and removed, `/usr/local/sbin/mem-sentinel`
  removed, `/etc/sysctl.d/92-mem-guard.conf` → `92-memguard.conf`,
  `/etc/systemd/system/-.slice.d/90-oomd-swap.conf` and `/etc/systemd/oomd.conf.d/90-mem-guard.conf`
  → the `90-memguard.conf` pair, `/var/log/hang-probe/sentinel.log` → `/var/log/memguard/`;
  the `install.sh` of each module performs these renames (idempotent) so nothing is lost on
  a new system and nothing is duplicated on this one.

## Decisions

- **Freeze, not kill, at critical**: freezing stops growth and the thrash instantly and is
  reversible; the user decides what dies, as on macOS. Kill layers stay armed below it for
  the case where the daemon itself cannot run.
- **Cgroups as the unit of accounting**: GNOME, flatpak, snap, docker and rig already put
  every app in its own scope; freezing a scope is atomic. Process trees are a fallback only.
- **TOML policy with `exec` hooks**, not a plugin API: rig needs names and a steer channel,
  both are one command each; the daemon stays stdlib and agent-agnostic.
- **GTK4 + libadwaita dialog as a user service**, not a GNOME Shell extension: it survives
  Shell restarts, works on any Wayland session, and `python3-gi` is already required by the
  CLI spec.
- **State in the daemon, not the dialog**: a frozen hog must stay frozen if the dialog
  crashes; auto-resume protects against a dialog nobody sees.
- **Daemon and dialog in python3**: the baseline `mem-sentinel.py` is python and the CLI
  spec already chose python; stdlib `tomllib`, sockets and `/proc` parsing need nothing else.
- **Per-machine profiles under `machines/`**, matched by DMI product name, so hardware
  workarounds never apply to another box.
- **oomd per-slice pressure kill stays with the slice owner**: this repo cannot know which
  slices exist; the policy's `[oomd]` list is the hook for others.
- **Secure Boot stays Disabled on msi-pulse-gl66**: nvidia DKMS, the HID livepatch and apfs
  DKMS are unsigned out-of-tree modules; MOK-signing every rebuild adds more failure points
  than value. Checked by `doctor`, documented in `BIOS.md`.
- **Every system change lands in this repo the same day**: three fixes (HDMI/runtime PM,
  the HID livepatch, the kernel pin) were made from `/tmp` sessions and had to be recovered
  from transcripts. A change with no module or `machines/` file is a bug.

## Open questions

1. GNOME 50's own `low-memory-monitor` / `xdg-desktop-portal` memory monitor signal: should
   the daemon also publish `org.freedesktop.LowMemoryMonitor` levels so apps that listen
   (Shell, browsers) trim themselves before we freeze?
2. Should `Quit` in the dialog try a graceful close first (`SIGTERM`, then `SIGKILL` after
   5 s) for `desktop-app` units? macOS force-quits; graceful risks the unfrozen app
   allocating again.
3. Does `docker-limited.slice` (8G) need per-compose-project sub-slices, or is one cap
   enough once sessions are capped at 20G each?
4. `apple-hid-battery` on a kernel where `hid_apple`/`hid_magicmouse` are signed in-tree but
   the livepatch is not: is it worth enrolling a MOK for this one module, or wait for 7.3?

### Sentinel false positives observed 2026-10-11 (requirements for memguard daemon)

- 19:5x: "WARNING #2, still growing" was sent with growth +0.0 GB, tree shrinking 9.8 -> 7.5 GB, avail 36.6 GB (79 %). Escalation (`nth`) MUST re-check the triggering reason on the current tick; a repeat notify without a live reason is a bug.
- `fall_per_min` showed 6.8 GB/min while avail moved 36.4 -> 36.6 GB: compute the slope over the 60 s ring, report 0 when |delta avail| < 256 MB.
- Tree accounting by RSS double-counts shared pages of omp + its subagents (7.5 GB for a tree whose top-3 sum to 0.7 GB): use PSS (`/proc/<pid>/smaps_rollup`) for process trees; cgroups already use `memory.current`.
- The 2.4 GB spike was `tasklib.classify_server` (task-cli local model, on-demand, lives ~minutes): policy needs `ignore = { comm = "..." }` entries for known short-lived daemons; rig ships one for classify_server in `50-agents.toml`.

### 2026-10-13 22:04 critical: headless sessions are unreachable by steer

- PSI full avg10 54 %, swap 24 % used, avail falling 5 GB/min with 21 GB still free: 9
  sessions; the heavy ones were `omp -p --auto-approve --no-session` batch runs (a `bun test`
  suite, two `review diff`, `docker run nixos/nix --memory=5g`). The broadcast reached TUI
  sessions only; `who-ran --live --steer` reported "no live omp owner" for every heavy pid —
  headless omp has no TUI bridge, so the steer channel is blind to exactly the sessions that
  run unattended batch work.
- Requirement: a unit whose `notify.exec` exits non-zero (no channel) escalates to `freeze` on
  the same tick already at stage `high`, not only at `critical`; the dialog lists it like any
  other unit. rig: the cgroup wrapper must wrap `omp -p` runs too (same scope and limits), and
  `50-agents.toml` must treat a failed `notify.exec` as "unreachable".
- The sentinel did fire at 40 % free (on 10-11 nothing fired at all): the heuristics work; the
  delivery channel was the gap.

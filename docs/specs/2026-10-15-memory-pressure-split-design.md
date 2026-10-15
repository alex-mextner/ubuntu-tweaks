# Memory-pressure stack: which repo owns what (ADR, 2026-10-15)

Amends [`memguard.md`](memguard.md) (owner-approved 2026-10-11). Status: **accepted**. The owner
delegated the call ("разберись без меня"), and two external model seats plus the author agreed (see *Quorum*).
Implementation plan: [`2026-10-15-memory-pressure-split-plan.md`](2026-10-15-memory-pressure-split-plan.md).

## Understanding

### What the owner said (verbatim)

- 2026-10-15, the placement rule: «разберись без меня. rig/agent-tools это то что про
  разработку и около этого. ubuntu-tweaks это только то что про убунту. если настройка про ос
  но может быть применима пусть и абстрактно и к macos то возможно стоит в rig, короч сделай
  брейншторм»
- 2026-10-15, follow-up relayed by the orchestrating session: keep the original memguard
  architecture if it is the better one, i.e. an agent-agnostic pressure daemon with `policy.d`
  exec hooks; the agent side ships only the policy and the `omp-steer`/`tg-ctl` CLIs;
  agent-tools `ops/mem-sentinel` is retired once its #888 fixes are ported into the daemon.
- 2026-10-11, requests 1–3 and the approved decisions are quoted in `memguard.md`
  (*Requests*, *Decisions approved by the owner*).

### Intended outcome

Every component of the memory-pressure stack has exactly one home repo and one installer.
A new machine can then be rebuilt from the repos, and no two installers overwrite the same
binary.

### Constraints

- ubuntu-tweaks is public and agent-agnostic. `memguard.md` § *Scope and non-goals* says:
  "No agent knowledge lives here. Nothing in this repo knows what `omp`, `omp-steer`,
  `tg-ctl` or a 'session' is."
- Root installs are run by the owner (`sudo`). Agents have no sudo.
- `ops/mem-sentinel` from agent-tools PR #888 has been the installed copy since 2026-10-15
  03:12 (`cmp` with the repo file: identical, unit identical). It stays installed until
  memguard replaces it, so there is no gap in protection.
- Work in progress in other sessions' branches (`memguard-spec`, `memguard-swap-drain`) is
  changed only by its owners. This ADR is a separate branch and PR.

### Success criteria

- The table below gives every component one owner. `memguard.md` § *Cross-repo split*
  matches it.
- Until memguard ships, `/usr/local/sbin/mem-sentinel` has exactly one writer, agent-tools
  `ops/mem-sentinel/install.sh`. After that it has none: memguard's `install.sh` removes it,
  per `memguard.md` § *Migration*.
- The tickets agree with the table: agent-tools #892/#889, ubuntu-tweaks HYP-1706, and the
  rig tickets listed under *Changes*.

### Assumptions (mine, not the owner's)

- "Applicable to macOS, even abstractly" means the *same function* would be installed or
  configured on a Mac by this ecosystem. Making Ubuntu behave the way macOS already behaves
  (the "out of application memory" pause and dialog) does not count: on a Mac that
  function is built in, and nothing gets shipped.
- rig `sysmon` (rig-cli#622) is the cross-platform resource monitor that already exists. It
  runs as a LaunchAgent on macOS and a `systemd --user` unit on Linux, and it already
  notifies agents through `tg-ctl omp-live send`. Any "abstractly macOS" part of memory
  monitoring belongs there. It does not belong in a new abstraction.

## The rule, as a decision procedure

1. Is it about development or coding agents (sessions, steering, admission of heavy dev
   commands, agent policy)? → **agent-tools** (content and CLIs) / **rig** (provisioning,
   machine services for the dev environment).
2. Otherwise, is it OS-level, and would this ecosystem ship the same function on a Mac? →
   **rig**.
3. Otherwise, it is Ubuntu/Linux system behavior → **ubuntu-tweaks**.

## Components

| # | Component | What it does | Interface | Depends on | macOS counterpart to ship? | Home |
|---|---|---|---|---|---|---|
| 1 | Pressure sampling core (memguard daemon) | `/proc/meminfo`, PSI `/proc/pressure/memory`, cgroup v2 `memory.current`/`memory.stat` per unit, 2 s tick, root, `mlockall` | internal; publishes `/run/memguard/sock` events | Linux cgroup v2, systemd | No: macOS has no cgroups, and jetsam already does this job in the kernel | **ubuntu-tweaks** `modules/memguard` |
| 2 | Policy engine (`policy.d`, classes, stages warn/high/critical) | maps cgroups to classes, runs `notify`/`display` exec hooks, freezes | TOML in `/etc/memguard/policy.d` + `~/.config/memguard/policy.d`; hook stdin JSON (`memguard.md` § *Policy model*) | #1 | No | **ubuntu-tweaks** |
| 3 | Freeze + dialog + `memguard` CLI | `cgroup.freeze`, GTK4/libadwaita dialog, auto-resume | socket protocol (`memguard.md` § *Dialog*, § *CLI*) | #1, #2, GNOME | No: it imitates a built-in macOS feature | **ubuntu-tweaks** |
| 4 | Swap hygiene: gated `swap-drain`, sysctl | `swapoff <dev>`/`swapon <dev>` when RAM can take the pages back, polkit for the desktop user | `/run/memguard/swap-drain.json` (contract in rig-cli#693); `memguard swap-drain` | Linux swap, systemd, polkit | No: macOS swap is dynamic and has no drain | **ubuntu-tweaks** (HYP-1706) |
| 5 | Kill layers: earlyoom, systemd-oomd drop-ins, `docker-limited.slice` | last resort when the daemon cannot run | files under `/etc` | Ubuntu packages | No | **ubuntu-tweaks** |
| 6 | Hang capture: probe, kdump, watchdog, after-hang | forensics | files under `/etc`, `/usr/local/sbin` | Ubuntu | No | **ubuntu-tweaks** `modules/hang-capture` |
| 7 | Agent attribution + steering for memguard: `50-agents.toml` (class `omp.slice/omp-*.scope`, `ignore` short-lived dev daemons), `omp-steer --memguard`, `tg-ctl omp-live status --memguard` | names a session in the dialog; sends the steer into it; exit ≠ 0 = unreachable → freeze at `high` | memguard hook JSON on stdin; exit code | #2; omp, tg-ctl | — (dev) | **agent-tools** (policy file + `omp-steer`); `tg-ctl` flag in **tg-cli**; dropped by **rig** on `rig apply` |
| 8 | Session containment: omp cgroup wrapper, `omp.slice` | one scope per session with limits | `harness_wrappers` `cgroup:` option | systemd --user | — (dev) | **rig** (rig-cli#692) |
| 9 | Heavy-op admission gate | refuses to start heavy dev commands under pressure; macOS (jetsam/vm_stat) and Linux (meminfo/PSI) | pre-bash hook; reads `swap-drain.json` (rig-cli#693) | — | already cross-platform | **agent-tools** `agent-hooks/heavy-op-memory-gate` |
| 10 | Cross-platform dev resource monitor | load, swap, wired, storms, GPU models; alerts and heal ladder for agent hosts | `rig sysmon`; `tg-ctl alert send` | — | Yes: this is the macOS-applicable part | **rig** `riglib/sysmon_daemon` (exists) |
| 11 | Interim agent-aware sentinel | per-omp-scope footprint (anon+shmem+swap), steers under real pressure (#888) | root unit `mem-sentinel.service` | omp scopes | — | **agent-tools** `ops/mem-sentinel`. **Retired** when memguard with the #888 port is installed |
| 12 | Installers | | | | | system parts: `modules/memguard/install.sh` (sudo, ubuntu-tweaks); dev parts: `rig apply`; interim: `ops/mem-sentinel/install.sh` |

Systemd watchdog (`Type=notify`, `WatchdogSec`): this is a property of the daemon's own unit.
It moves with the daemon into `memguard.service`. The HYP-1706 branch wrote it for its copy
of mem-sentinel, so the same code ports over.

The after-spike swap-drain trigger is machine-level: MemAvailable has stayed above headroom
for N minutes after a spike. It needs no agent knowledge, so it belongs to the memguard
daemon (#1). The swap-drain timer and on-demand runs (#4) work without the daemon. The
agent-aware sentinel (#11) never starts a drain.

## Approaches considered

**A — the owner-approved spec, confirmed (chosen).** An agent-agnostic memguard daemon,
policy engine, dialog, swap hygiene and kill layers live in ubuntu-tweaks. The agent side is
policy content and CLIs in agent-tools, provisioned by rig. `ops/mem-sentinel` is retired
after the #888 fixes are ported into memguard's heuristics. Every piece of #1–#6 is Linux- or
GNOME-only by mechanism (cgroup v2, `cgroup.freeze`, PSI, `swapoff`, polkit, systemd-oomd,
earlyoom, GTK4/libadwaita). `memguard.md` has no macOS side: "macOS-like" there describes the
*behavior* being reproduced on Ubuntu, not a Mac deliverable. Rule step 3 therefore applies.

**B — the pressure daemon in rig, as a cross-platform core with Linux/macOS adapters.**
Rejected. On macOS the adapter would have nothing to do: the kernel already pauses and asks,
and there are no cgroups to freeze. The "core" would be a Linux-only root GNOME daemon living
in a dev-tooling repo, which breaks rule step 1. The macOS-applicable part (watching memory and
nudging agents) already lives in rig `sysmon` (#10).

**C — keep agent-tools `ops/mem-sentinel` as the long-term agent-aware daemon (the 2026-10-14
"single source" comment).** Rejected. On Linux there would be two root daemons sampling the
same cgroups with the same heuristics, and both would steer. Agent knowledge would sit in a
root daemon instead of in a hook. It also contradicts the owner-approved spec decision
"TOML policy with `exec` hooks … the daemon stays stdlib and agent-agnostic".

YAGNI, applied to all three:

- No shared sampling library across repos. Public ubuntu-tweaks must not import from rig or
  agent-tools, and the `/proc` parsing involved is a few dozen lines.
- No macOS memguard.
- No new rig "pressure core".

## One steer channel per machine

rig `sysmon` (#10) also notifies agent hosts about memory findings, and on Linux memguard (#2,
through `50-agents.toml`) does the same. Once memguard is *operational* on a host
(`memguard.service` active and `50-agents.toml` in place), rig makes memguard the single
memory-steer channel for omp scopes there. `sysmon` keeps alerting and its other detectors,
but sends no session memory steer of its own. When memguard stops, sysmon's steer resumes.
rig provisions both pieces, so the switch belongs to rig (rig-cli#694). rig drops
`50-agents.toml` only on a host where memguard runs, and ubuntu-tweaks never depends on it.
On macOS nothing changes: `sysmon` is the only channel.

The hook stdin JSON (`memguard.md` § *Policy model*) is a public interface between public
ubuntu-tweaks and private agent-tools. It carries a `schema` version field. A hook that does
not know the version exits non-zero, which memguard treats as "no channel" for that unit.

## Quorum

Question: the rule (verbatim, above), the component inventory, and approaches A/B/C (brief:
`/tmp/msplit-brief.md`, no tools, no repo access). Two external seats were asked one at a
time. The `review` CLI was skipped because the machine was under memory load and the
orchestrator granted only text-only seats. The third voice is the author of this ADR, who
inspected the repos.

| Seat | Verdict | Confidence | Objections / additions |
|---|---|---|---|
| codex (`codex exec -s read-only`) | A | high | Switch sysmon's steer off only once memguard *and* its agent policy are operational, and back on if that stops. Verify that the #888 behavior is equivalent before uninstalling mem-sentinel. "The placement boundary is the concrete implementation's responsibility, not whether 'memory pressure' exists on both operating systems." |
| claude (`claude -p`, no tools) | A | high | Version the hook JSON as a public API, and add a contract test in agent-tools. rig installs `50-agents.toml` only when memguard is detected. Port the #888 cases as memguard fixtures and prove parity before removing mem-sentinel. The #888 footprint is a generic cgroup measure, so it does not leak agent knowledge. |
| author (repo inspection) | A | high | `memguard.md` has no macOS deliverable. rig `sysmon` already covers the cross-platform monitoring. |

The seats converged, so this is the decision. All additions are folded in: the
operational probe and the schema version above, and parity tests in agent-tools#892 and the
memguard port. Raw answers: `/tmp/msplit-codex.txt`, `/tmp/msplit-claude.txt`. An earlier
quorum on the same stack (2026-10-14/15, before the owner's rule: codex A, claude
"staged C aiming at A", in `/tmp/ms-codex.md` and `/tmp/ms-claude.jsonl`) is consistent with
this result.

## Changes made with this decision

- This branch: `memguard.md` amended (§ *Scope*, § *Architecture* L3, daemon baseline =
  agent-tools 0fe9f4a with the #888 corrections and #889 items, § *Units of accounting*,
  § *Policy model* ownership and hook `schema`, § *Cross-repo split*, § *Migration*,
  § *Decisions*), plus this ADR and the plan.
- rig-cli#694 (new): provision `50-agents.toml` only where memguard runs; sysmon defers its
  session memory steer to memguard there.
- agent-tools#892: re-scoped from "port the swap-drain trigger and watchdog into
  ops/mem-sentinel" to "agent side of memguard + retire ops/mem-sentinel after parity".
- agent-tools#889: its three items carry over to the memguard daemon (comment).
- agent-tools#884: comment 33540 ("agent-tools ops/mem-sentinel is the single source") is
  marked superseded.
- HYP-1706: re-scoped. Swap-drain only, no mem-sentinel copy, timer + on-demand triggers;
  the earlier ownership note is superseded.
- `~/.cache/omp-supervisor/root-fixes/mem-guard.sh` step h (unversioned; removed by the
  memguard module per § *Migration*): installs the memguard module first, then agent-tools
  `ops/mem-sentinel/install.sh` while `/usr/local/sbin/memguard` is absent, so the #888
  sentinel is always the copy that survives.

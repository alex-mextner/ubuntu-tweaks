# Memory-pressure placement: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans to carry this
> out task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the spec, the tickets and the installers agree with the placement decision, so
that each memory-pressure component has one owner and one installer.

**Architecture:** This plan carries out a placement decision. It writes no daemon code: the
memguard daemon and its port of the #888 fixes are built by the spec owner's session. This plan
changes docs, tickets, one unversioned root script, and sends the coordination messages.

**Tech stack:** git and the GitHub PR flow (ubuntu-tweaks), `task` CLI (agent-tools Forgejo
issues, HYP tickets), `fj`/Forgejo API (rig-cli issues), `omp-steer`, `tg`.

**Spec:** [`2026-10-15-memory-pressure-split-design.md`](2026-10-15-memory-pressure-split-design.md)

## Global constraints

- ubuntu-tweaks stays agent-agnostic: no `omp`/`tg-ctl`/session names in its code. Its specs
  may name them only as consumers of the extension point.
- `ops/mem-sentinel` stays installed until memguard with the #888 port is installed.
  Nothing in this plan uninstalls it.
- Never edit another session's uncommitted worktree (`memguard-swap-drain`) or push to its
  branch, or to `memguard-spec`. This plan uses its own branch, `memguard-placement-adr`.
- Every tracker change states *why* and links the design doc.

## Review focus

- A stale "agent-tools is the single source" comment stays unmarked, and an agent follows it
  → mark every one of them superseded (Task 3).
- The HYP-1706 installer still ships `modules/memguard/mem-sentinel` and overwrites the #888
  binary → the HYP-1706 acceptance criteria forbid it (Task 3), and the owning session is
  told (Task 5).
- `mem-guard.sh` step h claims memguard owns mem-sentinel *now* → after the edit, step h
  installs the interim sentinel only while `/usr/local/sbin/memguard` is absent (Task 4).
- On Linux, sysmon and memguard both steer the same session → rig ticket (Task 3).
- The owner's report claims something nobody checked → every link and state in the report is
  read back first (Task 6).

---

### Task 1: Quorum and decision doc

**Files:** Create `docs/specs/2026-10-15-memory-pressure-split-design.md`,
`docs/specs/2026-10-15-memory-pressure-split-plan.md`.

- [ ] Run the seats one at a time, and only after the orchestrator grants the heavy-op token:
  `codex exec` and `claude -p` on `/tmp/msplit-brief.md`, with no tools. The grant covered
  text-only seats only, so the `review` CLI is not run.
- [ ] Fill in *Quorum* with each seat's verdict, confidence and objections. If the seats
  disagree on a component, record it and resolve it in the table, or escalate if it stays
  open.
- [ ] Commit both docs.

### Task 2: Spec amendment PR (ubuntu-tweaks)

**Files:** Modify `docs/specs/memguard.md`: § *Architecture* table, § *Cross-repo split*,
§ *Migration*, § *Decisions*.

- [ ] Architecture L3: memguard is the single memory-steer channel on Linux, through hooks.
- [ ] Cross-repo split: `50-agents.toml` content and `omp-steer` in agent-tools, dropped by
  rig; `ops/mem-sentinel` is interim until the port; rig sysmon is the cross-platform monitor
  and defers session memory steers to memguard.
- [ ] Decisions: add the 2026-10-15 placement decision with a link to the design doc.
- [ ] Push `memguard-placement-adr` and open a PR with base `umbrella-cli`. The branch carries
  the unpushed `memguard-spec` commits plus this ADR. Say so in the PR body.

### Task 3: Tracker updates

- [ ] agent-tools #892: rewrite *What* and the acceptance criteria to "retire
  `ops/mem-sentinel` once memguard ships with the #888 port". Criteria: (1) `omp-steer`
  moves into agent-tools with a `--memguard` stdin-JSON mode and exit code ≠ 0 when no
  channel exists; (2) `50-agents.toml` ships in agent-tools; (3) `ops/mem-sentinel/` is
  deleted and its README points to memguard once memguard is installed on `home`;
  (4) no drain trigger and no `memguard_swap` import in agent-tools.
- [ ] agent-tools #889: comment that its three requirements (legacy-session VmSwap, bounded
  helper output, log fsync off the sampling thread) carry over to the memguard daemon. Link
  the design doc.
- [ ] agent-tools #884: comment that comment 33540 ("single source") is superseded. Link the
  design doc.
- [ ] HYP-1706: comment with the re-scope: drop `modules/memguard/mem-sentinel{,.service}`;
  `install.sh` must not touch `/usr/local/sbin/mem-sentinel`; triggers are timer + on demand;
  the after-spike trigger is the memguard daemon's. Mark the earlier ownership note
  superseded.
- [ ] rig-cli: create a ticket "memguard extension point: provision `50-agents.toml`, make
  memguard the single Linux memory-steer channel (sysmon defers session memory steers)".
  Link rig-cli#692, #693 and #685.

### Task 4: `mem-guard.sh` step h (unversioned, `~/.cache/omp-supervisor/root-fixes/`)

- [ ] Step h installs agent-tools `ops/mem-sentinel/install.sh` while
  `/usr/local/sbin/memguard` is absent, then the memguard module when its `install.sh`
  exists. Fix the comment that claims the module owns the sentinel now.
- [ ] `bash -n` passes on the script.

### Task 5: Coordination

- [ ] `omp-steer 14610` (spec owner): the final placement, the PR link, and the request to
  port the #888 fixes (it offered) plus #889 into the memguard daemon.
- [ ] `omp-steer 433553` (HYP-1706 owner): the re-scope comment link and the exact files to
  drop.

### Task 6: Report

- [ ] One `tg --tag report --format html`, in Russian, replying to tg#21465: the decision, the
  split table, the links, and the stopgap status (the #888 sentinel is already installed;
  re-running `sudo ops/mem-sentinel/install.sh` is a no-op).

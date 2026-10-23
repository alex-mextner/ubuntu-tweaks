# Spec: `docker-daemon` module (daemon.json and the container memory cap)

Ticket #87. Parent spec: `docs/specs/ubuntu-tweak-cli.md` (*Modules*, *Service modules*, *Coverage*). Kind `system`,
root, one Python script `modules/docker-daemon/docker-daemon` on the shared scaffolding
(`ubuntu_tweaks/module_script.py`). Off by default (`enabled = false`): a machine enables it in the profile.

## What it does

| File | Content |
|---|---|
| `/etc/docker/daemon.json` | the existing document **merged** with the declared keys; keys it does not manage stay, in their order |
| `/etc/systemd/system/<slice>.slice` | only with a memory limit: `MemoryAccounting=yes` and the limits; `<slice>` defaults to `docker-limited` |

The module does **not** install docker: `docker.io` / `docker-ce` come from `packages.toml` (and, for docker-ce, the
`apt-sources` module's docker repository). Without `dockerd` on disk `install` ends in `needs manual: docker is not
installed ...` (exit 10) and writes nothing; `check` reports the same fact as `manual` (drift). There is no
`requires = ["apt-sources"]`: an unrelated broken repository must not block the memory cap, and the module reads
the live state (is the nvidia runtime binary there?) instead.

## Options

```toml
[modules.docker-daemon]
enabled = true
nvidia_runtime = "auto"     # "auto" (default) | true | false
slice = "docker-limited"    # optional; the unit is <slice>.slice
memory_high = "24G"         # optional: throttling starts here (systemd MemoryHigh=)
memory_max = "32G"          # optional: hard cap (MemoryMax=)
memory_swap_max = "4G"      # optional (MemorySwapMax=)
[modules.docker-daemon.daemon]   # optional: any other daemon.json keys, merged
log-driver = "journald"
```

- Sizes are `<n>[K|M|G|T]` or `infinity`; `memory_high` may not exceed `memory_max`. An unknown option, a wrong type
  or a bad size is exit 1 naming the option.
- With at least one `memory_*` value the module writes the slice and sets `cgroup-parent` in daemon.json to
  `<slice>.slice`; a `daemon.cgroup-parent` naming another slice is refused (one of them has to go).
- `nvidia_runtime`: `"auto"` adds `runtimes.nvidia = {"path": "nvidia-container-runtime", "args": []}` only when
  `nvidia-container-runtime` exists in the usual bin directories (a machine without the NVIDIA container toolkit
  gets no dead runtime entry; `status` says `nvidia runtime skipped`; installing the toolkit later turns it into
  drift, and the next `apply` adds the entry); `true` requires it (`needs manual: install nvidia-container-toolkit`
  while it is missing); `false` never adds it. The package comes from the NVIDIA apt repository declared in
  `apt-sources` plus `nvidia-container-toolkit` in `packages.toml`.
- Merge rules: a table in the declared keys merges into the existing table (`runtimes.nvidia` does not remove a
  `runtimes.mine`), any other value replaces. Removing an option later **never deletes** keys from daemon.json:
  delete them by hand.

## Values for this PC and the laptop's origin

The laptop (`home`, memory-starved) used `docker-limited.slice` with `MemoryHigh=6G MemoryMax=8G
MemorySwapMax=1G` and daemon.json `{"runtimes": {"nvidia": {"path": "nvidia-container-runtime", "args": []}},
"cgroup-parent": "docker-limited.slice"}`, written by its mem-guard script. On a 60 GB desktop with an RTX 4060 (Ubuntu's
`docker.io` already installed; the owner's profile machine overlay) an 8 GB cap would kill ordinary builds, so the profile declares
`memory_high = "24G"`, `memory_max = "32G"`, `memory_swap_max = "4G"`: containers may use about half the RAM, are
throttled long before the cap, and leave at least 28 GB to the desktop and its heavy-run guard. The nvidia runtime stays
`auto`: this PC has no `nvidia-container-runtime` yet, so nothing is written until the toolkit is installed. The
laptop's numbers remain valid input (`6G / 8G / 1G` are accepted as they are).

## Behaviour of `install`

1. Preconditions (nothing is written when one fails): docker installed; `nvidia_runtime = true` ⇒ the runtime exists.
2. Slice: a differing file is replaced atomically (a foreign file of that name is first copied to
   `/var/backups/ubuntu-tweaks/docker-daemon/<UTC timestamp>/`), then `systemctl daemon-reload`. A slice file that
   already matches but that systemd reports as `NeedDaemonReload=yes` gets the `daemon-reload` too (a previous
   run died between write and reload).
3. daemon.json: parsed (a file that is not a JSON **object** is an error and is never overwritten), merged,
   rendered (4-space JSON), checked with `dockerd --validate --config-file <tmp>` (any exit other than 0, 126 or 127 is a rejection and writes nothing), the
   old file backed up, the new one written atomically. A file whose parsed content already holds the declared keys
   is **not** rewritten, whatever its formatting.
4. `systemctl reload docker` (SIGHUP) **only when daemon.json changed and docker is active**; an inactive docker reads
   the file at its next start. The module **never restarts** docker (a restart stops containers unless
   `live-restore` is on).

**`cgroup-parent` only affects new containers**: running containers stay in the cgroup they were created in. `plan` and
`install` print this whenever the value changes. If a new container does not appear under the slice after the reload
(`systemd-cgls /docker-limited.slice`), restart docker once yourself.

## Commands

| Command (alias) | Contract |
|---|---|
| `check` (`--check`) | Read-only, no root. 0 matches, 3 drift (unmet facts printed), other = crashed. |
| `status` | The same facts, all of them, one line each. |
| `plan` (`--dry-run`) | Read-only, from the same `inspect()` as `check` and `install` (so the preview never promises a write the installer will refuse): a unified diff for the slice and for daemon.json, the backup, `daemon-reload`, whether docker will be reloaded, the new-containers note; `nothing to change` when converged. |
| `install` | Root, as above. Idempotent: a second run prints `nothing to change`, touches no file and calls no `systemctl`. |

Facts: docker installed; `nvidia-container-runtime` present/absent (`auto`); slice file equals the declared text;
systemd has loaded it; daemon.json carries the declared keys.

## Staging and tests

`DOCKER_DAEMON_ROOT=DIR` stages every path below `DIR` (no root needed); `systemctl` (and `dockerd` found below
`DIR/usr/bin`) are doubles on `PATH` / in the staged tree. Tests: `modules/docker-daemon/tests`: merge preserving
foreign keys, idempotence, reload only when active and changed, invalid JSON never overwritten, validation failure
writes nothing, nvidia auto/true/false, slice backup and daemon-reload, docker missing = needs manual, option
validation, read-only `check`/`plan`.

## Coverage

`etc:/etc/docker/daemon.json` is `tracked` (module.toml). The slice's file name is an option, so a profile that sets
`slice` or `memory_*` declares `etc:/etc/systemd/system/<slice>.slice` in its own `coverage.toml` (the owner's machine
overlay does, for `docker-limited.slice`); the scanner does not list `.slice` files today, so nothing is blessed
by mistake.

## Real-root verification

`ubuntu-tweak apply --dry-run --only docker-daemon` shows the diffs; in a terminal `--commit`; `docker info` still
works; `systemctl show docker-limited.slice -p MemoryMax -p MemoryHigh -p MemorySwapMax` shows the limits;
a **new** container (`docker run --rm alpine sleep 30`) appears under `systemd-cgls /docker-limited.slice`; a second
`--commit` reports `0 changed`.

# tailnet-mounts

Design of the `tailnet-mounts` module (ticket [#265](https://github.com/alex-mextner/ubuntu-tweaks/issues/265)); the summary, *Alternatives considered* and *Known limitations* are in `docs/specs/ubuntu-tweak-cli.md`, *Tailnet mounts*.

The owner: *the disks of all my Linux computers
on the Tailscale network are mounted automatically on `home` and on `ubuntu-pc`, with no host or disk list anywhere.* The
module has no per-machine and no per-disk declaration: every machine that enables it (it is on by default) finds its peers
and their disks at run time and skips itself.

```toml
[modules.tailnet-mounts]          # all optional
exclude_hosts = ["clvm"]          # tailnet host names that are never mounted (default none)
identity_file = "/home/ultra/.ssh/id_ed25519"   # absolute; default <desktop user's home>/.ssh/id_ed25519
idle_timeout = 300                # seconds idle before an automount unmounts (10..86400)
roots = ["/mnt", "/media", "/run/media"]   # a peer's mount point is shared only below one of these
home = true                       # also share the peer's $HOME as <host>/home
```

**Pieces.** `modules/tailnet-mounts/tailnet-mounts` (`check`, `status`, `plan`, `install`; a `RootedModule`),
`tailnet_mounts_lib.py` (the engine, standard library plus `ubuntu_tweaks/disks.py` for `unit_name` and `errors.py`),
`tailnet-mounts-sync` (the program of the timer) and `tailnet-mounts-sync.service` / `.timer`. `install` copies the engine,
the sync program and a two-file `ubuntu_tweaks/` stub to `/usr/local/lib/tailnet-mounts` (root-owned: a root timer never runs
code a desktop user can write, and the installed copy does not need the repository), writes
`/etc/tailnet-mounts/config.json` (the resolved options, the desktop user's name, uid and gid), the service and the timer
(`OnBootSec=1min`, `OnUnitActiveSec=5min`; a monotonic timer that is enabled late fires at once, so `Persistent=` has
nothing to do; the service is `Wants=` / `After=tailscaled.service network-online.target`), installs the `sshfs` package
(`apt_get`, as `disks` installs its drivers) and runs one sync in the same process. Needs `/usr/bin/fusermount3` (fuse3) and
the desktop user's ssh key (`identity_file`; a missing key is a `manual` finding).

**Sync** (`Syncer.sync`, one at a time under `flock`):

1. `tailscale status --json`. Not `Running`, unreadable or failing: the sync **fails and changes nothing** (an empty answer
   must never be read as "every peer left"). Peers: `OS == "linux"`, `Online`, not this machine, not in `exclude_hosts`. The
   address is the host name when it is a DNS label, else the first IPv4 address; the directory name is the host name reduced
   to `[a-z0-9._-]`.
2. Per online peer (up to 8 in parallel): `ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 -o
   StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/lib/tailnet-mounts/known_hosts -i <identity_file>
   <user>@<address> 'lsblk -J -o NAME,TYPE,FSTYPE,LABEL,MOUNTPOINTS ...; printf HOME=$HOME'` (a second `lsblk` with the
   old `MOUNTPOINT` column is the fallback for util-linux before 2.37). `<user>` is the desktop user (the same account name on every
   machine, as the owner has it). Shares: every mount point of a filesystem that `lsblk` lists (so a device, never a network
   or FUSE filesystem: no recursion through `/mnt/tailnet` is possible) and that lies **below** `roots`, minus `swap`,
   `squashfs`, `LVM2_member`, `crypto_LUKS` and friends, plus the remote `$HOME` as `home`. `/`, `/boot`, `/var`, `/snap`
   and `/home` itself are never shared. The share name is the base name of the remote mount point reduced to `[a-z0-9._-]`;
   a collision gets `-2`, `-3` by sorted remote path (`home` is reserved for `$HOME`). Remote output is untrusted: a mount
   point must match `^/[A-Za-z0-9._/@+ -]{1,200}$` without `.` / `..` / empty components and not lie below `/mnt/tailnet`;
   a unit value never holds a control character or `%` (the renderer refuses them); names come from the reduced alphabet
   and are escaped with `systemd-escape --path` semantics (`unit_name`). A test feeds a hostile payload.
3. Layout: `/mnt/tailnet` is `0700`, owned by the desktop user (only the owner can traverse it, which is what makes
   `allow_other` safe on a one-user machine), `/mnt/tailnet/<host>/<disk>` plain `root` directories made one component at a
   time with `O_NOFOLLOW` (as `disks._make_mountpoint`; an existing mount point is only `lstat`-ed, never opened, so an
   active automount is not triggered). A link where a directory should be stops the sync.
4. Units in `/etc/systemd/system`, atomically, only when the text differs, header `# managed by ubuntu-tweaks
   tailnet-mounts`. `mnt-tailnet-<host>-<disk>.mount`: `What=<user>@<address>:<remote path>`, `Type=fuse.sshfs`, `Options=
   _netdev,nofail,allow_other,default_permissions,uid=<uid>,gid=<gid>,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,
   ConnectTimeout=8,BatchMode=yes,StrictHostKeyChecking=accept-new,UserKnownHostsFile=...,IdentityFile=...,compression=no,
   cache=yes,auto_cache`, `TimeoutSec=25`, `After=network-online.target tailscaled.service`. `.automount`:
   `TimeoutIdleSec=<idle_timeout>`, `WantedBy=multi-user.target`. After a `daemon-reload` the **automount** units are
   `enable --now`ed (only the ones that are not already enabled and active); the `.mount` is never started by the module, so
   a switched-off peer costs nothing at boot (an automount does not wait for the network or the peer).
   `uid=` / `gid=` (not `idmap=user`) make every file belong to the desktop user: sshfs under systemd runs as root, and
   `idmap=user` would map the remote user to root.
5. Reconcile, from the managed units found on disk (header on the first line and a `Where=` under `/mnt/tailnet/<host>/<disk>`;
   anything else is never touched):
   | host this run | its units |
   |---|---|
   | online, ssh worked | rewritten; one it no longer reports is removed |
   | online and ssh failed, or offline | **kept** (a reboot while a peer is down still boots with its automounts) |
   | in `exclude_hosts`, not Linux, or this machine | removed |
   | no longer in `tailscale status` | kept 30 days from the last day it was seen (`last_seen` in the state file, one counter per host, in days), then removed |
   Removal: `systemctl stop` the automount and the mount, `systemctl disable` the automount, `fusermount3 -uz` when something
   is still mounted, delete the files, `rmdir` the mount point and an empty host directory, `daemon-reload` once.
6. `/var/lib/tailnet-mounts/state.json` (world-readable; no timestamps except `last_seen` days, so an unchanged tailnet
   rewrites nothing): per peer its address, state (`ok`, `error`, `offline`, `absent`), the last error line and its shares.
   `check` and `status` read it, so they never run ssh.

**`check` / `status`.** Facts: package `sshfs`, `fusermount3`, the ssh key, each installed file is the installed copy
(content and mode), the timer enabled and active, the last run of the service succeeded, the state file exists, and per
share in the state: both unit files exist (and, for a peer that answered, equal the generated text), the automount is active,
and every sshfs mount that is already mounted answers `timeout 5 stat` (an idle automount is never touched: `stat` would
mount it). A peer that cannot be reached or refuses the key is a `WARNING` line in `status` (`ubuntu-hyper: Permission denied
(publickey)`), **not drift**: `check` exits 0 for it, because a peer may legitimately be dead or not mine. `install` that cannot
get sshfs without root prints `needs manual:` (exit 10). `plan` (and `apply --dry-run`) runs the discovery over ssh as the
desktop user with `UserKnownHostsFile=/dev/null` (it cannot write the root-owned file) and lists, per peer, every
`<user>@<address>:<path> -> /mnt/tailnet/<host>/<disk>` plus the files, directories and `systemctl` calls that would follow.

**Tests.** `modules/tailnet-mounts/tests/test_tailnet_mounts.py` over the recorded `tailscale status --json` and the real
`lsblk` output of odroidn2 and ubuntu-pc (`tests/fixtures/`), a stateful `FakeHost` (`tests/fake_host.py`) and a scratch root:
the expected shares (`odroidn2: storage wd500 home`, `ubuntu-pc: backups cc32a44a-... home`), a hostile payload, golden unit
text, a second sync and a second install that write nothing, units of an offline host kept, a vanished disk of a reachable host
removed with its mount, foreign units untouched, the 30-day rule, a broken tailscale changing nothing, a link in the mount root
refused, unreachable peers as warnings, a hung mount, and an installed copy that imports without the repository.

# Spec: the `earlyoom` module

Source of truth for `modules/earlyoom/`. Part of the memory-protection stack of a desktop (the kernel OOM killer
is too late, `systemd-oomd` watches cgroups): earlyoom acts on `MemAvailable` and free swap, `memguard` drains swap
back into RAM, and the profile's `mem-sentinel` warns the agent session that is eating the RAM (*Relation to the
other layers*).

## Request

User directive (23 Oct 2026): one declarative solution for all system and app configs of the user's desktop, reproducible by
`ubuntu-tweak init/apply` on a fresh machine, with failover. The laptop `home` ran earlyoom with a hand-written
`/etc/default/earlyoom` (written by a one-off `mem-guard.sh`). This module makes that file, the package and the
service declarative; the user's tuning stays in the profile.

## Module

`kind = "system"`, `root = true`, one Python script per command (*Service modules* in
`ubuntu-tweak-cli.md`): `earlyoom check|status|plan|install`, plus `earlyoom-user` for the failover. The shared code
is `modules/earlyoom/earlyoom_lib.py`.

| Fact (system install) | Checked by |
|---|---|
| the `earlyoom` package is installed | `dpkg-query` |
| `/etc/default/earlyoom` equals the rendered text (byte for byte) | file read (mode 644, readable by the desktop user) |
| `earlyoom.service` is enabled | `systemctl is-enabled` |
| `earlyoom.service` is active | `systemctl is-active` |

`install` (as root, through the engine's `RootSession`): `apt-get install -y -o Dpkg::Options::=--force-confold
earlyoom` when missing (the option keeps *our* file at a later upgrade instead of stopping to ask), write the file
atomically, `systemctl enable`, and `systemctl restart` **only when the file changed** (`start` when the service was
down; nothing when everything already matches, so a second run reports `nothing to change`). The Ubuntu package
already starts the service with its stock file; the first install therefore changes the file and restarts once.

*Rollback.* After a start or restart the module looks again after one second. earlyoom exits at once on a flag it
rejects (a bad `extra_args` word), and a last line of defence must never end up stopped by the module that configures
it: the previous file (or none) is put back, the failed state cleared (`reset-failed`: a crash-looping unit hits
systemd's start limit and refuses a plain restart), the service restarted, and the install fails with the reason. The rootless
copy below follows the same rule for its options file *and* its unit file (one shared routine).

`plan` (the manifest `preview`) prints the steps and the unified diff of the defaults file; the engine shows it under
the action in `apply --dry-run`.

## Options (`[modules.earlyoom]`)

| Option | Type | Default | Meaning |
|---|---|---|---|
| `mem_percent` | number, `0 < x <= 100` | `10` | `-m`: SIGTERM when `MemAvailable` is at or below this share of *user memory* (earlyoom's `user mem total` = `MemAvailable` + `AnonPages`, so a filling tmpfs shrinks it); SIGKILL at half of it, once swap is below its own half too |
| `swap_percent` | number, `0 < x <= 100` | `10` | `-s`: the same for free swap. earlyoom acts only when **both** thresholds are crossed |
| `report_interval` | integer seconds `>= 0` | `3600` | `-r`: one memory line per interval in the journal (`0` = none). The Ubuntu package default |
| `prefer` | POSIX ERE, `""` = off | none | `--prefer`: process names to stop first |
| `avoid` | POSIX ERE, `""` = off | `DEFAULT_AVOID` (systemd, dbus, gnome-shell, gnome-session, Xwayland, Xorg, gdm3, pipewire, wireplumber, sshd) | `--avoid`: process names to spare |
| `notify` | bool | `false` | `-n` (D-Bus notification). Needs `systembus-notify` running in the user session; the Ubuntu archive has no such package, so the default is off |
| `extra_args` | list of words | `[]` | appended verbatim, one word per element (for example `["--sort-by-rss"]`) |

Anything else is refused (exit 1, `[modules.earlyoom] unknown option(s): ...`), and so is a wrong type or range.
Why `prefer` and `avoid` are regular expressions of *process names*: earlyoom compiles them with libc `regcomp`
(`REG_EXTENDED | REG_NOSUB`) and matches `/proc/PID/comm`, at most 15 characters, not the command line. `tmux: server`
is therefore a name, `gnome-session-binary` is `gnome-session-b`, and `(^|/)name$` is the conventional anchored form.
A match is a score shift (`prefer` adds 300, `avoid` subtracts 300 on the kernel's 0..1000 `oom_score` scale; `--ignore`, available through `extra_args`, removes a process from the candidates altogether), so a small preferred process
can outrank a much larger one: list only processes that are safe to lose.

### Rendering and injection

The file is

```
# Managed by ubuntu-tweaks (modules/earlyoom, ...): edit the profile, not /etc/default/earlyoom.
# Read by earlyoom.service as its EnvironmentFile; `earlyoom -h` lists the flags.
EARLYOOM_ARGS="-m 4 -s 10 -r 60 --prefer '...' --avoid '...' -n"
```

with the flags in this fixed order: `-m`, `-s`, `-r`, `--prefer`, `--avoid`, `-n`, then `extra_args`. The Ubuntu unit
runs `ExecStart=/usr/bin/earlyoom $EARLYOOM_ARGS`: systemd splits the value at whitespace and honours the single quotes.
The module therefore refuses (before anything is written) a regex or word that contains a quote of either kind, a
backslash (systemd would unescape it inside the double quotes), a backtick, a `$` anywhere but at the end of the
expression or before `|` or `)` (the stock file is also sourced by `sh`, which expands `$name`, `${`, `$(`, `$$`, `$?`),
or any character outside printable ASCII (newline, tab, control). `(^|/)(a|b)$` is fine. `extra_args` words may not contain a space and may not repeat a flag the typed options own, in any spelling earlyoom's `getopt_long` accepts: `-m`, `-M`, `-s`, `-S`, `-r`, `-n`
(also with an attached value, `-m5`, or grouped, `-gm5`) and `--prefer` / `--avoid` (also `--prefer=x` and any
abbreviation such as `--pref`).
Regexes are compiled with the same `regcomp` call before they are written; a regex libc rejects is a configuration
error (earlyoom itself dies at start with `fatal: could not compile regexp`). Test: laptop options render the
laptop's line byte for byte, and with a real `earlyoom` on `PATH` its verdict is compared with libc's.

## Failover

Primary: root install above. When root is unavailable (no sudo, a locked-down session) or `apt-get` fails for good,
the engine runs `[[fallback]] earlyoom-user install` (`root = false`): `apt-get download earlyoom` needs no root, the
deb is extracted under `~/.local/opt/earlyoom`, the options are rendered into `~/.config/earlyoom/earlyoom.env`
(plus `--ignore-root-user`: a user-level earlyoom cannot signal root's processes; a failed kill makes earlyoom sleep for a second and try again, so it must never pick one), and
`~/.config/systemd/user/earlyoom.service` runs it. It stops only the user's own processes, which is the part that
matters on a desktop; it does not raise its priority or lock memory with system capabilities. The user unit carries
`ConditionPathExists=!/usr/bin/earlyoom`, which is only evaluated when the unit starts, so the root install also
**retires** the copy whenever its unit file exists (`systemctl --user --machine <user>@.host disable --now
earlyoom.service`, best effort and without a prior query: root's own `systemctl --user` would reach root's manager) and `check` reports `no rootless copy of earlyoom runs beside the service` as drift until it is gone: two
daemons would both act on one low-memory event. Once the system package is installed the system service wins; for the same reason `earlyoom-user install` refuses (without downloading
anything) while `/usr/bin/earlyoom` exists: with the package present the root install is the one to repair. `check` and
`status` judge the system install; when the machine has no package but has the rootless copy, they judge the copy, so
a converged fallback is not asked for root. `plan` always describes the primary (root) install: once an option
changes, the next `apply --commit` tries root first again and falls back to updating the copy only when root is
unavailable.
Without a network both installs fail the same way (`could not resolve` is classified as transient by the engine and
retried with backoff); the module has no offline alternative.

## Staging for tests

`EARLYOOM_ROOT=DIR` stages the install: only `DIR/etc/default/earlyoom` is read and written; no package, no systemd,
no root. Unit tests (`modules/earlyoom/tests`) run against a fake machine; `Cli` tests run the scripts with a staged
root and the JSON options in `UBUNTU_TWEAK_MODULE_CONFIG`.

## Coverage

`etc:/etc/default/earlyoom` and the rootless copy's files are `tracked` by the module (`[[coverage]]`).

## Relation to the other layers, and what changes per machine

`mem-sentinel` (agent-tools, installed by a profile module) acts first: 25 % available steers a session, 10 % is a
broadcast, "about a minute above earlyoom's 4 %". The defaults here are written for any desktop; a machine tunes them
in its overlay and explains why next to the values. Reference point, the laptop: 44 GB RAM, 16 GB swap, `-m 4 -s 10`
= about 1.8 GB available and 1.6 GB swap left before the first SIGTERM. The PC (60 GB RAM, 32 GB swap file) keeps `-m 4
-s 10`: 4 % of its user memory (62 GB `MemTotal`, 55 GB `user mem total` measured with earlyoom 1.9 run as a user) is 2.2 GB
(SIGKILL at 1.1 GB), which is enough headroom for the kernel and the shell to keep running while earlyoom, polling up
to ten times a second, picks a victim; a higher percentage would only move the kill
earlier while 40 GB are still reclaimable page cache. Its values are in the profile, not here.

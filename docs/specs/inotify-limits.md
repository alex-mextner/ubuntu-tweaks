# Spec: `inotify-limits` module (fs.inotify.max_user_instances)

Ticket #124. Parent spec: `docs/specs/ubuntu-tweak-cli.md` (*Service modules*). Kind `system`, root, one Python script
`modules/inotify-limits/inotify-limits` on `ubuntu_tweaks/managed_files.py` (the shared "own a few files, then run a
command" base). On by default: every fresh Ubuntu install has the problem.

## Why

Ubuntu allows 128 inotify instances per user. Several dev servers, file watchers and test runs of one user exhaust it
and the next `inotify_init` fails with `EMFILE` (hyper-saas `gh-ship` tests). The kernel value is raised to 2048.

## Facts and install

| Fact | Checked by |
|---|---|
| `/etc/sysctl.d/60-inotify.conf` equals the rendered text (a managed header, then `fs.inotify.max_user_instances=2048`) | file read |
| the running kernel reports that value | `/proc/sys/fs/inotify/max_user_instances` |

`install` (as root) writes the file (mode 644, atomically) and runs `sysctl --system` when the file changed **or** the
kernel does not carry the value (a file that was edited back, or a boot that did not read it). It then reads the proc
value again and fails with a message when a later file in `/etc/sysctl.d` or `/usr/lib/sysctl.d` overrides it. A file
that exists and was not written by the module is copied to `/var/backups/ubuntu-tweaks/inotify-limits/<UTC stamp>/`
before it is replaced (the machine's hand-made one-liner is such a file: its content is the same, the header is new).
A second `apply` reports nothing to change and runs no `sysctl`. `plan` prints the diff and `sysctl --system`.

Option `[modules.inotify-limits] max_user_instances` (integer 1 to 2147483647, default 2048); anything else is refused
before a write.

## Staging and tests

`INOTIFY_LIMITS_ROOT=DIR` stages the drop-in and a stand-in `proc/sys/fs/inotify/max_user_instances` below `DIR`; no
root, no `sysctl`. `modules/inotify-limits/tests` run the staged scripts, and the real converge step against a fake
`sysctl` (runs once after a write, again for a wrong kernel value, never when clean, reports an overriding file).

## Coverage

`etc:/etc/sysctl.d/60-inotify.conf` is `tracked`. memguard's inventory of this file on the msi machine
(`docs/specs/memguard.md`, presence only) stays valid: the content is the same line.

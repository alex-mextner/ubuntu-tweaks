# Spec: ssh-server (openssh-server installed, enabled, running and listening)

Moved out of `docs/specs/ubuntu-tweak-cli.md` (ticket #244: the main spec had reached the 3100-line ceiling of the commit-time lint gate). The module model
(`modules/<name>/module.toml`, the `system` source kind, actions, `apply --commit`) is defined there.

Facts, in the order `status` prints them:

1. the `openssh-server` package is installed (`dpkg-query`);
2. only when the profile sets `password_auth` (below): `/etc/ssh/sshd_config.d/00-ubuntu-tweaks.conf`
   has the expected text, `sshd_config` includes `sshd_config.d/*.conf`, and sshd's **effective**
   value (the first one it reads, `Include` processed inline) is the wanted one: a
   `PasswordAuthentication` above the `Include` line would win over the drop-in, which is a `manual` fact;
3. only when sshd listens on tailscale addresses (a `ListenAddress` inside `100.64.0.0/10` or
   `fd7a:115c:a1e0::/48`) and a `tailscaled.service` unit exists:
   `/etc/systemd/system/ssh.service.d/10-ubuntu-tweaks-tailscale.conf` has the expected text;
4. `ssh.socket` is neither enabled nor active: with socket activation (the Ubuntu 24.04+ default)
   systemd, not sshd, holds the listening socket and ignores `sshd_config`'s `Port`/`ListenAddress`, so
   the module runs sshd as a plain service (`systemctl disable --now ssh.socket`, the procedure Ubuntu
   documents for a non-default port);
5. `ssh.service` is enabled;
6. ... and active;
7. systemd has loaded the current unit files (`systemctl show -p NeedDaemonReload` is `no`): a
   `daemon-reload` that failed after a drop-in was written is drift again, not forgotten;
8. sshd accepts connections on every `(address, port)` its configuration names: the `Port` lines
   (default 22) and the `ListenAddress` lines, read from `/etc/ssh/sshd_config` and its `Include`
   chain (a `Match` block ends a file's global part), compared with `ss -H -ltn`; a wildcard
   socket covers a named address of its own family (`*` covers both, `0.0.0.0` only IPv4, `::` only IPv6: sshd sets `IPV6_V6ONLY`), a host
   *name* is matched by port only (`ss -n` shows numbers).

The checker reads the same files sshd does. A file that exists but the desktop user cannot read (a
`0600` `50-cloud-init.conf`) makes `check` fail with exit 1 and the file's name rather than guess a
different configuration than the root install sees; make it readable (mode 644).

**The module does not change `Port`, `AllowUsers`, `ListenAddress` or any other sshd setting that is
already there**: a machine's own `sshd_config.d` is its business (this machine's
`99-moshi-tailscale.conf` keeps working as it is). The stock Ubuntu `sshd_config` is already a sane
default (port 22, every address, `Include` of the drop-in directory), so with no configuration the
module writes none; it installs the package and enables and starts the service.

*The boot failure it fixes.* With `ListenAddress 100.74.26.95` and Port 2223 in a drop-in, sshd
starts before `tailscaled` has created the address, exits 255 (`Cannot assign requested address`) and
`ssh.service` stays failed, because the unit says `RestartPreventExitStatus=255`. The fix is a
systemd drop-in, not a change to sshd:

```ini
# /etc/systemd/system/ssh.service.d/10-ubuntu-tweaks-tailscale.conf
[Unit]
After=tailscaled.service
Wants=tailscaled.service
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=
```

`After=`/`Wants=` fix the order; the retry covers the short gap between `tailscaled` being up and the
address being assigned (at most 10 starts in 2 minutes, so a real configuration error does not
loop). *Decision*: not `net.ipv4.ip_nonlocal_bind` / `net.ipv6.ip_nonlocal_bind`: they would be
system-wide for every process, while the problem is one unit's ordering. The drop-in has its own
file name (`10-ubuntu-tweaks-tailscale.conf`, not `override.conf`) so the user's own overrides are
never replaced.

*Password policy, only when asked.* `[modules.ssh-server] password_auth = false` writes
`/etc/ssh/sshd_config.d/00-ubuntu-tweaks.conf` with `PasswordAuthentication no` and
`KbdInteractiveAuthentication no` (both: PAM keyboard-interactive would otherwise still take
a password); `true` writes `PasswordAuthentication yes`. `00-` is read first and sshd keeps the first
value it sees, so the profile wins over other drop-ins. Never locking the user out: `false` is refused
(exit 10, `needs manual`) while the desktop user has no usable public key in `~/.ssh/authorized_keys`
(a line with a key type and a base64 body, not a `command=` / `restrict` / `from=` key; `AuthorizedKeysFile` and `AllowUsers` are not consulted; the guard applies only when the file would be written, so repairs of an already converged policy still run), while
the installer cannot tell which user to protect (a root shell without `sudo`), while `sshd_config` does not
`Include` the drop-in directory (also when the file is already in place), or while a setting above the
`Include` line overrides the drop-in (the file just written is removed again). Without the option an
existing file is left as it is (disabling a module never uninstalls).

*Install order and validation.* As root: `apt-get install -y openssh-server` when missing; write the
password-policy drop-in (rolled back when `sshd -t` rejects it); `sshd -t` **before** anything is
restarted or the unit drop-in is written, so a rejected run leaves no file behind and the next run still
reloads systemd; the unit drop-in, then `systemctl daemon-reload` when systemd says it is needed;
`disable --now ssh.socket` when socket activation is on; `enable ssh.service`; `reset-failed` + `start`
when it is not active; otherwise `restart` when the socket was just switched off (the running sshd
was started by the socket and holds systemd's listener, so it must re-exec to bind `sshd_config`'s
addresses) or `reload` when only sshd's own config changed. A `reload` that fails removes the policy file
it just wrote, so `check` never reports a policy that sshd did not take. The unit drop-in needs no
restart of a healthy sshd; open sessions survive either way (`KillMode=process`).
`ChallengeResponseAuthentication` is read as its alias `KbdInteractiveAuthentication` when looking for a
shadowing line.

*memguard's polkit rule.* `docs/specs/memguard.md` makes the polkit rule `50-memguard-swap-drain.rules`
refuse `systemctl start ssh.service` for the **desktop user** (it grants only `swap-drain.service`; the
rest falls through to the default policy). That is unchanged and not in the way: the module's mutating
commands run as **root** through `sudo` (or `pkexec`), and polkit's `manage-units` check does not
apply to uid 0; `check`/`status` use only read-only `systemctl is-enabled` / `is-active`, which need no
authorization. The module never asks polkit for anything, and memguard's own guard needs no
exception. (`sshd` is on memguard's protected list, so it is never frozen or killed either.)

Real-root verification (what fake-root tests cannot prove; run once on a real machine, as root where
marked):

- In a terminal (the root step asks for a password): `ubuntu-tweak apply --only ssh-server` (preview
  lists the drop-in and the service start), then `--commit`.
- `systemctl status ssh.service` is `active (running)`; `systemctl is-enabled ssh.service` is `enabled`.
- `ss -ltn` shows sshd on every address and port in `sshd_config`.
- `ubuntu-tweak status --only ssh-server` exits 0; a second `apply --commit` reports `0 changed`.
- Reboot: `ssh.service` is `active` after boot (`journalctl -b -u ssh.service` has no `Cannot assign
  requested address`), and `ssh -p 2223 <tailscale-ip>` works from another tailnet host.
- With `password_auth = false` and a key in `~/.ssh/authorized_keys`: a password login is refused, the
  key login works; with no key the run stops with `needs manual` and the file does not exist.
- `systemctl is-enabled ssh.socket` is `disabled` and `systemctl is-active ssh.socket` is `inactive`
  (sshd runs as a service, so `Port`/`ListenAddress` apply); an unreadable drop-in makes `status` fail with
  its name.
- As the desktop user, `systemctl start --no-ask-password ssh.service` is still refused
  (memguard's rule is intact).

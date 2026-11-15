# Spec: the `usr-local` coverage adapter

Source of truth for `ubuntu_tweaks/adapters/usr_local.py`; the *Coverage* section of `ubuntu-tweak-cli.md` (items,
adapter interface, classes) is the frame, this file holds the detail of one adapter (that file is at the line limit of
`rig.yaml`, so adapter detail lives here).

Ticket #235. AGENTS.md says every file under `/usr/local` and `/opt` is a patch, yet no adapter looked there: the stray
xdg-desktop-portal conf that kept wireplumber from starting for ten days (see `wireplumber-portal-conf.md`) was invisible
to `ubuntu-tweak coverage`. The adapter `usr-local` makes such hand installs visible.

## Items

Ids use the `etc:` scheme with the absolute path as it is on the machine, so a module's `[[coverage]]` rule reads like
the path it claims: `etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf`, `etc:/opt/vendor`.
The section of `ubuntu-tweak coverage` is named `usr-local`. Nothing is written and no file is opened.

| What | Item | Where |
|---|---|---|
| Config areas | one per file or link | `etc/**`; `share/*/*.d/*` (drop-in directories of programs: `wireplumber.conf.d`, `pipewire.conf.d`, `dbus-1/system.d`, `polkit-1/rules.d`); `share/dbus-1/{services,system-services}`; `share/xdg-desktop-portal/portals`; `share/applications`; `share/glib-2.0/schemas`; `share/ca-certificates/**`; `lib/systemd/*/*`; `lib/udev/rules.d`; `lib/<triplet>/{wireplumber-0.5,pipewire-0.3}` (plugins of the session processes: the incident's `libxdg-desktop-portal.so`); `lib/<triplet>/mozilla/native-messaging-hosts` |
| Trees | one per top-level entry (`detail`: the number of files; an empty directory is not an item) | `/opt/*`; the entries of `/usr/local/share` and `/usr/local/lib` that no config area listed from, minus data trees (`interfaces`, `locale`, `man`, `doc`, `info`, `icons`, `fonts`, `themes`, `sounds`, `help`, `licenses`, `mime`, `metainfo`, `sgml`, `xml`, `zsh`, `bash-completion`, `pkgconfig`, `aclocal`, `cmake`, `gir-1.0`, `vala`, `gtk-doc`) and library trees (`*-linux-gnu`, `python3*`, `pkgconfig`, `cmake`, `node_modules`) and library files straight in `lib` (`lib*.so`, `lib*.so.*`, `lib*.a`, `lib*.la`: what `./configure && make install` leaves) |
| Not listed | | `bin`, `sbin`, `libexec`, `include`, `src`, `games`, `lib/<triplet>/lib*`: executables, headers and libraries are software, not configuration (the package adapters and the modules that install them own that), and listing every header of a hand-built library would bury the findings |

A tree an area already listed from is not one item itself: the area's files are items, and so is whatever else sits next to
them (`share/wireplumber/wireplumber.conf` and `scripts/` beside `wireplumber.conf.d/10-x.conf`, `gnome-portals.conf` beside
`portals/x.portal`, the `foo.service.d` directory and `*.wants` links beside a listed unit); only the data trees above stay
out. A link is an item of its own (`detail`: `hand-installed link -> <target>`) and is never followed: not when it is the
file, and not when it is a directory the areas would otherwise walk (`share/foo -> /srv/foo`: the link is the item, the
files behind it are not). A directory of an area this user cannot list (`/usr/local/etc/sudoers.d` at 0750) is one item
whose `detail` is `needs root to read: sudo ls -lR <dir>`, like a tree; so is an `/opt` this user cannot list. A link at
any step of an area (`/usr/local/etc -> /srv/config`, `/usr/local/share`, `/opt`) is the item and nothing behind it is
listed; a loop (`lib/vendor -> vendor`) is a link like any other. A tree whose only content is links to directories is
not empty: each link counts as a file. `/usr/local` itself as a link is the one item. A file name that is not UTF-8 is
listed like any other but never passed to `dpkg-query` (its output would not decode).

## What a package owns is not hand-installed

`dpkg-query -S -- <candidate paths, sorted>` runs before any directory is walked, in as many runs of at most 100 000
bytes of arguments as the candidates need (a longer command line fails with `E2BIG`); the paths it reports are
dropped (it exits 1 when some are not found, the found ones are still on stdout). A path with a glob character is never
passed (dpkg would read it as a pattern) and so is listed. Without `dpkg-query` everything found is listed.

A directory this user cannot list (`/opt/containerd` is `drwx--x--x`) is one item whose `detail` is
`needs root to read: sudo ls -lR <dir>`, as the `etc` adapter reports `/etc/sudoers.d`; a package's own directory is asked
about first, so it never gets that item.

## Classification

The adapter has no opinion (`classify`): an item is `tracked` when a module or the profile claims it, `excluded` when a
rule says so, else `untracked`. Claims added with the adapter, each for a real reason:

- `wireplumber-portal-conf`: the portal conf and its `.disabled` copies, and the two `libxdg-desktop-portal.so` (rules of
  its `module.toml` that waited for this adapter).
- `memguard`: `etc:/usr/local/lib/memguard` (`memguard_swap.py`, installed and checked by its `install.sh`).
- `disks`: `etc:/usr/local/share/ubuntu-tweaks` (the `apfs-fuse.commit` stamp the module writes).
- engine default (`coverage_defaults.toml`): `gschemas.compiled` of `/usr/local/share/glib-2.0/schemas` is `excluded`,
  generated by `glib-compile-schemas` from the `.gschema.xml` files next to it, which stay findings.

Everything else a hand build left (the xdg-desktop-portal and credentialsd units, D-Bus services, portals, native-messaging
host, `/opt/containerd`, ...) stays `untracked` until the owner declares it (a module, or a `[[coverage]]` rule with a
`ticket`): that is the purpose.

## Tests

`tests/test_coverage_adapters.py`, `UsrLocalTests` on a scratch root with a fake `dpkg-query`: the listed, claimed and
excluded entries (config files, trees, data / libraries / binaries never listed, a package's paths dropped, one query
sorted, no dpkg, no trees, a directory this user cannot list, a package directory this user cannot list), the scan with
the real `module.toml` of `wireplumber-portal-conf`, `memguard` and `disks`, the profile rule that excludes an entry, the
engine rule for the schema cache, and that discovery changes nothing.

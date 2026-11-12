# Spec: the `wireplumber-portal-conf` module

Source of truth for `modules/wireplumber-portal-conf/`; the module list in `ubuntu-tweak-cli.md` (*Modules*) points here.

Ticket #234. `modules/wireplumber-portal-conf/` (kind `system`, root, on by default, no options, order 57). Incident: a
hand-built xdg-desktop-portal under `/usr/local` (not dpkg-owned) put
`/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf` where wireplumber reads it (its profile
requires `policy.xdg-desktop-portal`, which needs the module component `libxdg-desktop-portal`), but the module itself in
`/usr/local/lib/<arch>/wireplumber-0.5/`, where wireplumber never looks (`type = module` components are loaded from
`/usr/lib/<arch>/wireplumber-0.5` only). `wireplumber.service` then exits 78 (CONFIG) on every start, PipeWire keeps only
the Dummy Output and a connected headset has no device.

- **`check`** (read-only, no root) reads the fragments wireplumber loads: the `*.conf` files of `/etc/xdg`, `/etc`,
  `/usr/local/share` and `/usr/share` (each + `/wireplumber/wireplumber.conf.d`, in that priority order: a file name in
  a higher directory masks the same name below it, even an empty one) and collects the elements of their top-level
  `wireplumber.components` arrays: `name = X, type = module` needs the file `X.so` in `/usr/lib/<arch>/wireplumber-0.5`
  (the host's own dpkg triplet only) and `type = pw-module` (the spelling of wireplumber's own `wireplumber.conf`)
  needs `X.so` in `/usr/lib/<arch>/pipewire-0.3`; an absolute `name` is a path of its own. Rule matches
  (`wireplumber.components.rules`) and profiles are not components. Each conf with a module that is not there is drift
  (exit 3) with the conf and module named; a conf that requires no module is ignored. The module does not resolve which
  features a profile requires: a profile that disables the feature (`feature = disabled`) or marks it `optional` (then a
  missing module is only a warning, not exit 78) is not evaluated, so such a conf still reports. `.disabled` files are
  not `*.conf` and are never read. User confs under `~/.config/wireplumber` are out of scope (a root script cannot find
  them reliably).
- **`install`** (root) retires, by renaming to `<name>.conf.disabled` (wireplumber reads only `*.conf`; rename it back to
  undo), exactly the files of `RETIRED_CONFS` (`10-xdg-desktop-portal.conf`) in `/usr/local/share/wireplumber/wireplumber.conf.d`,
  and only while a module of theirs is missing: a conf whose module exists (a proper install) is never touched. A
  different `.disabled` file already there is kept and the new one gets `.disabled.<UTC timestamp>` (then `.<n>`): a
  retired file is never overwritten; a copy with the same bytes already there makes the conf the duplicate. The name is
  only retired in the `/usr/local` directory: the same file name in `/usr/share`, `/etc/xdg` or `/etc` is not ours.
  Any other broken conf is the owner's: `install` still retires what it may, then ends in
  `needs manual` (exit 10) naming the file. The module `.so` under `/usr/local/lib` stays. Nothing is symlinked into
  dpkg-owned `/usr/lib`. A second `apply` is `0 changed`.
- **Restore** (the other direction; a retirement must not outlive the reason for it). A *retired copy* is a regular file
  (never a link) named `<name>.disabled` or `<name>.disabled.<suffix>` for a name of `RETIRED_CONFS` in the `/usr/local`
  directory. When every module such a copy requires exists where wireplumber looks (a package installed
  `libxdg-desktop-portal.so` into `/usr/lib/<arch>/wireplumber-0.5`, say), `check` reports the live name as drift (exit 3)
  and `install` renames the copy back to `<name>.conf` (`restored ... -> ...`), unless `/usr/share/wireplumber/wireplumber.conf.d`
  has a fragment of that name (the hand-built copy would shadow the package: no drift). A copy with one module still missing
  stays retired, so retire and restore never alternate. The live name is never overwritten: the copy is hard-linked to
  it (fails when the name exists) and then unlinked, so bytes are never lost. The same bytes already at the live name
  are the restore done: no drift, and the copy stays (a later retire finds it and treats the conf as the duplicate,
  as above). Any other file or link at the live name, or several restorable copies with different bytes, is
  `needs manual` (drift with the `manual` mark for `check`, exit 10 for `install`) naming the files; the owner keeps the
  one they want as `<name>.conf` and removes the rest. A live conf retired in the same run frees its name, so an older
  restorable copy goes back in that run (`plan` lists both). The retire, restore and `needs manual` outcomes are tested
  in `modules/wireplumber-portal-conf/tests/` (retire, module appears, restore, second run changes nothing).
- **`[[post]] wireplumber-start`** (user level, no root): after the installer, `systemctl --user reset-failed wireplumber.service`
  then `start`, then (3 s later) `is-active`: a unit that does not stay up (another config error) fails the post with
  the journal hint instead of passing as done. Its check is unsatisfied only for the incident's state: the unit's last
  main process exited with status 78 (`ExecMainStatus`, kept by systemd while the unit is inactive or failed), the unit is
  enabled and it is not running. A plain `stop`, a masked or disabled unit, a machine without the unit and a shell
  without a user bus all count as satisfied, so nothing the owner did not want is started. The engine runs a post only
  while the module's own `check` is satisfied: a broken conf the module cannot retire (`needs manual`) holds the restart
  back until the owner fixes it.
- **Coverage.** The module claims `etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf*` (the one
  file it retires and restores, and its `.disabled` copies) and, as exact ids with no glob,
  `etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so` and
  `etc:/usr/local/lib/x86_64-linux-gnu/pipewire-0.3/libxdg-desktop-portal.so` (the hand-built module the module leaves in
  place: inert there, since wireplumber loads modules from `/usr/lib/<arch>` only) as `tracked`, each with a reason; other
  `/usr/local` wireplumber or library files are not its. No coverage adapter scans `/usr/local` yet (ticket #235), so
  these ids are never produced today and the rules are declarative: they keep the files tracked the day one does. The
  rules are unit-tested against the ids they must (and must not) match.

`WIREPLUMBER_PORTAL_CONF_ROOT=DIR` stages every path below `DIR` and `WIREPLUMBER_PORTAL_CONF_MULTIARCH` names its dpkg
triplet (tests: `modules/wireplumber-portal-conf/tests/`; sudo's `env_reset` keeps both out of a real install).

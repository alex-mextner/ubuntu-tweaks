# Spec: the `firefox-snap-absent` module

Source of truth for `modules/firefox-snap-absent/`. Expresses "the Firefox snap is not installed" (ticket #179), the
state a one-off `remove-firefox-snap.sh` created by hand. **Opt-in**: `enabled = false` by default; a machine that runs
the Mozilla deb opts in with `[modules.firefox-snap-absent] enabled = true`.

## Contract

`kind = "system"`, `root = true` (checker protocol: `firefox-snap-absent check|status|plan|install`, exit 0 / 3).

| Fact | Read with |
|---|---|
| the `firefox` snap is not installed | `snap list firefox` (run with `LC_ALL=C`) says `no matching snaps installed` (or there is no `snap`); any other failure (snapd unreachable, timeout) is an error with exit 1, not "absent" |
| `~/.local/share/applications/firefox_firefox.desktop` is absent | file test, in the desktop user's home (`SUDO_USER`) |

Install: when `snap get system snapshots.automatic.retention` is `no` (snapd would then keep no snapshot) `snap save firefox` first, then `snap remove firefox` **without `--purge`**, so snapd keeps its automatic snapshot of the profile (`snap saved`
lists it, `snap restore <set-id>` brings it back; the plan line says so), then removes the stale launcher. Without root
(`[[fallback]]`, `root = false`) only the launcher goes and the run ends `needs manual` (exit 10) naming
`ubuntu-tweak apply --only firefox-snap-absent --commit (as your normal user: it asks for sudo itself, so your profile is found)` (a bare `snap remove` keeps no snapshot when `snapshots.automatic.retention` is `no`).

## What it never touches

- The Mozilla deb Firefox: `/usr/bin/firefox`, `/usr/lib/firefox` and its launcher are never read or written; the only
  commands run are `snap list firefox`, `snap get system snapshots.automatic.retention`, `snap save firefox` (only when that retention is `no`) and `snap remove firefox`.
- `~/.config/mimeapps.list`: the default browser (the deb's launcher) is preserved; the module never edits it.
- The dock favourite for Firefox belongs to the profile's dock settings, not to this module.

## Relation to `firefox-passkey-qr`

`firefox-passkey-qr` (also opt-in) runs the author's installer, which replaces the snap by Mozilla's deb inside its own
flow; the engine module never asserts "snap absent" as a fact. `firefox-snap-absent` is standalone and can be enabled
with or without it; `firefox-policy` reads the deb's `/etc/firefox/policies` natively, so it needs no snap plug once
the snap is gone.

# Spec: the `stray-gsettings-keyfile` module

Source of truth for `modules/stray-gsettings-keyfile/` (ticket #180). **Opt-in**: `enabled = false` by default; the
profile enables it per machine.

## Why

`~/.config/glib-2.0/settings/keyfile` is the store of GLib's *GKeyfile* settings backend. The desktop session uses the
dconf backend and never reads it; the only writers are processes that run with the keyfile backend, such as Homebrew's
`gsettings` (it has no dconf backend), which is what scripts and agents get when they call plain `gsettings`. A key set
that way never reaches the real settings, yet the file looks like configuration. The real tools are `/usr/bin/gsettings`
and `/usr/bin/dconf`. The engine reads effective values through Gio in its own process (dconf backend), so it does not
see this file either.

## Contract

`kind = "system"`, `root = false`: `stray-gsettings-keyfile check|status|plan|install` (exit 0 / 3).

| Fact | Read with |
|---|---|
| `<XDG_CONFIG_HOME or ~/.config>/glib-2.0/settings/keyfile` does not exist | file test (a dangling symlink counts as present) |

Install: move the file aside in its own directory (an atomic rename, so a writer that recreates it meanwhile makes a new file that the next run handles, never one that is deleted unseen), copy the moved file to `<XDG_STATE_HOME or ~/.local/state>/ubuntu-tweaks/backups/glib-keyfile-<UTC>.keyfile` with
mode 0600 (created exclusively: a second backup in the same second gets `-1`, `-2`, none is overwritten; a failed copy puts the file back), delete the moved file, `rmdir` the `settings`
directory when it is empty (a directory with other files is kept). A second run finds nothing.

Not safe on by default: a machine that *deliberately* runs `GSETTINGS_BACKEND=keyfile` would lose its store (the backup
keeps it), so the module is opt-in.

## Coverage

The keyfile path and the backups are `tracked` by this module's manifest. `capture` still lists the keyfile among the
files it can adopt (`ubuntu_tweaks/capture/defaults.toml`); adopt it only when the keyfile backend is intentional.

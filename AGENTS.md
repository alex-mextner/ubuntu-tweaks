# Agent guide

Repository instructions for coding agents (Claude Code, Codex, omp, ...). This is the canonical file;
`CLAUDE.md` is a symlink to it, so every agent reads the same guide.

## System and home changes go through the ubuntu-tweak CLI (reconciliation)

The owner's rule: «все должно быть через Ubuntu-tweaks cli! По реконцилляции. Просто добавляй туда все новые
патчи.» Nothing on this machine is configured by hand. A change is a declarative patch in this repository that
`ubuntu-tweak apply` reconciles, so it can be previewed, repeated, reviewed, and reproduced on a fresh machine.

**What counts** (all of it is a patch, never a one-off command):

- installs: apt, snap, flatpak, brew, tools fetched from a URL;
- launchers, icons and `.desktop` files, thumbnailers, MIME types and default applications, favorites;
- autostart entries, systemd `--user` units and their drop-ins, timers, linger;
- GNOME Shell extensions (install, enable, disable, retire);
- dconf / gsettings keys;
- any file or link under `~` (`~/.config`, `~/.local`, shell rc) or under `/etc`, `/usr/local`, `/opt`;
- system units, polkit rules, udev rules, fstab entries.

**Flow:**

1. Read `ubuntu-tweak --help` and the section of `README.md` called "`ubuntu-tweak` CLI". The spec is
   `docs/specs/ubuntu-tweak-cli.md` (*Modules*, *Profile layout*, *Plan and actions*); read it before adding a kind of
   change the CLI does not yet have.
2. Add or extend a module in `modules/<name>/` (`module.toml` plus its files; the CLI never names a module in code).
   Pick the source kind that fits: `builtin` (no extension code: settings for GNOME's own schemas, apt packages),
   `local` / `submodule` (an extension), `system` (an installer plus a read-only `check`; root by default,
   `root = false` for a user-level one). Per-person settings, `units.toml`, `packages.toml` and `files/`
   belong to the profile (the `ubuntu/` directory of the dotfiles repository, `--profile DIR`), not here.
3. Make it idempotent and declarative: it states the end state, a second `apply` reports `0 changed`, and a
   `check` / drift test sees a change made behind its back. Never "run this once".
4. Tests with the change: `modules/<name>/tests/` (unittest, run by `scripts/ci.sh`) for module scripts, `tests/`
   for the engine; the fake-machine helpers are `tests/util.py` and the `*_scenario.py` files. TDD: failing test
   first. Update `docs/specs/ubuntu-tweak-cli.md` (and `README.md` when a command or module list changes) in the same
   commit.
5. Preview with the CLI, never apply to the live machine on your own: `ubuntu-tweak apply --dry-run` (or
   `--only <module>`); it changes nothing and asks for no sudo. Paste its output as proof.
6. Steps that need root (`[sudo]` actions) cannot run in an agent session (`no_new_privileges`; the CLI stops with exit
   6 and prints the command). Leave them in the plan for the owner: `ubuntu-tweak apply --commit` or
   `ubuntu-tweak finish`. Say in the PR which steps are theirs.
7. After the owner applies: `ubuntu-tweak status` shows no drift (exit 0) and a second `apply` says `0 changed`.
   `ubuntu-tweak coverage` must not list the new location as untracked.

**Exempt:** scratch work in a temp dir (`mktemp`, `/tmp`), git worktrees, and nested or throwaway test shells
(`dbus-run-session` with a scratch `HOME`/`DCONF_PROFILE`, as `scripts/integration.sh` does). Reading the live
system (`status`, `list`, `coverage`, `capture` preview, `show`) is always fine. Tests never touch the real HOME,
dconf or `/etc`.

**If you already changed something by hand:** stop, do not pile more on top. Say so in your report. Then
`ubuntu-tweak capture` (preview) and `ubuntu-tweak status` show what differs from the declaration; encode the change as
a module or profile patch as above (or revert it), confirm with `--dry-run` that the plan now matches the machine,
and have the owner run the privileged part. A hand fix is not done until the engine reproduces it.

**Reading settings:** agent shells resolve `gsettings` to Homebrew's build (keyfile backend, writes
`~/.config/glib-2.0/settings/keyfile`, NOT the real dconf). For the user's real settings use `/usr/bin/gsettings` and
`/usr/bin/dconf`, and change them only through ubuntu-tweak (profile bundle), never by hand.

## Working here

- Ticket first (`task new`), own worktree (`rig worktree create <name>`), small conventional commits, PR, `gh ship <PR>`.
- `gnome-extensions/hyperdock` is a submodule with its own repository; change it there, not through this one.
- Checks: `scripts/ci.sh` (shellcheck, ruff, every unittest suite, JS syntax, JSON).

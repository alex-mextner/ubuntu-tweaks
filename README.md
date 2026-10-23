# ubuntu-tweaks

My personal Ubuntu (GNOME, Wayland) tweaks.

## Install

One command on a stock Ubuntu: the script installs `git curl ca-certificates python3-gi` with `sudo`, clones this
repo with its submodules to `~/ubuntu-tweaks` (override with `UBUNTU_TWEAKS_DIR`) and runs `ubuntu-tweak init`.

The CLI keeps its links (extensions, `~/.local/bin/ubuntu-tweak`) in one stable checkout: `$UBUNTU_TWEAKS_DIR`, else the main
working tree of the repo it runs from, so running from a temporary git worktree never leaves dangling links (spec: *Checkout*).

First, your settings profile lives in a repo of your own GitHub account:

```sh
gh auth login                          # once; the dotfiles repo is found through gh
gh repo create dotfiles --private --clone   # or name it .files; put your profile in ubuntu/profile.toml
```

Then run the bootstrap:

```sh
curl -fsSL https://raw.githubusercontent.com/alex-mextner/ubuntu-tweaks/main/scripts/bootstrap.sh | sh -s -- --yes
```

Without `--yes` it only previews (`init` without `--commit`); `--yes` is the explicit consent that turns it into
`init --commit`. No `curl` yet: `wget -qO- <same url> | sh -s -- --yes` or `sudo apt-get install -y curl`
first. `init` ends with a **Manual steps** block (gh token, AMO key, iCloud sign-in, ...): what only you can
finish, each with how, all in the spec's *Manual steps* table. The same by hand:

```sh
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/ubuntu-tweaks
~/ubuntu-tweaks/bin/ubuntu-tweak init           # preview: doctor, submodules, CLI link, plan
~/ubuntu-tweaks/bin/ubuntu-tweak init --commit  # do it
```

The dotfiles repo is found through `gh` in your account under the names `.files`, `dotfiles`, `.dotfiles`,
`dot-files`. Pick another with `--dotfiles-repo OWNER/NAME` or `UBUNTU_TWEAK_DOTFILES_REPO`, re-resolve with
`--refresh`, or pin it in the profile with `[modules.dotfiles] repo`, `dir`, `path`. The profile is
`<your dotfiles repo>/ubuntu`; it may carry its own modules in `<profile>/modules/<name>/module.toml`.


Then log out and back in (the Shell on Wayland loads extension code only at login). Repo
extensions, including the dock submodule, are symlinked into
`~/.local/share/gnome-shell/extensions`, so the repo stays the source of truth. Re-running is safe.
`./install.sh` still works: it is `bin/ubuntu-tweak init --commit`.

Upgrading a clone from before the dock rename (submodule path
`gnome-extensions/dash2dock-lite@icedman.github.com` → `gnome-extensions/hyperdock`):
`git pull && ubuntu-tweak apply --commit`. `apply` initializes the submodule at its new path and
replaces the old extension link. Git leaves the old submodule directory behind; remove it once
nothing unpushed is left in it. `status` reports the old
`dash2dock-lite@icedman.github.com` extension as an *Extra* until the hyperdock migration has
moved its settings.

## `ubuntu-tweak` CLI

`bin/ubuntu-tweak` is the umbrella CLI described in
[`docs/specs/ubuntu-tweak-cli.md`](docs/specs/ubuntu-tweak-cli.md): the modules of this repo plus a
private settings profile (default `<your dotfiles repo>/ubuntu`, found through `gh`; override with `--profile DIR` or
`$UBUNTU_TWEAK_PROFILE`; per-machine overlay named by `/etc/hostname` or `--machine`). Every
mutating command previews first and changes nothing without `--commit`.

**Root.** Actions marked `[sudo]` need root. With `--commit` (and no `--no-sudo`) the tool asks for your sudo
password **once**, before the first root action, and keeps the sudo ticket alive for the whole run; a preview never
asks. Where it cannot ask (a sandbox with `no_new_privs`, no terminal, a wrong password) it stops before the first
root action with exit **6**, says why and prints the command to run from a normal terminal.

**One command repairs everything.** `ubuntu-tweak apply --commit` (and `init --commit` on a fresh machine) asks for
sudo once, installs the apt packages that are missing (what `doctor --yes` does), applies the whole plan and ends with
`N changed` and what only you can finish, each with the reason. `--commit` is the consent: nothing else is asked.
There is no separate `fix` command. A second run says `0 changed`.

**`--dry-run` everywhere.** Every command accepts `--dry-run`: it previews exactly like the run without `--commit`,
changes nothing, never asks for sudo and says `dry run: nothing was changed`; together with `--commit` it is a usage
error (exit 2).

| Command | What it does |
|---|---|
| `init [--commit]` | fresh system: `doctor`, submodules, CLI link in `~/.local/bin`, then `apply`; preview without `--commit` |
| `apply [--commit] [--no-sudo]` | print the plan (what would change), or repair the machine: sudo once, missing apt packages, then the plan; a second run says `0 changed` |
| `status` | Missing / Modified / Extra, then a one-line coverage summary; exit 3 when anything declared is missing or modified |
| `coverage [--json] [--class untracked\|tracked\|excluded\|secret] [--propose]` | read-only scan of dconf, `~/.config` and friends, packages, systemd user units, GNOME extensions and `/etc`: every item is `tracked`, `excluded` (with a reason), `secret` (reported, never read) or `untracked`; exit 3 while an untracked item without a ticket remains (`[[coverage]] class = "untracked"` with a `ticket` and a `reason` keeps a finding visible but owned: `N untracked (M ticketed)`); `--propose` prints what `capture` would add. Rules: engine defaults < module `[[coverage]]` < profile `coverage.toml` < `machines/<host>/coverage.toml` |
| `coverage --check-profile [--online] [--json]` | the profile CI's static gate: no machine scan, no PyGObject, writes nothing. Every coverage rule parses (an `untracked` rule needs a ticket, `excluded` / `secret` a reason), no pattern can never match, none is duplicated or redundant; exit 2 when invalid; `--online` also checks through `gh issue view` that each ticket exists and is open |
| `update [--commit]` | `git pull --ff-only`, update submodules, then `apply` |
| `doctor [--yes]` | check `python3-gi`, `dconf-cli`, `git`, `glib-compile-schemas` and module deps; `--yes` installs them (asks for sudo once) |
| `finish [--step ID] [--yes]` | one list of the human-only steps still left (sudo password, iCloud sign-in, signing keys, ...), each with why, the exact action and an automatic check; the machine part of every step runs by itself. `--dry-run` only lists them. Modules declare their own steps with `[[finish]]` in `module.toml` (spec: *Finish*); the profile can install it as `finish-setup` |
| `list`, `machine` | read-only: modules (kind, enabled, source); machine overlay and where each setting comes from |
| `capture [--commit] [--dry-run] [--summary] [--report FILE] [--only …] [-v]` | read-only inventory of the live system, printed as the exact diff the profile would get (SUMMARY block first; `--dry-run` is the explicit no-write mode, `--summary` prints only the summary); `--commit` writes it into the profile working tree (never runs git) |
| `bundles [--show NAME]` | read-only: the settings bundles, where each applies, shadowed / duplicated / conflicting keys |
| `bundle-move <bundle> <key-or-glob>... [--commit]` | group keys (with their comments) into one bundle; preview by default |
| `note <bundle>:<key> "reason" [--commit]` | add, change or remove the reason of a setting (also `packages:apt:<name>`, `system:<key>`) |
| `show [--all] [--panel ID] [--format text\|md\|html] [--ssh [USER@]HOST]` | read-only Russian overview of all settings (explanations, readable values, defaults, status) grouped by Settings panel; `--ssh` shows another machine without installing anything there |

Modules live in `modules/<name>/module.toml`: `hyperdock` (submodule), `english-in-overview`
(local extension); `memguard` and `apple-hid-battery` are root-level `system` modules. The `init` pipeline is a
module per step, in this `order`: `base-tools` 10, `zsh` 11, `clipboard` 20, `git-gh` 30, `dotfiles` 35, `shell-rc` 36,
`brew` 40, `ssh-server` 50,
`passwords-app` 60 / `passwords-app-polkit` 61, `firefox` 70 / `firefox-policy` 71 / `firefox-autologin` 72 /
`firefox-passkey-qr` 73 (off by default), `hyperdock` 80, `quicklook` 85 (Space previews a file in Files), `gsconnect` 90 and `allowlockedremotedesktop` 91 (both opt-in), then your settings and `files/`, then the opt-in `rig`
100 (+ tmux, omp, ctmux) (spec: *Init pipeline*; switch one off with
`[modules.<name>] enabled = false` in `machine.toml`).

**Your dotfiles repository comes first.** `dotfiles` clones it right after `git` and `gh`, and the plan links its
shell rc files (`.zprofile`, `.zshrc`, `.profile` at the repository root) into `$HOME`; a real file already there is
backed up first, and a name your profile's `files/` links wins. `shell-rc` only creates a minimal `~/.zprofile` /
`~/.zshrc` for what neither has. The git identity is taken from `[git]` in the profile, else what `git config --global`
already has, else the repository's `.gitconfig`, else `gh api user`; only when nothing knows it, the plan warns
(not an error). Settings that wait for extension code the same run installs are listed as *staged*, not skipped.

`passwords-app`, `passwords-app-polkit`, `firefox`, `firefox-policy`, `firefox-autologin`, `firefox-passkey-qr`
and `rig` integrate tools published by the project author (passwords-app, firefox-passkey-qr, rig, ctmux). Each
fetches its tool from one `[source] repo` URL in its `module.toml`, which a profile can override with
`[modules.<name>] repo = "..."`; anyone who does not want one of them can switch it off with
`[modules.<name>] enabled = false`.

Tests: `scripts/ci.sh`; the end-to-end check on a scratch dconf and HOME is `scripts/integration.sh`
(module preset, two settings bundles with reasons, machine overlay, linked files, `capture` preview; it never
touches your dconf).

### Session log

Every run writes everything it printed (stdout, stderr marked with `! `, the questions it asked and your answers,
colors removed, secrets masked) to `~/.local/state/ubuntu-tweaks/logs/<UTC timestamp>-<command>.log`
(`$XDG_STATE_HOME` honoured; mode `0600`). **`~/.local/state/ubuntu-tweaks/logs/latest.log`** is the newest one: point an
agent at it ("read latest.log and tell me what went wrong"). The newest 50 are kept. Not logged: the sudo password
prompt and anything you type into a child tool's own prompt. Details: spec, *Session log*.

### Reconcile and failover

`apply --commit` / `init --commit` observe every declared item, act, observe again, and **never stop at the first
failure**: a transient error (network, dpkg lock, HTTP 429/5xx, installer exit 75) is retried with exponential
backoff and jitter; a permanent one is reported and its dependents (`requires = [...]` in `module.toml`, or the later
steps of the same extension) are marked *blocked* while everything independent still runs; a system module may declare
`[[fallback]]` installers (user-level variants run when sudo is unavailable); when root cannot be had, all user-level
work is finished and the root remainder is listed with the exact command. Exit codes: `0` converged, `7` something
failed or was blocked, `6` root actions remain because root was unavailable, `3` plan not converged. Every run
writes `~/.local/state/ubuntu-tweaks/run-journal.json` (an interrupted run is noticed and simply continues; `status`
lists the last run's failed/blocked/deferred items). Opt-in unattended self-heal: `[modules.autoheal] enabled = true`
(user timer running `apply --commit --no-sudo` at login and daily; never asks for root). Spec: *Reconcile and failover*.

### Settings workflow: preview → commit → review → PR

What you configure in GNOME Settings (and the config files you keep under `~/.config`) lives in the
private profile, `<your dotfiles repo>/ubuntu` (`settings/**/*.ini` bundles, `files/`, `packages.toml`,
`system.toml`, `units.toml` for the systemd user units that must be enabled, and `machines/<hostname>/` for what
differs per machine). The engine here holds no
personal data.

```sh
ubuntu-tweak capture                  # 1. preview: exactly what the profile would receive (+ new, ~ differs, = same)
ubuntu-tweak capture --commit         # 2. write it into <your dotfiles repo>/ubuntu (no git commit, secrets refuse the write)
git -C ~/dotfiles diff                 # 3. second look, then commit and open a PR in your dotfiles repo
ubuntu-tweak apply                    # 4. on any machine: preview what would change
ubuntu-tweak apply --commit           # 5. do it; `status` reports drift, a second run says `0 changed`
```

`capture --commit` replaces each adopted file by a symlink into the profile (the original is copied to
`~/.local/state/ubuntu-tweaks/backup/<UTC timestamp>/` first); `apply` links the same files on another
machine. Keys that only make sense for one machine (power, touchpad, monitors) are captured into
that machine's overlay.

#### Bundles and reasons

Any `settings/**/*.ini` file in the profile is a *bundle*: a keyfile named by purpose
(`dev-keyboard`, `laptop/power`) that may hold keys from any dconf path, merged with the others at
load (the idea is Home Assistant's `packages:`). `machines/<hostname>/settings/**/*.ini` are bundles
too and win key by key. The same key with different values in two bundles of one layer is an error
(exit 2, both files and lines named); identical values only warn.

```ini
[ubuntu-tweak]                         # optional header, not a dconf section
description = "Keys for the dev keyboard"
machines = ["laptop"]                  # apply only on these hostnames (also: requires-module = "hyperdock")

[org/gnome/desktop/wm/keybindings]
# Super+arrows belong to the tiler, so workspaces move on Alt   <- the reason: comments above a key
switch-to-workspace-left=['<Super><Alt>Left']
```

`apply` prints the reason next to each key, `status` shows it for modified keys, and `capture --commit`
rewrites only changed values, keeping every comment and the file's order. Capture puts a new key where
the profile already holds it, else into the bundle that `[capture.bundles]` in `profile.toml` routes it
to, else into the panel's own file:

```toml
# profile.toml
[capture.bundles]                      # first matching entry wins
"dev-keyboard" = ["org/gnome/desktop/wm/keybindings/**", "org/gnome/shell/extensions/tiling-assistant/**"]
"laptop/power" = ["org/gnome/settings-daemon/plugins/power/**"]
```

`packages.toml` keeps reasons too (`"tmux",  # shell tools`; `note packages:apt:tmux "reason"`) and takes
two optional keys: `flatpak_remote = "flathub"` (the remote flatpaks install from) and
`apt_update = true` (run `apt-get update` before the apt batch).

```sh
ubuntu-tweak bundles                   # list bundles, what applies here, shadowed / duplicated / conflicting keys
ubuntu-tweak bundles --show dev-keyboard   # the merged result of one bundle
ubuntu-tweak bundle-move dev-keyboard 'org/gnome/desktop/wm/keybindings/**'   # group keys (preview; --commit writes)
ubuntu-tweak note dev-keyboard:org/gnome/desktop/wm/keybindings/close "why"   # add a reason (also packages:apt:<name>)
ubuntu-tweak capture --dry-run         # the explicit no-write preview, SUMMARY block first (--summary: only that)
ubuntu-tweak show --format html          # Russian overview of every setting by Settings panel -> /tmp/ubuntu-tweak-show-<machine>.html
```

Only values you changed are captured (never schema defaults), and keys the system or an app registered
by itself (notification app ids, Ptyxis profile uuids, portal shortcuts, input-source MRU order, ...) are
listed in their own `auto` section instead; `[capture] include = ["org/gnome/..."]` promotes any of them.

## Tweaks

| Tweak | Path | What it does |
|---|---|---|
| English in Overview | `gnome-extensions/english-in-overview@ultra` | Switches the keyboard layout to `us` whenever the launcher (Super / Activities) opens. GNOME Shell 50. |
| macOS dock (`hyperdock`) | `gnome-extensions/hyperdock` (submodule), `modules/hyperdock/` | macOS 26-style dock replacing Ubuntu Dock: Liquid Glass with adaptive contrast, glass Trash showing its contents, stacks, badges, genie minimize, drag and drop. Spec: [`docs/specs/mac-dock.md`](docs/specs/mac-dock.md). Presets in `modules/hyperdock/dock.ini` (enforced by `apply`); the Screenshots stack is seeded once. |
| memguard swap drain | `modules/memguard/` (root: `sudo modules/memguard/install.sh`, `--dry-run` shows the diff) | Folds stale swap back into RAM when RAM can take it: one device at a time, aborted if memory turns tight. A 10-min timer decides (most runs skip with a logged reason); on demand: `memguard swap-drain` (no password). Publishes `/run/memguard/swap-drain.json` so gates can ignore a drain's 0 % SwapFree. Spec: [`docs/specs/memguard.md`](docs/specs/memguard.md) § Swap hygiene. |
| Disks (`disks`) | `modules/disks/`, declared in `machines/<host>/machine.toml` as `[modules.disks.mounts.<name>]` | Any USB or internal disk the profile lists (APFS from a Mac, NTFS, exFAT, ext4, ...) gets its driver, a marked block in `/etc/fstab`, a mount point and a systemd automount: mounted on first access, never blocks the boot when unplugged. APFS is read-only by default (`apfs-fuse`, built from a pinned commit; `driver = "kernel"` for `apfs-dkms`). `ubuntu-tweak capture` proposes the declaration for disks nobody declared. Spec: [`docs/specs/ubuntu-tweak-cli.md`](docs/specs/ubuntu-tweak-cli.md) § Disks. |

## macOS dock

![Dock with Liquid Glass: Apps grid, favorites | Downloads, Screenshots, Trash; Text Editor shows a badge and a progress bar](docs/dock/dock.png)

macOS 26 Tahoe (top) and this dock (bottom):

![macOS 26 Tahoe dock above this dock, same width](docs/dock/compare-tahoe.png)

*Top: Apple, macOS 26 Dock — screenshot from [Apple Support](https://support.apple.com/guide/mac-help/open-apps-from-the-dock-mh35859/26/mac/26), used for comparison.*

| Screenshots stack: glass popover with a notch at the icon | Glass Trash: empty, a few items, many items |
|---|---|
| ![](docs/dock/stack.png) | ![](docs/dock/trash.png) |

![Magnification, the Screenshots stack opening and closing, genie minimize into the icon and restore](docs/dock/demo.gif)

- **Liquid Glass** platter; separators and dots adapt their contrast to the wallpaper.
- **Glass Trash** that shows how full it is and thumbnails of the newest items.
- **Stacks:** Downloads and Screenshots by default; Fan, Grid or List; sort and display
  from the stack's right-click menu.
- **Badges and progress** from apps (Unity LauncherEntry: Telegram, Thunderbird, downloads)
  and red notification counts.
- **Genie minimize** into a tile of the window before Trash, as on macOS (own GPU
  effect); clicking the tile restores the window out of it. Launch and attention bounces.
- **Drag and drop:** drag an icon off the dock to remove it; drop files onto Trash to
  trash them, onto an app to open them with it, or onto a stack to move or copy them
  there.
- **Super+Alt+D** toggles auto-hide; right-click the dock background for hiding,
  magnification, position and settings.
- **Show All Windows** of an app from its menu; **recent apps** after the running ones.

Built on [Dash2Dock Animated](https://github.com/icedman/dash2dock-lite); the code lives in
[`alex-mextner/hyperdock`](https://github.com/alex-mextner/hyperdock) (branch `mac-tahoe`).
Everything is a setting: open the extension's *Settings* in the Extensions app.

- **Folder stacks:** *Others → Folder Stacks* adds any folder next to Downloads, or set
  it directly:
  `dconf write /org/gnome/shell/extensions/dash2dock-lite/folder-stacks "'[\"~/Projects\", {\"path\": \"~/Pictures/Screenshots\", \"icon\": \"folder-pictures\"}]'"`
- **Back to Ubuntu Dock:** `gnome-extensions disable <hyperdock uuid>` (the uuid is in
  `gnome-extensions/hyperdock/metadata.json`) and remove `ubuntu-dock@ubuntu.com` from
  `/org/gnome/shell/disabled-extensions`; set `[modules.hyperdock] enabled = false` in your profile
  so `ubuntu-tweak apply` stops managing it.

## Uninstall a GNOME extension

```sh
gnome-extensions disable <uuid>
rm -r ~/.local/share/gnome-shell/extensions/<uuid>
```

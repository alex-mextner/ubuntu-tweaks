# Spec: ubuntu-tweaks umbrella and the `ubuntu-tweak` CLI

Source of truth for how this repo installs, configures and updates its tweaks, and for the
Ubuntu settings profile it applies. Every user request is recorded verbatim under *Requests*;
*Requirements* is what they mean for the implementation. Change this file in the same commit
as any behavior change.

## Requests (verbatim, in order)

1. «сделай этот репозиторий зонтиком для других которые отдельные. добавь их сюда как
   субмодули думаю и сделай ubuntu-tweak init (cli) и другие команды чтобы все легко было
   установить на новой системе, настроить, обновить на существующией. напимер сюда же док,
   я его переименовал в hyperdock, обзор приложений и окон текущего приложения и тд.
   возможно стоит сделать реконциляцию как в http://git.hyperide.ai/ultrabricks/rig-cli»
2. «Обзор приложений это встроенная функция убунту, а окна вроде начаты в omp сессии про
   док, надо поставить задачу потом все поселить под зонтиком» — the app grid is GNOME's
   own; the per-app window overview is hyperdock's `AppSpread`
   ([#17](https://github.com/alex-mextner/ubuntu-tweaks/issues/17)).
3. «B» — CLI in Python, rig-cli style reconcile.
4. «A …» — hyperdock gets its own uuid/schema
   ([hyperdock#3](https://github.com/alex-mextner/hyperdock/issues/3)).
5. «про вопросам пока B, но с архитектунрой возможности добавить в будущем C» — modules
   cover extensions and built-in GNOME settings now; extensions.gnome.org later.
6. «обновления должны происходить автоматически бесшовно по релизу (должно
   настраиваться)» ([#19](https://github.com/alex-mextner/ubuntu-tweaks/issues/19)).
7. «у меня есть на гх .files. давай там сделаем поддиректорию ubuntu. и пусть все что я
   настраиваю помимо дотфайлов тоже там хранится. так же надо отдельные .config файлы там
   хранить а в .config держить симлинки. и нужна система развертывания всего этого добра.
   и перед тем как включим хочу увидеть что попадет в конфиг. так же ubuntu-tweaks должен
   тоже применятся автоматически. мб под него все и завернуть? я там просил сделать cli,
   если не доделано доделай. какой из проектов взять за основу сам подумай. я так понимаю
   как минимум надо все что в Settings настравается в системе. часть должно быть
   индивидуально для машины»

## Requirements

### Engine and profile

Request 7 is answered by splitting the system in two:

- **Engine = this repo** (`alex-mextner/ubuntu-tweaks`, public): the `ubuntu-tweak` CLI, the
  modules (hyperdock, english-in-overview, memguard, apple-hid-battery) and the machinery that
  applies a profile. There is no second tool.
- **Profile = a directory of personal data**, by default `~/.files/ubuntu` (the `ubuntu/`
  subdirectory of the private `alex-mextner/.files` repo): Settings-panel values, linked
  config files, packages, root-level system settings and per-machine overlays.

The public engine never contains personal settings; the private profile never contains engine
code. The mac-era dotfiles at the root of `.files` (zsh, zplug, brew) are not touched by this
system; `ubuntu/` is self-contained. Profile location: `--profile DIR` >
`$UBUNTU_TWEAK_PROFILE` > `~/.files/ubuntu`. An explicit location that does not exist is
invalid config (exit 2); a missing default location means "no profile" and only module
defaults apply.

Fresh machine:

```sh
gh repo clone alex-mextner/.files ~/.files
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/xp/ubuntu-tweaks
~/xp/ubuntu-tweaks/bin/ubuntu-tweak init      # doctor -> submodules -> apply preview -> --commit
```

### Safety invariants

1. Every mutating command is **preview by default**; only `--commit` changes anything.
   `capture` and `apply` both print the exact plan and change nothing without `--commit`.
2. The preview is computed by the same code path as the commit (the plan holds only actions
   whose `check()` fails), so what the user saw is what runs.
3. Nothing from the live system enters the profile except through `capture`, which shows
   everything it would write first (see *capture*).
4. **No secrets in the profile.** Deny rules and a value-pattern scan (PEM blocks, tokens,
   emails) skip or mask keys and make `capture --commit` refuse (exit 5). The `.files` repo
   CI runs gitleaks with `--exit-code 1`; `capture` never produces output that fails it.
5. Replacing a real file with a symlink always copies the original to
   `~/.local/state/ubuntu-tweaks/backup/<UTC timestamp>/<path>` first.
6. Tests and smoke runs never touch the user's dconf or `$HOME`: they use `dbus-run-session`
   with a scratch `DCONF_PROFILE`, a temporary `HOME` and temporary `XDG_*`. Reading the live
   system (capture preview, status) is allowed; writing happens only when the user runs a
   command with `--commit`.
7. Extras (on disk, not declared) are reported, never removed.

### Commands

`ubuntu-tweak` is one executable, `bin/ubuntu-tweak`, running the package
`ubuntu_tweaks/` from the repo checkout (no install step; the checkout is the program).
Commands self-register: `ubuntu_tweaks/commands/<name>.py` exposes
`register(subparsers)`, and `cli.py` never names a command; dropping a file adds one.

| Command | What it does |
|---|---|
| `init` | Fresh system: `doctor` (offers to install missing packages), `git submodule update --init --recursive`, symlinks itself into `~/.local/bin`, then `apply --commit`. Re-running is safe. |
| `apply` | Prints the plan: every action needed to make the system match modules + profile. Changes nothing. |
| `apply --commit` | Executes the plan. Idempotent: a second run reports `0 changed`. |
| `status` | Drift both ways (see *Drift*). Exit 3 when anything declared is missing or modified. |
| `update` | `git pull --ff-only` of the umbrella, `git submodule update --init --recursive`, then `apply --commit`. Channel and schedule come from #19. |
| `doctor [--yes]` | Checks required packages and binaries; `--yes` installs the missing ones with apt. |
| `list` | Modules with kind, enabled state and source, from the manifests and the merged config. Read-only. |
| `machine` | Read-only: detected machine name, whether its overlay exists, and for each module setting, panel file and linked file whether it comes from the shared profile or the machine overlay. |
| `capture [--commit] [--report FILE] [--only …] [-v]` | Inventory of the live system, printed as the exact diff the profile would receive (see *capture*). |

Global flags, accepted before or after the command: `--profile DIR`, `--machine NAME`,
`--only <name,…>` (modules for `apply`/`status`, panels for `capture`).

Exit codes: 0 ok, 1 internal error, 2 invalid config, 3 drift, 4 unknown module, 5 refused
(secret finding or unsafe path). The constants live in one place,
`ubuntu_tweaks/errors.py`, and top-level errors map to them. A command-line usage error
(unknown command or flag) is argparse's own exit 2. A name in `--only` that no module
declares is exit 4.

After any action that adds or changes extension code, the CLI says that a re-login is
needed: Shell on Wayland loads extension code only at login.

### Modules

A module is a directory `modules/<name>/` with `module.toml` and optional settings files.
Adding a tweak is: add the code (a submodule, a local directory or a root installer), add
`modules/<name>/`. The CLI never names a module in code.

```toml
description = "macOS-style dock"
enabled = true                 # default; profile.toml / machine.toml override it

[source]
kind = "submodule"            # submodule | local | builtin | system   (ego: reserved, #20)
path = "gnome-extensions/hyperdock"

[extension]                    # submodule and local only
conflicts = ["ubuntu-dock@ubuntu.com"]                       # disabled while enabled
retire = ["compiz-alike-magic-lamp-effect@hermes83.github.com"]  # dropped from enabled

[[settings]]
file = "dock.ini"              # keyfile: [/] then key=GVariant text
# schema defaults to the extension's metadata.json "settings-schema"; required for builtin
mode = "enforce"               # enforce: apply and drift-check every key
                               # seed: write only while the key is at its default

[[seeds]]
key = "folder-stacks"
provider = "screenshots-stack" # named function in ubuntu_tweaks/seeds.py

[[migrations]]
from_uuid = "dash2dock-lite@icedman.github.com"
from_path = "/org/gnome/shell/extensions/dash2dock-lite/"

[deps]
apt = ["gir1.2-gtk-4.0"]
```

Unknown keys, a missing `[source]`, a source without the fields its kind needs, or a path
that is absolute or contains `..` are invalid config (exit 2).

- **Source kinds**
  - `submodule`: a git submodule; uuid, schema id and schema dir are read from its
    `metadata.json`, never hardcoded, so the module keeps working across the hyperdock
    rename.
  - `local`: an extension directory inside this repo (`english-in-overview@ultra`).
  - `builtin`: no code, only settings for GNOME's own schemas (e.g. the app grid, the
    keyboard, the color scheme from #20).
  - `system`: root-level code installed outside `$HOME` (memguard's units and polkit rule, the
    apple-hid-battery livepatch). The manifest names its installer and a read-only checker,
    both command lines relative to the module directory:

    ```toml
    [source]
    kind = "system"
    install = "install.sh"
    check = "install.sh --check"   # exit 0 = installed files match; must be read-only
    ```

    The action's `check()` runs the checker; its `run()` runs the installer through
    sudo/pkexec. A system module takes no `[extension]`, `[[settings]]`, `[[seeds]]` or
    `[[migrations]]`. An installer that cannot check itself gets a `--check` mode before its
    module is added.

    Checker protocol: exit 0 = installed and matching, 3 = drift (the module is planned), any
    other exit code, a launch failure or a 60-second timeout = the check crashed, which fails
    `apply` / `status` with the module's name and output instead of guessing (exit 1). Both
    commands run with the module directory as working directory and `argv[0]` resolved inside
    it. The planner (`planners/system_modules.py`) emits one action per enabled `system`
    module: kind `system-module`, phase SYSTEM, root, scope = the module name, preview line
    `run <module>: sudo <command>`. Disabled modules (module default, profile or machine
    overlay) emit nothing; `--no-sudo` skips them and says so like every root action.
    `memguard` checks with `install.sh --check` (the `--dry-run` comparison: every installed
    file byte-equal to the repo copy and its mode, timer enabled; `MEMGUARD_ROOT=DIR` points it
    at a test root). `apple-hid-battery` checks the running kernel: module present, loaded at
    boot, livepatch enabled, or absent on kernels with the upstream fix (>= 7.3).
  - `ego`: an extension from extensions.gnome.org by uuid and optional version. The
    manifest schema reserves it and validation rejects it with "not supported yet (#20)"
    until it is implemented. Source kinds are one class each behind one interface
    (`resolve()` → uuid, schema dir, files; `fetch()`), so adding `ego` touches no other
    code.
- **Settings files** use `dconf dump` keyfile syntax, so a preset is made with
  `dconf dump /org/gnome/shell/extensions/<x>/ > modules/<x>/<x>.ini` and trimmed.
- **Seeds** are values computed on the machine (the Screenshots folder is localized), written
  only while the key is at its default, never drift-checked.
- **Migrations** run before settings: when `from_uuid` is still installed or `from_path`
  has user values and the module's current schema path has none, user values are copied
  to the new path, the old uuid is removed from `enabled-extensions`, and its symlink is
  removed if it points into this repo.

### Modules at cutover

| Module | Kind | Notes |
|---|---|---|
| `hyperdock` | submodule `gnome-extensions/hyperdock` → `alex-mextner/hyperdock`, branch `mac-tahoe` | from today's `install.sh`: symlink, schemas, enable, Ubuntu Dock conflict, Magic Lamp retire, `dock.ini` (enforce), Screenshots stack seed, migration from `dash2dock-lite` |
| `english-in-overview` | local `gnome-extensions/english-in-overview@ultra` | symlink, enable |
| `memguard` | `system` | `modules/memguard/` already ships `install.sh`, the `swap-drain` units and the polkit rule; its manifest needs `install.sh --check`; per-machine (`enabled = false` on machines that do not want it) |
| `apple-hid-battery` | `system` (livepatch kernel module) | only for machines with Apple HID: `enabled = false` by default, a machine overlay enables it |

No other module is added until it has real content.

### Profile layout

`<profile>` = `~/.files/ubuntu` by default.

```
<profile>/
  README.md
  profile.toml              # shared config: modules, capture rules
  settings/<panel>.ini      # dconf keyfile per GNOME Settings panel, shared by all machines
  files/                    # mirrors $HOME: files/.config/mimeapps.list -> ~/.config/mimeapps.list
  packages.toml             # apt / snap / flatpak names
  system.toml               # root-level: timezone, locale, console keyboard
  machines/<machine>/       # overlay, one dir per machine
    machine.toml            # same schema as profile.toml; wins over it
    settings/<panel>.ini    # key-level overlay on top of the shared panel files
    files/                  # path-level overlay: the same relative path wins over shared files/
    packages.toml  system.toml   # same schema, merged
```

**Machine name** = the contents of `/etc/hostname`, or `--machine NAME`. It is a directory
name, so it must be a single path component (no `/`, not `.` or `..`). Detection is one
function, so everything downstream is testable with `--machine`. A machine without an overlay
directory uses the shared profile only; `machine` and `status` say so.

**Merge order**, last wins: module defaults (`modules/<m>/module.toml`) < shared profile <
machine overlay. Tables merge key by key and scalars replace. Lists replace in
`profile.toml`/`machine.toml` (a list is one value) and **union** in `packages.toml` and
`system.toml` (shared order first, then the overlay's new items), so a machine adds packages
without restating the shared ones. Every merged value remembers its layer (module, shared or
overlay), which is what `machine` prints.

This replaces the earlier per-user `~/.config/ubuntu-tweaks/config.toml`: user overrides
live in the profile, so they are versioned with the rest. The `[update]` table owned by #19
lives in `profile.toml` as well.

#### `settings/<panel>.ini`

`dconf dump` keyfile syntax (section = dconf path without the leading `/`, values = GVariant
text), one file per Settings panel, named by the panel id in *Panel coverage*. Apply
semantic = **enforce only the keys present**; absent keys are never touched or reset. Values
are compared as typed `GLib.Variant`s through the schema (effective value = user value, else
default), exactly as in *Drift*. A key whose schema is not installed is skipped with a
warning, never guessed. Relocatable schemas (custom keybindings, tiling-assistant paths) go
through `Gio.Settings.new_with_path`.

`~/.config/glib-2.0/settings/keyfile` on a machine is a GSettings keyfile override that
changes *effective* values (power, idle-delay, enabled-extensions, favorite-apps). The engine
reads effective values through Gio, never freezes overrides into dconf (see *Plan and
actions* on `enabled-extensions`), and `capture` reports keys whose effective value comes
from the override rather than from the dconf user value. That file is itself a capturable
`files/` entry.

#### `files/`

A mirror of `$HOME`. Each *leaf file* under `files/` becomes a symlink in `$HOME` pointing at
the profile copy (`ln -sfnT`). Directories stay real directories in `$HOME`; only leaves are
linked, so apps can add siblings: `files/.config/foo/bar.conf` → `~/.config/foo/bar.conf`. A
machine overlay path wins over the shared file at the same path. `~/.config` keeps symlinks;
the real file lives in the profile.

- An existing real file at the destination with identical content is adopted silently
  (replaced by the symlink, still backed up). With different content, the `apply` plan shows
  `backup+replace` with a diff summary; never silently.
- An app that rewrites the symlink into a real file (atomic rename: gtk bookmarks,
  `mimeapps.list`) is reported by `status` as `replaced`; `capture` re-adopts it after
  showing the diff.
- Capturable files are an explicit allow-list only (see *capture*), never a recursive grab of
  `~/.config`.

#### `profile.toml` / `machine.toml`

```toml
[profile]
description = "alex personal Ubuntu settings"

[modules.hyperdock]             # per-module override
enabled = true
[modules.hyperdock.settings]
icon-size = "0.15"              # GVariant text, overrides the module's preset

[modules.memguard]              # per machine: set in machines/<machine>/machine.toml
enabled = false

[update]                        # owned by #19
channel = "release"

[capture]                       # rules evaluated by `capture`
exclude = ["/org/gnome/shell/app-picker-layout"]            # never captured
machine = ["/org/gnome/settings-daemon/plugins/power/**"]   # captured into machines/<machine>/
include_files = [".config/mimeapps.list", ".config/gtk-3.0/bookmarks"]   # file allow-list
```

Disabling a module stops managing it; it does not uninstall. `ubuntu-tweak` never edits the
repo's files on the user's behalf.

#### `system.toml` (root, applied through sudo/pkexec at `--commit`)

```toml
timezone = "Europe/Belgrade"           # timedatectl
locale   = "en_US.UTF-8"               # localectl
[keyboard]                             # /etc/default/keyboard
layout = "us,ru"
options = "grp_led:scroll"             # model and variant are accepted too
```

Checks are read-only (`timedatectl show`, `localectl status`, parsing
`/etc/default/keyboard`). The plan marks root actions `[sudo]`; `--no-sudo` skips them and
says so.

#### `packages.toml`

```toml
apt = ["tmux", "ripgrep"]
snap = ["firefox"]
flatpak = ["org.gnome.Extensions"]
```

`apply` installs the missing ones and never removes. `capture` proposes
`apt-mark showmanual` minus the installer baseline (`/var/log/installer/initial-status.gz`
when present, else minus the packages the `ubuntu-desktop` metapackage pulls in), so only
deliberate installs show up.

### Panel coverage

Every GNOME Settings panel gets an explicit disposition. `S` = shared, `M` = per-machine
overlay, `X` = never captured (secret, identity, hardware), `F` = file adapter, `R` = root
adapter. The exact dconf path globs per panel are finalized in `ubuntu_tweaks/panels.toml`.

| Panel (file id) | Source | Disp. |
|---|---|---|
| network (Wi-Fi, Ethernet, VPN, proxy) | NetworkManager connections hold secrets; `org.gnome.system.proxy` | proxy `S/M`; connections `X` |
| bluetooth | pairings are machine hardware | `X` |
| background | `org.gnome.desktop.background`, screensaver picture | `S` (absolute paths are flagged) |
| appearance | `org.gnome.desktop.interface` (theme, color-scheme, accent, fonts, cursor), dash/dock, `org.gnome.shell.extensions.*` Ubuntu dock | `S` |
| notifications | `org.gnome.desktop.notifications` (+ per-app children) | `S` |
| search | `org.gnome.desktop.search-providers`, `org.freedesktop.Tracker3.*` | `S` |
| multitasking | `org.gnome.mutter`, `org.gnome.desktop.wm.preferences`, hot corner, workspaces | `S` |
| applications / default apps | `~/.config/mimeapps.list` (`F`), per-app permissions in flatpak | `F` |
| privacy | `org.gnome.desktop.privacy`, `org.gnome.desktop.screensaver`, location | `S`; location coordinates `M` |
| online-accounts | tokens, identities | `X` |
| sharing | `org.gnome.desktop.remote-desktop.*` (RDP credentials), file sharing | settings `S`; credentials `X` |
| sound | `org.gnome.desktop.sound`; device choice lives in PipeWire state | event sounds `S`; devices `M` |
| power | `org.gnome.settings-daemon.plugins.power`, `org.gnome.desktop.session`, battery-specific keys | `M` by default (lid and idle differ per machine); profile mode `M` |
| displays | `~/.config/monitors.xml`, night light keys, `org.gnome.settings-daemon.plugins.color` | `monitors.xml` `F`+`M`; night light `S`, coordinates `M` |
| mouse-touchpad | `org.gnome.desktop.peripherals.{mouse,touchpad,trackball}` | `M` (laptop vs desktop) |
| keyboard | `org.gnome.desktop.input-sources`, `org.gnome.desktop.wm.keybindings`, `org.gnome.settings-daemon.plugins.media-keys` (custom bindings), `org.gnome.mutter.keybindings`, tiling-assistant | `S` |
| printers | CUPS queues, machine and network specific | `X` |
| removable-media | `org.gnome.desktop.media-handling` | `S` |
| color | ICC profiles per display | `M` / `X` |
| region-language | `org.gnome.system.locale`, `org.gnome.desktop.interface clock-format`; system locale | settings `S`; `locale` `R` |
| accessibility | `org.gnome.desktop.a11y.*`, `org.gnome.desktop.interface toolkit-accessibility` | `S` |
| users | accounts | `X` |
| date-time | `org.gnome.desktop.interface clock-*`, `org.gnome.desktop.datetime`; timezone | clock `S`; `timezone` `R`+`M` |
| about | hostname, device name | `X` |
| ubuntu-desktop | Ubuntu Dock, desktop icons (ding), tiling-assistant, app grid folders | `S` |
| shell-extensions | enabled/disabled lists and per-extension schemas | handled by *modules*, not by panel files |

### Per-machine

- The machine key is the hostname; `machines/<machine>/` is the overlay described above.
- Default `machine` rules ship with the engine (`ubuntu_tweaks/capture/defaults.toml`): power
  plugin keys including `battery-*`, `peripherals/touchpad`, `peripherals/mouse`,
  `night-light-last-coordinates`, `last-selected-power-profile`, sound device keys, absolute
  filesystem paths outside `/usr/share`, anything under `monitors.xml`.
- `ubuntu-tweak machine` prints the detected name, whether the overlay exists, and which
  module setting, panel file and linked file comes from the overlay and which from the shared
  profile. A panel file present in both is reported as `overlay over shared`; a linked file
  present in both is reported as `overlay (shadows shared)`.
- Modules are per-machine too: `[modules.memguard] enabled = false` in a machine overlay.

### capture

`ubuntu-tweak capture` is the read-only inventory of the live system, printed as the exact
diff the profile would receive. With `--commit` it writes into the profile working tree and
does not run `git commit`: the user reviews `git diff` in `.files` (the second preview) and
commits through the repo's PR flow. `--report FILE.md` writes the same report as markdown.
`--only <panel,…>` limits panels.

Sections, in this order:

1. **Settings → `settings/<panel>.ini`** (shared) and **→ `machines/<machine>/settings/…`**
   (machine): per panel, per dconf path, every key that has a user value differing from the
   schema default, or whose effective value is overridden by the keyfile backend. Marks: `+`
   new in the profile, `~` differs from the profile value, `=` already in the profile
   (hidden unless `-v`).
2. **Files → `files/…`**: only allow-list candidates (`include_files` plus engine built-ins:
   `mimeapps.list`, `user-dirs.dirs`, `gtk-3.0/bookmarks`, `gtk-3.0/settings.ini`,
   `gtk-4.0/settings.ini`, `glib-2.0/settings/keyfile`, `monitors.xml` (machine),
   `autostart/*.desktop`, hand-written dotfiles the user chose), plus a `candidates` listing
   of everything else in `~/.config` and `$HOME` dotfiles classified `portable-config` /
   `secret-bearing` / `cache-state` / `mixed` with the reason, so the user can promote an
   entry by adding it to `include_files`. Nothing outside the allow-list is ever adopted.
   Each adopted file shows size, destination, the `backup+symlink` action and
   `identical`/`differs` against the profile copy.
3. **Packages** (a `packages.toml` proposal) and **System** (a `system.toml` proposal).
4. **Skipped**, with a reason class: `secret` (online-accounts, gsconnect certificates, a
   value matching a PEM/token/email pattern; values are masked in the report), `identity`,
   `hardware`, `state` (window geometry, recents, app-picker-layout, `last-*` timestamps),
   `no-schema`, `excluded-by-rule`. Counts per class plus each key.
5. A totals line; exit 0. `--commit` refuses with exit 5 ("refused: secret finding") when
   the output scan finds anything.

Selection is deterministic rules (engine defaults plus `[capture]` in the profile), not an
interactive picker, so a preview is reproducible and reviewable and the rules live in the
repo.

### Plan and actions

`apply` builds an ordered list of actions, prints it, and with `--commit` runs it. Every
action has `check()` (already satisfied?) and `run()`; the plan holds only actions whose
check fails, so the preview is exactly what `--commit` will do. After an action runs, its
`check()` must hold; a plan that does not converge fails instead of reporting `changed`.

Order: submodules → migrations → schemas → **packages** → **system (root)** → symlinks
(extensions) → **files (links)** → enable/disable → settings (module presets, then profile
panel files, then machine overlay) → seeds.

| Action | Check | Run |
|---|---|---|
| `submodule` | submodule is initialized | `git submodule update --init`; a submodule checked out at another commit (dev work) is reported by `status`, never reset — only `update` moves it |
| `migrate` | see *Migrations* | copy values, retire old uuid |
| `compile-schemas` | `gschemas.compiled` newer than every `*.gschema.xml` | `glib-compile-schemas` |
| `packages` | apt / snap / flatpak name is installed | install the missing names; never remove |
| `system` (root) | `timedatectl`, `localectl`, `/etc/default/keyboard` or a `system` module's checker | the same through sudo; a `system` module runs its installer |
| `link` | `~/.local/share/gnome-shell/extensions/<uuid>` is a symlink to the source | `ln -sfnT`; an existing real directory is moved to `~/.local/state/ubuntu-tweaks/backup/<timestamp>/` first |
| `file-link` | `~/<path>` is a symlink to the profile file | back up a real file, then `ln -sfnT` |
| `enable` / `disable` | uuid in / not in the user value of `enabled-extensions` and `disabled-extensions` | add / remove the one item |
| `set-key` | effective value (user value, else schema default) equals the wanted typed value | write it |
| `seed-key` | key has a user value | write the provider's value |

Root actions are marked `[sudo]` in the plan; `--no-sudo` skips them and reports them as
skipped.

Writes go through `Gio.Settings` from `python3-gi` (installed on every Ubuntu desktop;
`doctor` checks it), with the extension's own schema dir via
`Gio.SettingsSchemaSource.new_from_directory`. The rest is stdlib: `tomllib`, `argparse`,
`subprocess` for git and apt.

`enabled-extensions` and `disabled-extensions` are edited as single-item additions and
removals of the **user value** (`get_user_value`), never by writing back the effective
value: on Ubuntu the effective value comes from the session's override and writing it back
would freeze it (the reason `install.sh` used `dconf` directly).

### Drift

`status` compares declared state with the machine, using the same `check()` as `apply`.

- **Missing** (declared, not on disk): a symlink, an enabled uuid, a conflict still
  enabled. `apply --commit` fixes it.
- **Modified** (declared, different on disk): a key whose value differs, a linked file
  replaced by a real file (`replaced`). Values are compared as typed `GLib.Variant`s read
  through the schema, so `5`, `uint32 5` and an unset key at its default compare correctly;
  a raw `dconf read` string diff would report every default-valued key. `apply --commit`
  overwrites it; the user's own changes belong in the profile (`capture` brings them there).
- **Extra** (on disk, not declared), reported only, never changed: extensions enabled by
  the user that no module declares, symlinks into this repo for uuids no module declares
  (e.g. the old dock uuid before the migration ran).

### Install

```sh
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/xp/ubuntu-tweaks
~/xp/ubuntu-tweaks/bin/ubuntu-tweak init
```

`install.sh` stays as a two-line wrapper calling `bin/ubuntu-tweak init`, so the old
instructions keep working. With a profile, clone `.files` first (see *Engine and profile*).

### Testing

- Unit (`python3 -m unittest`, TDD): profile resolution and config merge precedence,
  manifest parsing and validation, plan filtering, preview rendering and root skipping,
  keyfile round trip, plan building from a fake settings backend, drift classification,
  migrations, seed providers.
- Integration: `dbus-run-session` with a scratch `DCONF_PROFILE` and a temporary `HOME`;
  typed `GioBackend` reads and writes, then `apply --commit` on an empty profile, `status`
  exits 0, a second `apply --commit` reports `0 changed`. Runs in CI
  ([#18](https://github.com/alex-mextner/ubuntu-tweaks/issues/18)). Tests that need
  PyGObject run under the distro interpreter, because `python3-gi` is an apt package.

## Decisions

- **Python, stdlib plus `python3-gi`**, not bash: the plan/preview and the typed dconf
  diff are the core of the tool, and bash cannot compare GVariants. Not a `rig` plugin:
  this repo is public, and its users have no reason to install a dev tool.
- **The engine is `ubuntu-tweaks`, the profile is `.files/ubuntu`**: the CLI, the plan model
  and the modules already live here; a second tool would duplicate them. Personal data goes
  to the private repo only, so the public engine stays shareable.
- **TOML manifests per module**, not one central file: a module is added or removed as
  one directory, and `tomllib` is in the stdlib.
- **No lockfile**: drift is recomputed from the modules against the machine each time, as
  in rig. The submodule commit recorded in git is the version lock.
- **The repo's preset wins on `apply`** (enforce), the user's taste lives in the profile:
  otherwise every run would have to guess which side changed.
- **Only the keys a panel file lists are enforced**: absent keys are never reset, so a
  profile can be adopted panel by panel.
- **Extras are never removed**: the tool only undoes what it declared. `packages.toml`
  installs and never removes for the same reason.
- **Leaves, not directories, are linked** under `$HOME`: apps keep creating siblings in
  their own config directories.
- **`capture` is rules, not a picker**: reproducible, reviewable, and the rules are versioned.
- **Auto-apply is off until #19**: `apply` enforces and must not overwrite live tweaks the
  user has not seen.
- **The hyperdock uuid is read from `metadata.json`**: the umbrella works before and after
  hyperdock#3; the migration covers the switch.
- **Dev clone remotes**: in `~/xp/dash2dock-lite`, `origin` is upstream icedman; the
  fork is `fork`. Tickets for hyperdock are filed with `--repo alex-mextner/hyperdock`.

## Cutover checklist

- `.gitmodules`: `gnome-extensions/dash2dock-lite@icedman.github.com` →
  `gnome-extensions/hyperdock`, URL `https://github.com/alex-mextner/hyperdock.git`.
- `dconf/dock.ini` → `modules/hyperdock/dock.ini`; `install.sh` logic → modules +
  actions; `install.sh` → wrapper.
- README: install via `ubuntu-tweak init`, commands table, module list; links to
  `alex-mextner/dash2dock-lite` here and in `docs/specs/mac-dock.md` → `alex-mextner/hyperdock`.
- On this machine: `ubuntu-tweak apply --commit` replaces the old symlink
  `dash2dock-lite@icedman.github.com` → new submodule path, then `status` exits 0.

## Out of scope here (tickets)

- Login-time or scheduled auto-apply and release-channel auto-update: [#19](https://github.com/alex-mextner/ubuntu-tweaks/issues/19)
  (opt-in user-level systemd timer, default off).
- `ego` source kind and auto dark mode: [#20](https://github.com/alex-mextner/ubuntu-tweaks/issues/20).
- Tests, checks, hooks and CI beyond what the profile work needs: [#18](https://github.com/alex-mextner/ubuntu-tweaks/issues/18).
- Window overview as its own repo: [#17](https://github.com/alex-mextner/ubuntu-tweaks/issues/17).
- Secrets sync (keyring, SSH keys, tokens): never in the profile. A separate mechanism
  (sops/age, keychain) if wanted later.
- The work items of the profile sync itself (engine actions, capture, system modules, profile
  scaffold): [#25](https://github.com/alex-mextner/ubuntu-tweaks/issues/25).

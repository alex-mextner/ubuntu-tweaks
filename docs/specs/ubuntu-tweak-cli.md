# Spec: ubuntu-tweaks umbrella and the `ubuntu-tweak` CLI

Source of truth for how this repo installs, configures and updates its tweaks. Every user
request is recorded verbatim under *Requests*; *Requirements* is what they mean for the
implementation. Change this file in the same commit as any behavior change.

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

## Requirements

### Commands

`ubuntu-tweak` is one executable, `bin/ubuntu-tweak`, running the package
`ubuntu_tweaks/` from the repo checkout (no install step; the checkout is the program).

| Command | What it does |
|---|---|
| `init` | Fresh system: `doctor` (offers to install missing packages), `git submodule update --init --recursive`, symlinks itself into `~/.local/bin`, then `apply --commit`. Re-running is safe. |
| `apply` | Prints the plan: every action needed to make the system match the config. Changes nothing. |
| `apply --commit` | Executes the plan. Idempotent: a second run reports `0 changed`. |
| `status` | Drift both ways (see *Drift*). Exit 3 when anything declared is missing or modified. |
| `update` | `git pull --ff-only` of the umbrella, `git submodule update --init --recursive`, then `apply --commit`. Channel and schedule come from #19. |
| `doctor [--yes]` | Checks required packages and binaries; `--yes` installs the missing ones with apt. |
| `list` | Modules with kind, enabled state and source. |

Exit codes: 0 ok, 1 internal error, 2 invalid config, 3 drift, 4 unknown module (same
classes as rig). Every command takes `--only <module,…>`.

After any action that adds or changes extension code, the CLI says that a re-login is
needed: Shell on Wayland loads extension code only at login.

### Modules

A module is a directory `modules/<name>/` with `module.toml` and optional settings files.
Adding a tweak is: add the code (a submodule or a local directory), add `modules/<name>/`.
The CLI never names a module in code.

```toml
description = "macOS-style dock"

[source]
kind = "submodule"            # submodule | local | builtin   (ego: reserved, #20)
path = "gnome-extensions/hyperdock"

[extension]                    # submodule and local only
conflicts = ["ubuntu-dock@ubuntu.com"]                       # disabled while enabled
retire = ["compiz-alike-magic-lamp-effect@hermes83.github.com"]  # dropped from enabled

[[settings]]
file = "dock.ini"              # keyfile: [/] then key=GVariant text
# schema defaults to the extension's metadata.json "settings-schema"
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

- **Source kinds**
  - `submodule`: a git submodule; uuid, schema id and schema dir are read from its
    `metadata.json`, never hardcoded, so the module keeps working across the hyperdock
    rename.
  - `local`: an extension directory inside this repo (`english-in-overview@ultra`).
  - `builtin`: no code, only settings for GNOME's own schemas (e.g. the app grid, the
    keyboard, the color scheme from #20).
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

| Module | Kind | From today's `install.sh` |
|---|---|---|
| `hyperdock` | submodule `gnome-extensions/hyperdock` → `alex-mextner/hyperdock`, branch `mac-tahoe` | symlink, schemas, enable, Ubuntu Dock conflict, Magic Lamp retire, `dock.ini` (enforce), Screenshots stack seed, migration from `dash2dock-lite` |
| `english-in-overview` | local `gnome-extensions/english-in-overview@ultra` | symlink, enable |

No other module is added until it has real content.

### User overrides

`~/.config/ubuntu-tweaks/config.toml` is merged over the repo's modules: tables merge,
values replace.

```toml
[modules.english-in-overview]
enabled = false

[modules.hyperdock.settings]
icon-size = "0.15"            # GVariant text, overrides the module's preset

[update]                       # owned by #19
channel = "release"
```

Disabling a module stops managing it; it does not uninstall. `ubuntu-tweak` never edits
the repo's files on the user's behalf.

### Plan and actions

`apply` builds an ordered list of actions, prints it, and with `--commit` runs it. Every
action has `check()` (already satisfied?) and `run()`; the plan holds only actions whose
check fails, so the preview is exactly what `--commit` will do.

Order: submodules → migrations → schemas → symlinks → enable/disable → settings → seeds.

| Action | Check | Run |
|---|---|---|
| `submodule` | submodule is initialized | `git submodule update --init`; a submodule checked out at another commit (dev work) is reported by `status`, never reset — only `update` moves it |
| `compile-schemas` | `gschemas.compiled` newer than every `*.gschema.xml` | `glib-compile-schemas` |
| `link` | `~/.local/share/gnome-shell/extensions/<uuid>` is a symlink to the source | `ln -sfnT`; an existing real directory is moved to `~/.local/state/ubuntu-tweaks/backup/<timestamp>/` first |
| `enable` / `disable` | uuid in / not in the user value of `enabled-extensions` and `disabled-extensions` | add / remove the one item |
| `set-key` | effective value (`get_value`: user value, else schema default) equals the wanted value | write it |
| `seed-key` | key has a user value | write the provider's value |
| `migrate` | see *Migrations* | copy values, retire old uuid |

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
- **Modified** (declared, different on disk): a key whose value differs. Values are
  compared as typed `GLib.Variant`s read through the schema, so `5`, `uint32 5` and an
  unset key at its default compare correctly; a raw `dconf read` string diff would report
  every default-valued key. `apply --commit` overwrites it; the user's own changes belong in
  `config.toml`.
- **Extra** (on disk, not declared), reported only, never changed: extensions enabled by
  the user that no module declares, symlinks into this repo for uuids no module declares
  (e.g. the old dock uuid before the migration ran).

### Install

```sh
git clone --recurse-submodules https://github.com/alex-mextner/ubuntu-tweaks ~/xp/ubuntu-tweaks
~/xp/ubuntu-tweaks/bin/ubuntu-tweak init
```

`install.sh` stays as a two-line wrapper calling `bin/ubuntu-tweak init`, so the old
instructions keep working.

### Testing

- Unit (`python3 -m unittest`, TDD): manifest parsing and validation, config merge, plan
  building from a fake settings backend, drift classification, migrations, seed
  providers.
- Integration: `dbus-run-session` with a scratch `DCONF_PROFILE` and a temporary `HOME`;
  `apply --commit` on an empty profile, then `status` exits 0, then a second
  `apply --commit` reports `0 changed`. Runs in CI
  ([#18](https://github.com/alex-mextner/ubuntu-tweaks/issues/18)).

## Decisions

- **Python, stdlib plus `python3-gi`**, not bash: the plan/preview and the typed dconf
  diff are the core of the tool, and bash cannot compare GVariants. Not a `rig` plugin:
  this repo is public, and its users have no reason to install a dev tool.
- **TOML manifests per module**, not one central file: a module is added or removed as
  one directory, and `tomllib` is in the stdlib.
- **No lockfile**: drift is recomputed from the modules against the machine each time, as
  in rig. The submodule commit recorded in git is the version lock.
- **The repo's preset wins on `apply`** (enforce), the user's taste lives in
  `config.toml`: otherwise every run would have to guess which side changed.
- **Extras are never removed**: the tool only undoes what it declared.
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

- `ego` source kind and auto dark mode: [#20](https://github.com/alex-mextner/ubuntu-tweaks/issues/20).
- Automatic updates by release: [#19](https://github.com/alex-mextner/ubuntu-tweaks/issues/19).
- Tests, checks, hooks and CI: [#18](https://github.com/alex-mextner/ubuntu-tweaks/issues/18).
- Window overview as its own repo: [#17](https://github.com/alex-mextner/ubuntu-tweaks/issues/17).

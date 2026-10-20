# ubuntu-tweak capture

mode: preview (nothing is written; --commit writes exactly this)

profile: <tmp>/profile

machine: testbox (overlay does not exist yet)

only: everything

## 1. Settings

### settings/appearance.ini (shared)

|  | dconf path | key | value | note |
|---|---|---|---|---|
| + | `org/gnome/desktop/interface` | `color-scheme` | `'prefer-dark'` |  |

### settings/notifications.ini (shared)

|  | dconf path | key | value | note |
|---|---|---|---|---|
| + | `org/gnome/desktop/notifications/application/foo` | `enable` | `false` |  |

### machines/testbox/settings/background.ini (machine)

|  | dconf path | key | value | note |
|---|---|---|---|---|
| + | `org/gnome/desktop/background` | `picture-uri` | `'file:///home/me/Pictures/wall.jpg'` | absolute path |

### machines/testbox/settings/power.ini (machine)

|  | dconf path | key | value | note |
|---|---|---|---|---|
| + | `org/gnome/desktop/interface` | `show-battery-percentage` | `true` |  |
| + | `org/gnome/settings-daemon/plugins/power` | `sleep-inactive-ac-timeout` | `3600` |  |

## 2. Files

### adopted (allow-list)

| file | size | destination | action | vs profile | note |
|---|---|---|---|---|---|
| `~/.config/mimeapps.list` | 44 B | `files/.config/mimeapps.list` | backup+symlink | new |  |

### candidates (not adopted; add to include_files in the profile's [capture] to promote)

1 portable-config, 0 mixed, 0 secret-bearing, 0 cache-state

#### portable-config

| path | size or files | reason |
|---|---|---|
| `.config/htop/htoprc` | 15 B | small text file, no credential pattern or key name |

## 3. Packages and System

### packages.toml proposal

apt baseline: dependencies of ubuntu-desktop, ubuntu-desktop-minimal, ubuntu-minimal, ubuntu-standard, ubuntu-server (no installer baseline file)

+ apt (3 new, 0 already in the profile): libssl-dev, ripgrep, tmux

+ snap (2 new, 0 already in the profile): code, firefox

+ flatpak (1 new, 0 already in the profile): org.example.Flat

### system.toml proposal

|  | key | value | goes to | note |
|---|---|---|---|---|
| + | `timezone` | `Europe/Belgrade` | machines/testbox/system.toml |  |
| + | `locale` | `en_US.UTF-8` | system.toml |  |
| + | `keyboard.model` | `pc105` | system.toml |  |
| + | `keyboard.layout` | `us,ru` | system.toml |  |
| + | `keyboard.options` | `grp_led:scroll` | system.toml |  |

## 4. Skipped

classes: secret 1, identity 0, hardware 0, state 2, no-schema 1, excluded-by-rule 1

unmapped (user value, no panel owns it; nothing is captured for these): 2

at schema default (not listed): 1

### secret (1)

| key | panel | reason |
|---|---|---|
| `/org/example/app/contact` | - | value matches: email (masked) |

### state (2)

| key | panel | reason |
|---|---|---|
| `/org/gnome/Ptyxis/window-size` | apps | window geometry and UI position |
| `/org/gnome/shell/app-picker-layout` | - | recents, history and layouts the Shell regenerates |

### no-schema (1)

| key | panel | reason |
|---|---|---|
| `/org/example/ghost/level` | - | no installed schema describes this key |

### excluded-by-rule (1)

| key | panel | reason |
|---|---|---|
| `/org/gnome/shell/enabled-extensions` | shell-extensions | extension settings and enable lists belong to modules (modules/&lt;name>), not to panel files |

### unmapped (2)

| key | schema |
|---|---|
| `/org/example/app/mode` | org.example.App |
| `/org/example/weird/thing` | org.example.Weird |

## 5. Totals

totals: settings: 5 keys in 4 panels (2 shared, 3 machine; 5 new, 0 differ, 0 same), 2 unmapped, 5 skipped, 0 keyfile overrides, 1 at default, 13 user keys in scope; files: 1 to adopt, 0 not adopted, 1 candidates; packages: 6 new; system: 5 to write; secret findings in adopted items: 0

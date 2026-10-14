# ubuntu-tweaks

My personal Ubuntu (GNOME, Wayland) tweaks.

## Install

```sh
git clone https://github.com/alex-mextner/ubuntu-tweaks ~/ubuntu-tweaks
~/ubuntu-tweaks/install.sh
```

Then log out and back in. GNOME extensions are symlinked into
`~/.local/share/gnome-shell/extensions`, so the repo stays the source of truth.

## Tweaks

| Tweak | Path | What it does |
|---|---|---|
| English in Overview | `gnome-extensions/english-in-overview@ultra` | Switches the keyboard layout to `us` whenever the launcher (Super / Activities) opens. GNOME Shell 50. |
| memguard swap drain | `modules/memguard/` (root: `sudo modules/memguard/install.sh`, `--dry-run` shows the diff) | Folds stale swap back into RAM when RAM can take it: one device at a time, aborted if memory turns tight, started by mem-sentinel once things are calm or on demand with `memguard swap-drain` (no password). mem-sentinel stops reading a drain's 0 % SwapFree as full swap. Spec: [`docs/specs/memguard.md`](docs/specs/memguard.md) § Swap hygiene. |

## Uninstall a GNOME extension

```sh
gnome-extensions disable <uuid>
rm -r ~/.local/share/gnome-shell/extensions/<uuid>
```

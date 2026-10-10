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

## Uninstall a GNOME extension

```sh
gnome-extensions disable <uuid>
rm -r ~/.local/share/gnome-shell/extensions/<uuid>
```

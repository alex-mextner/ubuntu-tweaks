# ubuntu-tweaks

My personal Ubuntu (GNOME, Wayland) tweaks.

## Install

```sh
git clone https://github.com/alex-mextner/ubuntu-tweaks ~/xp/ubuntu-tweaks
~/xp/ubuntu-tweaks/install.sh
```

Then log out and back in. Repo extensions (including the dock submodule) are
symlinked into `~/.local/share/gnome-shell/extensions`, so the repo stays the
source of truth. Re-running `install.sh` is safe; it re-applies
`dconf/dock.ini` (change dock defaults there) but keeps your folder stacks.

## Tweaks

| Tweak | Path | What it does |
|---|---|---|
| English in Overview | `gnome-extensions/english-in-overview@ultra` | Switches the keyboard layout to `us` whenever the launcher (Super / Activities) opens. GNOME Shell 50. |
| macOS dock | `gnome-extensions/dash2dock-lite@icedman.github.com` (submodule), `dconf/dock.ini` | macOS 26-style dock replacing Ubuntu Dock: Liquid Glass with adaptive contrast, glass Trash showing its contents, stacks, badges, genie minimize, drag and drop. Spec: [`docs/specs/mac-dock.md`](docs/specs/mac-dock.md). |

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

Built on [Dash2Dock Animated](https://github.com/icedman/dash2dock-lite); the changes are on the
[`mac-tahoe` branch of the fork](https://github.com/alex-mextner/dash2dock-lite/tree/mac-tahoe).
Everything is a setting: open *Extensions → Dash2Dock Animated → Settings*.

- **Folder stacks:** *Others → Folder Stacks* adds any folder next to Downloads, or set
  it directly:
  `dconf write /org/gnome/shell/extensions/dash2dock-lite/folder-stacks "'[\"~/Projects\", {\"path\": \"~/Pictures/Screenshots\", \"icon\": \"folder-pictures\"}]'"`
- **Back to Ubuntu Dock:** `gnome-extensions disable dash2dock-lite@icedman.github.com`
  and remove `ubuntu-dock@ubuntu.com` from `/org/gnome/shell/disabled-extensions`.

## Uninstall a GNOME extension

```sh
gnome-extensions disable <uuid>
rm -r ~/.local/share/gnome-shell/extensions/<uuid>
```

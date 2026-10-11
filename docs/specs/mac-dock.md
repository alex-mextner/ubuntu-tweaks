# Spec: macOS-style dock

Source of truth for the dock tweak. Every user request is recorded verbatim under
*Requests*; *Requirements* is what they mean for the implementation. Change this file
in the same commit as any behavior change.

## Requests (verbatim, in order)

1. «а можешь сделать док на убунту тут как на маке максимально? мб найдешь чтото готовое?»
2. «док на ubuntu работает полностью как на маке но опенсорсный и гибкий. по умолчанию есть папки загрузки и скриншоты справа.»
3. «экспереминтеруй, сравнивай, делай скриншоты и скринкасты, найди маковские референсы, в идеале сделай самую соверменную версию с жидким стеклом»
4. «убедись что стекло будет оптимально реализовано и будет полностью отрисовываться на гпу без затраты больших ресурсов. все что я пишу фиксируй в задачах»
5. «и спеках»
6. «Публичный надо!» (Telegram, about this repo) — `ubuntu-tweaks` is public on GitHub.
7. «Сетка слева некрасивая. Должна подстраиваться для контрастности» (Telegram)
8. (Telegram, three paragraphs)
   «Сделай корзину похожую как в маке тоже красивую с полупрозрачностью и без лишнего

   И я бы хотела чтобы в корзине было видно что на самом деле там лежит то есть наполнение и миниатюры должны отображать количество и тип файлов

   А ещё убедись что перенёс все важные нюансы из мака и у бунту что как линуксовые приложения будут работать так как ожидают так и разные фишечки мака будут учтены»
9. «Скриншот тут бесполезный» (Telegram, about a status image) — status images show the
   whole dock at readable size, before/after side by side.
10. «Дивайдеры сползли. Должны дать центрированы по вертикали» (Telegram)
11. «Прямоугольные Оверлеи с файлами показываются не так как на маке. Перегораживают
    содержимое дока и не имеют уголка откуда растут» (Telegram)
12. «Обновленные скрины закинь в ридми потом» (Telegram)
14. «почини нормально и включи по умолчанию и на моей машине» (file drops,
    [#13](https://github.com/alex-mextner/ubuntu-tweaks/issues/13))

## Requirements

### Base and openness
- Built on an existing open-source dock: **Dash2Dock Animated** (GPL-3.0), chosen after a
  side-by-side comparison on GNOME Shell 50.1 (see *Decisions*). Changes live in the fork
  `alex-mextner/dash2dock-lite`, branch `mac-tahoe`, vendored here as a git submodule and
  written so they can go upstream.
- Flexible: every behavior below is a setting in the extension's preferences; the macOS
  look is only the preset in `dconf/dock.ini`.
- Replaces Ubuntu Dock (`ubuntu-dock@ubuntu.com` is disabled by `install.sh`).

### macOS 26 (Tahoe) behavior
- Bottom, centered, floating a few px above the screen edge, not full width.
- Order: apps (Apps grid icon first, then favorites) | separator | running non-favorite
  apps | separator | folder stacks | Trash last.
- Separators are centred vertically on the glass platter, at rest and while icons are
  magnified.
- Icon magnification on hover, bounce on launch, running-app dots, labels on hover.
- Minimize with the genie effect into the app's icon: the dock's own GPU effect
  (`minimize-to-icon`, `minimize-effect=1`).
- Clicking the focused app's icon does not minimize it (setting; upstream default kept).

### Folder stacks
- Downloads and Screenshots stacks on the right by default, left of Trash.
- Screenshots = `<XDG Pictures>/<localized "Screenshots">`, where GNOME saves them.
- Any folder can be a stack: setting `folder-stacks` (JSON array of `"path"` or
  `{"path", "title", "icon"}`), editable in Preferences → Others → Folder Stacks.
- A stack opens as a fan of the newest files (hidden files excluded); images show a
  scaled preview (freedesktop thumbnail if present). Folders update live.
- "More…"/the stack's own app open the folder itself.
- Grid/List view is a Liquid Glass popover above the dock (beside a side dock), never
  over the dock's icons, with a notch aimed at the stack's drawn icon. It grows out of
  the notch and shrinks back into it; it is clamped inside the screen (the notch slides
  along the edge, stopping at the corners). Magnification is off while it is open.

### Liquid Glass platter
- macOS 26 *Clear* glass: blur of what is behind the dock, edge refraction with slight
  colour fringing, bright 1 px top rim, fainter bottom rim, rounded rectangle
  (radius ≈ 0.25 × platter height), soft drop shadow, light tint from `background-color`.
- Backdrop: wallpaper (default) or live windows (`glass-backdrop`). The dock reserves
  screen space, so windows rarely sit under it and the wallpaper backdrop looks right.
- Rendered entirely on the GPU: Clutter clones + `Shell.BlurEffect` (ACTOR mode) + one
  GLSL pass. No CPU pixel readbacks, no per-frame JS allocation or actor rebuilds;
  geometry/uniforms are pushed only when they change; nothing renders while the dock is
  hidden.
- Measured gnome-shell CPU (headless 1920×1080, 10 s): idle 0.2–0.3 % in every mode;
  with a 30 fps video under the dock 4.1 % glass off, 4.4 % wallpaper backdrop, 6.8 %
  live-windows backdrop — hence wallpaper is the default.

### Adaptive contrast
- Separators and running marks follow wallpaper luminance (`adaptive-contrast`): dark
  on light wallpapers, light on dark ones.
- One threaded decode per wallpaper or colour-scheme change; nothing per frame.

### Trash
- Glass bin like macOS 26 (`trash-style=1`): translucent, no recycle logo.
- Fill level grows with the item count on a log scale; the newest 5 items show as
  thumbnails (images) or type cards.
- Redrawn only when the trash changes, by a separate process.

### macOS and Ubuntu parity
Checklist from the parity audit (macOS Dock + Ubuntu Dock behaviours), final status.

| # | Behaviour | Status | Setting / note |
|---|---|---|---|
| 1 | Drag a file onto Trash moves it to the trash | works | `file-drops` (on in the preset; a helper window takes the drop: Wayland drags never drop on Shell actors) |
| 2 | Drag a file onto an app opens it with that app | works | `file-drops` (see row 1) |
| 3 | Drag a file onto a stack moves/copies it | works | `file-drops` (see row 1) |
| 3a | Spring-loading: hover a dragged file over an app raises it | works | `spring-loading` |
| 4 | Drag an icon off the dock removes it (label + poof) | works | `drag-off-remove` |
| 5 | Keep in Dock (Pin/Unpin) | works | GNOME app menu |
| 6 | Options > Open at Login | works | `menu-open-at-login` |
| 7 | Show in Finder (→ Files) for apps | won't fix | stacks/Trash have it; an app's `.desktop` location is useless to users |
| 8 | Assign to Desktop | not done | GNOME has workspace pinning only via auto-move-windows; low value |
| 9 | Quit | works | GNOME app menu |
| 10 | Show All Windows (app Exposé) | works | `show-all-windows-item`, `click-action-spread` |
| 11 | Open window list / hover window thumbnails | works / not done | titles in the menu; thumbnails not ported, Show All Windows covers it |
| 12 | Press and hold opens the menu | works | inherited |
| 13 | .desktop quicklist actions | works | inherited |
| 14 | Launch bounce from anywhere | works | `open-app-animation`, `launch-bounce-anywhere` |
| 15 | Attention bounce for urgent windows, reveals hidden dock | works | `urgent-bounce` |
| 16 | Unity LauncherEntry badges | works | `launcher-api`; accepted spec risk: any session-bus client may update any app (as in Ubuntu Dock/Plasma). Unknown app ids are ignored, counts clamped to non-negative integers, updates rate-limited to one per 100 ms per app |
| 17 | Progress bar on the icon | works | `launcher-api`; badge style None hides count and progress |
| 18 | Red numbered notification badge | works | `notification-badge-style=6` |
| 19 | Hide toggle shortcut (⌥⌘D) | works | `toggle-autohide-shortcut` = Super+Alt+D |
| 20 | Dock background menu (hiding, magnification, position, settings) | works | `background-menu` |
| 21 | Recent apps section | works | `recent-apps-count=3` |
| 22 | Minimize into the app icon | works | `minimize-to-icon` |
| 23 | Genie minimize effect | works | `minimize-effect=1` (own GPU effect) |
| 24 | Minimized windows as tiles before Trash | works | `minimized-window-tiles` (off in the preset) |
| 25 | Magnification | configurable | `animate-icons`, `animation-magnify` |
| 26 | Size, position, auto-hide | configurable | `icon-size`, `dock-location`, `autohide-dash` |
| 27 | Open-app indicators | configurable | `running-indicator-style` |
| 28 | Stacks: Fan | works | folder stacks + Downloads |
| 29 | Stacks: Grid / List, Display as Folder/Stack | works | per-stack `view`/`display`; `downloads-stack-view/-display` |
| 30 | Stacks: Sort by | works | per-stack `sort`; `downloads-stack-sort` |
| 31 | Stack right-click menu | works | Sort by, Display as, View as, Open |
| 32 | Empty Trash | works | with confirmation |
| 33 | Trash full/empty state | works | glass bin shows contents (`trash-style=1`) |
| 34 | Click raises / launches | works | inherited |
| 35 | Click on focused app minimizes | configurable | `click-minimizes` (off, as macOS) |
| 36 | Middle/Ctrl+click opens a new window | works | — |
| 37 | Shift+click opens a new window | works | `shift-click-action=1` |
| 38 | Scroll cycles the app's windows | works | `scroll-sensitivity` |
| 39 | Super+1..9 / Super+Ctrl+N | works | GNOME-native |
| 40 | Number overlay while holding Super | works | `hotkeys-overlay` |
| 41 | Keyboard focus into the dock (Ctrl+Alt+Tab) | works | `keyboard-focus` |
| 42 | Show Apps button | configurable | `apps-icon`, `apps-icon-front` |
| 43 | AppIndicators untouched | works | — |
| 44 | Favourites synced with GNOME `favorite-apps` | works | — |
| 45 | Flatpak/Snap/AppImage/Electron grouping | works | inherited `Shell.WindowTracker` |
| 46 | Multi-monitor | configurable | `multi-monitor-preference`, `preferred-monitor` |
| 47 | Isolate workspaces | works | `isolate-workspaces` (off in the preset) |
| 48 | Mounted volumes / Trash | configurable | `mounted-icon`, `trash-icon` |
| 49 | Hide over fullscreen windows | works | — |
| 50 | Dock survives an unavailable inotify monitor | works | falls back to polling |

### Evidence
- Reference images of the Tahoe dock, side-by-side screenshots of candidates, and
  screenshots + a screencast of the result, produced in an isolated headless GNOME
  Shell 50.1 (1920×1080): `docs/dock/dock.png` (whole dock), `compare-tahoe.png` (Tahoe
  reference above this dock, same width), `stack.png` (Screenshots grid popover with its
  notch), `trash.png` (glass Trash empty / 4 / 36 items), `demo.gif` (magnification,
  stack open/close, genie minimize and restore).
- Final regression (fork `mac-tahoe`, mac + full preset): separators 0.3 px off the
  platter centre at rest and magnified; Grid and List popovers centred on the icon
  (x 1173 vs 1172) and ending 16 px above the platter; `dock-location` 0→1→2→0 twice,
  live, no hang, no recursion errors; badge count 3 + progress pill drawn; a file dragged
  from Files onto Trash lands in the trash; idle gnome-shell CPU 0.10 % over 20 s;
  with a 30 fps video under the dock 3.85 %.
- File drops ([#13](https://github.com/alex-mextner/ubuntu-tweaks/issues/13)), 30
  synthetic drags from Files: 10 begun right after a helper restart (5 of them before the
  helper was ready), drops onto Trash, Text Editor and the Screenshots stack, Esc, release
  off the dock, a refused drop, a 1.5 s pause. The drop window leaves input 10–29 ms
  after the release; before the fix it stayed 406–427 ms, until the helper unmapped it,
  after every drag not cancelled with Esc. The helper unmaps it within 430 ms, and the
  next click on a dock icon activates the app every time. With Meta.Dnd's leave blocked,
  the watchdog alone ends the drag 264 ms after the release, the helper's leave alone
  10 ms. A buttonless drag resting 1.2 s on Trash still drops. Shell exit mid-drag: 5/5
  clean.

## Decisions

| Candidate (GNOME 50) | Result in headless GNOME Shell 50.1 |
|---|---|
| Dash2Dock Animated | Works: magnification, bounce, Downloads stack, Trash; 845k installs, active upstream |
| MacOS Dock (vinnytherobot) | Dock never appears: `magnification.js:98` touches a disposed `St.BoxLayout` |
| Ubuntu Dock + Magnific Launcher | Left panel only; no stacks or glass |
| Cupertino Dock Lite | Theming layer for Dash to Dock, no magnification |

- Glass inside the dock uses ACTOR-mode blur on its own backdrop: BACKGROUND-mode blur
  draws nothing inside the dock's offscreen-redirected actor and has square corners on
  GNOME 50; a clone of `global.window_group` is 0×0, so its children are cloned instead.
- Magic Lamp dropped: `Clutter.DeformEffect` paints nothing on Mutter 50; the dock has its
  own GPU genie (`minimize-effect=1`). `install.sh` disables the lamp if enabled.
- Live-windows glass backdrop is optional (`glass-backdrop`); wallpaper is cheaper.
- `minimized-window-tiles` off in the preset: minimize goes into the app icon (macOS
  option "Minimize windows into application icon").
- `click-action-spread` off in the preset: macOS does nothing on clicking the active app.
- Shift+click opens a new window (Ubuntu behaviour); macOS has no shift-click.
- `isolate-workspaces` off: Ubuntu-only behaviour.
- `file-drops` on in the preset. The Shell owns the drop window's input: the layer
  holding it is visible only while a drag is live and hidden at whichever drag end comes
  first (Meta.Dnd's leave, the helper's drop, or, once a pointer drag's button is up, the
  helper's leave or a 250 ms watchdog), so the window cannot take a dock click after a
  drag, however late the helper unmaps it. The helper reports ready before it gets any
  "show" ([#13](https://github.com/alex-mextner/ubuntu-tweaks/issues/13)). A touch drag
  ends only on Meta.Dnd's leave or its drop: a silence timeout would lose the drop of a
  finger resting on its target. The shell crash on exit while the drop window was up
  ([#12](https://github.com/alex-mextner/ubuntu-tweaks/issues/12)) is fixed: the file-drop
  layer hands window actors back to `window_group` when it is destroyed.

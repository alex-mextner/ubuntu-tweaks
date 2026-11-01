# Thumbnailers (`thumbnailers`)

Part of the `ubuntu-tweak` CLI ([docs/specs/ubuntu-tweak-cli.md](ubuntu-tweak-cli.md), *Init pipeline*, step 8c).

User request of 1 Nov ("сделай в системе тумбнейлы для web-dev и 3D ассоциированных типов файлов", ticket #166): Files
(Nautilus), the file chooser and the freedesktop cache `~/.cache/thumbnails` (which the dock's Downloads and Screenshots
stacks read too) show a picture for STL, OBJ, 3MF, PLY, glTF/GLB, FBX, Collada, 3DS, OFF, AMF, X3D/VRML, STEP, IGES, G-code,
`.bgcode`, Blender and FreeCAD files, and for the files of the web stack (TS, TSX, JSX, JS, CSS/SCSS/Less, JSON, YAML, TOML,
Markdown/MDX, Vue, Svelte, Astro, PHP, GraphQL, SQL, Dockerfile, `.env`, shell, XML, OpenSCAD, Python, Rust, Go),
HTML, Jupyter notebooks and WOFF2 fonts. `kind = "system"`, `root = false`, order 86, on by default; `[modules.thumbnailers]
enabled = false` switches it off. `[deps] apt = python3-pil python3-pygments shared-mime-info gnome-font-viewer`: all are on
a stock Ubuntu desktop, and `doctor` reports them otherwise.

**The constraint that shapes it.** libgnome-desktop (44.x, `gnome-desktop-thumbnail-script.c`) runs every thumbnailer in
a bubblewrap sandbox that binds `/usr`, `/etc/ld.so.cache`, `/etc/alternatives`, a private `/tmp` for the output and the one
input file read-only as `/tmp/gnome-desktop-file-to-thumbnail.<extension>` (no extension for `Dockerfile`, `.env`), and nothing else:
`$HOME`, `~/.local/bin`, AppImages and venvs do not exist inside it, and there is no GPU (`/dev` is minimal). So f3d, stl-thumb,
`blender-thumbnailer` or FreeCAD under `~/.local/opt` can never run from Files; installing them would need root into `/usr`.
The module instead ships **one pure-Python package, `uthumb`** (`modules/thumbnailers/uthumb/`), for the system `/usr/bin/python3`
with Pillow and Pygments, which the sandbox does see. The package is deflated, base64-encoded and put in the `Exec=` line of
`~/.local/share/thumbnailers/ubuntu-tweaks-previews.thumbnailer` (about 60 KiB; `uthumb/bundle.py`); the line writes it to the
sandbox's `/tmp`, imports it with `zipimport` and deletes it at exit. `%i %o %s %m` carry input, output, size and MIME type
(`%m` is how extension-less files such as `Dockerfile` are routed). `~/.local/share/thumbnailers/ubuntu-tweaks-woff2.thumbnailer`
lists `font/woff2` for gnome-font-viewer's own `gnome-thumbnail-font`, which renders WOFF2 but whose entry omits the type.

**What each type gets** (`uthumb/registry.py` is the list):

| Files | Preview |
|---|---|
| STL, OBJ, PLY, OFF, AMF, glTF/GLB, Collada, X3D/VRML, 3DS, FBX | parsed in Python, drawn by a software rasteriser (`raster.py`): three-quarter orthographic view, smooth shading with a 25 degree crease angle, painter's order, anti-aliased on a studio gradient, vertex/material colours kept; at most 90 000 triangles are drawn (the rest is sampled, the sampled ones enlarged so no holes show); point-cloud PLY as dots |
| 3MF | the slicer's own picture (`_rels/.rels` thumbnail relationship, `Metadata/plate_1.png`, `thumbnail.png`) else the meshes with build transforms and components |
| G-code | the largest embedded `; thumbnail begin WxH N` block (PNG/JPG/QOI: Orca, Snapmaker, Prusa, Cura), else the extruding moves drawn in 3D coloured by height |
| `.bgcode` | the binary format's thumbnail block |
| Blender | the preview Blender stores (`TEST` block; plain, gzip and zstd files; old and `BLENDER17-01` headers) |
| FreeCAD | `thumbnails/Thumbnail.png` of the `.FCStd` zip |
| STEP, IGES | wireframe of the B-rep edges (lines, circle and ellipse arcs, B-spline control polygons): no CAD kernel exists in the sandbox, so surfaces are not tessellated |
| TS, TSX, CSS, JSON, YAML, Vue, Dockerfile, ... | a dark window with traffic-light dots and the first ~30 lines highlighted by Pygments (`one-dark`), the file type as a badge |
| HTML | a static page card: `<title>` (else `<h1>`), meta description, an `HTML` badge; a page with neither is shown as highlighted source. Rendering the page itself would need a browser engine |
| `.ipynb` | the first cells as one highlighted listing, markdown cells as comments |

Not covered, by design or for lack of a way: `.gltf` with external `.bin` buffers (outside the sandbox), Draco/meshopt glTF,
STEP/IGES surfaces and assemblies' placement, IGES transformation matrices, textures (the colour is the material's base colour), Fusion
`.f3d`, SolidWorks/Inventor/SketchUp files (proprietary, no open reader), Lottie animation frames (shown as JSON), SVG/WebP/AVIF/JXL/ICO/HEIC
(already handled by the system's librsvg, glycin and heif thumbnailers). Files whose rendering fails (a corrupt file, an empty one) keep
Files' generic icon.

**MIME types.** shared-mime-info 2.4 lacks STEP, PLY, OFF, AMF, Collada, X3D, FBX, `.bgcode`, `.gco`, FreeCAD, Vue, Svelte, Astro, Less, GraphQL,
JSONC, Dockerfile and `.env`, and maps `*.ts` to a Qt translation file, `*.tsx` to a Tiled tileset, `*.3ds` to a Nintendo ROM and `*.mdx` to a
Sega ROM. `uthumb/mimedb.py` writes `~/.local/share/mime/packages/ubuntu-tweaks-previews.xml` (glob weight 60 where a system glob must lose;
text types are subclasses of `text/plain`, so editors still open them) and the module runs `update-mime-database ~/.local/share/mime`
when that file changes. A `.ts` file that is really XML (a Qt translation), or a `.tsx` Tiled tileset, is detected by its content and shown
as XML.

**Idempotent and read-only to check.** `check` compares the three files byte for byte with what the repository would write (the zip is
deterministic) and that `globs2` of the compiled database knows every defined type; a changed `uthumb` source or a zlib upgrade therefore
shows as drift and `apply` rewrites the entry. `install` also deletes the failure markers (`~/.cache/thumbnails/fail/*/*.png`, found by the
`Thumb::URI` text chunk) of files this module can now preview, because Files never retries a failed thumbnail on its own; markers of other
files and the thumbnails themselves are left alone. Nothing needs a restart: the factory re-reads the thumbnailer directories on each run,
so folders already open show the pictures after a reload (F5) or on the next visit. The code is not run by `apply`; it runs only inside the
sandbox, one process per file, bounded in input size (96 MiB text, 256 MiB binary) and in triangles.

**Filling the cache outside Files.** The dock (and anything else that only reads `~/.cache/thumbnails`) sees an entry only after Files
browsed the folder. `modules/thumbnailers/uthumb-cache FILE...` writes the same entries with the same renderers, from outside the sandbox
(so also from a user unit): `large` (256 px) PNGs named by the MD5 of the file URI, with `Thumb::URI`, `Thumb::MTime` and `Thumb::Size`
chunks as the Thumbnail Managing Standard says (GIO's `thumbnail::path` resolves them); a current entry is kept, a file that changes while it
is drawn gets none, a file that cannot be drawn is skipped and the batch goes on. Wiring it into the dock's stacks is ticket #172.

**Tests.** `modules/thumbnailers/tests/`: every format's minimal valid file (built in memory) must render a rounded card; garbage must exit 1,
never crash; a 150 000-triangle file and a 60 000-triangle fan around one vertex stay fast; the shipped `Exec=` code runs inside `bwrap` with only
`/usr` and the input visible and leaves no file behind (skipped where bubblewrap cannot start); hostile inputs (compressed bombs in FBX and 3MF and Blender headers, glTF
node cycles, 100-million-triangle component trees, pathological HTML) are refused or bounded in time; the module against a scratch home (writes only
what differs, forgets stale failures, is idempotent, reports missing Pillow/Pygments as a manual step).

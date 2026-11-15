"""usr-local adapter: configuration somebody installed by hand under ``/usr/local`` and ``/opt`` (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``.

Assumptions:

- Items are ``etc:/usr/local/...`` and ``etc:/opt/...``: the ``etc:`` scheme of the machine-wide configuration, with the
  path as it is on the machine, so the rules of a module (``etc:/usr/local/share/wireplumber/...``) read like the paths
  they claim. Nothing under these trees is dpkg's, so what a package owns is left out (one ``dpkg-query -S`` for every
  candidate; without dpkg everything found is listed).
- **Config areas, one item per file** (``_FILE_AREAS``, below ``/usr/local``): ``etc``, the ``*.d`` drop-in directories
  of ``share/<program>``, D-Bus services and policy, polkit rules, xdg-desktop-portal portals, desktop entries, GLib
  schemas, local CA certificates, systemd units, udev rules, the plugin directories of the PipeWire and WirePlumber
  processes (the ``libxdg-desktop-portal.so`` that kept wireplumber from starting) and browser native-messaging hosts. A
  file or link there changes what runs or how it behaves, which is what a profile is for.
- **Beside the files of an area**: a tree an area listed files from is not one item; what sits next to those files
  (a main ``wireplumber.conf``, a ``foo.service.d`` directory, a link) is, except the data trees. No link is followed
  while the areas are walked (``_Areas``: ``os.scandir``, never ``Path.glob``, which follows a link at a ``*``).
- **Tree items, one per top-level entry** that no area above listed from: ``/opt/*``, and the entries of
  ``/usr/local/share`` and ``/usr/local/lib`` outside the data trees (``_DATA_TREES``: locale, man, icons, ...), the
  multiarch library directories (``*-linux-gnu``) and the Python and pkg-config trees. ``detail`` carries the number of
  files; an empty directory is not an item. A directory this user cannot list is one item whose ``detail`` is
  ``needs root to read: sudo ls -lR <dir>``.
- Binaries and libraries (``bin``, ``sbin``, ``libexec``, ``include``, ``lib/<triplet>/lib*``, ``lib/lib*.so*``) are software, not
  configuration: the package adapters and the modules that install them are where they belong, and listing every
  header of a hand-built library would only bury the findings. Everything is read below ``System.root``; nothing is
  written, no file is opened.

Public API: ``NAME``, ``discover(ctx)``.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item

NAME = "usr-local"
_LOCAL = "/usr/local"
_OPT = "/opt"
_UNREADABLE = "needs root to read"
_GLOB_CHARACTERS = "*?[\\"

# Files below /usr/local that are configuration, a unit, a rule or a plugin loaded by a process: one item each.
_FILE_AREAS = (
    "etc/**/*",
    "share/*/*.d/*",
    "share/dbus-1/services/*",
    "share/dbus-1/system-services/*",
    "share/xdg-desktop-portal/portals/*",
    "share/applications/*",
    "share/glib-2.0/schemas/*",
    "share/ca-certificates/**/*",
    "lib/systemd/*/*",
    "lib/udev/rules.d/*",
    "lib/*/wireplumber-0.5/*",
    "lib/*/pipewire-0.3/*",
    "lib/*/mozilla/native-messaging-hosts/*",
)
# Top-level entries of these directories are tree items, unless they are plain data or libraries.
_TREE_PARENTS = ("share", "lib")
_DATA_TREES = frozenset(
    {
        "locale", "man", "doc", "info", "icons", "fonts", "themes", "sounds", "help", "licenses", "mime", "metainfo",
        "sgml", "xml", "zsh", "interfaces", "bash-completion", "pkgconfig", "aclocal", "cmake", "gir-1.0", "vala", "gtk-doc",
    }
)  # fmt: skip
_MULTIARCH = "*-linux-gnu"
_LIBRARY_TREES = (_MULTIARCH, "python3*", "pkgconfig", "cmake", "node_modules")
# What `./configure && make install` and CMake leave straight in /usr/local/lib: libraries, not configuration.
_LIBRARY_FILES = ("lib*.so", "lib*.so.*", "lib*.a", "lib*.la")
_QUERY_BYTES = 100_000  # one dpkg-query command line stays well below ARG_MAX and the 128 KiB limit of one argument
# `dpkg-query -S` prints these for a diverted path instead of `package: path`.
_DIVERSION_LINES = ("diversion by ", "local diversion ")


def _entry(path: Path, identifier: str, files: int | None = None, error: bool = False) -> Item | None:
    """The item for ``path``; None when it vanished since it was listed (an installer ran during the scan)."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        return Item(NAME, f"etc:{identifier}", "file", f"{_UNREADABLE}: sudo ls -ld {identifier}")
    if os.path.islink(path):
        try:
            return Item(NAME, f"etc:{identifier}", "link", f"hand-installed link -> {os.readlink(path)}")
        except FileNotFoundError:
            return None
        except OSError:
            pass  # replaced by a file or directory since: described as what it is now
    if _real_directory_at(path):
        detail = f"{_UNREADABLE}: sudo ls -lR {identifier}" if error else f"hand-installed directory, {files} files"
        return Item(NAME, f"etc:{identifier}", "dir", detail)
    return Item(NAME, f"etc:{identifier}", "file", "hand-installed file")


def _files_below(directory: Path) -> tuple[int, bool]:
    """How many files and links ``directory`` holds, and whether a subdirectory could not be listed."""
    count = 0
    unreadable = False

    def failed(_: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    for walked, directories, names in os.walk(directory, onerror=failed):
        count += len(names) + sum(os.path.islink(os.path.join(walked, name)) for name in directories)
    return count, unreadable


def _real_directory(entry: os.DirEntry[str]) -> bool:
    return entry.is_dir(follow_symlinks=False)


def _real_directory_at(path: Path) -> bool:
    """Whether ``path`` is a directory itself, not a link to one; False when it cannot be looked at."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _library_name(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in (*_LIBRARY_FILES, *_LIBRARY_TREES))


def _library_link(name: str) -> bool:
    """A link that is data or a library, not a stand-in for a config directory (a linked multiarch directory is one)."""
    trees = (pattern for pattern in _LIBRARY_TREES if pattern != _MULTIARCH)
    return name in _DATA_TREES or any(fnmatch.fnmatchcase(name, pattern) for pattern in (*_LIBRARY_FILES, *trees))


def _askable(path: str) -> bool:
    """Whether dpkg-query can be asked about ``path``: not a pattern, and a name its UTF-8 output can carry."""
    if any(character in path for character in _GLOB_CHARACTERS):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class _Areas:
    """The files the config areas list, and the directories they could not read (one walk, no link followed)."""

    def __init__(self, root: Path) -> None:
        self.files: dict[Path, None] = {}
        self.unreadable: dict[Path, None] = {}
        for pattern in _FILE_AREAS:
            self._match(root, pattern.split("/"))

    def entries(self, directory: Path) -> list[os.DirEntry[str]]:
        if os.path.islink(directory):
            return []  # never listed through a link; the caller reports the link itself
        try:
            with os.scandir(directory) as scanned:
                return sorted(scanned, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError:
            self.unreadable[directory] = None
            return []

    def _match(self, root: Path, pattern: list[str]) -> None:
        pending = [(root, pattern)]
        while pending:  # a stack, not recursion: a directory tree can be deeper than the interpreter's limit
            directory, parts = pending.pop()
            head, rest = parts[0], parts[1:]
            entries = self.entries(directory)
            if head == "**":
                pending.append((directory, rest))
                pending.extend((Path(entry.path), parts) for entry in entries if _real_directory(entry))
                continue
            for entry in entries:
                if not fnmatch.fnmatchcase(entry.name, head):
                    continue
                if rest and _real_directory(entry):
                    pending.append((Path(entry.path), rest))
                elif rest and entry.is_symlink() and not _library_link(entry.name):
                    self.files[Path(entry.path)] = None  # a link where a directory should be: the link is the finding
                elif not rest and not _real_directory(entry):
                    self.files[Path(entry.path)] = None


def _plain_data_or_library(parent: str, entry: os.DirEntry[str]) -> bool:
    if entry.name in _DATA_TREES:
        return True
    if parent == "share":
        return False
    if not _real_directory(entry):
        return _library_name(entry.name)
    return any(fnmatch.fnmatchcase(entry.name, pattern) for pattern in _LIBRARY_TREES)


def _data_tree(name: str) -> bool:
    return name in _DATA_TREES


def _library_or_data(name: str) -> bool:
    return name in _DATA_TREES or _library_name(name)


def _unlisted_below(
    areas: _Areas, directory: Path, ancestors: set[Path], skipped: Callable[[str], bool] | None
) -> Iterator[Path]:
    """What a directory that holds listed files also holds: a sibling config, a drop-in directory, a link.

    ``skipped`` names what is not configuration straight in that directory (the data trees; the libraries of a
    multiarch directory); it applies to that level only.
    """
    pending = [(directory, skipped)]
    while pending:  # a stack, not recursion: a directory tree can be deeper than the interpreter's limit
        current, skip = pending.pop()
        for entry in areas.entries(current):
            path = Path(entry.path)
            if path in areas.files or (skip is not None and skip(entry.name)):
                continue
            if path in ancestors and _real_directory(entry):
                pending.append((path, None))
            else:
                yield path


def _tree_entries(root: Path, areas: _Areas) -> Iterator[Path]:
    """The top-level entries of ``share`` and ``lib`` that are not plain data or libraries.

    One an area listed files from is not an item itself: the files are, and so is whatever else sits next to them.
    """
    ancestors = {parent for path in (*areas.files, *areas.unreadable) for parent in path.parents}
    for parent in _TREE_PARENTS:
        if os.path.islink(root / parent):
            yield root / parent
            continue
        for entry in areas.entries(root / parent):
            path = Path(entry.path)
            library = _plain_data_or_library(parent, entry)
            if path in ancestors and _real_directory(entry):
                yield from _unlisted_below(areas, path, ancestors, _library_or_data if library else _data_tree)
            elif not library:
                yield path


def _owned_by_packages(paths: Iterable[str]) -> set[str]:
    """The paths a package owns (``dpkg-query -S``; it exits 1 when one is not found, the found ones are still on stdout)."""
    queried = sorted(path for path in paths if _askable(path))
    owned: set[str] = set()
    for batch in _batches(queried):
        result = system().process.query(["dpkg-query", "-S", "--", *batch], timeout=60.0)
        for line in result.stdout.splitlines():
            if line.startswith(_DIVERSION_LINES):
                continue  # a diversion names a path, not the package that owns it
            _, separator, path = line.partition(": ")
            if separator:
                owned.add(path)
    return owned


def _batches(paths: list[str]) -> Iterator[list[str]]:
    """``paths`` in runs whose arguments fit the kernel's argument limit (``execve`` fails with E2BIG above it)."""
    batch: list[str] = []
    size = 0
    for path in paths:
        length = len(path.encode("utf-8")) + 1
        if batch and size + length > _QUERY_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(path)
        size += length
    if batch:
        yield batch


def discover(ctx: Context) -> Iterable[Item]:
    root = system().root
    local = root / _LOCAL.lstrip("/")
    areas = _Areas(local)
    entries = [local] if os.path.islink(local) else [*areas.files, *_tree_entries(local, areas)]
    opt = root / _OPT.lstrip("/")
    if os.path.islink(opt):
        entries.append(opt)
    else:
        entries += [Path(entry.path) for entry in areas.entries(opt)]
    entries += list(areas.unreadable)
    candidates = {"/" + str(path.relative_to(root)): path for path in entries}
    owned = _owned_by_packages(candidates)  # before walking a tree: a package's directory may not be listable
    items = []
    for identifier, path in candidates.items():
        if identifier in owned:
            continue
        if _real_directory_at(path):
            count, unreadable = _files_below(path)
            item = _entry(path, identifier, count, unreadable) if count or unreadable else None
        else:
            item = _entry(path, identifier)
        if item is not None:
            items.append(item)
    return sorted(items, key=lambda item: item.id)

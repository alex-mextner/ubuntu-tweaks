"""packages adapter: what was installed on purpose, per package manager (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``, ``capture``.

Assumptions:

- Items are ``<manager>:<name>``: ``apt`` (``apt-mark showmanual``), ``snap``, ``flatpak`` (applications only),
  ``brew`` (``brew leaves``: what was asked for, not its dependencies), ``pipx``, ``uv`` (tools), ``npm`` and ``bun``
  (global packages), ``cargo`` (``cargo install --list``). Every manager is asked through ``Process.query``; one
  that is not installed (exit 127) or fails yields no items: a machine without ``snap`` has no snaps.
- ``owned`` is what the profile declares: the names in ``packages.toml`` (shared + machine overlay) and, for every
  enabled module, its ``[install] apt`` (and the ``provides`` names) and ``[deps] apt``.
- ``capture`` proposes the ``packages.toml`` text for the untracked ``apt``, ``snap``, ``flatpak`` and ``brew`` items
  (the managers ``packages.toml`` has a key for); the other managers have no place in the profile to land in.

Public API: ``NAME``, ``MANAGERS``, ``discover(ctx)``, ``owned(ctx)``, ``capture(ctx, items)``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence

from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Item, Proposal
from ubuntu_tweaks.manifest import module_defaults
from ubuntu_tweaks.process import Process

NAME = "packages"
_TIMEOUT = 30.0

type Parser = Callable[[str], list[str]]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _snap(text: str) -> list[str]:
    return [line.split()[0] for line in text.splitlines()[1:] if line.split()]


def _first_word(text: str) -> list[str]:
    return [words[0] for line in text.splitlines() if (words := line.split())]


def _uv(text: str) -> list[str]:
    return [line.split()[0] for line in text.splitlines() if line and not line[0].isspace() and line[0] != "-"]


_NODE_MODULES = "node_modules/"


def _npm(text: str) -> list[str]:
    return [line.partition(_NODE_MODULES)[2] for line in _lines(text) if _NODE_MODULES in line]


_BUN = re.compile(r"^\s*[├└]──\s+(@?[^@\s]+)@\S+\s*$")


def _bun(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := _BUN.match(line))]


_CARGO = re.compile(r"^(\S+) v\S+.*:\s*$")


def _cargo(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := _CARGO.match(line))]


MANAGERS: Mapping[str, tuple[tuple[str, ...], Parser]] = {
    "apt": (("apt-mark", "showmanual"), _lines),
    "snap": (("snap", "list"), _snap),
    "flatpak": (("flatpak", "list", "--app", "--columns=application"), _lines),
    "brew": (("brew", "leaves"), _lines),
    "pipx": (("pipx", "list", "--short"), _first_word),
    "uv": (("uv", "tool", "list"), _uv),
    "npm": (("npm", "ls", "-g", "--depth=0", "--parseable"), _npm),
    "bun": (("bun", "pm", "ls", "-g"), _bun),
    "cargo": (("cargo", "install", "--list"), _cargo),
}


def _listed(process: Process, manager: str) -> list[str]:
    argv, parse = MANAGERS[manager]
    result = process.query(argv, timeout=_TIMEOUT)
    return parse(result.stdout) if result.ok else []


def discover(ctx: Context) -> Iterable[Item]:
    process = system().process
    for manager in MANAGERS:
        for name in sorted(set(_listed(process, manager))):
            yield Item(NAME, f"{manager}:{name}", "package", f"installed with {manager}")


def owned(ctx: Context) -> Mapping[str, str]:
    found: dict[str, str] = {}
    packages = ctx.profile.load_packages()
    for manager, names in (
        ("apt", packages.apt),
        ("snap", packages.snap),
        ("flatpak", packages.flatpak),
        ("brew", packages.brew),
    ):
        for name in names:
            found.setdefault(f"{manager}:{name}", "packages.toml")
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    for manifest in manifests:
        if not config.module_enabled(manifest.name):
            continue
        names = (*manifest.install.apt, *(name for name, _ in manifest.install.provides), *manifest.deps.apt)
        for name in names:
            found.setdefault(f"apt:{name}", f"module {manifest.name}")
    return found


def capture(ctx: Context, items: Sequence[Item]) -> Iterable[Proposal]:
    names: dict[str, list[str]] = defaultdict(list)
    members: list[Item] = []
    for item in items:
        manager, _, name = item.id.partition(":")
        if manager in ("apt", "snap", "flatpak", "brew"):
            names[manager].append(name)
            members.append(item)
    if not members:
        return
    blocks = [
        f"{manager} = [\n" + "".join(f'  "{name}",\n' for name in sorted(names[manager])) + "]\n"
        for manager in sorted(names)
    ]
    yield Proposal("packages.toml", "\n".join(blocks), tuple(members))

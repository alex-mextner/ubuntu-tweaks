"""Planner: initialize the git submodule behind every enabled ``submodule`` module.

Accessed via: planner discovery (``planners/__init__.py``).

Assumptions: a submodule is initialized when ``<path>/.git`` exists. A submodule checked out at
another commit than the umbrella records (dev work) is satisfied, never reset, and is reported
by ``extras()``; only ``update`` moves it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import extension_modules
from ubuntu_tweaks.manifest import SourceKind
from ubuntu_tweaks.proc import CommandError, run
from ubuntu_tweaks.settings import SettingsBackend


class SubmoduleInit(Action):
    phase = Phase.SUBMODULE
    kind = "submodule"

    def __init__(self, repo_root: Path, path: str, module: str) -> None:
        self.repo_root = repo_root
        self.path = path
        self.scope = module

    def describe(self) -> str:
        return f"init submodule {self.path} (git submodule update --init --recursive)"

    def check(self) -> bool:
        return (self.repo_root / self.path / ".git").exists()

    def run(self) -> None:
        run(["git", "-C", str(self.repo_root), "submodule", "update", "--init", "--recursive", "--", self.path])


def _submodule_paths(ctx: Context) -> list[tuple[str, str]]:
    return [
        (module.name, module.manifest.source.path)
        for module in extension_modules(ctx)
        if module.manifest.source.kind is SourceKind.SUBMODULE and module.manifest.source.path is not None
    ]


def build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]:
    return [SubmoduleInit(ctx.repo_root, path, name) for name, path in _submodule_paths(ctx)]


def extras(ctx: Context, backend: SettingsBackend) -> Sequence[str]:
    initialized = [path for _, path in _submodule_paths(ctx) if (ctx.repo_root / path / ".git").exists()]
    if not initialized:
        return []
    try:
        listing = run(["git", "-C", str(ctx.repo_root), "submodule", "status", "--", *initialized], capture=True)
    except CommandError as error:
        return [f"cannot inspect submodules: {error}"]
    lines: list[str] = []
    for line in listing.splitlines():
        if line.startswith("+"):  # `git submodule status`: '+' = checked out at another commit
            sha, path = line[1:].split()[:2]
            lines.append(
                f"submodule {path} is at {sha[:12]}, not the commit the umbrella records "
                "(dev work: apply never resets it, only `ubuntu-tweak update` moves it)"
            )
    return lines

"""Planner: initialize the git submodule behind every enabled ``submodule`` module.

Accessed via: planner discovery (``planners/__init__.py``).

Assumptions: a submodule is initialized when ``<path>/.git`` exists. A submodule checked out at
another commit than the umbrella records (dev work) is satisfied, never reset, and is reported
by ``extras()``; only ``update`` moves it. In a tree without ``.git`` (an unpacked archive) nothing can
be initialized: a non-empty ``<path>`` counts as present, an empty one is a manual step.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, ManualStepRequired, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import extension_modules
from ubuntu_tweaks.manifest import SourceKind
from ubuntu_tweaks.process import CommandError, run
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

    def _is_git_checkout(self) -> bool:
        return (self.repo_root / ".git").exists()

    def check(self) -> bool:
        if (self.repo_root / self.path / ".git").exists():
            return True
        if self._is_git_checkout():
            return False
        # an unpacked archive has no git to ask: the sources are either shipped in the tree or not
        directory = self.repo_root / self.path
        return directory.is_dir() and any(directory.iterdir())

    def run(self) -> None:
        if not self._is_git_checkout():
            raise ManualStepRequired(
                f"{self.path} is empty and {self.repo_root} is not a git checkout: use a release that ships it, "
                "or clone with `git clone --recurse-submodules`"
            )
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

"""What every command receives besides its parsed arguments.

Accessed via: ``cli.main`` builds one ``Context`` from the global flags and passes it to the
handler a command registered with ``parser.set_defaults(handler=run)``; the handler signature
is ``run(ctx: Context, args: argparse.Namespace) -> int | None`` (``None`` means exit 0).

Public API:

- ``Context(repo_root, profile, only)``: ``repo_root`` is the umbrella checkout, ``profile`` a
  ``Profile`` (already resolved from ``--profile`` / ``--machine``), ``only`` the frozenset
  from ``--only a,b`` (empty = everything); ``modules_dir`` is ``repo_root / "modules"``;
  ``manifests()`` is every module, the profile's own included (``manifest.discover_modules``);
  ``selects(name)`` is True when ``only`` is empty or contains ``name``;
  ``require_modules(known)`` raises ``UnknownModuleError`` for a ``--only`` name not in ``known``.
- ``REPO_ROOT``: the checkout this package runs from.
"""

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.errors import UnknownModuleError
from ubuntu_tweaks.manifest import Manifest, discover_modules
from ubuntu_tweaks.profile import Profile

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Context:
    repo_root: Path
    profile: Profile
    only: frozenset[str] = frozenset()

    @property
    def modules_dir(self) -> Path:
        return self.repo_root / "modules"

    def manifests(self) -> tuple[Manifest, ...]:
        """Every module: the engine's ``modules/`` plus the profile's own (a profile module overrides an engine
        module of the same name)."""
        return discover_modules(self.modules_dir, self.profile)

    def selects(self, name: str) -> bool:
        return not self.only or name in self.only

    def require_modules(self, known: Collection[str]) -> None:
        """Raise ``UnknownModuleError`` (exit 4) for a ``--only`` name that is not in ``known``."""
        unknown = sorted(self.only - set(known))
        if unknown:
            raise UnknownModuleError(f"unknown module(s): {', '.join(unknown)}")

"""Where the engine's persistent files live: the stable checkout (spec *Checkout*).

Accessed via: ``cli.main`` calls ``stable_checkout(REPO_ROOT, os.environ)`` once, builds the ``Context`` from
the result and prints ``warning_line`` when the running code is not in the stable checkout.

Public API:

- ``ENV_VAR``: ``UBUNTU_TWEAKS_DIR``, the variable ``scripts/bootstrap.sh`` already uses for its clone directory.
- ``Checkout(root, running, reason)``: ``root`` is the stable checkout, ``running`` the checkout whose code
  runs, ``reason`` how ``root`` was found (``env``, ``git`` or ``running``); ``is_stable`` is ``root == running``,
  ``usable`` is True when ``root`` has ``modules/`` and the ``ubuntu_tweaks/`` package, and
  ``require_usable(command)`` raises ``ConfigError`` (exit 2) saying why and how to override.
- ``stable_checkout(running_root, env) -> Checkout``: ``$UBUNTU_TWEAKS_DIR``, else the main working tree of the
  git repository ``running_root`` belongs to, else ``running_root``. A path in ``$UBUNTU_TWEAKS_DIR`` that is not
  a directory raises ``ConfigError``.
- ``warning_line(stable, running) -> str``: the one-line warning for a running tree outside the stable checkout.

Why: links, submodules and extension code are persistent; a linked worktree is temporary, so a link into it
dangles once it is removed. The stable checkout is also what ``update`` pulls.
"""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ubuntu_tweaks.errors import ConfigError

ENV_VAR = "UBUNTU_TWEAKS_DIR"
GIT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Checkout:
    root: Path
    running: Path
    reason: Literal["env", "git", "running"]

    @property
    def is_stable(self) -> bool:
        return self.root == self.running

    @property
    def usable(self) -> bool:
        return (self.root / "modules").is_dir() and (self.root / "ubuntu_tweaks").is_dir()

    def require_usable(self, command: str) -> None:
        """Raise ``ConfigError`` when ``root`` is no engine checkout: a plan made against it would be wrong."""
        if self.usable:
            return
        missing = [name for name in ("modules/", "ubuntu_tweaks/") if not (self.root / name.rstrip("/")).is_dir()]
        fix = f"{ENV_VAR}=<checkout> ubuntu-tweak {command}"
        error = ConfigError(
            f"the stable checkout {self.root} is not an engine checkout (missing {', '.join(missing)}), so `{command}` "
            f"would link extensions and modules from the wrong tree; running from {self.running}; "
            f"to use another checkout run `{fix}`"
        )
        error.fix = fix
        raise error


def _main_tree(running: Path) -> Path | None:
    """The main working tree of ``running``'s repository; None when it is not that repository's top level, git
    is unavailable, or the common directory is not a plain ``.git`` (a bare repository, a submodule)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(running), "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.splitlines()
    if done.returncode != 0 or len(lines) != 2:
        return None
    common, toplevel = Path(lines[0]), Path(lines[1])
    if toplevel.resolve() != running or common.name != ".git":
        return None
    return common.parent.resolve()


def stable_checkout(running_root: Path, env: Mapping[str, str]) -> Checkout:
    running = running_root.resolve()
    explicit = env.get(ENV_VAR)
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_dir():
            raise ConfigError(f"{ENV_VAR}={explicit} is not a directory: point it at a checkout of ubuntu-tweaks")
        return Checkout(root.resolve(), running, "env")
    main = _main_tree(running)
    if main is not None:
        return Checkout(main, running, "git")
    return Checkout(running, running, "running")


def warning_line(stable: Path, running: Path) -> str:
    kind = "a git worktree" if (running / ".git").is_file() else "a copy"
    return (
        f"warning: running from {running}, {kind}; links, submodules and extension code target the stable "
        f"checkout {stable} (set {ENV_VAR} to change)"
    )

"""Planner for modules of source kind ``system``: root-installed files and kernel modules.

Accessed via: planner discovery (``apply`` / ``status`` / ``init``) calls ``build(ctx, backend)``.

Assumptions: the module's own ``check`` command is the source of truth for "installed and
matching" (spec, *Source kinds*); it must be read-only and run without root. Its protocol is
exit 0 = satisfied, ``DRIFT_EXIT`` (3) = drift, anything else = the checker itself broke, which
raises ``ActionFailed`` with the module's name and output rather than planning a reinstall on a
guess. ``argv[0]`` of both commands is resolved inside the module directory (never ``$PATH``)
and both run with it as the working directory. ``run()`` executes the install command through
``sudo`` (not when already root) with the terminal attached, so the installer's diff is visible.

Public API:

- ``build(ctx, backend, *, runner=run_command) -> list[Action]``: one ``SystemModuleAction``
  per enabled ``system`` module (module default < profile < machine overlay); disabled modules
  produce nothing. ``backend`` is unused: system modules do not touch dconf.
- ``SystemModuleAction(manifest, display_root, *, runner=run_command, elevate=...)``: kind
  ``system-module``, phase ``SYSTEM``, ``needs_root``, scope = module name.
- ``run_command(argv, *, cwd, timeout, capture) -> CompletedProcess`` is the injectable runner.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, ActionFailed, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.manifest import Manifest, SourceKind, discover_modules, module_defaults
from ubuntu_tweaks.settings import SettingsBackend

CHECK_TIMEOUT_SECONDS = 60
DRIFT_EXIT = 3
_OUTPUT_TAIL_LINES = 10

type Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_command(
    argv: Sequence[str], *, cwd: Path, timeout: float | None, capture: bool
) -> subprocess.CompletedProcess[str]:
    """Run ``argv``; with ``capture`` the output is collected and stdin closed, else inherited."""
    return subprocess.run(
        list(argv),
        cwd=cwd,
        timeout=timeout,
        capture_output=capture,
        stdin=subprocess.DEVNULL if capture else None,
        text=True,
        check=False,
    )


def _tail(text: str | None) -> str:
    lines = (text or "").strip().splitlines()[-_OUTPUT_TAIL_LINES:]
    return "\n".join(f"    {line}" for line in lines)


class SystemModuleAction(Action):
    phase = Phase.SYSTEM
    kind = "system-module"
    needs_root = True

    def __init__(
        self,
        manifest: Manifest,
        display_root: Path,
        *,
        runner: Runner = run_command,
        elevate: bool | None = None,
    ) -> None:
        self.scope = manifest.name
        self._manifest = manifest
        self._display_root = display_root
        self._runner = runner
        self._elevate = os.geteuid() != 0 if elevate is None else elevate

    def _argv(self, command: Sequence[str]) -> list[str]:
        directory = self._manifest.directory
        return [str(directory / command[0]), *command[1:]]

    def _install_argv(self) -> list[str]:
        argv = self._argv(self._manifest.source.install)
        return ["sudo", *argv] if self._elevate else argv

    def describe(self) -> str:
        shown = self._install_argv()
        try:
            index = 1 if self._elevate else 0
            shown[index] = str(Path(shown[index]).relative_to(self._display_root))
        except ValueError:
            pass
        return f"run {self._manifest.name}: {shlex.join(shown)}"

    def check(self) -> bool:
        name = self._manifest.name
        argv = self._argv(self._manifest.source.check)
        try:
            result = self._runner(argv, cwd=self._manifest.directory, timeout=CHECK_TIMEOUT_SECONDS, capture=True)
        except subprocess.TimeoutExpired as error:
            raise ActionFailed(
                f"{name}: check {shlex.join(argv)} did not finish within {CHECK_TIMEOUT_SECONDS}s"
            ) from error
        except OSError as error:
            raise ActionFailed(f"{name}: cannot run check {shlex.join(argv)}: {error}") from error
        if result.returncode == 0:
            return True
        if result.returncode == DRIFT_EXIT:
            return False
        output = _tail(result.stderr) or _tail(result.stdout)
        raise ActionFailed(
            f"{name}: check {shlex.join(argv)} crashed (exit {result.returncode}, expected 0 or {DRIFT_EXIT})"
            + (f"\n{output}" if output else "")
        )

    def run(self) -> None:
        name = self._manifest.name
        argv = self._install_argv()
        try:
            result = self._runner(argv, cwd=self._manifest.directory, timeout=None, capture=False)
        except OSError as error:
            raise ActionFailed(f"{name}: cannot run {shlex.join(argv)}: {error}") from error
        if result.returncode != 0:
            raise ActionFailed(f"{name}: {shlex.join(argv)} exited {result.returncode}")


def build(ctx: Context, backend: SettingsBackend, *, runner: Runner = run_command) -> list[Action]:
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    return [
        SystemModuleAction(manifest, ctx.repo_root, runner=runner)
        for manifest in manifests
        if manifest.source.kind is SourceKind.SYSTEM and config.module_enabled(manifest.name)
    ]

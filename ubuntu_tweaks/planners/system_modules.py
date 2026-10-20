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

- ``build(ctx, backend, *, process=None) -> list[Action]``: one ``SystemModuleAction``
  per enabled ``system`` module (module default < profile < machine overlay); disabled modules
  produce nothing. ``backend`` is unused: system modules do not touch dconf.
- ``SystemModuleAction(manifest, display_root, *, process=None)``: kind ``system-module``, phase
  ``SYSTEM``, ``needs_root``, scope = module name. Commands go through ``process.Process`` (the one
  runner; its ``elevate`` adds ``sudo --`` unless root) so tests pass a ``FakeProcess``-style double.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, ActionFailed, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.manifest import Manifest, SourceKind, discover_modules, module_defaults
from ubuntu_tweaks.process import CommandError, Process
from ubuntu_tweaks.settings import SettingsBackend

CHECK_TIMEOUT_SECONDS = 60
DRIFT_EXIT = 3
MISSING_EXIT = 127  # Process.query's answer for a binary that does not exist
UNRUNNABLE_EXIT = 126  # ... for one that cannot be executed (no exec bit, bad cwd)
TIMEOUT_EXIT = 124  # ... for a command that outlived its timeout
_OUTPUT_TAIL_LINES = 10


def _tail(text: str | None) -> str:
    lines = (text or "").strip().splitlines()[-_OUTPUT_TAIL_LINES:]
    return "\n".join(f"    {line}" for line in lines)


class SystemModuleAction(Action):
    phase = Phase.SYSTEM
    kind = "system-module"
    needs_root = True

    def __init__(self, manifest: Manifest, display_root: Path, *, process: Process | None = None) -> None:
        self.scope = manifest.name
        self._manifest = manifest
        self._display_root = display_root
        self._process = process or Process()

    def _argv(self, command: Sequence[str]) -> list[str]:
        directory = self._manifest.directory
        return [str(directory / command[0]), *command[1:]]

    def _install_argv(self) -> list[str]:
        try:
            return self._process.elevate(self._argv(self._manifest.source.install))
        except CommandError as error:
            raise ActionFailed(f"{self._manifest.name}: {error}") from error

    def describe(self) -> str:
        try:
            shown = self._process.elevate(self._argv(self._manifest.source.install))
        except CommandError:
            shown = ["sudo", "--", *self._argv(self._manifest.source.install)]  # run() reports the missing helper
        for index, part in enumerate(shown):
            try:
                shown[index] = str(Path(part).relative_to(self._display_root))
            except ValueError:
                continue
            break
        return f"run {self._manifest.name}: {shlex.join(shown)}"

    def check(self) -> bool:
        name = self._manifest.name
        argv = self._argv(self._manifest.source.check)
        result = self._process.query(argv, timeout=CHECK_TIMEOUT_SECONDS, cwd=self._manifest.directory)
        if result.returncode == 0:
            return True
        if result.returncode == DRIFT_EXIT:
            return False
        if result.returncode == TIMEOUT_EXIT:
            raise ActionFailed(f"{name}: check {shlex.join(argv)} did not finish within {CHECK_TIMEOUT_SECONDS}s")
        if result.returncode in (MISSING_EXIT, UNRUNNABLE_EXIT):
            detail = _tail(result.stderr)
            raise ActionFailed(f"{name}: cannot run check {shlex.join(argv)}" + (f"\n{detail}" if detail else ""))
        output = _tail(result.stderr) or _tail(result.stdout)
        raise ActionFailed(
            f"{name}: check {shlex.join(argv)} crashed (exit {result.returncode}, expected 0 or {DRIFT_EXIT})"
            + (f"\n{output}" if output else "")
        )

    def run(self) -> None:
        argv = self._install_argv()
        try:
            self._process.execute(argv, cwd=self._manifest.directory)
        except CommandError as error:
            raise ActionFailed(f"{self._manifest.name}: {error}") from error


def build(ctx: Context, backend: SettingsBackend, *, process: Process | None = None) -> list[Action]:
    manifests = discover_modules(ctx.modules_dir)
    config = ctx.profile.load_config(module_defaults(manifests))
    return [
        SystemModuleAction(manifest, ctx.repo_root, process=process)
        for manifest in manifests
        if manifest.source.kind is SourceKind.SYSTEM and config.module_enabled(manifest.name)
    ]

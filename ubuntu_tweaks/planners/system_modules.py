"""Planner for modules of source kind ``system``: root-installed files and kernel modules.

Accessed via: planner discovery (``apply`` / ``status`` / ``init``) calls ``build(ctx, backend)``.

Assumptions: the module's own ``check`` command is the source of truth for "installed and
matching" (spec, *Source kinds*); it must be read-only and run without root. Its protocol is
exit 0 = satisfied, ``DRIFT_EXIT`` (3) = drift, anything else = the checker itself broke, which
raises ``ActionFailed`` with the module's name and output rather than planning a reinstall on a
guess. ``argv[0]`` of both commands is resolved inside the module directory (never ``$PATH``)
and both run with it as the working directory. ``run()`` executes the install command through
``sudo`` (not when already root, and not at all for ``[source] root = false``) with the terminal
attached, so the installer's diff is visible. The merged ``[modules.<name>]`` options (without
``enabled``) reach ``check``, ``preview`` and ``install`` as JSON in ``UBUNTU_TWEAK_MODULE_CONFIG``, carried
by ``env`` so sudo's ``env_reset`` keeps it; they are visible in ``ps`` and in sudo's log, so a module
option is never a secret. Installer exit ``NEEDS_MANUAL_EXIT`` (10) is a ``ManualStepRequired``, exit ``EX_TEMPFAIL``
(75) a ``Transient`` (retried with backoff).

Public API:

- ``build(ctx, backend, *, process=None) -> list[Action]``: one ``SystemModuleAction``
  per enabled ``system`` module (module default < profile < machine overlay); disabled modules
  produce nothing. ``backend`` is unused: system modules do not touch dconf.
- ``SystemModuleAction(manifest, display_root, *, process=None, module_config=None)``: kind
  ``system-module``, phase ``SYSTEM``, ``needs_root`` from ``[source] root``, scope = module name.
  Commands go through ``process.Process`` (the one runner; its ``elevate`` adds ``sudo --`` unless
  root) so tests pass a ``FakeProcess``-style double. ``describe()`` appends the manifest's optional
  ``preview`` output (computed once).
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from ubuntu_tweaks.actions import EX_TEMPFAIL, Action, ActionFailed, ManualStepRequired, Phase, Transient
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.manifest import Fallback, Manifest, SourceKind, module_defaults
from ubuntu_tweaks.identity import MODULE_CONFIG_ENV
from ubuntu_tweaks.profile import module_config_json
from ubuntu_tweaks.process import CommandError, Process
from ubuntu_tweaks.settings import SettingsBackend

CHECK_TIMEOUT_SECONDS = 60
DRIFT_EXIT = 3
NEEDS_MANUAL_EXIT = 10  # an installer's "I cannot finish without you": reported, not a failure
MISSING_EXIT = 127  # Process.query's answer for a binary that does not exist
UNRUNNABLE_EXIT = 126  # ... for one that cannot be executed (no exec bit, permission denied)
TIMEOUT_EXIT = 124  # ... for a command that outlived its timeout
_OUTPUT_TAIL_LINES = 10


def _tail(text: str | None) -> str:
    lines = (text or "").strip().splitlines()[-_OUTPUT_TAIL_LINES:]
    return "\n".join(f"    {line}" for line in lines)


class SystemModuleAction(Action):
    phase = Phase.SYSTEM
    kind = "system-module"

    def __init__(
        self,
        manifest: Manifest,
        display_root: Path,
        *,
        process: Process | None = None,
        module_config: str | None = None,
    ) -> None:
        self.scope = manifest.name
        self.key = f"system-module:{manifest.name}"
        self.needs_root = manifest.source.root
        self._manifest = manifest
        self._display_root = display_root
        self._process = process or Process()
        self._module_config = module_config
        self._preview_text: str | None = None

    def _argv(self, command: Sequence[str]) -> list[str]:
        directory = self._manifest.directory
        argv = [str(directory / command[0]), *command[1:]]
        if self._module_config is None:
            return argv
        # `env` carries the options through sudo, whose env_reset would drop a plain environment variable.
        return ["env", f"{MODULE_CONFIG_ENV}={self._module_config}", *argv]

    def _install_argv(self) -> list[str]:
        argv = self._argv(self._manifest.source.install)
        if not self.needs_root:
            return argv
        try:
            return self._process.elevate(argv)
        except CommandError as error:
            raise ActionFailed(f"{self._manifest.name}: {error}") from error

    def describe(self) -> str:
        try:
            shown = self._install_argv()
        except ActionFailed:
            shown = ["sudo", "--", *self._argv(self._manifest.source.install)]  # run() reports the missing helper
        for index, part in enumerate(shown):
            try:
                shown[index] = str(Path(part).relative_to(self._display_root))
            except ValueError:
                continue
            break
        listed = "".join(
            f"\n        fallback{'' if fallback.root else ' (no root)'}: {shlex.join(fallback.install)}"
            for fallback in self._manifest.fallbacks
        )
        return f"run {self._manifest.name}: {shlex.join(shown)}" + self._preview() + listed

    def _preview(self) -> str:
        """What the installer would do, from the manifest's read-only ``preview`` command, indented under
        the action line. Run once per action (``describe()`` is called several times per run). A failing
        preview shows its first stderr line and never fails the plan."""
        command = self._manifest.source.preview
        if not command:
            return ""
        if self._preview_text is not None:
            return self._preview_text
        result = self._process.query(self._argv(command), timeout=CHECK_TIMEOUT_SECONDS, cwd=self._manifest.directory)
        if result.returncode == 0:
            lines = result.stdout.rstrip().splitlines()
        else:
            first = next((line for line in result.stderr.splitlines() if line.strip()), "no output")
            lines = [f"(preview unavailable: exit {result.returncode}: {first.strip()})"]
        self._preview_text = "".join(f"\n        {line}" for line in lines)
        return self._preview_text

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
        self._execute(self._install_argv())

    def _execute(self, argv: list[str]) -> None:
        try:
            self._process.execute(argv, cwd=self._manifest.directory)
        except CommandError as error:
            if error.returncode == NEEDS_MANUAL_EXIT:
                hint = f": {self._manifest.manual}" if self._manifest.manual else ""
                raise ManualStepRequired(
                    f"{self._manifest.name}: the installer needs a manual step (exit {NEEDS_MANUAL_EXIT}; "
                    f"its output above says which){hint}"
                ) from error
            if error.returncode == EX_TEMPFAIL:
                raise Transient(f"{self._manifest.name}: {error}") from error
            raise ActionFailed(f"{self._manifest.name}: {error}") from error

    def alternatives(self) -> Sequence[Action]:
        return [_FallbackInstall(self, fallback) for fallback in self._manifest.fallbacks]

    def run_fallback(self, fallback: Fallback) -> None:
        argv = self._argv(fallback.install)
        if fallback.root:
            try:
                argv = self._process.elevate(argv)
            except CommandError as error:
                raise ActionFailed(f"{self._manifest.name}: {error}") from error
        self._execute(argv)


class _FallbackInstall(Action):
    """A ``[[fallback]]`` installer of a system module (spec, *Reconcile and failover*): it only runs; whether it
    worked is the module's own ``check`` through the primary action."""

    phase = Phase.SYSTEM
    kind = "system-module-fallback"

    def __init__(self, primary: SystemModuleAction, fallback: Fallback) -> None:
        self.scope = primary.scope
        self.needs_root = fallback.root
        self._primary = primary
        self._fallback = fallback

    def describe(self) -> str:
        return self._fallback.description or f"fallback {shlex.join(self._fallback.install)}"

    def check(self) -> bool:
        return self._primary.check()

    def run(self) -> None:
        self._primary.run_fallback(self._fallback)


def build(ctx: Context, backend: SettingsBackend, *, process: Process | None = None) -> list[Action]:
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    return [
        SystemModuleAction(
            manifest,
            ctx.repo_root,
            process=process,
            module_config=module_config_json(config, manifest.name, manifest.source.repo),
        )
        for manifest in manifests
        if manifest.source.kind is SourceKind.SYSTEM and config.module_enabled(manifest.name)
    ]

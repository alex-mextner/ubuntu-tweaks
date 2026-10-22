"""Planner for what a module installs and sets up: ``[install]``, ``[[step]]`` and ``[[post]]``.

Accessed via: planner discovery (``apply`` / ``status`` / ``init``) calls ``build(ctx, backend)``; the
init pipeline (base tools, zsh, clipboard, git/gh, brew, ...) is made of modules that use these tables.

Assumptions:

- One apt batch: the ``[install] apt`` names of every enabled module with the same phase are one
  ``package`` action (``apt-get install -y a b c``), so base tools and clipboard utilities are one
  ``apt`` run, while each module stays switchable on its own. A package counts as installed when
  ``dpkg-query`` says ``ii`` **or** the command the module's ``[install.provides]`` names for it is on
  ``PATH`` (a brew-installed ``gh`` or ``zsh`` satisfies the package).
- After the batch come the module's ``[[step]]`` actions (``ubuntu_tweaks/steps.py``) in declared
  order, then its ``[[post]]`` actions in declared order. Everything of a module has the module's
  ``order`` (posts ``order + 0.5``, so they follow the module's install even when another planner
  creates it, like the system-module planner) and its ``phase`` (``PACKAGES`` when the module names none).
- ``[[post]]`` argv may contain the whole-argument placeholders ``{packages.apt}``, ``{packages.snap}``,
  ``{packages.flatpak}`` and ``{packages.brew}``: each expands to the names of ``packages.toml``
  (shared + overlay union); a post whose placeholder expands to nothing is not planned.
  An absolute ``argv[0]`` that does not exist yet (brew before its install ran) makes ``run()`` report
  ``needs manual`` instead of failing with "command not found".
- Disabled modules and modules outside ``--only`` produce nothing.

Public API: ``build(ctx, backend, *, process=None, env_overrides=None)``, ``warnings(ctx, backend)``,
``PostAction``, ``ModuleAptAction``, ``expand_placeholders``.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ubuntu_tweaks import dotfiles_rc, git_identity
from ubuntu_tweaks.actions import Action, ActionFailed, ManualStepRequired, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.git_identity import KEYS
from ubuntu_tweaks.manifest import (
    PACKAGE_PLACEHOLDER,
    POST_AFTER_INSTALL,
    Manifest,
    Post,
    SourceKind,
    module_defaults,
)
from ubuntu_tweaks.planners.packages import PackageAction, installed_apt
from ubuntu_tweaks.planners.system_modules import SystemModuleAction, module_config_json
from ubuntu_tweaks.process import CommandError, Process
from ubuntu_tweaks.profile import Config, Packages
from ubuntu_tweaks.settings import SettingsBackend
from ubuntu_tweaks.steps import StepEnv, get_step, git_section

POST_ORDER_OFFSET = 0.5
POST_CHECK_TIMEOUT_SECONDS = 120
DEFAULT_MODULE_PHASE = Phase.PACKAGES
APT_LISTS = "/var/lib/apt/lists"
APT_LISTS_MAX_AGE_SECONDS = 2 * 24 * 3600
APT_UPDATE = ("apt-get", "update")


def apt_lists_stale(lists: Path, *, now: float | None = None) -> bool:
    """No package index at all (a fresh image) or none refreshed within two days: an install would 404."""
    try:
        stamps = [entry.stat().st_mtime for entry in lists.iterdir() if "_Packages" in entry.name]
    except OSError:
        return True
    return not stamps or (time.time() if now is None else now) - max(stamps) > APT_LISTS_MAX_AGE_SECONDS


def module_phase(manifest: Manifest) -> Phase:
    """The phase of a module's install-time actions: its ``phase``, else ``SYSTEM`` for a ``system``
    module (its posts follow its installer) and ``PACKAGES`` for the rest."""
    if manifest.phase is not None:
        return manifest.phase
    return Phase.SYSTEM if manifest.source.kind is SourceKind.SYSTEM else DEFAULT_MODULE_PHASE


type EnvOverrides = Callable[[StepEnv], StepEnv]


def expand_placeholders(argv: Sequence[str], packages: Packages) -> tuple[str, ...] | None:
    """Replace ``{packages.<manager>}`` arguments by the manager's names; ``None`` when one is empty."""
    expanded: list[str] = []
    for item in argv:
        match = PACKAGE_PLACEHOLDER.fullmatch(item)
        if match is None:
            expanded.append(item)
            continue
        names: tuple[str, ...] = getattr(packages, match.group(1))
        if not names:
            return None
        expanded.extend(names)
    return tuple(expanded)


class ModuleAptAction(PackageAction):
    """The one apt batch of the modules that share a phase."""

    def __init__(
        self,
        names: Sequence[str],
        modules: Sequence[str],
        process: Process,
        provides: Mapping[str, str],
        which: Callable[[str], str | None],
        *,
        phase: Phase,
        scope: str,
        order: float,
        update_first: Callable[[], bool],
    ) -> None:
        def lister(runner: Process, wanted: Sequence[str]) -> set[str]:
            present = installed_apt(runner, wanted)
            return present | {name for name in wanted if name in provides and which(provides[name])}

        super().__init__(
            "apt",
            names,
            process,
            lister,
            lambda missing: ["apt-get", "install", "-y", *missing],
            needs_root=True,
            before=None,
        )
        self._modules = tuple(modules)
        self._update_first = update_first
        self.phase = phase
        self.scope = scope
        self.order = order
        self.key = f"apt:{phase.name.lower()}"

    @property
    def modules(self) -> tuple[str, ...]:
        return self._modules

    def _update_command(self) -> Sequence[str] | None:
        return APT_UPDATE if self._update_first() else None

    def describe(self) -> str:
        return f"{super().describe()} [{', '.join(self._modules)}]"


class PostAction(Action):
    kind = "post"

    def __init__(
        self,
        manifest: Manifest,
        post: Post,
        run_argv: Sequence[str],
        check_argv: Sequence[str],
        process: Process,
        *,
        phase: Phase,
        waits_for: Sequence[Action] = (),
    ) -> None:
        self._manifest = manifest
        self._post = post
        self._run_argv = tuple(run_argv)
        self._check_argv = tuple(check_argv)
        self._process = process
        self.phase = phase
        self.scope = manifest.name
        self.order = manifest.order + POST_ORDER_OFFSET
        self.key = f"post:{manifest.name}:{post.name}"
        self.needs_root = post.needs_root
        self._waits_for = tuple(waits_for)

    def describe(self) -> str:
        what = self._post.description or f"{self._post.name}: {shlex.join(self._run_argv)}"
        return f"{self._manifest.name}: {what}"

    def check(self) -> bool:
        result = self._process.query(self._check_argv, timeout=POST_CHECK_TIMEOUT_SECONDS, cwd=self._manifest.directory)
        return result.ok

    def run(self) -> None:
        for prerequisite in self._waits_for:
            if not prerequisite.check():
                raise ManualStepRequired(
                    f"{self._manifest.name}: {self._post.name} waits for its install "
                    f"([{prerequisite.kind}] did not complete; run again after fixing it)"
                )
        program = self._run_argv[0]
        if program.startswith("/") and not Path(program).exists():
            raise ManualStepRequired(
                f"{self._manifest.name}: {self._post.name} needs {program}, which is not there yet "
                "(an earlier step did not run)"
            )
        argv = list(self._run_argv)
        try:
            self._process.execute(
                self._process.elevate(argv) if self.needs_root else argv, cwd=self._manifest.directory
            )
        except CommandError as error:
            raise ActionFailed(f"{self._manifest.name}: {self._post.name}: {error}") from error


class SystemInstalled(Action):
    """What a ``system`` module's ``[[post]] after = "install"`` waits for: the installer's own check
    (the system-module planner owns the real action; this one is only its read-only checker)."""

    kind = "system-module"
    phase = Phase.SYSTEM

    def __init__(self, action: SystemModuleAction) -> None:
        self._action = action
        self.scope = action.scope

    def describe(self) -> str:
        return f"{self.scope} installed"

    def check(self) -> bool:
        try:
            return self._action.check()
        except ActionFailed:
            return False  # a broken checker is the system-module action's failure to report, not this probe's

    def run(self) -> None:
        raise ActionFailed("SystemInstalled is a probe, never planned")


def _enabled(ctx: Context, manifests: Sequence[Manifest], config: Config) -> list[Manifest]:
    return [manifest for manifest in manifests if ctx.selects(manifest.name) and config.module_enabled(manifest.name)]


def build(
    ctx: Context,
    backend: SettingsBackend,
    *,
    process: Process | None = None,
    env_overrides: EnvOverrides | None = None,
) -> list[Action]:
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    enabled = _enabled(ctx, manifests, config)
    if not any(manifest.install.apt or manifest.steps or manifest.post for manifest in enabled):
        return []
    runner = process or Process()
    packages = ctx.profile.load_packages()
    # `--only` drops the links of `files/` and of the dotfiles repository (they carry no module scope of the
    # module asking), so nothing is left to do the rc file's job
    linked = (
        frozenset()
        if ctx.only
        else frozenset(str(file.rel) for file in ctx.profile.files()) | frozenset(dotfiles_rc.provided(ctx))
    )
    env = StepEnv(config, runner, linked=linked, dotfiles=dotfiles_rc.checkout(ctx))
    if env_overrides is not None:
        env = env_overrides(env)

    actions: list[Action] = []
    groups: dict[Phase, list[Manifest]] = {}
    for manifest in enabled:
        if manifest.install.apt:
            groups.setdefault(module_phase(manifest), []).append(manifest)
    batches: list[ModuleAptAction] = []
    for phase, members in groups.items():
        first = min(members, key=lambda manifest: (manifest.order, manifest.name))
        names = list(dict.fromkeys(name for manifest in members for name in manifest.install.apt))
        provides = {package: command for manifest in members for package, command in manifest.install.provides}
        batch = ModuleAptAction(
            names,
            [manifest.name for manifest in members],
            runner,
            provides,
            env.which,
            phase=phase,
            scope=first.name,
            order=first.order,
            update_first=lambda: packages.apt_update or apt_lists_stale(env.system_path(APT_LISTS)),
        )
        batches.append(batch)
        actions.append(batch)

    for manifest in enabled:
        phase = module_phase(manifest)
        installs: list[Action] = [batch for batch in batches if manifest.name in batch.modules]
        for step in manifest.steps:
            for index, action in enumerate(get_step(step.name)(env, manifest.name, step.args)):
                action.scope = manifest.name
                action.phase = phase
                action.order = manifest.order
                action.key = f"step:{manifest.name}:{step.name}:{index}"
                actions.append(action)
                installs.append(action)
        planned: dict[str, list[Action]] = {}  # post name -> what it waits for, plus itself when it is planned
        if manifest.source.kind is SourceKind.SYSTEM:
            installs.append(
                SystemInstalled(
                    SystemModuleAction(
                        manifest,
                        ctx.repo_root,
                        process=runner,
                        module_config=module_config_json(config, manifest.name, manifest.source.repo),
                    )
                )
            )
        for post in manifest.post:
            if post.after in (None, POST_AFTER_INSTALL):
                waits_for = installs
            else:
                waits_for = planned.get(post.after, [])
            if any(name in linked for name in post.unless_linked):
                planned[post.name] = waits_for  # the profile's own file does this job
                continue
            run_argv = expand_placeholders(post.run, packages)
            check_argv = expand_placeholders(post.check, packages)
            if run_argv is None or check_argv is None:
                planned[post.name] = waits_for  # not planned, but whoever follows it still waits for its prerequisites
                continue
            action = PostAction(manifest, post, run_argv, check_argv, runner, phase=phase, waits_for=waits_for)
            planned[post.name] = [*waits_for, action]
            actions.append(action)
    return actions


def warnings(ctx: Context, backend: SettingsBackend, *, process: Process | None = None) -> list[str]:
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    asks_identity = any(
        step.name == "git-identity" for manifest in _enabled(ctx, manifests, config) for step in manifest.steps
    )
    if not asks_identity:
        return []
    runner = process or Process()
    section = git_section(config)
    unset = [key for key in KEYS if key not in section and git_identity.current(runner, key) is None]
    if not unset:
        return []
    known = git_identity.discover(runner, dotfiles_rc.checkout(ctx), unset)
    unknown = [key for key in unset if key not in known]
    if not unknown:
        return []
    keys = " and ".join(f"user.{key}" for key in unknown)
    return [
        f"git: {keys} not set and nothing knows it (profile.toml [git], the dotfiles .gitconfig, gh): not an error, "
        "git only asks for it at your first commit. To fix it, set `[git] name` / `email` in profile.toml, or run "
        "`git config --global user.name ...` (`gh auth login` lets ubuntu-tweak take both from your GitHub account)"
    ]

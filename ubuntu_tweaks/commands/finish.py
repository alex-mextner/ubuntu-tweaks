"""``ubuntu-tweak finish [--step ID] [--yes]``: what is still left for you, in order, with the machine parts done for you.

Accessed via: ``bin/ubuntu-tweak finish`` (the profile installs it as ``finish-setup`` and as a desktop launcher).

The checklist is the built-in ``apply`` step (the root work, with an optional go/wait gate from ``[finish.apply]``)
followed by the ``[[finish]]`` tables of every enabled module, the engine's and the profile's, and the profile's own
``[[finish.steps]]`` (``profile.toml``, then ``machine.toml``). ``--dry-run`` lists
every step with its state and runs only the read-only ``check`` and ``gate`` commands; without it the pending steps
run in order (machine part, then the part that needs you, then the check again), see ``ubuntu_tweaks.finish`` and
the spec's *Finish*. ``--step ID`` limits the walk to one step and the steps it requires (``--only`` is refused: a module filter would
drop the ``apply`` step that others require); ``--yes`` does not pause between steps and leaves optional steps
alone. Without a terminal on stdin nothing is run. Exit 0: every required
step is done; 3: one is not.

Public API: ``register(subparsers)``, ``run(ctx, args)``, ``collect_steps(ctx, ...) -> list[Step]``.

Every step command (and the child ``apply``) inherits this process's environment; when that has no display (a shell
started before the graphical login, such as a boot-time tmux server), the session's ``DISPLAY`` / ``WAYLAND_DISPLAY``
come from the user's systemd manager first, see ``ubuntu_tweaks.graphical_env``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace

from ubuntu_tweaks import apply_exit, finish, sessionlog, ui
from ubuntu_tweaks.commands._support import apply_command, has_gi, rerun_command
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.finish import ExitReport, Probe, Shell, Step
from ubuntu_tweaks.graphical_env import with_graphical_session
from ubuntu_tweaks.manifest import FinishStep, module_defaults, parse_finish_tables
from ubuntu_tweaks.process import Process, run_streamed
from ubuntu_tweaks.identity import MODULE_CONFIG_ENV
from ubuntu_tweaks.profile import module_config_json, read_toml

COMMAND_ENV = "UBUNTU_TWEAK_FINISH_COMMAND"  # what to tell the user to run again (the profile's launcher name)
DRY_RUN_ENV = "UBUNTU_TWEAK_FINISH_DRY_RUN"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "finish",
        help="walk through the human-only steps that are still left (--dry-run only lists them)",
    )
    parser.add_argument("--step", metavar="ID", help="only this step and the steps it requires")
    parser.add_argument("--yes", action="store_true", help="do not pause between steps; optional steps are skipped")
    parser.set_defaults(handler=run)


def _apply_probe(ctx: Context) -> Probe:
    if not has_gi():
        return Probe(False, ui.t("finish.apply.no_gi"))
    return finish.plan_probe(ctx)


def collect_steps(
    ctx: Context,
    *,
    shell: Shell,
    environ: Mapping[str, str],
    dry_run: bool,
    probe: Callable[[Context], Probe],
    apply_run: Callable[[], int],
    explain: Callable[[int], ExitReport | None] | None = None,
) -> list[Step]:
    """The built-in ``apply`` step, the ``[[finish]]`` steps of the enabled modules (ordered by the module's ``order``
    and name, then file order) and the profile's own ``[[finish.steps]]``. ``apply_run`` starts ``apply --commit``;
    ``explain`` says what its non-zero exits mean."""
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    # what the caller inherited about modules and dry runs must not leak into a command that is told nothing
    base = {key: value for key, value in environ.items() if key not in (MODULE_CONFIG_ENV, DRY_RUN_ENV)}
    base[ui.LANG_ENV] = ui.lang()
    if dry_run:
        base[DRY_RUN_ENV] = "1"
    owners: dict[str, str] = {}
    steps: list[Step] = []

    def add(step: Step, owner: str) -> None:
        if step.id in owners:
            raise ConfigError(f"finish step id {step.id!r} is declared twice: by {owners[step.id]} and by {owner}")
        owners[step.id] = owner
        steps.append(step)

    # a profile that is not cloned yet has no directory to run in: `apply` itself clones it
    profile_dir = ctx.profile.root if ctx.profile.exists else ctx.repo_root
    add(
        finish.apply_step(
            finish.apply_settings(config),
            directory=profile_dir,
            shell=shell,
            env=base,
            probe=lambda: probe(ctx),
            run=apply_run,
            explain=explain,
        ),
        "the built-in apply step",
    )
    for manifest in sorted(manifests, key=lambda item: (item.order, item.name)):
        if not manifest.finish or not config.module_enabled(manifest.name):
            continue
        options = module_config_json(config, manifest.name, manifest.source.repo)
        env = {**base, MODULE_CONFIG_ENV: options} if options is not None else base
        for spec in manifest.finish:
            add(
                finish.command_step(spec, directory=manifest.directory, env=env, shell=shell), f"module {manifest.name}"
            )
    for spec in _profile_specs(ctx):
        add(
            finish.command_step(spec, directory=profile_dir, env=base, shell=shell),
            "the profile's [[finish.steps]]",
        )
    finish.validate(steps)
    return steps


def _profile_specs(ctx: Context) -> list[FinishStep]:
    """``[[finish.steps]]`` of ``profile.toml``, then of the machine's ``machine.toml``: the profile's own steps
    (a command is run from the profile directory; they carry no module options). Both files are read on their own
    because the merged config replaces a list: the machine adds its steps to the shared ones."""
    specs: list[FinishStep] = []
    for path in (ctx.profile.profile_toml, ctx.profile.machine_toml):
        table = read_toml(path)
        section = table.get("finish", {})
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: finish must be a table, got {section!r}")
        tables = section.get("steps", [])
        if not isinstance(tables, list) or not all(isinstance(item, dict) for item in tables):
            raise ConfigError(f"{path}: finish.steps must be an array of tables ([[finish.steps]])")
        specs += parse_finish_tables(tables, f"{path} [[finish.steps]]")
    return specs


def _run_apply(ctx: Context) -> int:
    """``apply --commit`` through the session log, like ``update`` runs it; a program that cannot start is an exit
    code here too, not a traceback that ends the walk."""
    try:
        with finish.child_owns_ctrl_c():  # the apply child decides what Ctrl-C means; we wait for it
            return run_streamed(apply_command(ctx))
    except OSError as error:
        print(f"apply: cannot run: {error.strerror or error}", file=sys.stderr)
        return 127 if isinstance(error, FileNotFoundError) else 126


def _started_apply(ctx: Context, applied: list[float]) -> int:
    applied.append(time.time())
    return _run_apply(ctx)


def _explain_apply_exit(args: argparse.Namespace, code: int, since: float) -> ExitReport | None:
    """What ``apply --commit`` ending with ``code`` means, read from the run journal it has just written (only a
    journal started after ``since``: a run that died before writing its own is not blamed for an older one's items)."""
    from ubuntu_tweaks import journal

    try:
        return apply_exit.explain(
            code,
            journal.previous(journal.journal_path()),
            rerun=lambda subcommand: rerun_command(args, subcommand),
            command=os.environ.get(COMMAND_ENV, finish.DEFAULT_COMMAND),
            log=os.environ.get(sessionlog.SESSION_ENV, ""),
            since=since,
        )
    except (AttributeError, TypeError):  # a journal whose fields have the wrong types: the generic text stays
        return None


def run(ctx: Context, args: argparse.Namespace) -> int:
    if ctx.only:
        raise ConfigError("finish takes no --only: use --step ID for one step (and the steps it requires)")

    applied: list[float] = []  # when each `apply` of this walk was started: a journal older than that is another run's
    # a shell started before the graphical login (a boot-time tmux server) has no display, the user's systemd manager
    # does; our own environment gets it, so the step commands and the child `apply` (run_streamed) inherit it alike
    os.environ.update(with_graphical_session(os.environ, Process()))

    def current_steps() -> list[Step]:
        """Collected again after every attempt: `apply` may have cloned the profile, which holds more steps."""
        steps = collect_steps(
            ctx,
            shell=finish.SubprocessShell(),
            environ=os.environ,
            dry_run=args.dry_run,
            probe=_apply_probe,
            apply_run=lambda: _started_apply(ctx, applied),
            explain=lambda code: _explain_apply_exit(args, code, applied[-1] if applied else 0.0),
        )
        if args.step:
            steps = finish.closure(steps, args.step)
            steps = [
                replace(step, optional=False) if step.id == args.step else step for step in steps
            ]  # named: required
        return steps

    from ubuntu_tweaks import journal

    walked = finish.walk(
        current_steps,
        out=sys.stdout,
        ask=input,
        interactive=sys.stdin.isatty(),
        assume_yes=args.yes,
        dry_run=args.dry_run,
        command=os.environ.get(COMMAND_ENV, finish.DEFAULT_COMMAND),
        obligations=finish.FileObligations(
            journal.journal_path().with_name("finish-state.json"), f"{ctx.profile.root}|{ctx.profile.machine}"
        ),
    )
    return finish.exit_code(walked.steps, walked.statuses)

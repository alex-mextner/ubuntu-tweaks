"""The logic behind ``ubuntu-tweak finish``: the human-only steps that are still left, in the order they can be done.

Accessed via: ``commands/finish.py`` builds the steps (the built-in ``apply`` step and every enabled module's
``[[finish]]`` tables, spec *Finish*) and calls ``walk``.

Assumptions: a step's **check** is the only source of truth for "done"; it must be read-only, quick and need no
terminal. Nothing here ever reads, stores or prints a secret: a check's output is shown only as its first line. The
walk never asks for a sudo password: the ``apply`` step runs ``ubuntu-tweak apply --commit``, which does that itself.
The one prompt the walk owns (Enter / s / q) says "password" on purpose: the session log masks an answer given to such
a prompt, so a password typed there by mistake is not written to disk.

Public API:

- ``State`` (``DONE``, ``PENDING``, ``WAIT``, ``BLOCKED``, ``FAILED``), ``Text(en, ru)`` (``.get(lang)`` falls back to
  English), ``Probe(ok, detail, settled)``, ``Step`` (id, texts, ``check``, optional ``gate`` / ``prepare`` / ``run`` /
  ``explain_exit``, ``requires``, ``optional``, ``reapply``, ``gate_why``), ``Gate(reason)`` (``.go``), ``Status(state,
  detail, gate)``. A step's ``requires`` are satisfied by a probe that is ``ok`` or ``settled`` (ran to its end,
  nothing left that only another run could do): a failed module does not block the steps that are independent of it.
  ``ExitReport(text, problem)`` is what ``explain_exit(code)`` returns for a non-zero exit of ``run`` (``None``: the
  generic text): ``text`` is printed right after the run, ``problem`` is why the step is shown as not done.
- ``evaluate(steps, failed=None, reopened=None) -> dict[str, Status]``: the state of every step. ``Status.gate`` is
  ``None`` without a gate, else a ``Gate``. ``failed`` maps a step id to why its last attempt did not take; ``reopened``
  maps the id of a done step to why it has to run again.
- ``walk(steps, *, out, ask, interactive, assume_yes, dry_run=False, command=..., obligations=None) -> Walked``: print the
  checklist and, when ``interactive``, run the pending steps (prepare, run, check again, at most ``MAX_TRIES`` times
  each); ``steps`` may be a function that returns the current list (it is called again after every attempt: running
  ``apply`` can clone the profile that holds more steps); a step with neither ``prepare`` nor ``run`` is only listed
  (you do it yourself); ``dry_run`` only lists. ``Walked(steps, statuses)`` is the last list the walk looked at and
  the state of each step. ``obligations`` (``MemoryObligations`` / ``FileObligations``) keeps the ids of done steps that
  have to run again across runs: a ``reapply`` step that is done leaves one for ``apply`` until ``apply`` ran.
- ``exit_code(steps, statuses) -> int``: 0 when every required step is done, else 3.
- ``validate(steps)``: a step that requires itself or sits in a requirement cycle is a ``ConfigError``;
  ``requirements(steps, step_id)`` and ``closure(steps, step_id)`` walk the requirement graph.
- ``Shell`` / ``SubprocessShell`` / ``Output``: the one place a command is started (a seam for tests). ``stream`` (a
  machine part) copies the output through ``sys.stdout``, so the session log has it (stderr is merged into it), and returns
  its last non-blank line, secrets masked, as ``Output.out``; ``interactive`` (the part that needs you) inherits the terminal and is not logged.
- ``command_step(spec, *, directory, env, shell) -> Step`` for a manifest's ``[[finish]]``; ``ApplySettings`` /
  ``apply_settings(config)`` / ``apply_step(...)`` / ``apply_probe(actions, last_run)`` for the built-in step.
"""

from __future__ import annotations

import contextlib
import json
import signal
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, TextIO

from ubuntu_tweaks import ui
from ubuntu_tweaks.atomic_write import atomic_write
from ubuntu_tweaks.errors import ConfigError, UbuntuTweakError
from ubuntu_tweaks.manifest import FinishStep
from ubuntu_tweaks.process import run_streamed

if TYPE_CHECKING:
    from ubuntu_tweaks.actions import Action
    from ubuntu_tweaks.context import Context
    from ubuntu_tweaks.journal import Run
    from ubuntu_tweaks.profile import Config

APPLY_ID = "apply"
CHECK_TIMEOUT_SECONDS = 60
MAX_TRIES = 2
EXIT_PENDING = 3
WAITING_FOR_A_PERSON = "manual"  # the journal status of an action that only a finish step can unblock
APPLY_PROBLEMS = ("failed", "deferred")  # journal statuses that mean apply has more to do
INTERRUPTED_EXITS = (130, -signal.SIGINT)  # a command that ended on Ctrl-C
DEFAULT_COMMAND = "ubuntu-tweak finish"  # what the summary tells you to run again; the profile's launcher overrides it


class State(StrEnum):
    DONE = "done"
    PENDING = "pending"
    WAIT = "wait"
    BLOCKED = "blocked"
    FAILED = "failed"


ACTIONABLE = (State.PENDING, State.FAILED)


class Decision(StrEnum):
    START = "start"
    SKIP = "skip"
    QUIT = "quit"


@dataclass(frozen=True)
class Text:
    en: str
    ru: str = ""

    def get(self, lang: str) -> str:
        return self.ru if lang == "ru" and self.ru else self.en


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str = ""
    settled: bool = False  # not done, but it ran to its end: nothing is left that only another run could do


@dataclass(frozen=True)
class ExitReport:
    """What a step says about a non-zero exit of its ``run``: ``text`` is printed right after the run (``""``: nothing
    to add), ``problem`` is why the step is shown as not done."""

    text: str
    problem: str


@dataclass(frozen=True)
class Step:
    id: str
    title: Text
    why: Text
    action: Text
    check: Callable[[], Probe]
    gate: Callable[[], str] | None = None
    prepare: Callable[[], Probe] | None = None
    run: Callable[[], int] | None = None
    requires: tuple[str, ...] = ()
    optional: bool = False
    reapply: bool = False
    gate_why: Text = Text("")
    explain_exit: Callable[[int], ExitReport | None] | None = None  # None from it: the generic text


@dataclass(frozen=True)
class Gate:
    """What a step's gate said: no reason is go, any text is the reason to wait."""

    reason: str = ""

    @property
    def go(self) -> bool:
        return not self.reason


@dataclass(frozen=True)
class Status:
    state: State
    detail: str = ""
    gate: Gate | None = None  # None: the step has no gate


# ---------------------------------------------------------------------------------------------------
# Running commands


@dataclass(frozen=True)
class Output:
    code: int
    out: str = ""
    err: str = ""


class Shell(Protocol):
    def capture(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> Output: ...

    def stream(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> Output: ...

    def interactive(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int: ...


def _exit_for(error: OSError) -> int:
    return 127 if isinstance(error, FileNotFoundError) else 126


@contextlib.contextmanager
def child_owns_ctrl_c() -> Iterator[None]:
    """Ctrl-C goes to the whole foreground group: the command decides what it means (a TUI cancels a field, a script
    cleans up). Without a handler of our own Python would stop it a quarter of a second later. A handler, not SIG_IGN:
    an ignored signal would be inherited by the command."""
    previous = signal.signal(signal.SIGINT, lambda _signum, _frame: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class SubprocessShell:
    """The real runner. A command that is missing, not executable or too slow is an exit code and a line of text,
    never an exception: a broken check must not stop the whole checklist. ``capture`` (checks and gates) has a
    timeout and no terminal; ``interactive`` (a machine part, the part that needs you) owns the terminal and has no
    timeout: a venv build may take minutes and its output must be visible. ``stream`` is ``interactive`` for a
    command that needs no keyboard: its output goes through our streams (so the session log keeps it) and its last
    non-blank line comes back in ``Output.out``, the reason a failure shows."""

    def capture(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> Output:
        try:
            done = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=CHECK_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Output(124, err=f"{argv[0]}: no answer within {CHECK_TIMEOUT_SECONDS}s")
        except OSError as error:  # missing, not executable, no shebang, a file where a directory should be
            return Output(_exit_for(error), err=f"{argv[0]}: cannot run: {error.strerror or error}")
        return Output(done.returncode, done.stdout, done.stderr)

    def stream(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> Output:
        last: list[str] = []
        try:
            with child_owns_ctrl_c():
                code = run_streamed(argv, cwd=cwd, env=env, last_line=last)
        except OSError as error:
            message = f"{argv[0]}: cannot run: {error.strerror or error}"
            print(message, file=sys.stderr)
            return Output(_exit_for(error), out=message)
        return Output(code, out="".join(last))

    def interactive(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        try:
            with child_owns_ctrl_c():
                return subprocess.run(list(argv), cwd=cwd, env=dict(env), check=False).returncode
        except OSError as error:
            print(f"{argv[0]}: cannot run: {error.strerror or error}", file=sys.stderr)
            return _exit_for(error)


def _first_line(*texts: str) -> str:
    for text in texts:
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    return ""


def _argv(directory: Path, command: Sequence[str]) -> list[str]:
    program = command[0]
    resolved = str(directory / program[2:]) if program.startswith("./") else program
    return [resolved, *command[1:]]


def _run_gate(shell: Shell, directory: Path, command: Sequence[str], env: Mapping[str, str]) -> str:
    """The reason to wait, or ``""`` for go. A gate that cannot answer is never a go."""
    output = shell.capture(_argv(directory, command), cwd=directory, env=env)
    if output.code != 0:
        return ui.t(
            "finish.gate_error", command=command[0], code=output.code, detail=_first_line(output.err, output.out)
        )
    return output.out.strip()


def command_step(spec: FinishStep, *, directory: Path, env: Mapping[str, str], shell: Shell) -> Step:
    """A step from a manifest's ``[[finish]]``: its commands run with ``directory`` as the working directory."""

    def check() -> Probe:
        output = shell.capture(_argv(directory, spec.check), cwd=directory, env=env)
        return Probe(output.code == 0, _first_line(output.out, output.err))

    prepare: Callable[[], Probe] | None = None
    run: Callable[[], int] | None = None
    if spec.prepare:

        def prepare() -> Probe:
            output = shell.stream(_argv(directory, spec.prepare), cwd=directory, env=env)
            return Probe(output.code == 0, output.out or ui.t("finish.exit_code", code=output.code))

    if spec.run:

        def run() -> int:
            return shell.interactive(_argv(directory, spec.run), cwd=directory, env=env)

    return Step(
        id=spec.id,
        title=Text(spec.title, spec.title_ru),
        why=Text(spec.why, spec.why_ru),
        action=Text(spec.action, spec.action_ru),
        check=check,
        gate=(lambda: _run_gate(shell, directory, spec.gate, env)) if spec.gate else None,
        prepare=prepare,
        run=run,
        requires=spec.requires,
        optional=spec.optional,
        reapply=spec.reapply,
    )


# ---------------------------------------------------------------------------------------------------
# The built-in step: the root work of `apply --commit`


@dataclass(frozen=True)
class ApplySettings:
    """``[finish.apply]`` in ``profile.toml`` / ``machine.toml``."""

    gate: tuple[str, ...] = ()
    gate_why: Text = Text("")


def apply_settings(config: Config) -> ApplySettings:
    table = config.table("finish", "apply")
    unknown = sorted(set(table) - {"gate", "gate_why", "gate_why_ru"})
    if unknown:
        raise ConfigError(f"[finish.apply]: unknown key(s) {', '.join(unknown)}; allowed: gate, gate_why, gate_why_ru")
    gate = table.get("gate", [])
    if not isinstance(gate, list) or not all(isinstance(item, str) and item for item in gate):
        raise ConfigError(f"[finish.apply] gate must be a command as a list of strings, got {gate!r}")
    if "gate" in table and not gate:
        raise ConfigError("[finish.apply] gate must not be empty: remove the key for no gate")
    texts: dict[str, str] = {}
    for key in ("gate_why", "gate_why_ru"):
        value = table.get(key, "")
        if not isinstance(value, str):
            raise ConfigError(f"[finish.apply] {key} must be a string, got {value!r}")
        texts[key] = value
    return ApplySettings(tuple(gate), Text(texts["gate_why"], texts["gate_why_ru"]))


def apply_probe(actions: Sequence[Action], last: Run | None) -> Probe:
    """Done when no root action is left that apply could still do and the last finished run did not fail or defer
    anything. A root action the last run reported as ``manual`` waits for a person (another finish step says what to
    do), so it does not keep ``apply`` pending: otherwise a root module that waits for the AMO key would keep the step
    that has to come first pending for ever. A step that unblocks root work says ``reapply``.

    Not done can still be *settled*: the last run finished and every root action left in the plan is one it reported
    (failed, blocked or manual), so no run has anything more to try and the steps that ``require`` apply may go on.
    A ``deferred`` action (root was not obtained) is work a run with root still has to do, so it never settles: an
    interrupted run, deferred root work, or root work no run reported is not settled."""
    from ubuntu_tweaks.journal import action_key

    if last is not None and not last.finished:
        return Probe(False, ui.t("finish.apply.interrupted"))
    entries = last.entries if last is not None else []
    waiting = {entry.key for entry in entries if entry.status == WAITING_FOR_A_PERSON}
    reported = {entry.key for entry in entries if entry.status in ("failed", "blocked", WAITING_FOR_A_PERSON)}
    root_keys = [action_key(action) for action in actions if action.needs_root]
    root_left = sum(1 for key in root_keys if key not in waiting)
    problems = sum(1 for entry in entries if entry.status in APPLY_PROBLEMS)
    if not (root_left or problems):
        return Probe(True)
    deferred = any(entry.status == "deferred" for entry in entries)  # root work no run has done, whatever the plan says
    if last is not None and not deferred and all(key in reported for key in root_keys):
        shown = sum(1 for entry in entries if entry.status in ("failed", "blocked"))
        return Probe(False, ui.t("finish.apply.reported", n=shown), settled=True)
    return Probe(False, ui.t("finish.apply.left", root=root_left, problems=problems))


def plan_probe(ctx: Context) -> Probe:
    """``apply_probe`` on the live plan (needs PyGObject; the caller has checked for it)."""
    from ubuntu_tweaks import journal, reconcile

    try:
        backend = reconcile.open_backend(ctx)
        plan = reconcile.build_plan(ctx, backend)
    except UbuntuTweakError as error:  # a module whose check cannot run: the step stays pending and says why
        return Probe(False, ui.t("finish.apply.plan_error", detail=_first_line(str(error))))
    return apply_probe(plan.actions, journal.previous(journal.journal_path()))


def apply_step(
    settings: ApplySettings,
    *,
    directory: Path,
    shell: Shell,
    env: Mapping[str, str],
    probe: Callable[[], Probe],
    run: Callable[[], int],
    explain: Callable[[int], ExitReport | None] | None = None,
) -> Step:
    """The built-in step. ``run`` starts ``ubuntu-tweak apply --commit`` (the command layer streams it through the
    session log); ``directory`` is where its gate runs; ``explain`` says in plain words what a non-zero exit of ``run``
    means (``Step.explain_exit``)."""
    return Step(
        id=APPLY_ID,
        title=Text("Apply the setup (needs your sudo password)", "Применить настройки (нужен пароль sudo)"),
        why=Text(
            "The packages, system services and the dock need root: only you can type the sudo password.",
            "Пакеты, системные службы и док ставятся с root: пароль sudo можешь ввести только ты.",
        ),
        action=Text(
            "Type the sudo password once when asked; the tool does the rest and checks itself.",
            "Один раз введи пароль sudo, когда спросят; остальное инструмент сделает и проверит сам.",
        ),
        check=probe,
        gate=(lambda: _run_gate(shell, directory, settings.gate, env)) if settings.gate else None,
        run=run,
        gate_why=settings.gate_why,
        explain_exit=explain,
    )


# ---------------------------------------------------------------------------------------------------
# States


def evaluate(
    steps: Sequence[Step], failed: Mapping[str, str] | None = None, reopened: Mapping[str, str] | None = None
) -> dict[str, Status]:
    failed = failed or {}
    reopened = reopened or {}
    probes = {step.id: Probe(False, reopened[step.id]) if step.id in reopened else step.check() for step in steps}
    known = {step.id: step for step in steps}
    statuses: dict[str, Status] = {}
    gates: dict[str, str] = {}  # a gate runs at most once per evaluation

    def gate_reason(step: Step) -> str:
        if step.gate is None:
            return ""
        if step.id not in gates:
            gates[step.id] = step.gate()
        return gates[step.id]

    def satisfied(need: Step) -> bool:
        """Done, or settled and not held back by its own gate: a gated ``apply`` that waits for a release has not
        finished its turn, whatever its last run left behind."""
        probe = probes[need.id]
        return probe.ok or (probe.settled and not gate_reason(need))

    for step in steps:
        probe = probes[step.id]
        if probe.ok:
            statuses[step.id] = Status(State.DONE)
            continue
        unmet = [known[need] for need in step.requires if need in known and not satisfied(known[need])]
        if unmet:
            titles = ", ".join(other.title.get(ui.lang()) for other in unmet)
            statuses[step.id] = Status(State.BLOCKED, ui.t("finish.blocked_by", titles=titles))
            continue
        gate = Gate(gate_reason(step)) if step.gate is not None else None
        if gate is not None and not gate.go:
            statuses[step.id] = Status(State.WAIT, probe.detail, gate)
        elif step.id in failed:
            statuses[step.id] = Status(State.FAILED, failed[step.id], gate)
        else:
            statuses[step.id] = Status(State.PENDING, probe.detail, gate)
    return statuses


def requirements(steps: Sequence[Step], step_id: str) -> set[str]:
    """Every step ``step_id`` needs, directly or through others (ids nobody declares are ignored: their module is off)."""
    known = {step.id: step for step in steps}
    found: set[str] = set()
    pending = [need for need in known[step_id].requires if need in known] if step_id in known else []
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(need for need in known[current].requires if need in known)
    return found


def validate(steps: Sequence[Step]) -> None:
    """A step that requires itself, or steps that require each other, could never run: refuse them up front."""
    for step in steps:
        if step.id in requirements(steps, step.id):
            raise ConfigError(f"finish step {step.id!r} requires itself (directly or through other steps)")


def closure(steps: Sequence[Step], step_id: str) -> list[Step]:
    """``step_id`` and what it requires, in the original order."""
    if all(step.id != step_id for step in steps):
        raise ConfigError(f"unknown finish step {step_id!r}; the steps are: {', '.join(step.id for step in steps)}")
    wanted = {step_id} | requirements(steps, step_id)
    return [step for step in steps if step.id in wanted]


def _left(steps: Sequence[Step], statuses: Mapping[str, Status]) -> list[Step]:
    """The required steps that are not done."""
    return [step for step in steps if not step.optional and statuses[step.id].state is not State.DONE]


def exit_code(steps: Sequence[Step], statuses: Mapping[str, Status]) -> int:
    return EXIT_PENDING if _left(steps, statuses) else 0


# ---------------------------------------------------------------------------------------------------
# Presentation

_BADGE = {
    State.DONE: "ok",
    State.PENDING: "need",
    State.WAIT: "manual",
    State.BLOCKED: "info",
    State.FAILED: "error",
}


def _say(out: TextIO, lines: Sequence[str]) -> None:
    out.write("\n".join(lines) + "\n")


def _announce(out: TextIO, kind: str, text: str) -> None:
    _say(out, [ui.say(kind, text)])


def _badge(state: State) -> str:
    word = ui.t(f"finish.state.{state.value}")
    if not ui.rich():
        return f"[{word}]"
    return f"{ui.symbol(_BADGE[state])} {ui.style(word, 'bold')}"


def _gate_lines(step: Step, status: Status, *, indent: int) -> list[str]:
    if status.gate is None:
        return []
    if status.gate.go:
        return ui.wrap(ui.t("finish.gate.go"), indent=indent, names=("green", "bold"))
    why = step.gate_why.get(ui.lang())
    head = ui.t("finish.gate.wait", why=why) if why else ui.t("finish.gate.wait_plain")
    lines = ui.wrap(head, indent=indent, names=("yellow", "bold"))
    for line in status.gate.reason.splitlines():
        lines += ui.wrap(line, indent=indent + 2, names=("yellow",))
    return lines


def _step_lines(number: int, total: int, step: Step, status: Status, *, full: bool) -> list[str]:
    lang = ui.lang()
    optional = f" {ui.t('finish.optional')}" if step.optional else ""
    head = (
        f"{ui.style(f'{number}/{total}', 'dim')} {_badge(status.state)}  {ui.heading(step.title.get(lang))}{optional}"
    )
    lines = [head]
    if status.state is State.DONE:
        return lines
    if status.detail:
        lines += ui.wrap(status.detail, indent=6)
    if full:
        lines += ui.wrap(f"{ui.t('finish.why')} {step.why.get(lang)}", indent=6)
        lines += ui.wrap(f"{ui.t('finish.action')} {step.action.get(lang)}", indent=6, names=("cyan",))
    lines += _gate_lines(step, status, indent=6)
    return lines


def _runnable(step: Step) -> bool:
    """A step with neither a machine part nor a command is only listed: the person does it on their own."""
    return step.prepare is not None or step.run is not None


def _offerable(step: Step, status: Status, *, assume_yes: bool) -> bool:
    """Will the walk offer this step: it has something to run, is pending and is not an optional one under ``--yes``."""
    return _runnable(step) and status.state in ACTIONABLE and not (assume_yes and step.optional)


def _checklist(steps: Sequence[Step], statuses: Mapping[str, Status], *, titles_only: frozenset[str]) -> list[str]:
    """One block per step. A step in ``titles_only`` is listed by its title alone: its full card follows, or the list
    is the closing summary. The overview does not say everything twice."""
    lines: list[str] = []
    for number, step in enumerate(steps, 1):
        lines += _step_lines(number, len(steps), step, statuses[step.id], full=step.id not in titles_only)
        lines.append("")
    return lines[:-1] if lines else lines


def _summary(steps: Sequence[Step], statuses: Mapping[str, Status], command: str) -> list[str]:
    left = _left(steps, statuses)
    if not left:
        return ui.wrap(ui.t("finish.summary.all"), names=("green", "bold"))
    lines = ui.wrap(ui.t("finish.summary.left", n=len(left)), names=("yellow", "bold"))
    waiting = [step for step in left if statuses[step.id].state is State.WAIT]
    if waiting:
        lines += ui.wrap(
            ui.t("finish.summary.wait", titles=", ".join(s.title.get(ui.lang()) for s in waiting)), indent=2
        )
    lines += ui.wrap(ui.t("finish.summary.again", command=command), indent=2, names=("cyan",))
    return lines


def _decide(ask: Callable[[str], str], out: TextIO) -> Decision:
    """Enter starts the step, ``s`` skips it, ``q`` (or end of input, or Ctrl-C) stops the walk. Anything else is
    asked again: a step with a sudo prompt behind it must never start on a stray ``n`` or ``no``."""
    while True:
        try:
            answer = ask(ui.t("finish.pause")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return Decision.QUIT
        match answer:
            case "":
                return Decision.START
            case "s":
                return Decision.SKIP
            case "q":
                return Decision.QUIT
        _announce(out, "manual", ui.t("finish.pause_again"))


def _attempt(step: Step, out: TextIO) -> str:
    """The machine part, then the human part. Returns a problem text, or ``""`` when both went well."""
    try:
        if step.prepare is not None:
            _announce(out, "info", ui.t("finish.prepare"))
            prepared = step.prepare()
            if not prepared.ok:
                return ui.t("finish.prepare_failed", detail=prepared.detail)
        if step.run is not None:
            code = step.run()
            if code in INTERRUPTED_EXITS:
                _say(out, [""])
                return ui.t("finish.interrupted")
            if code != 0:
                report = step.explain_exit(code) if step.explain_exit is not None else None
                if report is None:
                    return ui.t("finish.run_failed", code=code)
                if report.text:
                    _say(out, ["", report.text])
                return report.problem
    except KeyboardInterrupt:  # Ctrl-C inside the step's own command: the walk goes on to its summary
        _say(out, [""])
        return ui.t("finish.interrupted")
    return ""


class Walked(NamedTuple):
    """What ``walk`` ends with: the steps it looked at last (running one can add steps) and the state of each."""

    steps: list[Step]
    statuses: dict[str, Status]


class Obligations(Protocol):
    """Done steps that have to run again, as ``{step id: why}``; they survive a quit, a failure and a new run."""

    def load(self) -> dict[str, str]: ...

    def save(self, owed: Mapping[str, str]) -> None: ...


class MemoryObligations:
    def __init__(self) -> None:
        self.owed: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        return dict(self.owed)

    def save(self, owed: Mapping[str, str]) -> None:
        self.owed = dict(owed)


class FileObligations:
    """``<state>/ubuntu-tweaks/finish-state.json``, one entry per scope (profile and machine): what A owes is not paid
    by applying B. A missing or unreadable file owes nothing: the worst case is that ``apply`` is offered once less,
    and the journal still says what is left."""

    def __init__(self, path: Path, scope: str) -> None:
        self._path = path
        self._scope = scope

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                str(scope): {str(key): str(value) for key, value in owed.items()}
                for scope, owed in document["scopes"].items()
            }
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            return {}

    def load(self) -> dict[str, str]:
        return self._read().get(self._scope, {})

    def save(self, owed: Mapping[str, str]) -> None:
        scopes = self._read()
        if owed:
            scopes[self._scope] = dict(owed)
        else:
            scopes.pop(self._scope, None)
        document = json.dumps({"scopes": scopes}, ensure_ascii=False, indent=1) + "\n"
        atomic_write(self._path, document.encode("utf-8"))


@dataclass
class _Progress:
    """What the walk remembers between steps."""

    tries: Counter[str] = field(default_factory=Counter)
    skipped: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)
    owed: dict[str, str] = field(default_factory=dict)


def _next_step(
    steps: Sequence[Step], statuses: Mapping[str, Status], progress: _Progress, *, assume_yes: bool
) -> tuple[int, Step] | None:
    for number, step in enumerate(steps, 1):
        if (
            _offerable(step, statuses[step.id], assume_yes=assume_yes)
            and progress.tries[step.id] < MAX_TRIES
            and step.id not in progress.skipped
        ):
            return number, step
    return None


def _after_attempt(
    out: TextIO, step: Step, problem: str, progress: _Progress, steps: Sequence[Step], obligations: Obligations
) -> dict[str, Status]:
    """Check everything again after ``step`` was tried, say how it went and remember what must run again."""
    title = step.title.get(ui.lang())
    created = False  # did this attempt add the apply debt (only that one may be taken back below)
    if problem:
        progress.failed[step.id] = problem
    else:
        progress.failed.pop(step.id, None)
        if progress.owed.pop(step.id, None) is not None:  # an owed run that went well is paid; a failed one stays owed
            obligations.save(progress.owed)
        if step.reapply and APPLY_ID not in progress.owed:
            created = True
            # written before the slow re-check: a Ctrl-C or a closed terminal after this point cannot lose it
            progress.owed[APPLY_ID] = ui.t("finish.reapply", title=title)
            obligations.save(progress.owed)
            progress.tries[APPLY_ID] = 0
            progress.skipped.discard(APPLY_ID)
    _announce(out, "info", ui.t("finish.checking"))
    statuses = evaluate(steps, progress.failed, progress.owed)
    after = statuses.get(step.id)
    if after is None:  # the step is gone (a step's command changed the profile): nothing more to say about it
        return statuses
    if after.state is State.DONE:
        progress.failed.pop(step.id, None)
        _announce(out, "ok", ui.t("finish.step_done", title=title))
        return statuses
    if created:  # it did not take: nothing is unlocked, so the debt this attempt added is taken back
        del progress.owed[APPLY_ID]
        obligations.save(progress.owed)
        statuses = evaluate(steps, progress.failed, progress.owed)
    if after.state is State.PENDING:  # it was tried and did not take: shown as failed, not as untouched
        progress.failed.setdefault(step.id, after.detail)
        statuses = {**statuses, step.id: Status(State.FAILED, progress.failed[step.id], after.gate)}
    _announce(
        out, "error", ui.t("finish.step_not_done", title=title, detail=progress.failed.get(step.id, after.detail))
    )
    if progress.tries[step.id] >= MAX_TRIES:
        _announce(out, "manual", ui.t("finish.gave_up", title=title))
    return statuses


def walk(
    steps: Sequence[Step] | Callable[[], Sequence[Step]],
    *,
    out: TextIO,
    ask: Callable[[str], str],
    interactive: bool,
    assume_yes: bool,
    dry_run: bool = False,
    command: str = DEFAULT_COMMAND,
    obligations: Obligations | None = None,
) -> Walked:
    source: Callable[[], Sequence[Step]] = steps if callable(steps) else (lambda: steps)
    ledger = obligations if obligations is not None else MemoryObligations()
    current = list(source())
    _say(out, [ui.heading(ui.t("finish.title")), *ui.wrap(ui.t("finish.intro")), ""])
    _announce(out, "info", ui.t("finish.checking"))
    progress = _Progress(owed=ledger.load())
    statuses = evaluate(current, reopened=progress.owed)
    walking = interactive and not dry_run
    offered = frozenset(s.id for s in current if _offerable(s, statuses[s.id], assume_yes=assume_yes))
    _say(out, ["", *_checklist(current, statuses, titles_only=offered if walking else frozenset()), ""])
    if dry_run or not offered:
        _say(out, _summary(current, statuses, command))
        return Walked(current, statuses)
    if not interactive:
        _announce(out, "manual", ui.t("finish.needs_terminal", command=command))
        _say(out, _summary(current, statuses, command))
        return Walked(current, statuses)

    while (found := _next_step(current, statuses, progress, assume_yes=assume_yes)) is not None:
        number, step = found
        _say(out, ["", *_step_lines(number, len(current), step, statuses[step.id], full=True)])
        if not assume_yes:
            decision = _decide(ask, out)
            if decision is Decision.QUIT:
                break
            if decision is Decision.SKIP:
                progress.skipped.add(step.id)
                continue
        progress.tries[step.id] += 1
        problem = _attempt(step, out)
        try:
            current = list(source())  # running a step (apply clones the profile) may have added steps
        except UbuntuTweakError as error:  # the new profile is invalid: say so and finish with what is known
            _announce(out, "error", str(error))
        statuses = _after_attempt(out, step, problem, progress, current, ledger)
    _say(
        out,
        [
            "",
            ui.heading(ui.t("finish.summary.title")),
            *_checklist(current, statuses, titles_only=frozenset(s.id for s in current)),
            "",
        ],
    )
    _say(out, _summary(current, statuses, command))
    return Walked(current, statuses)

"""Actions and the plan: what ``apply`` previews and ``apply --commit`` runs.

Accessed via: ``apply`` / ``status`` / ``init`` / ``update`` build a list of ``Action`` objects
(one subclass per kind of change), pass it to ``Plan.build``, print ``Plan.render()`` and, with
``--commit``, call ``Plan.execute()``.

Assumptions: ``check()`` and ``describe()`` are read-only and cheap enough to call twice;
``run()`` is the only mutation. The preview and the commit are the same ``Plan`` object, so
"what you saw is what runs". A plan holds only actions whose ``check()`` is false.

Two rules for whoever builds the candidate list, because ``Plan.build`` evaluates every
``check()`` once, before anything runs:

- Candidates that write the same state in different layers (module preset, profile panel file,
  machine overlay) must be collapsed to the winning layer by the builder; a dropped lower-layer
  action whose check happens to pass would otherwise be undone by a kept one.
- ``check()`` must return False, not raise, for state that an earlier phase creates (a schema
  of an extension that is not linked yet): the action then shows up in the plan.

Public API:

- ``Phase`` (``IntEnum``): the plan order, ``BASE`` < ``SUBMODULE`` < ``MIGRATE`` < ``SCHEMAS`` <
  ``PACKAGES`` < ``SYSTEM`` < ``LINK`` (extension symlinks) < ``FILES`` (profile file links) <
  ``TOGGLE`` (enable/disable) < ``SETTINGS_MODULE`` < ``SETTINGS_PROFILE`` <
  ``SETTINGS_MACHINE`` < ``SEED``. ``BASE`` holds the init pipeline's tool installs (base tools,
  zsh, clipboard, git/gh, brew): they run before even the submodule init.
- ``DEFAULT_ORDER`` (100) and ``Action.order``: the tie-break inside a phase (lower first, stable
  otherwise). A module's ``order`` in ``module.toml`` sets it for the module's actions (spec,
  *Init pipeline*).
- ``Action`` (ABC): class/instance attributes ``phase: Phase``, ``kind: str`` (the short label
  shown in the plan, e.g. ``"set-key"``), ``needs_root: bool = False`` and ``scope: str | None
  = None`` (the module or panel the action belongs to, matched by ``--only``); methods
  ``describe() -> str``, ``check() -> bool`` (True = already satisfied) and ``run() -> None``.
  ``run()`` may raise ``ManualStepRequired(message)`` when only the user can finish it (a missing
  credential): the plan goes on, the action stays unsatisfied and the message is reported as
  ``needs manual: ...`` (``ExecutionResult.manual``).
- ``Plan.build(candidates, *, only=None) -> Plan``: when ``only`` is non-empty, keeps the
  candidates whose ``scope`` is in it (a candidate with ``scope=None`` is dropped); an empty or
  ``None`` ``only`` selects everything. Orders by ``(phase, order)`` (stable inside both), keeps the
  actions whose ``check()`` is false and counts the satisfied ones in ``Plan.satisfied``.
- ``Plan.actions``, ``Plan.satisfied``, ``len(plan)``.
- ``Plan.render(*, no_sudo=False) -> str``: the preview text; root actions carry ``[sudo]`` and,
  with ``no_sudo``, say they are skipped.
- ``Plan.execute(*, no_sudo=False, on_start=None) -> ExecutionResult``: runs the actions in
  order, skips root actions when ``no_sudo``, skips (without counting) an action that became
  satisfied since ``build`` (an earlier action or another process did its work), verifies
  ``check()`` after each ``run()`` and raises ``ActionFailed`` (exit 1) when it still fails.
  ``on_start`` is called with each action before it runs.
- ``ExecutionResult(changed, skipped, manual)``, ``ExecutionResult.render_skipped() -> str`` (the
  ``--no-sudo`` skips) and ``ExecutionResult.render_manual() -> str`` (the closing ``Manual steps`` block).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from enum import IntEnum

from ubuntu_tweaks.errors import UbuntuTweakError

DEFAULT_ORDER = 100


class Phase(IntEnum):
    BASE = 5
    SUBMODULE = 10
    MIGRATE = 20
    SCHEMAS = 30
    PACKAGES = 40
    SYSTEM = 50
    LINK = 60
    FILES = 70
    TOGGLE = 80
    SETTINGS_MODULE = 90
    SETTINGS_PROFILE = 100
    SETTINGS_MACHINE = 110
    SEED = 120


class ActionFailed(UbuntuTweakError):
    """An action ran but its ``check()`` still fails: the plan does not converge."""


class ManualStepRequired(UbuntuTweakError):
    """Raised by ``Action.run()`` when the action cannot finish without the user (a credential that is
    not in the keychain). The plan goes on, the action stays unsatisfied (``status`` lists it as Missing)
    and the message is printed as ``needs manual: <message>``. ``run()`` must raise it *before* any side
    effect: a staged ``apply`` rebuilds the plan, so the same action runs (and raises) once per pass."""


class Action(ABC):
    phase: Phase
    kind: str
    needs_root: bool = False
    scope: str | None = None
    order: float = DEFAULT_ORDER
    key: str | None = None  # stable identity across plan rebuilds (``describe()`` can embed live state)

    @abstractmethod
    def describe(self) -> str:
        """One line saying what ``run()`` will do, e.g. ``set /org/x/key = 5``."""

    @abstractmethod
    def check(self) -> bool:
        """True when the system already matches; must not change anything."""

    @abstractmethod
    def run(self) -> None:
        """Make ``check()`` true."""


@dataclass(frozen=True)
class ExecutionResult:
    changed: int
    skipped: tuple[Action, ...] = ()
    manual: tuple[tuple[Action, str], ...] = ()

    def render_skipped(self) -> str:
        if not self.skipped:
            return ""
        lines = [f"Skipped {len(self.skipped)} root action(s) because of --no-sudo:"]
        lines += [f"  [{action.kind}] {action.describe()}" for action in self.skipped]
        return "\n".join(lines)

    def render_manual(self) -> str:
        """The closing ``Manual steps`` block: what only the user can finish (spec, *Manual steps*)."""
        if not self.manual:
            return ""
        lines = [
            f"Manual steps ({len(self.manual)}): what is left for you, see docs/specs/ubuntu-tweak-cli.md, Manual steps"
        ]
        lines += [f"  {number}. [{action.kind}] {message}" for number, (action, message) in enumerate(self.manual, 1)]
        return "\n".join(lines)


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...] = ()
    satisfied: int = 0

    @classmethod
    def build(cls, candidates: Iterable[Action], *, only: Collection[str] | None = None) -> Plan:
        selected = [action for action in candidates if not only or action.scope in only]
        ordered = sorted(selected, key=lambda action: (action.phase, action.order))
        pending = tuple(action for action in ordered if not action.check())
        return cls(pending, len(ordered) - len(pending))

    def __len__(self) -> int:
        return len(self.actions)

    def render(self, *, no_sudo: bool = False) -> str:
        if not self.actions:
            return f"Nothing to do ({self.satisfied} already satisfied)."
        root = sum(action.needs_root for action in self.actions)
        header = f"Plan: {len(self.actions)} action(s) to run, {self.satisfied} already satisfied"
        if root:
            header += f", {root} need root"
        lines = [header]
        for number, action in enumerate(self.actions, 1):
            marker = ""
            if action.needs_root:
                marker = "[sudo, skipped: --no-sudo] " if no_sudo else "[sudo] "
            lines.append(f"{number:>3}. {marker}[{action.kind}] {action.describe()}")
        return "\n".join(lines)

    def execute(
        self,
        *,
        no_sudo: bool = False,
        on_start: Callable[[Action], None] | None = None,
    ) -> ExecutionResult:
        changed = 0
        skipped: list[Action] = []
        manual: list[tuple[Action, str]] = []
        for action in self.actions:
            if action.check():
                continue
            if action.needs_root and no_sudo:
                skipped.append(action)
                continue
            if on_start is not None:
                on_start(action)
            try:
                action.run()
            except ManualStepRequired as needed:
                manual.append((action, str(needed)))
                continue
            if not action.check():
                raise ActionFailed(f"[{action.kind}] {action.describe()}: still not satisfied after running")
            changed += 1
        return ExecutionResult(changed, tuple(skipped), tuple(manual))

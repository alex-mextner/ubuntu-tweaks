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
  ``PACKAGES`` < ``SYSTEM`` < ``LINK`` (extension symlinks) < ``FILES`` (profile file links) < ``UNITS`` (user
  units: daemon-reload, enable, start, linger) < ``TOGGLE`` (enable/disable) < ``SETTINGS_MODULE`` <
  ``SETTINGS_PROFILE`` < ``SETTINGS_MACHINE`` < ``SEED``. ``BASE`` holds the init pipeline's tool installs (base tools,
  zsh, clipboard, git/gh, brew): they run before even the submodule init.
- ``DEFAULT_ORDER`` (100) and ``Action.order``: the tie-break inside a phase (lower first, stable
  otherwise). A module's ``order`` in ``module.toml`` sets it for the module's actions (spec,
  *Init pipeline*).
- ``Action`` (ABC): class/instance attributes ``phase: Phase``, ``kind: str`` (the short label
  shown in the plan, e.g. ``"set-key"``), ``needs_root: bool = False`` and ``scope: str | None
  = None`` (the module or panel the action belongs to, matched by ``--only``); methods
  ``describe() -> str``, ``why() -> str`` (optional: one sentence on why this kind of action exists;
  default ``ui.kind_why(kind)``), ``check() -> bool`` (True = already satisfied) and ``run() -> None``.
  ``run()`` may raise ``ManualStepRequired(message)`` when only the user can finish it (a missing
  credential): the plan goes on, the action stays unsatisfied and the message is reported as
  ``needs manual: ...`` (``ExecutionResult.manual``).
  ``blocker() -> Action | None`` (optional, default ``None``): an action standing for what this one needs and
  lacks (a unit file nobody provides); asked right before ``run()``, a non-``None`` answer makes the action
  *blocked by* it: reported, never run, not a failure.
- ``Plan.build(candidates, *, only=None) -> Plan``: when ``only`` is non-empty, keeps the
  candidates whose ``scope`` is in it (a candidate with ``scope=None`` is dropped); an empty or
  ``None`` ``only`` selects everything. Orders by ``(phase, order)`` (stable inside both), keeps the
  actions whose ``check()`` is false and counts the satisfied ones in ``Plan.satisfied``.
- ``Plan.actions``, ``Plan.satisfied``, ``len(plan)``.
- ``Plan.render(*, no_sudo=False) -> str``: the preview text; root actions carry ``[sudo]`` and,
  with ``no_sudo``, say they are skipped. In a terminal (``ui.rich()``) it is the explained
  preview: badge, ``[sudo]``, what, and a dim ``why`` (``Action.why()``) with the module.
- ``Plan.execute(*, no_sudo=False, on_start=None) -> ExecutionResult``: runs the actions in
  order, skips root actions when ``no_sudo``, skips (without counting) an action that became
  satisfied since ``build`` (an earlier action or another process did its work), verifies
  ``check()`` after each ``run()`` and raises ``ActionFailed`` (exit 1) when it still fails.
  ``on_start`` is called with each action before it runs, ``on_done`` after it ran and verified.
- ``ExecutionResult(changed, skipped, manual)``, ``ExecutionResult.render_skipped() -> str`` (the
  ``--no-sudo`` skips) and ``ExecutionResult.render_manual() -> str`` (the closing ``Manual steps`` block).
  Like ``Plan.render`` they print the compact text, or the explained one (``ui.rich()``).
"""

from __future__ import annotations

import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from ubuntu_tweaks import ui
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
    UNITS = 75
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


class Transient(ActionFailed):
    """A failure worth retrying: the network, a package lock or a busy service. ``Plan.execute`` retries it with
    backoff; once the attempts are used it is a permanent failure that says it was transient."""


EX_TEMPFAIL = 75  # sysexits.h: an installer's "try again later"
_TRANSIENT_TEXT = re.compile(
    r"could not resolve|name or service not known|temporary failure|temporarily unavailable|timed out|timeout"
    r"|connection (?:reset|refused|closed)|network is unreachable|could not get lock|unable to acquire the dpkg"
    r"|dpkg frontend lock|resource temporarily unavailable|try again|http (?:429|502|503|504)"
    r"|\b(?:429|502|503|504)\b.*(?:too many|bad gateway|unavailable|gateway)|rate limit|ssl: |tls handshake",
    re.IGNORECASE,
)


def is_transient(error: BaseException) -> bool:
    """``Transient``, or an error whose text names a network / lock / busy cause (``returncode`` 75 counts too)."""
    if isinstance(error, Transient):
        return True
    if getattr(error, "returncode", None) == EX_TEMPFAIL:
        return True
    return _TRANSIENT_TEXT.search(str(error)) is not None


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter: attempt ``n`` (0-based) waits ``uniform(0, min(cap, base * 2**n))``."""

    attempts: int = 3
    base: float = 2.0
    cap: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[float, float], float] = random.uniform

    def delay(self, attempt: int) -> float:
        return self.jitter(0.0, min(self.cap, self.base * 2**attempt))


@dataclass(frozen=True)
class Failure:
    """An action that failed for good: ``transient`` says the last error looked retryable (the attempts ran out),
    ``tried`` lists the fallbacks that were attempted, ``fix`` is the error's own next command when it has one."""

    action: Action
    message: str
    transient: bool
    attempts: int
    tried: tuple[str, ...] = ()
    fix: str | None = None

    def next_command(self) -> str:
        if self.fix:
            return self.fix
        scope = self.action.scope
        return f"ubuntu-tweak apply --commit --only {scope}" if scope else "ubuntu-tweak apply --commit"


@dataclass(frozen=True)
class Blocked:
    """An action that was not tried because ``by`` (an action it needs) failed or was itself blocked."""

    action: Action
    by: Action


def _fix(error: BaseException) -> str | None:
    return error.fix if isinstance(error, UbuntuTweakError) else None


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

    def why(self) -> str:
        """One sentence on why this kind of action exists (shown in interactive output); the default is the
        per-kind wording of ``ui.kind_why``. Override for an action whose reason depends on its state."""
        return ui.kind_why(self.kind)

    @abstractmethod
    def check(self) -> bool:
        """True when the system already matches; must not change anything."""

    def blocker(self) -> Action | None:
        """An action that must exist for this one to be tried and does not (a unit file nobody provides). Asked
        right before ``run()``; when it returns one the action is *blocked* by it (spec, *Reconcile and failover*),
        never run and never counted as failed."""
        return None

    @abstractmethod
    def run(self) -> None:
        """Make ``check()`` true."""

    def alternatives(self) -> Sequence[Action]:
        """Other ways to reach the same ``check()`` (spec, *Reconcile and failover*): tried in order when ``run()``
        failed for good, or when this action needs root and root is not available. Each runs through the same
        retry policy and counts only when this action's ``check()`` then passes."""
        return ()


@dataclass(frozen=True)
class ExecutionResult:
    changed: int
    skipped: tuple[Action, ...] = ()
    manual: tuple[tuple[Action, str], ...] = ()
    failed: tuple[Failure, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    fallbacks: tuple[tuple[Action, str], ...] = ()  # (action, the alternative that made it pass)

    def render_skipped(self) -> str:
        if not self.skipped:
            return ""
        if ui.rich():
            lines = [ui.section(ui.t("skipped.title"), len(self.skipped), "manual")]
            lines += ui.wrap(ui.t("skipped.fix"), indent=2)
            for action in self.skipped:
                lines += ui.wrap(action.describe(), first=f"  {ui.symbol('manual')} ", names=())
            return "\n".join(lines)
        lines = [f"Skipped {len(self.skipped)} root action(s) because of --no-sudo:"]
        lines += [f"  [{action.kind}] {action.describe()}" for action in self.skipped]
        return "\n".join(lines)

    def render_manual(self) -> str:
        """The closing ``Manual steps`` block: what only the user can finish (spec, *Manual steps*)."""
        if not self.manual:
            return ""
        if ui.rich():
            lines = [ui.section(ui.t("manual.title"), len(self.manual), "manual")]
            lines += ui.wrap(ui.t("manual.what"), indent=2)
            for number, (_action, message) in enumerate(self.manual, 1):
                lines += ui.wrap(message, first=f"  {number}. ", names=("yellow",))
            return "\n".join(lines)
        lines = [
            f"Manual steps ({len(self.manual)}): what is left for you, see docs/specs/ubuntu-tweak-cli.md, Manual steps"
        ]
        lines += [f"  {number}. [{action.kind}] {message}" for number, (action, message) in enumerate(self.manual, 1)]
        return "\n".join(lines)

    def render_failed(self) -> str:
        """The ``Failed`` block: what went wrong, whether it was transient, what was tried and the next command."""
        if not self.failed:
            return ""
        lines = (
            [ui.section(ui.t("failed.title"), len(self.failed), "manual")]
            if ui.rich()
            else [f"Failed ({len(self.failed)}): the run went on without these"]
        )
        for number, failure in enumerate(self.failed, 1):
            cause = failure.message.strip().splitlines()[0] if failure.message.strip() else "failed"
            how = f"after {failure.attempts} attempts (transient)" if failure.transient else "permanent"
            lines.append(f"  {number}. [{failure.action.kind}] {failure.action.describe()}: {cause} ({how})")
            lines += [f"       tried: {line}" for line in failure.tried]
            lines.append(f"       next: {failure.next_command()}")
        return "\n".join(lines)

    def render_blocked(self) -> str:
        if not self.blocked:
            return ""
        lines = (
            [ui.section(ui.t("blocked.title"), len(self.blocked), "need")]
            if ui.rich()
            else [f"Blocked ({len(self.blocked)}): not tried because something they need failed"]
        )
        for item in self.blocked:
            lines.append(
                f"  [{item.action.kind}] {item.action.describe()}: blocked by [{item.by.kind}] {item.by.describe()}"
            )
        return "\n".join(lines)

    def render_fallbacks(self) -> str:
        return "\n".join(f"  [{action.kind}] {action.describe()}: via fallback {how}" for action, how in self.fallbacks)


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
        if ui.rich():
            return self._render_rich(no_sudo)
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

    def _render_rich(self, no_sudo: bool) -> str:
        """The interactive preview: a header, then per action a badge, ``[sudo]`` when it needs root, what it
        does and (dim) why and which module it belongs to."""
        if not self.actions:
            return f"{ui.badge('ok')} {ui.style(ui.t('apply.nothing', satisfied=self.satisfied), 'green')}"
        root = sum(action.needs_root for action in self.actions)
        summary = ui.t("apply.summary", n=len(self.actions), satisfied=self.satisfied)
        if root:
            summary += ", " + ui.t("apply.root", n=root)
        lines = [ui.heading(ui.t("apply.title")), "  " + ui.style(summary, "dim"), ""]
        for number, action in enumerate(self.actions, 1):
            skipped = action.needs_root and no_sudo
            tag = ""
            if action.needs_root:
                tag = "[sudo" + (", " + ui.t("apply.skipped_root") if skipped else "") + "] "
            lead = f"{ui.style(f'{number:>3}.', 'dim')} {ui.symbol('manual' if skipped else 'need')} "
            lines += ui.wrap(action.describe(), first=lead, names=(), accent=tag)
            reason = " ".join(
                part
                for part in (
                    f"{ui.t('why')} {action.why()}" if action.why() else "",
                    f"(module {action.scope})" if action.scope else "",
                )
                if part
            )
            if reason:
                lines += ui.wrap(reason, indent=7)
        return "\n".join(lines)

    def execute(
        self,
        *,
        no_sudo: bool = False,
        on_start: Callable[[Action], None] | None = None,
        on_done: Callable[[Action], None] | None = None,
        policy: RetryPolicy | None = None,
        requires: Mapping[str, Collection[str]] | None = None,
        observer: Observer | None = None,
        on_retry: Callable[[Action, int, float, str], None] | None = None,
    ) -> ExecutionResult:
        """Run the plan; a failing action is recorded and the run goes on (spec, *Reconcile and failover*)."""
        execution = _Execution(
            no_sudo=no_sudo,
            policy=policy or RetryPolicy(),
            requires=requires or {},
            on_start=on_start,
            on_done=on_done,
            observer=observer,
            on_retry=on_retry,
        )
        for action in self.actions:
            execution.step(action)
        return execution.result()


type Observer = Callable[[Action, str, str], None]

CHAIN_BLOCKERS = frozenset({"submodule", "compile-schemas", "link", "link-replace"})
CHAIN_BLOCKED = frozenset({Phase.TOGGLE, Phase.SETTINGS_MODULE, Phase.SEED})


class _Execution:
    """The state of one ``Plan.execute`` call: results, failed scopes and the retry / fallback mechanics."""

    def __init__(
        self,
        *,
        no_sudo: bool,
        policy: RetryPolicy,
        requires: Mapping[str, Collection[str]],
        on_start: Callable[[Action], None] | None,
        on_done: Callable[[Action], None] | None,
        observer: Observer | None,
        on_retry: Callable[[Action, int, float, str], None] | None,
    ) -> None:
        self.no_sudo = no_sudo
        self.policy = policy
        self.requires = requires
        self.on_start = on_start
        self.on_done = on_done
        self.observer = observer
        self.on_retry = on_retry
        self.changed = 0
        self.skipped: list[Action] = []
        self.manual: list[tuple[Action, str]] = []
        self.failed: list[Failure] = []
        self.blocked: list[Blocked] = []
        self.fallbacks: list[tuple[Action, str]] = []
        self.unhealthy: dict[str, Action] = {}  # scope -> the action that failed or was blocked first
        self.chain: dict[str, Action] = {}  # scope -> the first failed code-installing action

    def result(self) -> ExecutionResult:
        return ExecutionResult(
            self.changed,
            tuple(self.skipped),
            tuple(self.manual),
            tuple(self.failed),
            tuple(self.blocked),
            tuple(self.fallbacks),
        )

    def _note(self, action: Action, status: str, detail: str = "") -> None:
        if self.observer is not None:
            self.observer(action, status, detail)

    def _blocker(self, action: Action) -> Action | None:
        named = action.blocker()
        if named is not None:
            return named
        scope = action.scope
        if scope is None:
            return None
        for needed in self.requires.get(scope, ()):
            if needed in self.unhealthy:
                return self.unhealthy[needed]
        chained = self.chain.get(scope)
        if chained is not None and action.phase in CHAIN_BLOCKED and action.phase > chained.phase:
            return chained
        return None

    def _try(self, action: Action, runner: Callable[[], None]) -> tuple[BaseException | None, int]:
        """Run ``runner`` with the retry policy; ``(error or None, attempts used)``. A manual step propagates."""
        for attempt in range(1, max(self.policy.attempts, 1) + 1):
            try:
                runner()
            except ManualStepRequired:
                raise
            except Exception as error:
                if is_transient(error) and attempt < self.policy.attempts:
                    delay = self.policy.delay(attempt - 1)
                    if self.on_retry is not None:
                        self.on_retry(action, attempt, delay, str(error))
                    self.policy.sleep(delay)
                    continue
                return error, attempt
            return None, attempt
        return None, 0

    def _fail(self, action: Action, error: BaseException, attempts: int, tried: Sequence[str]) -> None:
        self.failed.append(
            Failure(
                action, str(error) or type(error).__name__, is_transient(error), attempts, tuple(tried), _fix(error)
            )
        )
        if action.scope is not None:
            self.unhealthy.setdefault(action.scope, action)
            if action.kind in CHAIN_BLOCKERS:
                self.chain.setdefault(action.scope, action)
        self._note(action, "failed", str(error))

    def _fallback(self, action: Action, tried: list[str]) -> bool:
        """Try the declared alternatives in order; True when one made the primary's ``check()`` pass."""
        for alternative in action.alternatives():
            if alternative.needs_root and self.no_sudo:
                tried.append(f"{alternative.describe()} (needs root: skipped)")
                continue
            try:
                error, _attempts = self._try(alternative, alternative.run)
            except ManualStepRequired as needed:
                tried.append(f"{alternative.describe()} (needs you: {needed})")
                continue
            if error is None and action.check():
                self.fallbacks.append((action, alternative.describe()))
                return True
            tried.append(f"{alternative.describe()} ({error or 'check still fails'})")
        return False

    def step(self, action: Action) -> None:
        try:
            if action.check():
                return
        except Exception as error:
            self._fail(action, error, 1, ())
            return
        blocker = self._blocker(action)
        if blocker is not None:
            self.blocked.append(Blocked(action, blocker))
            if action.scope is not None:
                self.unhealthy.setdefault(action.scope, blocker)
            self._note(action, "blocked", f"{blocker.kind} {blocker.describe()}")
            return
        tried: list[str] = []
        if action.needs_root and self.no_sudo:
            if not self._fallback(action, tried):
                self.skipped.append(action)
                self._note(action, "deferred", "needs root")
                return
            self._succeeded(action)
            return
        if self.on_start is not None:
            self.on_start(action)
        try:
            error, attempts = self._try(action, action.run)
        except ManualStepRequired as needed:
            self.manual.append((action, str(needed)))
            self._note(action, "manual", str(needed))
            return
        if error is None:
            try:
                satisfied = action.check()
            except Exception as check_error:
                error, satisfied = check_error, True
            if not satisfied:
                error = ActionFailed(f"[{action.kind}] {action.describe()}: still not satisfied after running")
        if error is None:
            self._succeeded(action)
            return
        tried.append(f"{action.describe()}: {error}")
        if self._fallback(action, tried):
            self._succeeded(action)
            return
        self._fail(action, error, attempts, tried if len(tried) > 1 else ())

    def _succeeded(self, action: Action) -> None:
        self.changed += 1
        self._note(action, "done")
        if self.on_done is not None:
            self.on_done(action)

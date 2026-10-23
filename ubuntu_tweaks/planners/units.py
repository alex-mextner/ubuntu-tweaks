"""Units planner: ``units.toml`` makes systemd *user* units enabled, started or disabled, and switches lingering.

Accessed via: planner discovery (``build``). Runs in ``Phase.UNITS``, after the ``files`` planner has linked the
unit files and drop-ins the profile carries under ``files/.config/systemd/user/``.

Actions (spec, *Plan and actions* and *Reconcile and failover*):

- ``linger`` (``loginctl enable-linger <user>`` / ``disable-linger``): only when ``linger`` is set; no root for the
  own user. ``loginctl show-user <user> -p Linger`` says ``yes`` or the user is not known to it (= off).
- ``daemon-reload``: one ``systemctl --user daemon-reload`` when a unit the profile declares or links reports
  ``NeedDaemonReload=yes``, or a profile unit file / drop-in is not linked into ``$HOME`` yet (the plan is built
  before the files phase runs, so the reload remembers that it was needed when the plan was built).
- ``user-unit``: one per name of ``enable`` (``systemctl --user enable --now <unit>``; ``enable`` alone with
  ``start = false`` or for a ``Type=oneshot`` service, which is converged when enabled) and of ``disable``
  (``disable --now``). A failing unit is one failed item; the others still run. A unit whose file is the profile's
  own link in ``~/.config/systemd/user`` is *not* disabled with ``systemctl disable`` (that deletes the link, the
  files planner would put it back on the next run): it is stopped and its ``*.wants`` / ``*.requires`` links are
  removed instead.
- ``user-units``: the failover. When no user manager answers (``systemctl --user show-environment`` fails: no
  ``XDG_RUNTIME_DIR`` or session bus, as over ssh without a login session) the units are not tried one by one: one
  manual step lists the exact commands. ``linger`` still runs (it talks to the system manager).
- A unit with no file in the profile, none in the user or system unit directories and not loaded is *blocked* by
  ``unit-file`` (``Action.blocker``): reported with the file it expects, never counted as a failure.

Assumptions: every query goes through the injected ``Process`` (``systemctl --user show -p <props> <unit>``, one per
unit and check), so tests need no systemd. Enabled means ``UnitFileState`` is ``enabled``, ``enabled-runtime`` or
one of the states ``is-enabled`` also answers 0 for (``static``, ``indirect``, ``generated``, ``alias``,
``transient``); started means ``ActiveState`` ``active``, ``activating`` or ``reloading``. ``disable`` is converged
when the unit is neither enabled by the user nor running (a unit the machine does not know is). Commands are shown
shell-quoted, so a preview or manual step can be pasted.

Public API: ``build(ctx, backend, *, process=None, home=None, root=None, user=None)``.
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

from ubuntu_tweaks.actions import Action, ActionFailed, ManualStepRequired, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.process import CommandError, Process
from ubuntu_tweaks.profile import UNIT_SUFFIXES, USER_UNIT_DIR, ProfileFile, unit_template
from ubuntu_tweaks.settings import SettingsBackend

type Want = Literal["enabled", "disabled"]
type Argv = list[str]

HOME_UNIT_DIRS = (USER_UNIT_DIR, PurePosixPath(".local/share/systemd/user"))
SYSTEM_UNIT_DIRS = (
    PurePosixPath("etc/systemd/user"),
    PurePosixPath("run/systemd/user"),
    PurePosixPath("usr/lib/systemd/user"),
    PurePosixPath("usr/local/lib/systemd/user"),
    PurePosixPath("usr/share/systemd/user"),
    PurePosixPath("usr/local/share/systemd/user"),
    PurePosixPath("lib/systemd/user"),
)
SYSTEMCTL = ("systemctl", "--user")
PROPERTIES = "Id,LoadState,UnitFileState,ActiveState,Type,NeedDaemonReload"
ENABLED = frozenset({"enabled", "enabled-runtime", "static", "indirect", "generated", "alias", "transient"})
ENABLED_BY_USER = frozenset({"enabled", "enabled-runtime"})
RUNNING = frozenset({"active", "activating", "reloading"})
_DROP_IN_SUFFIXES = tuple(f"{suffix}.d" for suffix in UNIT_SUFFIXES)
_ONESHOT = re.compile(r"^\s*Type\s*=\s*oneshot\s*$", re.MULTILINE)
_REASON_LIMIT = 80


def _short(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


def _systemctl(*words: str) -> Argv:
    return [*SYSTEMCTL, *words]


def enable_argv(names: Sequence[str], *, now: bool) -> Argv:
    return _systemctl("enable", *(["--now"] if now else []), *names)


def disable_argv(names: Sequence[str]) -> Argv:
    return _systemctl("disable", "--now", *names)


def _line(argv: Sequence[str]) -> str:
    return shlex.join(argv)


@dataclass(frozen=True)
class UnitState:
    id: str  # the canonical name: it differs from the name asked for when that is an alias
    load: str
    file: str
    active: str
    kind: str
    reload: bool


class UserManager:
    """The few things asked of ``systemctl --user``."""

    def __init__(self, process: Process) -> None:
        self._process = process

    def reachable(self) -> bool:
        return self._process.query(_systemctl("show-environment")).ok

    def show(self, names: Sequence[str]) -> dict[str, UnitState] | None:
        """The state of each unit by the name asked for, ``None`` when the manager could not be asked. systemctl
        prints one block per argument, in argument order (``Id`` would be the canonical name, which differs for an
        alias), so a different number of blocks is an answer that cannot be trusted."""
        if not names:
            return {}
        result = self._process.query(_systemctl("show", "-p", PROPERTIES, *names))
        blocks = [block for block in result.stdout.split("\n\n") if block.strip()]
        if not result.ok or len(blocks) != len(names):
            return None
        found: dict[str, UnitState] = {}
        for name, block in zip(names, blocks, strict=True):
            props = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
            found[name] = UnitState(
                id=props.get("Id") or name,
                load=props.get("LoadState", ""),
                file=props.get("UnitFileState", ""),
                active=props.get("ActiveState", ""),
                kind=props.get("Type", ""),
                reload=props.get("NeedDaemonReload", "no") == "yes",
            )
        return found


@dataclass(frozen=True)
class ProfileUnits:
    """The unit files and drop-ins under the profile's ``files/.config/systemd/user`` (shared + overlay)."""

    home: Path
    entries: tuple[tuple[str, ProfileFile], ...]
    files: Mapping[str, ProfileFile]

    @classmethod
    def of(cls, profile_files: Sequence[ProfileFile], home: Path) -> ProfileUnits:
        depth = len(USER_UNIT_DIR.parts)
        entries: list[tuple[str, ProfileFile]] = []
        units: dict[str, ProfileFile] = {}
        for entry in profile_files:
            parts = entry.rel.parts
            if parts[:depth] != USER_UNIT_DIR.parts or len(parts) <= depth:
                continue
            top = parts[depth]
            if len(parts) == depth + 1 and top.endswith(UNIT_SUFFIXES):
                entries.append((top, entry))
                units[top] = entry
            elif top.endswith(_DROP_IN_SUFFIXES):
                entries.append((top.removesuffix(".d"), entry))
        return cls(home, tuple(entries), MappingProxyType(units))

    def source_of(self, name: str) -> ProfileFile | None:
        return self.files.get(name) or self.files.get(unit_template(name))

    def is_oneshot(self, name: str) -> bool:
        """``Type=oneshot`` in the profile's file for ``name`` (the unit may not be loaded yet)."""
        source = self.source_of(name)
        if source is None or not name.endswith(".service"):
            return False
        try:
            return _ONESHOT.search(source.source.read_text(encoding="utf-8")) is not None
        except (OSError, UnicodeDecodeError):
            return False

    def link_pending(self) -> bool:
        return any(
            os.path.realpath(self.home.joinpath(*entry.rel.parts)) != os.path.realpath(entry.source)
            for _name, entry in self.entries
        )

    def names(self) -> set[str]:
        """Concrete units the profile ships (a bare template is no unit to ask systemd about)."""
        return {
            name
            for name, _entry in self.entries
            if "@." not in name and not name.startswith("-") and not any(char.isspace() for char in name)
        }

    def enablement_links(self, name: str) -> list[Path]:
        """The ``*.wants`` / ``*.requires`` / ``*.upholds`` entries for ``name`` in the user unit directory."""
        directory = self.home / USER_UNIT_DIR
        suffixes = (".wants", ".requires", ".upholds")
        groups = [path for suffix in suffixes for path in sorted(directory.glob(f"*{suffix}"))]
        return [group / name for group in groups if os.path.lexists(group / name)]


def _expected_path(name: str) -> str:
    """Where the profile would carry the file of ``name`` (the template for an instance)."""
    return f"files/{USER_UNIT_DIR.as_posix()}/{unit_template(name)}"


class MissingUnitFile(Action):
    """What a unit nobody provides is blocked by: never planned, only named in the ``Blocked`` report."""

    phase = Phase.UNITS
    kind = "unit-file"

    def __init__(self, name: str) -> None:
        self._name = name

    def describe(self) -> str:
        return (
            f"{self._name} has no unit file: put it at {_expected_path(self._name)} in the profile, "
            "or install the package that ships it"
        )

    def check(self) -> bool:
        return False

    def run(self) -> None:
        raise ActionFailed(self.describe())


class LingerAction(Action):
    phase = Phase.UNITS
    kind = "linger"
    order = 40
    key = "linger"

    def __init__(self, process: Process, user: str, *, on: bool) -> None:
        self._process = process
        self._user = user
        self._on = on

    def _lingering(self) -> bool:
        result = self._process.query(["loginctl", "show-user", self._user, "-p", "Linger"])
        return result.ok and "Linger=yes" in result.stdout.split()

    def _argv(self) -> Argv:
        return ["loginctl", "enable-linger" if self._on else "disable-linger", self._user]

    def check(self) -> bool:
        return self._lingering() == self._on

    def describe(self) -> str:
        return _line(self._argv())

    def run(self) -> None:
        try:
            self._process.execute(self._argv())
        except CommandError as error:
            # polkit only lets an active local session change lingering without a password: over ssh it is the
            # user's step, not a failure of the run
            raise ManualStepRequired(
                f"`{_line(self._argv())}` was refused ({error}); run it in a login session of this user "
                f"or as root: `sudo {_line(self._argv())}`"
            ) from error


class DaemonReload(Action):
    phase = Phase.UNITS
    kind = "daemon-reload"
    order = 50
    key = "daemon-reload"

    def __init__(self, process: Process, manager: UserManager, names: Sequence[str], *, links_pending: bool) -> None:
        self._process = process
        self._manager = manager
        self._names = tuple(names)
        self._waiting_for_links = links_pending  # decided at build time: the links do not exist yet
        self._ran = False

    def check(self) -> bool:
        if self._waiting_for_links and not self._ran:
            return False
        states = self._manager.show(self._names)
        return states is not None and not any(state.reload for state in states.values())

    def describe(self) -> str:
        return _line(_systemctl("daemon-reload"))

    def run(self) -> None:
        self._process.execute(_systemctl("daemon-reload"))
        self._ran = True


def _target(name: str, state: UnitState | None) -> str:
    """The name systemctl accepts for enable, disable and stop: the canonical one (it refuses an alias)."""
    return state.id if state is not None and state.load == "loaded" else name


def _disable_commands(name: str, provided: ProfileUnits, state: UnitState | None = None) -> list[Argv]:
    """``systemctl disable`` removes a unit file that is a link outside the search path, which is exactly what the
    profile's own units are: for those, stop the unit (when it runs) and remove its enablement links instead. A unit
    a package enables system-wide stays enabled after ``disable`` (its link in ``/etc`` is out of reach): the run
    then reports it as not converged instead of guessing at a mask."""
    target = _target(name, state)
    if provided.source_of(name) is None and provided.source_of(target) is None:
        return [disable_argv([target])]
    commands = [_systemctl("stop", target)] if state is None or state.active in RUNNING else []
    links = provided.enablement_links(target)
    if links:
        commands += [["rm", "-f", "--", *map(str, links)], _systemctl("daemon-reload")]
    return commands


class UserUnitAction(Action):
    phase = Phase.UNITS
    kind = "user-unit"

    def __init__(
        self,
        name: str,
        want: Want,
        *,
        start: bool,
        process: Process,
        manager: UserManager,
        provided: ProfileUnits,
        root: Path,
        reason: str,
    ) -> None:
        self._name = name
        self._want = want
        self._start = start
        self._process = process
        self._manager = manager
        self._provided = provided
        self._root = root
        self._reason = reason
        self.key = f"user-unit:{name}"

    def _state(self) -> UnitState | None:
        states = self._manager.show([self._name])
        return None if states is None else states.get(self._name)

    def _is_oneshot(self, state: UnitState | None) -> bool:
        if not self._name.endswith(".service"):
            return False
        if state is not None and state.load == "loaded":
            return state.kind == "oneshot"
        return self._provided.is_oneshot(self._name)

    def _commands(self, state: UnitState | None) -> list[Argv]:
        if self._want == "disabled":
            return _disable_commands(self._name, self._provided, state)
        now = self._start and not self._is_oneshot(state)
        return [enable_argv([_target(self._name, state)], now=now)]

    def _has_file(self, state: UnitState | None) -> bool:
        if self._provided.source_of(self._name) is not None:
            return True
        names = (self._name, unit_template(self._name))
        directories = [self._provided.home / path for path in HOME_UNIT_DIRS]
        directories += [self._root / path for path in SYSTEM_UNIT_DIRS]
        if any(os.path.lexists(directory / name) for directory in directories for name in names):
            return True
        return state is not None and state.load == "loaded"

    def check(self) -> bool:
        state = self._state()
        if state is None:
            return False
        if self._want == "disabled":
            return state.file not in ENABLED_BY_USER and state.active not in RUNNING
        if state.file not in ENABLED:
            return False
        return not self._start or self._is_oneshot(state) or state.active in RUNNING

    def blocker(self) -> Action | None:
        state = self._state()
        if self._want == "disabled" or state is None or self._has_file(state):
            return None  # a manager that could not be asked says nothing about the file
        return MissingUnitFile(self._name)

    def describe(self) -> str:
        state = self._state()
        text = "; ".join(_line(argv) for argv in self._commands(state))
        if not text:
            text = f"disable {self._name} by hand: its enablement is outside ~/.config/systemd/user"
        if self._want == "enabled" and not self._has_file(state):
            text += f"  [no unit file yet: {_expected_path(self._name)} in the profile, or a package that ships it]"
        return f"{text}  # {_short(self._reason)}" if self._reason else text

    def run(self) -> None:
        commands = self._commands(self._state())
        if not commands:
            # a plain `systemctl disable` would delete the profile's own link: leave that to the user
            raise ActionFailed(self.describe())
        for argv in commands:
            self._process.execute(argv)


class DeferredUnits(Action):
    """No user manager answers: the commands to run later, as one manual step (the units are not tried)."""

    phase = Phase.UNITS
    kind = "user-units"
    key = "user-units"

    def __init__(self, commands: Sequence[Argv]) -> None:
        self._commands = tuple(commands)

    def describe(self) -> str:
        return "; ".join(_line(argv) for argv in self._commands)

    def check(self) -> bool:
        return False

    def run(self) -> None:
        raise ManualStepRequired(
            "no systemd user manager is reachable from this session (no XDG_RUNTIME_DIR or session bus: ssh without "
            "a login session?). In a login session of this user run: "
            + self.describe()
            + ". Then `ubuntu-tweak apply --commit` reports it converged."
        )


def _user_name() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _deferred_commands(
    enable: Sequence[str], disable: Sequence[str], *, start: bool, provided: ProfileUnits
) -> list[Argv]:
    """What ``apply`` would have run, as the commands to paste: a oneshot of the profile is enabled without
    ``--now`` here too (the type of a unit the profile does not ship is unknown without a manager)."""
    commands = [_systemctl("daemon-reload")]
    if start:
        oneshot = [name for name in enable if provided.is_oneshot(name)]
        running = [name for name in enable if name not in oneshot]
        commands += [enable_argv(names, now=now) for names, now in ((running, True), (oneshot, False)) if names]
    elif enable:
        commands.append(enable_argv(enable, now=False))
    for name in disable:
        commands += _disable_commands(name, provided)
    return commands


def build(
    ctx: Context,
    backend: SettingsBackend,
    *,
    process: Process | None = None,
    home: Path | None = None,
    root: Path | None = None,
    user: str | None = None,
) -> Sequence[Action]:
    profile = ctx.profile
    declared = profile.load_units()
    runner = process or Process()
    base = home if home is not None else Path.home()
    provided = ProfileUnits.of(profile.files(), base)
    actions: list[Action] = []
    if declared.linger is not None:
        actions.append(LingerAction(runner, user or _user_name(), on=declared.linger))
    if not (declared.enable or declared.disable or provided.entries):
        return actions
    manager = UserManager(runner)
    if not manager.reachable():
        if declared.enable or declared.disable:
            commands = _deferred_commands(declared.enable, declared.disable, start=declared.start, provided=provided)
            actions.append(DeferredUnits(commands))
        return actions
    shown = sorted({*declared.enable, *declared.disable, *provided.names()})
    actions.append(DaemonReload(runner, manager, shown, links_pending=provided.link_pending()))
    reasons = profile.unit_reasons()
    system_root = root if root is not None else Path("/")
    wanted: list[tuple[Want, str, Sequence[str]]] = [
        ("enabled", "enable", declared.enable),
        ("disabled", "disable", declared.disable),
    ]
    for want, key, names in wanted:
        actions.extend(
            UserUnitAction(
                name,
                want,
                start=declared.start,
                process=runner,
                manager=manager,
                provided=provided,
                root=system_root,
                reason=reasons.get((key, name), ""),
            )
            for name in names
        )
    return actions

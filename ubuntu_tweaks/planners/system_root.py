"""Root-level system settings from ``system.toml``: timezone, locale and the console keyboard.

Accessed via: planner discovery (``build``). Every action has kind ``system`` and
``needs_root = True``; ``Plan.execute(no_sudo=True)`` skips them and says so.

Assumptions:

- Checks are read-only: ``timedatectl show -p Timezone --value``, ``localectl status`` and a
  parse of ``/etc/default/keyboard``. A failing check command means "not satisfied" (the plan
  shows the action; ``run()`` then reports the real error).
- ``locale`` sets ``LANG`` only. The locale must already be generated (``locale-gen``); otherwise
  ``localectl`` refuses and the action fails with its message.
- Only the keyboard fields ``system.toml`` lists are compared and written; other lines of
  ``/etc/default/keyboard`` (``BACKSPACE=`` ...) are kept. The new file is built as the user in a
  temporary file and installed with ``install -m 644`` through sudo/pkexec.

Public API: ``build(ctx, backend, *, process=None, keyboard_file=KEYBOARD_FILE)``,
``parse_keyboard(text)``, ``KEYBOARD_FILE``.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError, UbuntuTweakError
from ubuntu_tweaks.process import Process
from ubuntu_tweaks.profile import Keyboard
from ubuntu_tweaks.settings import SettingsBackend

KEYBOARD_FILE = Path("/etc/default/keyboard")
_KEYBOARD_VARIABLES = (
    ("model", "XKBMODEL"),
    ("layout", "XKBLAYOUT"),
    ("variant", "XKBVARIANT"),
    ("options", "XKBOPTIONS"),
)
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")


def parse_keyboard(text: str) -> dict[str, str]:
    """``/etc/default/keyboard`` is shell syntax: ``NAME="value"`` lines, ``#`` comments."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[match.group(1)] = value
    return found


class _RootAction(Action):
    phase = Phase.SYSTEM
    kind = "system"
    needs_root = True
    scope = None

    def __init__(self, process: Process) -> None:
        self._process = process


class TimezoneAction(_RootAction):
    def __init__(self, process: Process, wanted: str) -> None:
        super().__init__(process)
        self._wanted = wanted

    def _current(self) -> str | None:
        result = self._process.query(["timedatectl", "show", "-p", "Timezone", "--value"])
        return result.stdout.strip() if result.ok else None

    def check(self) -> bool:
        return self._current() == self._wanted

    def describe(self) -> str:
        return f"timezone {self._current() or '(unknown)'} -> {self._wanted} (timedatectl set-timezone)"

    def run(self) -> None:
        self._process.execute(self._process.elevate(["timedatectl", "set-timezone", self._wanted]))


class LocaleAction(_RootAction):
    def __init__(self, process: Process, wanted: str) -> None:
        super().__init__(process)
        self._wanted = wanted

    def _assignments(self) -> dict[str, str] | None:
        """The ``System Locale:`` block of ``localectl status`` as ``{name: value}``; None when it fails."""
        result = self._process.query(["localectl", "status"])
        if not result.ok:
            return None
        lines = result.stdout.splitlines()
        for index, line in enumerate(lines):
            if "System Locale:" not in line:
                continue
            block = [line.split("System Locale:", 1)[1]]
            for follow in lines[index + 1 :]:
                if not re.match(r"^\s+[A-Z_]+=", follow):
                    break
                block.append(follow)
            return dict(re.findall(r"\b([A-Z_]+)=(\S+)", " ".join(block)))
        return {}

    def _current(self) -> str | None:
        assignments = self._assignments()
        return None if assignments is None else assignments.get("LANG", "")

    def check(self) -> bool:
        return self._current() == self._wanted

    def _kept(self) -> list[str]:
        """The other ``LC_*`` assignments: ``localectl set-locale`` replaces the whole set, so they are passed on."""
        return [f"{name}={value}" for name, value in (self._assignments() or {}).items() if name != "LANG"]

    def describe(self) -> str:
        now = self._current()
        text = f"locale LANG={now if now is not None else '(unknown)'} -> LANG={self._wanted} (localectl set-locale)"
        kept = self._kept()
        return text + (f", keeping {' '.join(kept)}" if kept else "")

    def run(self) -> None:
        argv = ["localectl", "set-locale", f"LANG={self._wanted}", *self._kept()]
        self._process.execute(self._process.elevate(argv))


class KeyboardAction(_RootAction):
    def __init__(self, process: Process, wanted: Keyboard, path: Path) -> None:
        super().__init__(process)
        self._path = path
        self._wanted = {
            variable: value for field, variable in _KEYBOARD_VARIABLES if (value := getattr(wanted, field)) is not None
        }

    def _text(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as error:
            raise UbuntuTweakError(f"cannot read {self._path}: {error.strerror or error}") from error

    def _differences(self) -> Mapping[str, tuple[str, str]]:
        current = parse_keyboard(self._text())
        return {
            variable: (current.get(variable, ""), value)
            for variable, value in self._wanted.items()
            if current.get(variable, "") != value
        }

    def check(self) -> bool:
        return not self._differences()

    def describe(self) -> str:
        changes = "; ".join(f'{name} "{old}" -> "{new}"' for name, (old, new) in self._differences().items())
        return f"keyboard {self._path}: {changes}"

    def _new_text(self) -> str:
        pending = dict(self._wanted)
        lines: list[str] = []
        for line in self._text().splitlines():
            match = _ASSIGNMENT.match(line)
            if match and match.group(1) in pending:
                lines.append(f'{match.group(1)}="{pending.pop(match.group(1))}"')
            else:
                lines.append(line)
        lines += [f'{name}="{value}"' for name, value in pending.items()]
        return "\n".join(lines) + "\n"

    def run(self) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix="ubuntu-tweak-keyboard.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self._new_text())
            self._process.execute(
                self._process.elevate(["install", "-m", "644", "-o", "root", "-g", "root", temporary, str(self._path)])
            )
        finally:
            Path(temporary).unlink(missing_ok=True)


def build(
    ctx: Context,
    backend: SettingsBackend,
    *,
    process: Process | None = None,
    keyboard_file: Path = KEYBOARD_FILE,
) -> Sequence[Action]:
    runner = process or Process()
    system = ctx.profile.load_system()
    actions: list[Action] = []
    if system.timezone:
        actions.append(TimezoneAction(runner, system.timezone))
    if system.locale:
        actions.append(LocaleAction(runner, system.locale))
    if any(getattr(system.keyboard, field) is not None for field, _ in _KEYBOARD_VARIABLES):
        actions.append(KeyboardAction(runner, system.keyboard, keyboard_file))
    for field, _ in _KEYBOARD_VARIABLES:
        value = getattr(system.keyboard, field)
        if value is not None and re.search(r'[\x00-\x1f"$`\\]', value):
            raise ConfigError(
                f"system.toml [keyboard] {field}: {value!r} has a character that is unsafe in a shell file"
            )
    return actions

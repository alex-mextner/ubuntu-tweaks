"""What ``finish`` says when ``apply --commit`` ends with 7, 6 or 3 (spec *Finish*): a plain summary, not a bare number.

Accessed via: ``commands/finish.py`` hands ``explain`` (bound to the last run journal, the exact commands to print and
the session log) to ``finish.apply_step`` as the step's ``explain_exit``; ``finish._attempt`` prints the result.

Assumptions: ``apply --commit`` exits 7 (an action failed or was blocked), 6 (root could not be obtained and root
actions were left) or 3 (the settings did not converge) after it ran to its end, so the run journal it wrote holds what
failed. A journal that is not that finished run (none, interrupted, another exit code) explains nothing: the generic
line stays. The journal already masks secrets; nothing else is read.

Public API: ``explain(code, last, *, rerun, command, log="", since=0.0) -> ExitReport | None``. ``last`` is the last
journal run and counts only when it started at or after ``since`` (when this ``apply`` was launched),
``rerun(subcommand)`` the full command line to type for ``ubuntu-tweak <subcommand>`` (it keeps the global flags the user
gave), ``command`` the launcher that finishes the remaining steps, ``log`` the session log path (``""`` unknown). ``None``
means "nothing to add": the caller keeps its generic text.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from ubuntu_tweaks import ui
from ubuntu_tweaks.errors import ExitCode
from ubuntu_tweaks.finish import ExitReport

if TYPE_CHECKING:
    from ubuntu_tweaks.journal import Entry, Run

MAX_CAUSE = 200
MAX_NAME = 100
MODULE_KIND = "system-module"
SHOWN = ("failed", "blocked")  # the journal statuses an exit of 7 is about
# what process.CommandError puts before the tool's own words (the module's argv with its options precedes it)
_COMMAND_FAILURE = re.compile(r"exited with status (\d+):?\s*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # a journal line is a failing tool's words: never replay its escapes


def _first_line(text: str) -> str:
    for line in text.splitlines():
        printable = _CONTROL.sub("", line).strip()
        if printable:
            return printable
    return ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _cause(detail: str) -> str:
    """The first line of the journal's detail, reduced to what the failing tool said: a command failure reads
    ``<module>: <argv with the module's options>: exited with status N: <its words>``, and the argv is noise here. A
    tool that said nothing gives ``exited with status N, no output``, never the argv."""
    line = _first_line(detail)
    found = list(_COMMAND_FAILURE.finditer(line))
    if not found:
        if "UBUNTU_TWEAK_MODULE_CONFIG" in line:  # a command that never ran: its argv carries the module's options
            return ui.t("finish.exit.see_log")
        return _clip(line, MAX_CAUSE)
    for match in reversed(found):  # the last marker with words after it; a tool that repeats the phrase keeps its own
        tail = line[match.end() :].strip()
        if tail:
            return _clip(tail, MAX_CAUSE)
    return _clip(ui.t("finish.exit.no_output", code=found[-1][1]), MAX_CAUSE)


def _module(entry: Entry) -> str | None:
    """The module behind a ``system-module|<module>|...`` journal key, else ``None`` (control characters dropped)."""
    kind, _, rest = entry.key.partition("|")
    scope = _CONTROL.sub("", rest.partition("|")[0])
    return scope if kind == MODULE_KIND and scope else None


def _command_line(command: str) -> str:
    return "  " + ui.style(_CONTROL.sub("", command), "cyan")


def _item(entry: Entry, rerun: Callable[[str], str]) -> list[str]:
    module = _module(entry)
    kind = _CONTROL.sub("", entry.kind)
    name = module or f"[{kind}] {_clip(_first_line(entry.text), MAX_NAME)}"
    if entry.status == "blocked":
        cause = (
            ui.t("finish.exit.blocked_cause", detail=_clip(_first_line(entry.detail), MAX_CAUSE))
            if entry.detail
            else ""
        )
    else:
        cause = _cause(entry.detail)
    if module and cause.startswith(f"{module}: "):  # the installer's own message repeats the module name
        cause = cause[len(module) + 2 :]
    retry = rerun(f"apply --commit --only {shlex.quote(module)}") if module else rerun("apply --commit")
    text = ui.t("finish.exit.item", name=name, cause=cause) if cause else name
    lines = ui.wrap(text, first=f"  {ui.symbol('error')} ", names=())
    return [*lines, "      " + ui.t("finish.exit.retry", command=ui.style(retry, "cyan"))]


def _partial(last: Run, rerun: Callable[[str], str], command: str) -> ExitReport:
    counts = last.counts()
    lines = ui.wrap(
        ui.t(
            "finish.exit.partial",
            changed=counts.get("done", 0),
            failed=counts.get("failed", 0),
            blocked=counts.get("blocked", 0),
        ),
        names=("yellow", "bold"),
    )
    for entry in last.entries:
        if entry.status in SHOWN:
            lines += _item(entry, rerun)
    if counts.get("manual", 0):
        lines += ui.wrap(ui.t("finish.exit.waiting", n=counts["manual"]), indent=2)
    lines += ui.wrap(ui.t("finish.exit.carry_on"), names=("cyan",))
    lines.append(_command_line(command))
    return ExitReport("\n".join(lines), ui.t("finish.exit.reported"))


def _no_root(rerun: Callable[[str], str]) -> ExitReport:
    lines = ui.wrap(ui.t("finish.exit.no_root"), names=("yellow", "bold"))
    lines.append(_command_line(rerun("apply --commit")))
    return ExitReport("\n".join(lines), ui.t("finish.exit.reported"))


def _drift(rerun: Callable[[str], str]) -> ExitReport:
    lines = ui.wrap(ui.t("finish.exit.drift"), names=("yellow", "bold"))
    lines += [_command_line(rerun("apply --commit")), _command_line(rerun("status"))]
    return ExitReport("\n".join(lines), ui.t("finish.exit.reported"))


def explain(
    code: int, last: Run | None, *, rerun: Callable[[str], str], command: str, log: str = "", since: float = 0.0
) -> ExitReport | None:
    """``since`` is when the ``apply`` that just ended was started: a journal older than that belongs to another run
    (this one died before writing its own) and is never blamed."""
    started = last.started if last is not None and isinstance(last.started, int | float) else None
    ours = last is not None and last.finished and last.exit_code == code and started is not None and started >= since
    match code:
        case ExitCode.PARTIAL if ours:
            return _partial(last, rerun, command)
        case ExitCode.NO_ROOT:
            return _no_root(rerun)
        case ExitCode.DRIFT:
            return _drift(rerun)
    if not log:
        return None
    return ExitReport("", f"{ui.t('finish.run_failed', code=code)} {ui.t('finish.exit.log', log=log)}")

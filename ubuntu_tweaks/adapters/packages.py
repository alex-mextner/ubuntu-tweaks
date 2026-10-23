"""packages adapter: what was installed on purpose, per package manager (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``owned``, ``classify``, ``capture``.

Assumptions:

- Items are ``<manager>:<name>``: ``apt`` (``apt-mark showmanual``), ``snap``, ``flatpak`` (applications only),
  ``brew`` (``brew leaves``: what was asked for, not its dependencies), ``pipx``, ``uv`` (tools), ``npm`` (global
  packages under ``~/.local``, where ``packages.toml`` installs them) and ``bun`` (global packages), ``cargo``
  (``cargo install --list``). Every manager is asked through ``Process.query``; one that is not installed (exit 127)
  or fails yields no items: a machine without ``snap`` has no snaps.
- ``owned`` is what the profile declares: the NAMES parsed from the entries of ``packages.toml`` (shared + machine
  overlay; ``glances[web]`` owns ``uv:glances``) and, for every enabled module, its ``[install] apt`` (and the
  ``provides`` names) and ``[deps] apt``.
- ``classify`` gives an opinion on ``apt`` and ``snap`` items only: what ``capture`` refuses to propose (installer
  baseline, ``required``/``important`` priority, ``[packages] ignore`` / ``ignore_snaps``; the very scan capture
  runs, ``capture.system_scan.scan_apt`` / ``scan_snaps``) is ``excluded`` with capture's reason, unless the profile
  declares the name or a coverage rule claims it (``adapters.scan`` asks only for unclaimed items). Capture and
  coverage therefore agree.
- ``capture`` proposes the ``packages.toml`` text for every untracked item (each manager has a key). An entry is the
  install spec: the name for apt/snap/flatpak/brew/npm/bun/cargo/pipx; for ``uv`` the source recorded in the tool's
  ``uv-receipt.toml`` (``name @ git+URL``, ``name @ file://PATH``, extras and specifier of a registry install), and a
  tool without a usable receipt, or a crate installed from git or a path, is written as a comment ``# "name",  # source unknown: set the spec by hand``:
  a URL is never guessed.

Public API: ``NAME``, ``MANAGERS``, ``discover(ctx)``, ``owned(ctx)``, ``classify(ctx, item, rules)``,
``capture(ctx, items)``.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ubuntu_tweaks import tool_packages
from ubuntu_tweaks.adapters._capture_view import capture_view
from ubuntu_tweaks.adapters._system import System, system
from ubuntu_tweaks.capture.system_scan import INSTALLER_BASELINE, SNAP_SEED, scan_apt, scan_snaps
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Finding, Item, Proposal, Rules
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import module_defaults
from ubuntu_tweaks.process import Process

NAME = "packages"
_TIMEOUT = 30.0

type Parser = Callable[[str], list[str]]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _snap(text: str) -> list[str]:
    return [line.split()[0] for line in text.splitlines()[1:] if line.split()]


MANAGERS: Mapping[str, tuple[tuple[str, ...], Parser]] = {
    "apt": (("apt-mark", "showmanual"), _lines),
    "snap": (("snap", "list"), _snap),
    "flatpak": (("flatpak", "list", "--app", "--columns=application"), _lines),
    "brew": (("brew", "leaves"), _lines),
}
_ORDER = (*MANAGERS, *tool_packages.TOOL_MANAGERS)


def _listed(process: Process, home: Path, manager: str) -> set[str]:
    if manager in tool_packages.TOOL_MANAGERS:
        return tool_packages.installed_names(process, manager, home)
    argv, parse = MANAGERS[manager]
    result = process.query(argv, timeout=_TIMEOUT)
    return set(parse(result.stdout)) if result.ok else set()


def discover(ctx: Context) -> Iterable[Item]:
    global _opinion
    _opinion = None  # a new scan: what ``classify`` remembers was computed for the previous one
    machine = system()
    for manager in _ORDER:
        for name in sorted(_listed(machine.process, machine.home, manager)):
            yield Item(NAME, f"{manager}:{name}", "package", f"installed with {manager}")


def owned(ctx: Context) -> Mapping[str, str]:
    found: dict[str, str] = {}
    packages = ctx.profile.load_packages()
    for manager, names in (
        ("apt", packages.apt),
        ("snap", packages.snap),
        ("snap", packages.snap_classic),  # classic snaps share the snap: id namespace
        ("flatpak", packages.flatpak),
        ("brew", packages.brew),
    ):
        for name in names:
            found.setdefault(f"{manager}:{name}", "packages.toml")
    for manager in tool_packages.TOOL_MANAGERS:
        for spec in getattr(packages, manager):
            name = tool_packages.parse_spec(manager, spec)
            found.setdefault(f"{manager}:{name}", "packages.toml")
            if (fallback := tool_packages.FALLBACK.get(manager)) is not None:
                found.setdefault(f"{fallback}:{name}", "packages.toml")  # installed there when the primary failed
    manifests = ctx.manifests()
    config = ctx.profile.load_config(module_defaults(manifests))
    for manifest in manifests:
        if not config.module_enabled(manifest.name):
            continue
        names = (*manifest.install.apt, *(name for name, _ in manifest.install.provides), *manifest.deps.apt)
        for name in names:
            found.setdefault(f"apt:{name}", f"module {manifest.name}")
    return found


class _Opinion:
    """What capture refuses, id -> reason, computed once per scan (``discover`` starts a new one)."""

    def __init__(self, ctx: Context, machine: System) -> None:
        self.ctx = ctx
        self.machine = machine
        rules = capture_view(ctx).rules.packages

        def run(argv: Sequence[str]) -> str | None:
            result = machine.process.query(argv, timeout=_TIMEOUT)
            return result.stdout if result.ok else None

        baseline = machine.root / INSTALLER_BASELINE.relative_to("/")
        seed = machine.root / SNAP_SEED.relative_to("/")
        self.refused: dict[str, str] = {}
        for name, reason in scan_apt(run, rules, installer_baseline=baseline).refused.items():
            self.refused[f"apt:{name}"] = reason
        for name, reason in scan_snaps(run, rules, snap_seed=seed).refused.items():
            self.refused[f"snap:{name}"] = reason


_opinion: _Opinion | None = None


def _current(ctx: Context) -> _Opinion:
    global _opinion
    machine = system()
    if _opinion is None or _opinion.ctx is not ctx or _opinion.machine is not machine:
        _opinion = _Opinion(ctx, machine)
    return _opinion


def classify(ctx: Context, item: Item, rules: Rules) -> Finding | None:
    """Only asked for an item no declaration and no rule claims (``adapters.scan``)."""
    if not item.id.startswith(("apt:", "snap:")):
        return None
    reason = _current(ctx).refused.get(item.id)
    return Finding(item, Coverage.EXCLUDED, reason) if reason else None


_UNKNOWN_SOURCE = "source unknown: set the spec by hand"
_REGISTRY_KEYS = frozenset({"name", "extras", "specifier"})
_GIT_QUERY = frozenset({"rev", "tag", "branch", "subdirectory"})  # what uv writes into a git receipt URL


def _extras(requirement: Mapping[str, object]) -> str:
    extras = requirement.get("extras")
    names = [extra for extra in extras if isinstance(extra, str)] if isinstance(extras, list) else []
    return f"[{','.join(names)}]" if names else ""


def _text(requirement: Mapping[str, object], key: str) -> str | None:
    value = requirement.get(key)
    return value if isinstance(value, str) and value else None


def _git_source(requirement: Mapping[str, object], git: str) -> str:
    """``git+URL[@ref][#subdirectory=...]`` from a receipt's ``git`` value: a plain URL with the ref and the
    subdirectory in sibling keys, or uv's wire form ``URL?rev=...&subdirectory=...#<resolved commit>``."""
    parts = urlsplit(git)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    repository = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    ref = next((value for key in ("rev", "tag", "branch") if (value := _text(requirement, key) or query.get(key))), "")
    subdirectory = _text(requirement, "subdirectory") or query.get("subdirectory")
    return f"git+{repository}{f'@{ref}' if ref else ''}{f'#subdirectory={subdirectory}' if subdirectory else ''}"


def _credential(url: str) -> bool:
    """A password, a user name on an http(s) URL (``https://<token>@host``) or a query other than the ref and
    subdirectory uv records (``?token=...``): what a URL may carry besides its address. An ``ssh`` user is no secret."""
    parts = urlsplit(url)
    if parts.password or set(parse_qs(parts.query)) - _GIT_QUERY:
        return True
    return bool(parts.username) and parts.scheme in ("http", "https")


def _plain_install(tool: Mapping[str, object]) -> bool:
    """Whether the receipt records nothing besides the requirement that a spec would lose."""
    if any(
        tool.get(key)
        for key in ("constraints", "overrides", "build-constraints", "build-constraint-dependencies", "python")
    ):
        return False
    options = tool.get("options")
    return not (isinstance(options, dict) and any("index" in key or "find" in key for key in options))


def _uv_spec(home: Path, name: str) -> str | None:
    """The ``packages.toml`` spec of an installed uv tool out of its receipt, ``None`` when it does not say."""
    try:
        receipt = tomllib.loads((home / ".local/share/uv/tools" / name / "uv-receipt.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tool = receipt.get("tool")
    requirements = tool.get("requirements") if isinstance(tool, dict) else None
    if not isinstance(requirements, list) or len(requirements) != 1 or not _plain_install(tool):
        return None  # --with packages, constraints, a python pin or a private index: more than a name and a source
    mine = next(
        (
            req
            for req in requirements
            if isinstance(req, dict)
            and isinstance(req.get("name"), str)
            and tool_packages.normalize(req["name"]) == name
        ),
        None,
    )
    if mine is None:
        return None
    if "marker" in mine:
        return None  # the spec would install it where the receipt excluded it
    if any(_credential(value) for key in ("git", "url") if (value := _text(mine, key))):
        return None  # a credential in the URL must not be proposed for a git-tracked profile
    base = name + _extras(mine)
    subdirectory = _text(mine, "subdirectory")
    if git := _text(mine, "git"):
        spec = f"{base} @ {_git_source(mine, git)}"
    elif (url := _text(mine, "url")) and not urlsplit(url).query:
        spec = f"{base} @ {url}{f'#subdirectory={subdirectory}' if subdirectory else ''}"
    elif path := _text(mine, "directory") or _text(mine, "path"):
        spec = f"{base} @ file://{path}"
    elif set(mine) <= _REGISTRY_KEYS:
        specifier = _text(mine, "specifier")
        spec = base + (re.sub(r"\s+", "", specifier) if specifier else "")
    else:
        return None  # editable, virtual, a private index...: a bare name would resolve against the public index
    try:
        tool_packages.parse_spec("uv", spec)
    except ConfigError:
        return None
    return spec


def _quote(text: str) -> str:
    """A TOML basic string: a listing is the output of other programs, so a quote or a newline in a name must
    not become TOML."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _entry(manager: str, name: str, home: Path, off_registry: Collection[str]) -> str | None:
    """The spec to write for an installed item, ``None`` when its source is unknown (never a guess)."""
    if manager == "uv":
        return _uv_spec(home, name)
    if manager == "cargo" and name in off_registry:
        return None  # installed from git or a path: the bare name would be another crate
    return name


def capture(ctx: Context, items: Sequence[Item]) -> Iterable[Proposal]:
    machine = system()
    off_registry: set[str] = set()
    if any(item.id.startswith("cargo:") for item in items):
        listing = machine.process.query(tool_packages.list_argv("cargo", machine.home), timeout=_TIMEOUT)
        off_registry = tool_packages.cargo_off_registry(listing.stdout)
    entries: dict[str, list[tuple[str, str | None]]] = defaultdict(list)  # manager -> (name, spec or None)
    members: list[Item] = []
    for item in items:
        manager, _, name = item.id.partition(":")
        if manager not in _ORDER:
            continue
        spec = _entry(manager, name, machine.home, off_registry)
        entries[manager].append((name, spec))
        if spec is not None:
            members.append(item)
    if not entries:
        return
    blocks = []
    for manager in sorted(entries):
        rows = "".join(
            f"  {_quote(spec)},\n" if spec else f"  # {_quote(name)},  # {_UNKNOWN_SOURCE}\n"
            for name, spec in sorted(entries[manager])
        )
        blocks.append(f"{manager} = [\n{rows}]\n")
    yield Proposal("packages.toml", "\n".join(blocks), tuple(members))

"""Finding the user's dotfiles repository and the ubuntu profile inside it, with no name hardcoded.

Accessed via: ``cli.main`` builds the default profile location with ``locate`` (``Profile.resolve``),
``reconcile.profile_lines`` prints ``missing_lines`` when nothing was found, and
``modules/dotfiles/install.sh`` calls ``python3 -m ubuntu_tweaks.identity resolve|same-repo|remember``.

Assumptions: every command goes through an injected ``Process`` (tests pass a ``FakeProcess``); a
missing or logged-out ``gh`` is a ``manual`` hint (``gh auth login``), never a prompt and never an
exception. The only thing written is ``<state>/ubuntu-tweaks/dotfiles.json`` (``$XDG_STATE_HOME``, else
``~/.local/state``): the discovery cache (``gh`` is asked once per ``CACHE_TTL_SECONDS``, not on every
command; ``--refresh`` forces it; an unreachable GitHub is remembered for ``FAILURE_TTL_SECONDS`` so an
offline machine does not wait on every command) and the checkout the dotfiles module recorded. A checkout
whose ``origin`` is another repository than the requested one is never read as a profile.

Order of discovery (first hit wins):

1. an explicit repository: ``--dotfiles-repo OWNER/NAME`` or ``$UBUNTU_TWEAK_DOTFILES_REPO`` (the module's
   ``[modules.dotfiles] repo`` option only reaches the module), in the checkout the module recorded for it,
   else at the default clone target;
2. a checkout already on disk that holds a profile: the one the dotfiles module remembered, then
   ``~/.files``, ``~/dotfiles``, ``~/.dotfiles``, ``~/dot-files``;
3. the fresh cache of an earlier ``gh`` discovery;
4. ``gh api user`` for the login, then the user's own repository named ``.files``, ``dotfiles``,
   ``.dotfiles`` or ``dot-files`` (in that order; several matches pick the first and say so).

Inside a checkout the profile is the first of ``ubuntu/``, ``ubuntu-tweaks/`` (a directory with a
``profile.toml`` or any other profile entry) or the repository root (``profile.toml`` there), or the
configured ``path``.

Public API:

- ``DOTFILES_NAMES``, ``PROFILE_SUBDIRS``, ``REPO_ENV``, ``GH_LOGIN_COMMAND``, ``CACHE_TTL_SECONDS``.
- ``RepoRef(spec)``: ``OWNER/NAME`` slug, git URL or absolute path; ``name``, ``is_slug``.
- ``same_repo(a, b) -> bool``: URL spellings of one repository compare equal.
- ``default_checkout(repo, home, configured=None) -> Path``: ``~/.files`` for a repository named
  ``.files``, else ``~/<name>``; ``configured`` (``~/...`` or absolute) wins.
- ``find_profile_dir(checkout, configured=None) -> Path | None``.
- ``discover_via_gh(process) -> Discovery``: the ``gh`` half of the algorithm, no cache, no disk.
- ``locate(*, home, env, process=None, repo_override=None, refresh=False, now=None) -> Location``.
- ``remember(home, env, checkout, profile_dir, repo=None)``: the dotfiles module records where it put the checkout.
- ``missing_lines(location) -> list[str]``: the plain-language lines printed when no profile exists.
- ``main(argv)``: the ``resolve`` / ``same-repo`` / ``remember`` helper CLI of the dotfiles module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ubuntu_tweaks.atomic_write import atomic_write
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.process import Process

DOTFILES_NAMES = (".files", "dotfiles", ".dotfiles", "dot-files")
PROFILE_SUBDIRS = ("ubuntu", "ubuntu-tweaks")
PROFILE_ENTRIES = ("profile.toml", "settings", "files", "packages.toml", "system.toml", "machines", "modules")
REPO_ENV = "UBUNTU_TWEAK_DOTFILES_REPO"
MODULE_CONFIG_ENV = "UBUNTU_TWEAK_MODULE_CONFIG"
GH_LOGIN_COMMAND = "gh auth login"
CACHE_TTL_SECONDS = 3600
FAILURE_TTL_SECONDS = 60
CACHE_FILE = "dotfiles.json"
LIST_LIMIT = 300
GH_TIMEOUT_SECONDS = 15
GH_LIST_TIMEOUT_SECONDS = GH_TIMEOUT_SECONDS  # a module check is killed at 60 s: discovery must fit inside it
REPO_FIELDS = "name,nameWithOwner,isPrivate,isArchived,isFork,defaultBranchRef"
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_URL_TAIL = re.compile(r"(?:^|[/:])([^/:]+/[^/:]+?)(?:\.git)?/*$")
_URL_HOST = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?:[^@/]+@)?([^/:@]+)[/:]")
_SCP_URL = re.compile(r"[^@\s/]+@[^:\s/]+:\S+")


class Source(StrEnum):
    FLAG = "flag"
    ENV = "env"
    CONFIG = "config"
    LOCAL = "local"
    CACHE = "cache"
    GH = "gh"


@dataclass(frozen=True)
class RepoRef:
    """The repository to clone: ``OWNER/NAME`` (cloned with ``gh``), a git URL or an absolute path."""

    spec: str

    def __post_init__(self) -> None:
        spec = self.spec
        slug = _SLUG.fullmatch(spec) is not None
        url = "://" in spec or _SCP_URL.fullmatch(spec) is not None
        usable = slug or url or spec.startswith("/")
        if spec.startswith("-") or any(c.isspace() for c in spec) or not usable or self.name in {"", ".", ".."}:
            raise ConfigError(f"invalid dotfiles repository {spec!r}: use OWNER/NAME, a git URL or an absolute path")

    @property
    def is_slug(self) -> bool:
        return _SLUG.fullmatch(self.spec) is not None

    @property
    def name(self) -> str:
        tail = re.split(r"[/:]", self.spec.rstrip("/"))[-1]
        return tail.removesuffix(".git") if tail != ".git" else tail


def _host_and_tail(spec: str) -> tuple[str | None, str]:
    """``(host, owner/name)``: the host only when the spelling carries one (a URL), lower case. A local path
    is compared as a whole, so it never equals a slug or another directory with the same last two names."""
    if spec.startswith("/"):
        return None, "path:" + os.path.normpath(spec).removesuffix(".git")
    match = _URL_TAIL.search(spec)
    tail = (match.group(1) if match else spec).lower()
    host = _URL_HOST.match(spec) if "://" in spec or _SCP_URL.fullmatch(spec) else None
    return (host.group(1).lower() if host else None), tail


def same_repo(first: str, second: str) -> bool:
    """``https://host/o/n.git``, ``git@host:o/n`` and ``o/n`` name one repository (case does not matter); two
    spellings that both name a host must name the same one, ``o/n`` alone is a repository on any host."""
    (first_host, first_tail), (second_host, second_tail) = _host_and_tail(first), _host_and_tail(second)
    return first_tail == second_tail and (first_host is None or second_host is None or first_host == second_host)


def expand_dir(home: Path, configured: str) -> Path:
    expanded = home / configured[2:] if configured.startswith("~/") else Path(configured)
    if not expanded.is_absolute():
        raise ConfigError(f"[modules.dotfiles] dir must be absolute or start with ~/: {configured}")
    return expanded


def default_checkout(repo: RepoRef, home: Path, configured: str | None = None) -> Path:
    if configured:
        return expand_dir(home, configured)
    return home / (".files" if repo.name == ".files" else repo.name)


def _is_profile_dir(directory: Path, *, root: bool) -> bool:
    if root:
        return (directory / "profile.toml").is_file()
    return directory.is_dir() and any((directory / entry).exists() for entry in PROFILE_ENTRIES)


def find_profile_dir(checkout: Path, configured: str | None = None) -> Path | None:
    if configured is not None:
        parts = Path(configured).parts
        if Path(configured).is_absolute() or ".." in parts:
            raise ConfigError(f"[modules.dotfiles] path must be relative to the checkout, without '..': {configured}")
        target = checkout.joinpath(*parts)
        return target if _is_profile_dir(target, root=target == checkout) else None
    for name in PROFILE_SUBDIRS:
        if _is_profile_dir(checkout / name, root=False):
            return checkout / name
    return checkout if _is_profile_dir(checkout, root=True) else None


@dataclass(frozen=True)
class Candidate:
    slug: str
    private: bool | None = None
    default_branch: str | None = None


@dataclass(frozen=True)
class Discovery:
    """What ``gh`` knows: the chosen repository (first of ``candidates``), or why there is none."""

    login: str | None = None
    candidates: tuple[Candidate, ...] = ()
    notes: tuple[str, ...] = ()
    manual: str | None = None  # a step the user must do; the run goes on without a repository
    transient: bool = False  # manual is about connectivity, not about a login or an install

    @property
    def repo(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


def _gh_login(process: Process) -> tuple[str | None, str | None, bool]:
    """``(login, None, False)``, or ``(None, manual_message, transient)``; transient = GitHub could not be reached
    (worth remembering for a minute so an offline machine does not wait on every command)."""
    result = process.query(["gh", "api", "user", "-q", ".login"], timeout=GH_TIMEOUT_SECONDS)
    if result.returncode == 127:
        return None, f"gh is not installed (module git-gh installs it); then run `{GH_LOGIN_COMMAND}`", False
    login = result.stdout.strip()
    if result.returncode == 0 and _LOGIN.fullmatch(login):
        return login, None, False
    detail = _first_line(result.stderr)
    if result.returncode == 4 or GH_LOGIN_COMMAND in result.stderr or "not logged in" in result.stderr.lower():
        return None, f"not logged in to GitHub: run `{GH_LOGIN_COMMAND}`", False
    return (
        None,
        f"GitHub could not be asked ({detail or f'gh exited {result.returncode}'}); run again when online",
        True,
    )


def _candidate(entry: object, login: str) -> Candidate | None:
    if not isinstance(entry, dict) or entry.get("isArchived") is True or entry.get("isFork") is True:
        return None
    name = entry.get("name")
    if not isinstance(name, str):
        return None
    slug = entry.get("nameWithOwner")
    branch = entry.get("defaultBranchRef")
    private = entry.get("isPrivate")
    return Candidate(
        slug if isinstance(slug, str) and _SLUG.fullmatch(slug) else f"{login}/{name}",
        private if isinstance(private, bool) else None,
        branch.get("name") if isinstance(branch, dict) and isinstance(branch.get("name"), str) else None,
    )


def discover_via_gh(process: Process) -> Discovery:
    login, manual, transient = _gh_login(process)
    if login is None:
        return Discovery(manual=manual, transient=transient)
    listed = process.query(
        ["gh", "repo", "list", login, "--json", REPO_FIELDS, "--limit", str(LIST_LIMIT)],
        timeout=GH_LIST_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        detail = _first_line(listed.stderr) or f"gh exited {listed.returncode}"
        return Discovery(
            login=login,
            manual=f"GitHub could not list the repositories of {login} ({detail}); run again",
            transient=True,
        )
    try:
        entries = json.loads(listed.stdout)
    except json.JSONDecodeError:
        return Discovery(
            login=login, manual=f"`gh repo list {login}` printed something that is not JSON; run again", transient=True
        )
    entries = entries if isinstance(entries, list) else []
    by_name = {}
    for entry in entries:
        found = _candidate(entry, login)
        if found is not None:
            by_name.setdefault(found.slug.rsplit("/", 1)[-1].lower(), found)
    truncated = len(entries) >= LIST_LIMIT  # a long list may hide a match: ask for each missing name
    matches: list[Candidate] = []
    for name in DOTFILES_NAMES:
        found = by_name.get(name.lower())
        if found is None and truncated:
            viewed = process.query(
                ["gh", "repo", "view", f"{login}/{name}", "--json", REPO_FIELDS], timeout=GH_TIMEOUT_SECONDS
            )
            if viewed.returncode == 0:
                try:
                    found = _candidate(json.loads(viewed.stdout), login)
                except json.JSONDecodeError:
                    found = None
        if found is not None:
            matches.append(found)
    notes: tuple[str, ...] = ()
    if len(matches) > 1:
        names = ", ".join(match.slug for match in matches)
        notes = (
            f"several dotfiles repositories found ({names}): using {matches[0].slug}; "
            "choose another with --dotfiles-repo OWNER/NAME",
        )
    return Discovery(login=login, candidates=tuple(matches), notes=notes)


@dataclass(frozen=True)
class Location:
    """Where the profile is (or will be once the dotfiles module has cloned the repository)."""

    root: Path
    checkout: Path | None = None
    repo: RepoRef | None = None
    source: Source | None = None
    login: str | None = None
    notes: tuple[str, ...] = ()
    manual: str | None = None
    private: bool | None = None


def _state_file(home: Path, env: Mapping[str, str]) -> Path:
    configured = env.get("XDG_STATE_HOME")
    return (Path(configured) if configured else home / ".local/state") / "ubuntu-tweaks" / CACHE_FILE


def _read_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, data: Mapping[str, object]) -> None:
    try:
        atomic_write(path, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass  # a read-only state directory only costs the cache


def _gh_account(home: Path, env: Mapping[str, str]) -> str:
    """A cheap fingerprint of which gh account is active: ``gh auth login`` and ``gh auth switch`` rewrite
    gh's ``hosts.yml``, so the cached answer of the previous account is not reused after either."""
    config = env.get("GH_CONFIG_DIR") or (
        f"{env['XDG_CONFIG_HOME']}/gh" if env.get("XDG_CONFIG_HOME") else f"{home}/.config/gh"
    )
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or env.get("GH_ENTERPRISE_TOKEN") or ""
    token_part = f":{hashlib.sha256(token.encode()).hexdigest()[:16]}" if token else ""
    try:
        info = (Path(config) / "hosts.yml").stat()
    except OSError:
        return f"no-hosts-file{token_part}"
    return f"{info.st_mtime_ns}:{info.st_size}{token_part}"


def _cached(state: Mapping[str, object], now: float, account: str) -> Discovery | None:
    stored = state.get("discovery")
    if not isinstance(stored, dict) or stored.get("account") != account:
        return None
    checked = stored.get("checked_at")
    login = stored.get("login")
    if not isinstance(checked, int | float) or now < checked:
        return None
    failure = stored.get("failure")
    if isinstance(failure, str):  # GitHub could not be reached a moment ago: do not wait for it again yet
        return Discovery(manual=failure, transient=True) if now - checked < FAILURE_TTL_SECONDS else None
    slugs = stored.get("candidates")
    if not isinstance(slugs, list) or now - checked >= CACHE_TTL_SECONDS:
        return None
    candidates = tuple(
        Candidate(item["slug"], item.get("private") if isinstance(item.get("private"), bool) else None)
        for item in slugs
        if isinstance(item, dict) and isinstance(item.get("slug"), str) and _SLUG.fullmatch(item["slug"])
    )
    notes = stored.get("notes")
    return Discovery(
        login if isinstance(login, str) else None,
        candidates,
        tuple(n for n in notes if isinstance(n, str)) if isinstance(notes, list) else (),
    )


def _store(state: dict[str, object], discovery: Discovery, now: float, account: str) -> bool:
    """Put a discovery into ``state``; False when it is not worth remembering (a login or install is missing)."""
    if discovery.manual is not None:
        if not discovery.transient:
            return False
        state["discovery"] = {"checked_at": now, "failure": discovery.manual, "account": account}
        return True
    state["discovery"] = {
        "checked_at": now,
        "account": account,
        "login": discovery.login,
        "candidates": [{"slug": c.slug, "private": c.private} for c in discovery.candidates],
        "notes": list(discovery.notes),
    }
    return True


def remember(
    home: Path, env: Mapping[str, str], checkout: Path, profile_dir: Path | None, repo: str | None = None
) -> None:
    path = _state_file(home, env)
    state = _read_state(path)
    state["checkout"] = {
        "dir": str(checkout),
        "profile_dir": str(profile_dir) if profile_dir else None,
        "repo": repo,
    }
    _write_state(path, state)


def _remembered(state: Mapping[str, object]) -> tuple[Path, Path | None, str | None] | None:
    stored = state.get("checkout")
    if not isinstance(stored, dict) or not isinstance(stored.get("dir"), str) or not stored["dir"].startswith("/"):
        return None
    profile, repo = stored.get("profile_dir"), stored.get("repo")
    return (
        Path(stored["dir"]),
        Path(profile) if isinstance(profile, str) and Path(profile).is_dir() else None,
        repo if isinstance(repo, str) else None,
    )


def _local_profile(
    home: Path, state: Mapping[str, object], checkout_dir: str | None, profile_path: str | None
) -> tuple[Path, Path] | None:
    """``(checkout, profile_dir)`` of the first checkout on disk that holds a profile."""
    if checkout_dir:
        checkout = expand_dir(home, checkout_dir)
        profile = find_profile_dir(checkout, profile_path)
        return (checkout, profile) if profile is not None else None
    remembered = _remembered(state)
    if remembered is not None and remembered[1] is not None:
        return remembered[0], remembered[1]
    for name in DOTFILES_NAMES:
        profile = find_profile_dir(home / name, profile_path)
        if profile is not None:
            return home / name, profile
    return None


def _origin_url(process: Process, checkout: Path) -> str | None:
    if not (checkout / ".git").exists():
        return None
    result = process.query(
        ["git", "-C", str(checkout), "config", "--get", "remote.origin.url"], timeout=GH_TIMEOUT_SECONDS
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _planned(
    repo: RepoRef,
    home: Path,
    source: Source,
    process: Process,
    state: Mapping[str, object],
    *,
    checkout_dir: str | None = None,
    profile_path: str | None = None,
    verify_origin: bool = True,
    login: str | None = None,
    notes: tuple[str, ...] = (),
    private: bool | None = None,
) -> Location:
    """The profile of ``repo`` in the configured checkout, else the one the module recorded for it, else at the
    default clone target. A checkout whose origin is another repository is never read: its profile belongs to
    someone else."""
    remembered = _remembered(state)
    if checkout_dir:
        checkout = expand_dir(home, checkout_dir)
        profile = find_profile_dir(checkout, profile_path)
    elif remembered is not None and remembered[2] is not None and same_repo(remembered[2], repo.spec):
        checkout = remembered[0]
        profile = remembered[1] or find_profile_dir(checkout, profile_path)
    else:
        checkout = default_checkout(repo, home)
        profile = find_profile_dir(checkout, profile_path)
    origin = _origin_url(process, checkout) if verify_origin else None
    if origin is not None and not same_repo(origin, repo.spec):
        manual = (
            f"{checkout} is a checkout of {origin}, not of {repo.spec}: move it away, or set "
            "[modules.dotfiles] dir to another place"
        )
        return Location(checkout / "<origin mismatch>", checkout, repo, source, login, notes, manual, private)
    root = profile or checkout / (profile_path or PROFILE_SUBDIRS[0])
    return Location(root, checkout, repo, source, login, notes, None, private)


def locate(
    *,
    home: Path,
    env: Mapping[str, str],
    process: Process | None = None,
    repo_override: str | None = None,
    refresh: bool = False,
    now: float | None = None,
    checkout_dir: str | None = None,
    profile_path: str | None = None,
    verify_origin: bool = True,
    persist: bool = True,
) -> Location:
    """``checkout_dir`` / ``profile_path`` are the dotfiles module's ``dir`` / ``path`` options; the module
    checks origins itself (it knows how to fix them), so it passes ``verify_origin=False``. ``persist=False``
    (a ``--dry-run``) does not write the discovery cache: nothing is written outside the session log."""
    clock = time.time() if now is None else now
    runner = process or Process()
    state_path = _state_file(home, env)
    state = _read_state(state_path)
    where = {"checkout_dir": checkout_dir, "profile_path": profile_path, "verify_origin": verify_origin}
    override, source = (repo_override, Source.FLAG) if repo_override else (env.get(REPO_ENV) or None, Source.ENV)
    if override:
        return _planned(RepoRef(override), home, source, runner, state, **where)
    local = _local_profile(home, state, checkout_dir, profile_path)
    if local is not None:
        return Location(local[1], local[0], None, Source.LOCAL)
    account = _gh_account(home, env)
    discovery = None if refresh else _cached(state, clock, account)
    from_cache = discovery is not None
    if discovery is None:
        discovery = discover_via_gh(runner)
        if _store(state, discovery, clock, account) and persist:
            _write_state(state_path, state)
    found = Source.CACHE if from_cache else Source.GH
    chosen = discovery.repo
    if chosen is None:
        return Location(
            home / ".files" / PROFILE_SUBDIRS[0],
            source=found,
            login=discovery.login,
            notes=discovery.notes,
            manual=discovery.manual,
        )
    return _planned(
        RepoRef(chosen.slug),
        home,
        found,
        runner,
        state,
        login=discovery.login,
        notes=discovery.notes,
        private=chosen.private,
        **where,
    )


def missing_lines(location: Location) -> list[str]:
    """What to tell the user when the profile directory does not exist."""
    if location.manual is not None:
        lines = [f"  needs manual: {location.manual}"]
    elif location.repo is not None:
        given = location.source.value if location.source else "given"
        origin = f"{given}, gh login {location.login}" if location.login else given
        found = f"found your dotfiles repository {location.repo.spec} ({origin})"
        if location.checkout is not None and location.checkout.is_dir():
            lines = [
                f"  {found}, cloned at {location.checkout}, but it holds no ubuntu profile:",
                "  add ubuntu/profile.toml to it (or ubuntu-tweaks/), or set [modules.dotfiles] path",
            ]
        else:
            lines = [
                f"  {found}; it is not cloned yet",
                "  `ubuntu-tweak init --commit` (module dotfiles) clones it and finds the ubuntu profile in it",
            ]
    else:
        who = f" for {location.login}" if location.login else ""
        names = ", ".join(DOTFILES_NAMES)
        lines = [
            f"  no dotfiles repository{who} (looked for {names}); only the modules apply",
            "  to use your own settings, create one with an ubuntu/profile.toml in it, or pass",
            "  --dotfiles-repo OWNER/NAME (a repository of yours) or --profile DIR",
        ]
    return [*lines, *(f"  note: {note}" for note in location.notes)]


# -- the helper CLI of modules/dotfiles/install.sh -------------------------------------------------------


def _module_options(env: Mapping[str, str]) -> dict[str, str]:
    text = env.get(MODULE_CONFIG_ENV, "")
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError as error:
        raise ConfigError(f"{MODULE_CONFIG_ENV} is not JSON: {error}") from error
    allowed = ("repo", "path", "dir")
    return {key: str(data[key]) for key in allowed if isinstance(data, dict) and data.get(key) not in (None, "")}


def resolve_for_module(home: Path, env: Mapping[str, str], process: Process | None = None) -> dict[str, object]:
    """The dotfiles module's answer: what to clone where, or why there is nothing to do or a manual step."""
    options = _module_options(env)
    location = locate(
        home=home,
        env=env,
        process=process,
        repo_override=env.get(REPO_ENV) or options.get("repo"),
        checkout_dir=options.get("dir"),
        profile_path=options.get("path"),
        verify_origin=False,
    )
    repo, checkout = location.repo, location.checkout
    if checkout is None and options.get("dir") and expand_dir(home, options["dir"]).is_dir():
        checkout = expand_dir(home, options["dir"])  # a configured checkout without a profile (yet) is still its dir
    return {
        "repo": repo.spec if repo else None,
        "is_slug": repo.is_slug if repo else False,
        "dir": str(checkout) if checkout else None,
        "profile_dir": str(location.root) if location.root.is_dir() else None,
        "source": location.source.value if location.source else None,
        "login": location.login,
        "notes": list(location.notes),
        "manual": location.manual,
    }


def shell_assignments(answer: Mapping[str, object], env: Mapping[str, str]) -> str:
    """``resolve`` as shell assignments (every value quoted) for ``modules/dotfiles/install.sh``."""
    notes = "\n".join(f"note: {note}" for note in answer["notes"])  # type: ignore[attr-defined]
    values = {
        "repo": answer["repo"] or "",
        "is_slug": "True" if answer["is_slug"] else "False",
        "dir": answer["dir"] or "",
        "source": answer["source"] or "",
        "login": answer["login"] or "",
        "blocked": answer["manual"] or "",
        "profile_path": _module_options(env).get("path", ""),
        "notes": notes,
    }
    return "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())


def _remember_command(home: Path, env: Mapping[str, str], args: list[str]) -> int:
    """``remember DIR [--path P] [--repo R]``: record the checkout the dotfiles module put at ``DIR``."""
    checkout, path, repo = Path(args[0]), None, None
    rest = args[1:]
    while rest:
        if len(rest) < 2 or rest[0] not in {"--path", "--repo"}:
            return 2
        path, repo = (rest[1], repo) if rest[0] == "--path" else (path, rest[1])
        rest = rest[2:]
    remember(home, env, checkout, find_profile_dir(checkout, path), repo)
    return 0


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if env is None else env
    home = Path(environment["HOME"]) if environment.get("HOME") else Path.home()
    try:
        if args[:1] == ["resolve"] and len(args) == 1:
            print(json.dumps(resolve_for_module(home, environment)))
            return 0
        if args == ["resolve", "--shell"]:
            print(shell_assignments(resolve_for_module(home, environment), environment))
            return 0
        if args[:1] == ["same-repo"] and len(args) == 3:
            return 0 if same_repo(args[1], args[2]) else 1
        if args[:1] == ["remember"] and len(args) >= 2 and _remember_command(home, environment, args[1:]) == 0:
            return 0
        if args[:1] == ["profile"] and len(args) in (2, 3):
            found = find_profile_dir(Path(args[1]), args[2] if len(args) == 3 else None)
            if found is not None:
                print(found)
            return 0 if found is not None else 1
    except ConfigError as error:
        print(f"ubuntu-tweaks identity: {error}", file=sys.stderr)
        return 2
    print(
        "usage: identity resolve | same-repo A B | profile DIR [PATH] | remember DIR [--path P] [--repo R]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

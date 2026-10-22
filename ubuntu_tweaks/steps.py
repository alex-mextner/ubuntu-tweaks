"""Built-in steps: the named actions a module's ``[[step]]`` entries ask for.

Accessed via: ``planners/base.py`` looks each ``[[step]] name = "..."`` of an enabled module up in
``STEPS`` (the way ``[[seeds]]`` look up ``seeds.PROVIDERS``) and builds its actions.

Assumptions:

- A step is for what a plain command line cannot express: a check that reads the passwd database,
  a file that is created only while absent, a download verified against a pinned checksum. Anything
  that is just "run this, it is done when that exits 0" is a ``[[post]]`` instead.
- ``check()`` only reads. ``run()`` never prompts: a credential that is not in the keychain is a
  ``ManualStepRequired``, which the plan reports as ``needs manual: ...`` and goes on.
- Everything that touches the machine goes through ``StepEnv``: the ``Process`` (commands), ``root``
  (the directory that ``/usr``, ``/home/linuxbrew`` and ``/etc`` live under: ``/`` for real, a
  temporary directory in tests), ``home``, ``user``/``group``/``uid``, ``fetch`` (HTTPS download)
  and ``which``. No step reads the real system on its own.
- Secrets: the GitHub token is handed from the keychain to ``gh auth login --with-token`` through
  ``keychain exec --stdin``: it is never in argv, an environment variable, a file or this process.

Steps (``[[step]] name = ...``, arguments as extra keys of the entry):

- ``login-shell`` (``shell``, default ``zsh``): ``chsh -s /usr/bin/<shell> <user>`` through sudo;
  satisfied when the passwd entry's shell is named ``<shell>`` (a brew-installed zsh counts).
- ``shell-rc`` (``shell``, default ``zsh``): creates ``~/.zprofile`` and ``~/.zshrc`` that put brew
  and ``~/.local/bin`` on ``PATH``, **only while absent**: an existing file is never read or changed.
- ``git-identity``: ``git config --global user.name`` / ``user.email`` from ``[git] name`` / ``email``
  of ``profile.toml`` / ``machine.toml``; a value that is not set there is not touched (no default).
- ``gh-login`` (``host``, ``token_ref``): ``gh auth login --with-token`` with the token of the keychain
  item ``token_ref`` (default ``ubuntu-tweaks/gh-token``; ``[git] token_ref`` overrides).
- ``git-credential-helper`` (``host``): ``gh auth setup-git`` for the host.
- ``brew-prefix`` (``prefix``): creates the Homebrew prefix owned by the user, the one root step the
  installer would otherwise ask sudo for.
- ``brew-install`` (``prefix``, ``url``, ``sha256``): downloads the pinned installer, refuses it unless
  its SHA-256 matches, runs it with ``NONINTERACTIVE=1``.

Public API: ``STEPS``, ``StepEnv``, ``StepFactory``, ``get_step(name)``, ``default_fetch``, and the action
classes ``LoginShellAction``, ``RcFileAction``, ``GitConfigAction``, ``GitCredentialAction``,
``GhLoginAction``, ``BrewPrefixAction``, ``BrewInstallAction``.
"""

from __future__ import annotations

import grp
import hashlib
import os
import pwd
import shutil
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ubuntu_tweaks.actions import Action, ActionFailed, ManualStepRequired, Phase
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.process import CommandError, Process
from ubuntu_tweaks.profile import Config, TomlValue

DEFAULT_BREW_PREFIX = "/home/linuxbrew/.linuxbrew"
DEFAULT_TOKEN_REF = "ubuntu-tweaks/gh-token"
KEYCHAIN_CALLER = "ubuntu-tweaks"
DOWNLOAD_TIMEOUT_SECONDS = 60
GH_STATUS_TIMEOUT_SECONDS = 30
GIT_SECTION_KEYS = ("name", "email", "token_ref", "host")

ZPROFILE = """\
# Created by ubuntu-tweak (module zsh) because this file did not exist. It is yours: ubuntu-tweak
# never rewrites a file that is already there.
typeset -U path PATH
[[ -x /home/linuxbrew/.linuxbrew/bin/brew ]] && eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
path=("$HOME/.local/bin" $path)
export PATH
"""

ZSHRC = """\
# Created by ubuntu-tweak (module zsh) because this file did not exist. It is yours: ubuntu-tweak
# never rewrites a file that is already there.
typeset -U path PATH
[[ -z "$HOMEBREW_PREFIX" && -x /home/linuxbrew/.linuxbrew/bin/brew ]] && eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
path=("$HOME/.local/bin" $path)
"""

RC_FILES = {"zsh": ((".zprofile", ZPROFILE), (".zshrc", ZSHRC))}


def default_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ubuntu-tweak"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 (https only, checked)
        return response.read()


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _current_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


def _owned_by(path: Path, uid: int) -> bool:
    """``path`` is a directory owned by ``uid``; a path that vanishes mid-check is simply not owned."""
    try:
        return path.is_dir() and path.stat().st_uid == uid
    except OSError:
        return False


_UNSET = object()


@dataclass(frozen=True)
class StepEnv:
    config: Config
    process: Process
    root: Path = Path("/")
    home: Path = field(default_factory=Path.home)
    user: str = field(default_factory=_current_user)
    group: str = field(default_factory=_current_group)
    uid: int = field(default_factory=os.getuid)
    fetch: Callable[[str], bytes] = default_fetch
    which: Callable[[str], str | None] = shutil.which
    linked: frozenset[str] = frozenset()  # $HOME-relative paths the profile's files/ links (FILES phase)

    def system_path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def shown(self, path: Path) -> str:
        try:
            return f"~/{path.relative_to(self.home)}"
        except ValueError:
            return str(path)


type StepFactory = Callable[[StepEnv, str, Mapping[str, TomlValue]], list[Action]]


def _text_arg(args: Mapping[str, TomlValue], key: str, where: str, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise ConfigError(f"{where}: step argument '{key}' must be a non-empty string, got {value!r}")
    return value


def _only_args(args: Mapping[str, TomlValue], where: str, *allowed: str) -> None:
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise ConfigError(
            f"{where}: unknown step argument(s) {', '.join(unknown)}; allowed: {', '.join(allowed) or 'none'}"
        )


def git_section(config: Config) -> dict[str, str]:
    """``[git]`` of profile.toml / machine.toml (merged): the identity and the keychain reference."""
    table = config.table("git")
    unknown = sorted(set(table) - set(GIT_SECTION_KEYS))
    if unknown:
        raise ConfigError(f"[git]: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(GIT_SECTION_KEYS)}")
    section: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str) or not value.strip() or value.startswith("-"):
            raise ConfigError(f"[git] {key} must be a non-empty string, got {value!r}")
        section[key] = value
    return section


class LoginShellAction(Action):
    phase = Phase.BASE
    kind = "login-shell"
    needs_root = True

    def __init__(self, env: StepEnv, shell: str) -> None:
        self._env = env
        self._shell = shell

    @property
    def _target(self) -> str:
        return f"/usr/bin/{self._shell}"

    def _current(self) -> str | None:
        result = self._env.process.query(["getent", "passwd", self._env.user])
        if not result.ok or not result.stdout.strip():
            return None
        return result.stdout.strip().splitlines()[0].rsplit(":", 1)[-1]

    def describe(self) -> str:
        return f"chsh: set the login shell of {self._env.user} to {self._target} (now {self._current() or 'unknown'})"

    def check(self) -> bool:
        current = self._current()
        return current is not None and Path(current).name == self._shell

    def run(self) -> None:
        if not self._env.system_path(self._target).exists():
            raise ManualStepRequired(f"{self._target} is not installed yet: its package step did not run")
        _execute(self._env, self._env.process.elevate(["chsh", "-s", self._target, self._env.user]), "login-shell")


class RcFileAction(Action):
    phase = Phase.BASE
    kind = "shell-rc"

    def __init__(self, env: StepEnv, path: Path, content: str) -> None:
        self._env = env
        self._path = path
        self._content = content

    def describe(self) -> str:
        return f"create {self._env.shown(self._path)} (brew shellenv, ~/.local/bin on PATH); only because it is absent"

    def check(self) -> bool:
        return os.path.lexists(self._path)

    def run(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("x", encoding="utf-8") as handle:  # "x": never overwrite what appeared meanwhile
                handle.write(self._content)
        except FileExistsError:
            return  # it appeared since the preview: that is what the step wants, and it is not ours to touch


class GitConfigAction(Action):
    phase = Phase.BASE
    kind = "git-config"

    def __init__(self, env: StepEnv, key: str, value: str) -> None:
        self._env = env
        self._key = key
        self._value = value

    def _current(self) -> str | None:
        result = self._env.process.query(["git", "config", "--global", "--get", self._key])
        return result.stdout.strip() if result.ok else None

    def describe(self) -> str:
        return f"git config --global {self._key} {self._value} (now {self._current() or 'unset'})"

    def check(self) -> bool:
        return self._current() == self._value

    def run(self) -> None:
        if self._env.which("git") is None:
            raise ManualStepRequired(f"git config {self._key} (git is not installed yet: its package step did not run)")
        _execute(self._env, ["git", "config", "--global", self._key, self._value], "git-config")


class GitCredentialAction(Action):
    phase = Phase.BASE
    kind = "git-credential"
    HELPER_MARK = "gh auth git-credential"

    def __init__(self, env: StepEnv, host: str) -> None:
        self._env = env
        self._host = host

    def describe(self) -> str:
        return f"gh auth setup-git --hostname {self._host}: git asks gh for {self._host} credentials"

    def check(self) -> bool:
        result = self._env.process.query(
            ["git", "config", "--global", "--get-all", f"credential.https://{self._host}.helper"]
        )
        return result.ok and self.HELPER_MARK in result.stdout

    def run(self) -> None:
        if self._env.which("gh") is None:
            raise ManualStepRequired("gh auth setup-git (gh is not installed yet: its package step did not run)")
        _execute(self._env, ["gh", "auth", "setup-git", "--hostname", self._host, "--force"], "git-credential")


class GhLoginAction(Action):
    phase = Phase.BASE
    kind = "gh-login"

    def __init__(self, env: StepEnv, host: str, token_ref: str) -> None:
        self._env = env
        self._host = host
        self._ref = token_ref
        self._problem: str | None | object = _UNSET

    def _keychain(self, *words: str) -> list[str]:
        return ["keychain", *words, "--no-prompt", "--caller", KEYCHAIN_CALLER]

    def _token_problem(self) -> str | None:
        """Why no token can be taken from the keychain, or ``None`` when one can."""
        result = self._env.process.query([*self._keychain("exists"), self._ref])
        reasons = {
            0: None,
            127: "the keychain CLI is not installed",
            4: f"keychain item {self._ref} does not exist",
            5: "no secret store (Secret Service) is reachable",
            8: f"keychain denied caller {KEYCHAIN_CALLER} access to {self._ref} (see ~/.config/keychain-cli/policy.toml)",
            9: "the keyring is locked (`keychain unlock` in a terminal)",
        }
        if result.returncode in reasons:
            return reasons[result.returncode]
        return f"`keychain exists` exited {result.returncode}"

    def describe(self) -> str:
        if self._problem is _UNSET:
            self._problem = self._token_problem()  # one keychain probe per action, not one per describe()
        if self._problem is None:
            return f"gh: log in to {self._host} with the token of keychain item {self._ref} (read at run time, never stored)"
        return f"needs manual: gh auth login ({self._problem}; store a token with `keychain set {self._ref}`)"

    def check(self) -> bool:
        result = self._env.process.query(
            ["gh", "auth", "status", "--hostname", self._host], timeout=GH_STATUS_TIMEOUT_SECONDS
        )
        return result.ok

    def run(self) -> None:
        if self._env.which("gh") is None:
            raise ManualStepRequired("gh auth login (gh is not installed yet: its package step did not run)")
        problem = self._token_problem()
        if problem is not None:
            raise ManualStepRequired(f"gh auth login ({problem}; store a token with `keychain set {self._ref}`)")
        try:
            self._env.process.execute(
                [
                    *self._keychain("exec"),
                    "--stdin",
                    self._ref,
                    "--",
                    "gh",
                    "auth",
                    "login",
                    "--hostname",
                    self._host,
                    "--with-token",
                ]
            )
        except CommandError as error:
            first = next((line.strip() for line in str(error).splitlines() if line.strip()), "no output")
            raise ManualStepRequired(
                f"gh auth login (the keychain token was not accepted or {self._host} is unreachable: {first}; "
                f"fix the token with `keychain set {self._ref}` or run `gh auth login` yourself)"
            ) from error


class BrewPrefixAction(Action):
    phase = Phase.BASE
    kind = "brew-prefix"
    needs_root = True

    def __init__(self, env: StepEnv, prefix: str) -> None:
        self._env = env
        self._prefix = prefix

    def _owned(self) -> bool:
        return _owned_by(self._env.system_path(self._prefix), self._env.uid)

    def describe(self) -> str:
        return f"create {self._prefix} owned by {self._env.user}:{self._env.group} (the one step of the Homebrew install that needs root)"

    def check(self) -> bool:
        return self._owned() or self._env.system_path(f"{self._prefix}/bin/brew").exists()

    def run(self) -> None:
        path = self._env.system_path(self._prefix)
        if path.exists() and not _owned_by(path, self._env.uid):
            raise ManualStepRequired(
                f"{self._prefix} exists but is not owned by {self._env.user}: "
                f"run `sudo chown -R {self._env.user}:{self._env.group} {self._prefix}` yourself"
            )
        argv = ["install", "-d", "-o", self._env.user, "-g", self._env.group, self._prefix]
        _execute(self._env, self._env.process.elevate(argv), "brew-prefix")


class BrewInstallAction(Action):
    phase = Phase.BASE
    kind = "brew-install"

    def __init__(self, env: StepEnv, prefix: str, url: str, sha256: str) -> None:
        self._env = env
        self._prefix = prefix
        self._url = url
        self._sha256 = sha256

    def describe(self) -> str:
        return (
            f"brew: run the Homebrew installer non-interactively into {self._prefix} "
            f"(downloaded from {self._url}, refused unless sha256 is {self._sha256[:12]}...)"
        )

    def check(self) -> bool:
        return self._env.system_path(f"{self._prefix}/bin/brew").exists()

    def run(self) -> None:
        if not _owned_by(self._env.system_path(self._prefix), self._env.uid):
            raise ManualStepRequired(
                f"{self._prefix} does not exist or is not owned by {self._env.user}: its root step did not run "
                "(run again without --no-sudo, or create it with sudo)"
            )
        script = self._env.fetch(self._url)
        digest = hashlib.sha256(script).hexdigest()
        if digest != self._sha256:
            raise ActionFailed(f"refusing the Homebrew installer: sha256 is {digest}, the manifest pins {self._sha256}")
        directory = Path(tempfile.mkdtemp(prefix="ubuntu-tweak-brew-"))
        try:
            installer = directory / "install.sh"
            installer.write_bytes(script)
            installer.chmod(0o600)
            try:
                self._env.process.execute(["env", "NONINTERACTIVE=1", "/bin/bash", str(installer)])
            except CommandError as error:
                raise ActionFailed(f"the Homebrew installer failed: {error}") from error
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def _login_shell(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    where = f"module {module} step login-shell"
    _only_args(args, where, "shell")
    shell = _text_arg(args, "shell", where, "zsh")
    if "/" in shell:
        raise ConfigError(f"{where}: step argument 'shell' is a name like 'zsh', not a path: {shell!r}")
    return [LoginShellAction(env, shell)]


def _shell_rc(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    where = f"module {module} step shell-rc"
    _only_args(args, where, "shell")
    shell = _text_arg(args, "shell", where, "zsh")
    if shell not in RC_FILES:
        raise ConfigError(f"{where}: no rc file template for shell {shell!r}; known: {', '.join(RC_FILES)}")
    return [RcFileAction(env, env.home / name, content) for name, content in RC_FILES[shell] if name not in env.linked]


def _git_identity(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    _only_args(args, f"module {module} step git-identity")
    section = git_section(env.config)
    return [GitConfigAction(env, f"user.{key}", section[key]) for key in ("name", "email") if key in section]


def _gh_login(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    where = f"module {module} step gh-login"
    _only_args(args, where, "host", "token_ref")
    section = git_section(env.config)  # the profile is the user's: it wins over what a manifest ships
    ref = section.get("token_ref") or _text_arg(args, "token_ref", where, DEFAULT_TOKEN_REF)
    host = section.get("host") or _text_arg(args, "host", where, "github.com")
    return [GhLoginAction(env, host, ref)]


def _git_credential_helper(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    where = f"module {module} step git-credential-helper"
    _only_args(args, where, "host")
    section = git_section(env.config)
    return [GitCredentialAction(env, section.get("host") or _text_arg(args, "host", where, "github.com"))]


def _brew_prefix(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    where = f"module {module} step brew-prefix"
    _only_args(args, where, "prefix")
    return [BrewPrefixAction(env, _text_arg(args, "prefix", where, DEFAULT_BREW_PREFIX))]


def _brew_install(env: StepEnv, module: str, args: Mapping[str, TomlValue]) -> list[Action]:
    where = f"module {module} step brew-install"
    _only_args(args, where, "prefix", "url", "sha256")
    url = _text_arg(args, "url", where)
    sha256 = _text_arg(args, "sha256", where).lower()
    if not url.startswith("https://"):
        raise ConfigError(f"{where}: url must be https, got {url!r}")
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ConfigError(f"{where}: sha256 must be 64 hex digits, got {sha256!r}")
    return [BrewInstallAction(env, _text_arg(args, "prefix", where, DEFAULT_BREW_PREFIX), url, sha256)]


STEPS: dict[str, StepFactory] = {
    "login-shell": _login_shell,
    "shell-rc": _shell_rc,
    "git-identity": _git_identity,
    "gh-login": _gh_login,
    "git-credential-helper": _git_credential_helper,
    "brew-prefix": _brew_prefix,
    "brew-install": _brew_install,
}


def get_step(name: str) -> StepFactory:
    try:
        return STEPS[name]
    except KeyError:
        raise ConfigError(f"unknown step {name!r}; known: {', '.join(sorted(STEPS))}") from None


def _execute(env: StepEnv, argv: list[str], step: str) -> None:
    """Run a mutating command; a failure names the step instead of surfacing a bare command error."""
    try:
        env.process.execute(argv)
    except CommandError as error:
        raise ActionFailed(f"{step}: {error}") from error

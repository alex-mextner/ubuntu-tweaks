"""tailnet-mounts engine: find the Linux peers of a Tailscale network, ask each for its disks, keep systemd sshfs units.

Accessed via: ``modules/tailnet-mounts/tailnet-mounts`` (the ``check`` / ``status`` / ``plan`` / ``install`` script) and
``modules/tailnet-mounts/tailnet-mounts-sync`` (the program ``tailnet-mounts-sync.service`` runs as root). Both import this
file, and ``install`` copies it, with ``ubuntu_tweaks/disks.py`` and ``errors.py``, to ``/usr/local/lib/tailnet-mounts``:
the timer must never run code a desktop user can write, and the installed copy must not need the repository.
That is why this file imports nothing but the standard library and those two files.

Assumptions: a ``Runner`` (``run(argv, timeout) -> object with returncode, stdout, stderr``) is the only way the
engine touches the machine (tailscale, ssh, systemctl, findmnt, fusermount3); every path it reads or writes is under
``root`` (``/`` for real, a temporary directory in tests). Everything a peer prints is untrusted: only values that
pass the regular expressions below ever reach a unit file, and a unit value never holds a newline or ``%``.

Public API:

- ``parse_config(options, *, user, uid, gid, home) -> Config`` (``ConfigError`` on an unknown or mistyped option),
  ``Config.to_json()`` / ``Config.from_json(text)``.
- ``parse_status(text) -> Tailnet`` (``SyncError`` unless tailscale is running), ``parse_shares(output, config)``
  (``DiscoveryError`` when a peer's answer is not the expected lsblk JSON), ``clean_name(text)``.
- ``render_mount(host, address, share, config)``, ``render_automount(host, share, config)``, ``unit_stem(host, name)``.
- ``Syncer(root, config, run, *, now, known_hosts)``: ``observe()`` (read-only; asks the peers), ``plan(observation)``
  (read-only; what would change) and ``apply(changes)`` or simply ``sync()``; ``read_state(root)``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from ubuntu_tweaks.disks import unit_name
from ubuntu_tweaks.errors import ConfigError

HEADER = "# managed by ubuntu-tweaks tailnet-mounts"
MOUNT_BASE = "/mnt/tailnet"
STATE_DIR = "var/lib/tailnet-mounts"
KNOWN_HOSTS = "/var/lib/tailnet-mounts/known_hosts"
STATE_FILE = f"{STATE_DIR}/state.json"
UNIT_DIR = "etc/systemd/system"
RETENTION_DAYS = 30
DAY = 86400
DEFAULT_ROOTS = ("/mnt", "/media", "/run/media")
ALLOWED_OPTIONS = frozenset({"exclude_hosts", "identity_file", "idle_timeout", "roots", "home"})
SKIP_FILESYSTEMS = frozenset({"swap", "squashfs", "LVM2_member", "crypto_LUKS", "linux_raid_member", "zfs_member"})
REMOTE_COMMAND = (
    "lsblk -J -o NAME,TYPE,FSTYPE,LABEL,MOUNTPOINTS 2>/dev/null || lsblk -J -o NAME,TYPE,FSTYPE,LABEL,MOUNTPOINT;"
    " printf 'HOME=%s\\n' \"$HOME\""
)

_PATH = re.compile(r"^/[A-Za-z0-9._/@+ -]{1,200}$")
_KEY_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]{1,200}$")
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_ROOT = re.compile(r"^/(mnt|media|run/media)(/[A-Za-z0-9._-]+)*$")
_WHERE = re.compile(r"^Where=" + re.escape(MOUNT_BASE) + r"/([a-z0-9][a-z0-9._-]*)/([a-z0-9][a-z0-9._-]*)$", re.M)
_WHAT = re.compile(r"^What=[a-z_][a-z0-9_-]*@[^:\n]+:(/[^\n]*)$", re.M)


class SyncError(Exception):
    """The sync cannot be trusted to have seen the tailnet (tailscale down, a link in the mount root, ...)."""


class DiscoveryError(Exception):
    """A peer answered, but not with the lsblk JSON and HOME line this module asked for."""


class Result(Protocol):
    returncode: int
    stdout: str | None
    stderr: str | None


Runner = Callable[[Sequence[str], float], Result]


# -- configuration -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    user: str
    uid: int
    gid: int
    identity_file: str
    exclude_hosts: tuple[str, ...] = ()
    idle_timeout: int = 300
    roots: tuple[str, ...] = DEFAULT_ROOTS
    include_home: bool = True

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Config:
        data = json.loads(text)
        data["exclude_hosts"] = tuple(data["exclude_hosts"])
        data["roots"] = tuple(data["roots"])
        return cls(**data)


def _fail(message: str) -> ConfigError:
    return ConfigError(f"[modules.tailnet-mounts] {message}")


def _strings(options: Mapping[str, Any], key: str, default: Sequence[str]) -> tuple[str, ...]:
    value = options.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _fail(f"{key} must be a list of non-empty strings")
    return tuple(value)


def parse_config(options: Mapping[str, Any], *, user: str, uid: int, gid: int, home: str) -> Config:
    unknown = sorted(set(options) - ALLOWED_OPTIONS)
    if unknown:
        raise _fail(f"unknown option {unknown[0]!r}; known: {', '.join(sorted(ALLOWED_OPTIONS))}")
    if not _USER.match(user):
        raise _fail(f"cannot use the user name {user!r}")
    identity = options.get("identity_file", f"{home.rstrip('/')}/.ssh/id_ed25519")
    if not isinstance(identity, str) or not _KEY_PATH.match(identity) or ".." in identity.split("/"):
        raise _fail("identity_file must be an absolute path of letters, digits and . _ / @ + -")
    timeout = options.get("idle_timeout", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 86400:
        raise _fail("idle_timeout must be a whole number of seconds between 10 and 86400")
    include_home = options.get("home", True)
    if not isinstance(include_home, bool):
        raise _fail("home must be true or false")
    roots = _strings(options, "roots", DEFAULT_ROOTS)
    for root in roots:
        if not _ROOT.match(root) or ".." in root.split("/"):
            raise _fail(f"roots: {root!r} must be /mnt, /media or /run/media, or a directory below one of them")
    return Config(
        user=user,
        uid=uid,
        gid=gid,
        identity_file=identity,
        exclude_hosts=tuple(clean_name(name) for name in _strings(options, "exclude_hosts", ())),
        idle_timeout=timeout,
        roots=roots,
        include_home=include_home,
    )


# -- names -------------------------------------------------------------------------------------------------------


def clean_name(text: str) -> str:
    """Lower case, ``[a-z0-9._-]`` only, no leading or trailing ``-`` or ``.``: a safe directory and unit part."""
    reduced = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9._-]", "-", text.lower())).strip("-.")
    return reduced[:60].strip("-.") or "disk"


def unit_stem(host: str, name: str) -> str:
    return unit_name(f"{MOUNT_BASE}/{host}/{name}", "mount")[: -len(".mount")]


# -- peers -------------------------------------------------------------------------------------------------------


class Peer(NamedTuple):
    host: str  # the directory name under /mnt/tailnet
    address: str | None  # what ssh connects to: the MagicDNS short name, else the first IPv4 address
    os: str
    online: bool


class Tailnet(NamedTuple):
    self_host: str
    peers: tuple[Peer, ...]


def parse_status(text: str) -> Tailnet:
    try:
        data = json.loads(text)
        state = data["BackendState"]
        self_host = clean_name(str(data["Self"]["HostName"]))
        raw_peers = data.get("Peer") or {}
        if not isinstance(raw_peers, dict):
            raise TypeError("Peer")
    except (ValueError, KeyError, TypeError) as error:
        raise SyncError(f"cannot read `tailscale status --json`: {error!r}") from error
    if state != "Running":
        raise SyncError(f"tailscale is {state}, not Running: units are left as they are")
    found: dict[str, Peer] = {}
    for entry in raw_peers.values():
        name = str(entry.get("HostName", ""))
        host = clean_name(name)
        if host == self_host or host in found:
            continue
        addresses = [ip for ip in entry.get("TailscaleIPs") or [] if isinstance(ip, str) and ":" not in ip]
        label = name.lower()
        address = label if _LABEL.match(label) else (addresses[0] if addresses else None)
        found[host] = Peer(host, address, str(entry.get("OS", "")).lower(), entry.get("Online") is True)
    return Tailnet(self_host, tuple(found.values()))


# -- shares ------------------------------------------------------------------------------------------------------


class Share(NamedTuple):
    name: str  # the directory under /mnt/tailnet/<host>
    path: str  # the directory on the peer


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not _PATH.match(path):
        return False
    return not any(part in ("", ".", "..") for part in path.split("/")[1:])


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path.startswith(root + "/") for root in roots)


def _walk(devices: object, depth: int = 0) -> list[dict[str, Any]]:
    if not isinstance(devices, list) or depth > 8:
        raise DiscoveryError("lsblk output has no device list")
    found: list[dict[str, Any]] = []
    for device in devices:
        if isinstance(device, dict):
            found.append(device)
            if "children" in device:
                found += _walk(device["children"], depth + 1)
    return found


def parse_shares(output: str, config: Config) -> list[Share]:
    lines = output.splitlines()
    home_at = max((index for index, line in enumerate(lines) if line.startswith("HOME=")), default=None)
    try:
        data = json.loads("\n".join(lines if home_at is None else lines[:home_at]))
        devices = _walk(data["blockdevices"])
    except (ValueError, KeyError, TypeError) as error:
        raise DiscoveryError(f"not the lsblk JSON that was asked for ({type(error).__name__})") from error
    paths: set[str] = set()
    for device in devices:
        fstype = device.get("fstype")
        if not isinstance(fstype, str) or fstype in SKIP_FILESYSTEMS:
            continue
        points = device.get("mountpoints", [device.get("mountpoint")])
        for point in points if isinstance(points, list) else []:
            if _valid_path(point) and _under(point, config.roots) and not (point + "/").startswith(MOUNT_BASE + "/"):
                paths.add(point)
    home = lines[home_at][len("HOME=") :] if home_at is not None else ""
    shares = []
    taken: set[str] = set()
    if config.include_home and _valid_path(home):
        shares.append(Share("home", home))
        taken.add("home")
    for path in sorted(paths):
        base = clean_name(path.rsplit("/", 1)[1])
        name, number = base, 1
        while name in taken:
            number += 1
            name = f"{base}-{number}"
        taken.add(name)
        shares.append(Share(name, path))
    return sorted(shares)


# -- unit text ---------------------------------------------------------------------------------------------------


def _value(text: str) -> str:
    """A value fit for a unit file line: one line, no specifier, no control characters."""
    if "%" in text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"refusing to put {text!r} into a unit file")
    return text


def _where(host: str, share: Share) -> str:
    return f"{MOUNT_BASE}/{host}/{share.name}"


def render_mount(host: str, address: str, share: Share, config: Config) -> str:
    options = ",".join(
        [
            "_netdev",
            "nofail",
            "allow_other",
            "default_permissions",
            f"uid={config.uid}",
            f"gid={config.gid}",
            "reconnect",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
            "ConnectTimeout=8",
            "BatchMode=yes",
            "StrictHostKeyChecking=accept-new",
            f"UserKnownHostsFile={KNOWN_HOSTS}",
            f"IdentityFile={config.identity_file}",
            "compression=no",
            "cache=yes",
            "auto_cache",
        ]
    )
    return (
        f"{HEADER}\n"
        "[Unit]\n"
        f"Description=sshfs {_value(host)}:{_value(share.path)} (tailnet-mounts)\n"
        "After=network-online.target tailscaled.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Mount]\n"
        f"What={_value(config.user)}@{_value(address)}:{_value(share.path)}\n"
        f"Where={_value(_where(host, share))}\n"
        "Type=fuse.sshfs\n"
        f"Options={_value(options)}\n"
        "TimeoutSec=25\n"
    )


def render_automount(host: str, share: Share, config: Config) -> str:
    return (
        f"{HEADER}\n"
        "[Unit]\n"
        f"Description=sshfs {_value(host)}:{_value(share.path)} on first access (tailnet-mounts)\n"
        "\n"
        "[Automount]\n"
        f"Where={_value(_where(host, share))}\n"
        f"TimeoutIdleSec={config.idle_timeout}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# -- files -------------------------------------------------------------------------------------------------------


def write_if_changed(path: Path, text: str | bytes, mode: int = 0o644) -> bool:
    """Atomic write (mode set before the rename); ``False`` and no write when the file already is exactly this."""
    data = text.encode() if isinstance(text, str) else text
    try:
        if path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return True


def read_state(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((root / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -- sync --------------------------------------------------------------------------------------------------------


@dataclass
class PeerView:
    host: str
    address: str | None
    state: str  # ok | error | offline | excluded | not-linux
    error: str = ""
    shares: list[Share] = field(default_factory=list)


@dataclass
class Observation:
    tailnet: Tailnet
    views: dict[str, PeerView]


@dataclass
class Existing:
    host: str
    name: str
    path: str
    files: list[str]


@dataclass
class Changes:
    writes: dict[str, str] = field(default_factory=dict)
    removals: list[Existing] = field(default_factory=list)
    mountpoints: list[tuple[str, str]] = field(default_factory=list)
    enable: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.writes) + len(self.removals) + len(self.enable) + len(self.mountpoints)

    def lines(self) -> list[str]:
        out = list(self.notes)
        for host, name in self.mountpoints:
            out.append(f"mkdir {MOUNT_BASE}/{host}/{name}")
        for file in sorted(self.writes):
            out.append(f"write /{UNIT_DIR}/{file}")
        for gone in self.removals:
            out.append(f"remove {MOUNT_BASE}/{gone.host}/{gone.name}: stop and delete {', '.join(gone.files)}")
        if self.enable:
            out.append("systemctl enable --now " + " ".join(self.enable))
        out += [f"warning: {line}" for line in self.warnings]
        return out


@dataclass
class SyncReport:
    lines: list[str]
    warnings: list[str]
    changed: int


def _one_line(text: str | None, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        line = "".join(char if char.isprintable() else "?" for char in line).strip()
        if line:
            return line[:limit]
    return ""


class Syncer:
    def __init__(
        self,
        root: Path,
        config: Config,
        run: Runner,
        *,
        now: Callable[[], float] = time.time,
        known_hosts: str = KNOWN_HOSTS,
    ) -> None:
        self._root = root
        self._config = config
        self._run = run
        self._now = now
        self._known_hosts = known_hosts

    # -- observe (read-only) ---------------------------------------------------------------------------------

    def _ssh(self, peer: Peer) -> list[str]:
        options = [
            "BatchMode=yes",
            "ConnectTimeout=8",
            "StrictHostKeyChecking=accept-new",
            f"UserKnownHostsFile={self._known_hosts}",
            "LogLevel=ERROR",
            "IdentitiesOnly=yes",
            "ServerAliveInterval=5",
            "ServerAliveCountMax=2",
        ]
        argv = ["ssh", "-F", "/dev/null"]
        for option in options:
            argv += ["-o", option]
        return [*argv, "-i", self._config.identity_file, f"{self._config.user}@{peer.address}", REMOTE_COMMAND]

    def _discover(self, peer: Peer) -> PeerView:
        result = self._run(self._ssh(peer), 40)
        if result.returncode != 0:
            reason = _one_line(result.stderr) or f"ssh exited {result.returncode}"
            return PeerView(peer.host, peer.address, "error", f"ssh: {reason}")
        try:
            return PeerView(peer.host, peer.address, "ok", "", parse_shares(result.stdout or "", self._config))
        except DiscoveryError as error:
            return PeerView(peer.host, peer.address, "error", str(error))

    def observe(self) -> Observation:
        result = self._run(["tailscale", "status", "--json"], 30)
        if result.returncode != 0:
            raise SyncError(f"tailscale status failed: {_one_line(result.stderr) or result.returncode}")
        tailnet = parse_status(result.stdout or "")
        views: dict[str, PeerView] = {}
        asked: list[Peer] = []
        for peer in tailnet.peers:
            if peer.os != "linux":
                views[peer.host] = PeerView(peer.host, peer.address, "not-linux")
            elif peer.host in self._config.exclude_hosts:
                views[peer.host] = PeerView(peer.host, peer.address, "excluded")
            elif not peer.online:
                views[peer.host] = PeerView(peer.host, peer.address, "offline")
            elif peer.address is None:
                views[peer.host] = PeerView(peer.host, peer.address, "error", "no IPv4 address and no DNS-safe name")
            else:
                asked.append(peer)
        if asked:
            with ThreadPoolExecutor(max_workers=min(8, len(asked))) as pool:
                for view in pool.map(self._discover, asked):
                    views[view.host] = view
        return Observation(tailnet, views)

    # -- plan (read-only) ------------------------------------------------------------------------------------

    def _units(self) -> Path:
        return self._root / UNIT_DIR

    def _existing(self) -> dict[str, Existing]:
        """The units this module wrote: header on the first line and a ``Where=`` that is ``/mnt/tailnet/<host>/<disk>``."""
        found: dict[str, Existing] = {}
        base = self._units()
        if not base.is_dir():
            return found
        for path in sorted(base.glob("mnt-tailnet-*.*mount")):
            if path.suffix not in (".mount", ".automount") or not path.is_file() or path.is_symlink():
                continue
            text = path.read_text(errors="replace")
            where = _WHERE.search(text)
            if not text.startswith(HEADER + "\n") or not where or unit_stem(*where.groups()) != path.stem:
                continue
            what = _WHAT.search(text)
            entry = found.setdefault(path.stem, Existing(where.group(1), where.group(2), "", []))
            entry.files.append(path.name)
            if what and _valid_path(what.group(1)):
                entry.path = what.group(1)
        return found

    def _not_running(self, units: list[str]) -> list[str]:
        """The automount units that are not both enabled and active (one systemctl call each for the two facts)."""
        if not units:
            return []
        active = (self._run(["systemctl", "is-active", *units], 30).stdout or "").split()
        enabled = (self._run(["systemctl", "is-enabled", *units], 30).stdout or "").split()
        if len(active) != len(units) or len(enabled) != len(units):
            return units
        return [unit for unit, a, e in zip(units, active, enabled, strict=True) if a != "active" or e != "enabled"]

    def plan(self, observation: Observation) -> Changes:
        config = self._config
        existing = self._existing()
        previous = read_state(self._root) or {}
        today = int(self._now() // DAY)
        last_seen: dict[str, int] = {
            host: day for host, day in (previous.get("last_seen") or {}).items() if isinstance(day, int)
        }
        changes = Changes()
        present = {peer.host for peer in observation.tailnet.peers}
        for host in present:
            last_seen[host] = today
        wanted: dict[str, tuple[str, Share, PeerView]] = {}
        for view in observation.views.values():
            if view.state == "ok":
                for share in view.shares:
                    wanted[unit_stem(view.host, share.name)] = (view.host, share, view)
        for view in observation.views.values():
            if view.state == "error":
                changes.warnings.append(f"{view.host}: {view.error}")
        keep: dict[str, Existing] = {}
        for stem, entry in existing.items():
            view = observation.views.get(entry.host)
            if stem in wanted:
                continue
            if view is not None and view.state in ("error", "offline"):
                keep[stem] = entry
            elif view is None and entry.host != observation.tailnet.self_host:
                seen = last_seen.setdefault(entry.host, today)
                if today - seen <= RETENTION_DAYS:
                    keep[stem] = entry
                    changes.notes.append(
                        f"keep {entry.host}/{entry.name}: {entry.host} left the tailnet {today - seen} day(s) ago"
                    )
                    continue
                changes.removals.append(entry)
            else:  # reachable and no longer reporting it, excluded, not Linux, or this very machine
                changes.removals.append(entry)
        state_peers: dict[str, Any] = {}
        for host, view in sorted(observation.views.items()):
            if view.state in ("excluded", "not-linux"):
                continue
            shares = view.shares if view.state == "ok" else self._kept_shares(keep, host)
            state_peers[host] = {
                "address": view.address,
                "state": view.state,
                "error": view.error,
                "shares": [{"name": share.name, "path": share.path} for share in shares],
            }
        for host in sorted({entry.host for entry in keep.values()} - set(state_peers)):
            state_peers[host] = {"address": None, "state": "absent", "error": "", "shares": [
                {"name": share.name, "path": share.path} for share in self._kept_shares(keep, host)
            ]}  # fmt: skip
        for stem, (host, share, view) in wanted.items():
            address = view.address or ""
            for suffix, text in (
                ("mount", render_mount(host, address, share, config)),
                ("automount", render_automount(host, share, config)),
            ):
                file = f"{stem}.{suffix}"
                try:
                    current = (self._units() / file).read_text()
                except FileNotFoundError:
                    current = None
                if current != text:
                    changes.writes[file] = text
            if not self._mountpoint_exists(host, share.name):
                changes.mountpoints.append((host, share.name))
        automounts = sorted([f"{stem}.automount" for stem in [*wanted, *keep]])
        new = {f"{stem}.automount" for stem in wanted if f"{stem}.automount" in changes.writes}
        changes.enable = sorted(set(self._not_running([unit for unit in automounts if unit not in new])) | new)
        changes.state = {"peers": state_peers, "last_seen": dict(sorted(last_seen.items()))}
        return changes

    @staticmethod
    def _kept_shares(keep: Mapping[str, Existing], host: str) -> list[Share]:
        return sorted(Share(entry.name, entry.path) for entry in keep.values() if entry.host == host and entry.path)

    def _mountpoint_exists(self, host: str, name: str) -> bool:
        return (self._root / MOUNT_BASE.lstrip("/") / host / name).is_dir()

    # -- apply (root) ----------------------------------------------------------------------------------------

    def _chain(self, parts: Sequence[str], *, create: bool) -> int:
        """An open descriptor of the directory ``parts`` below the root, every step opened without following links;
        a missing step is made when ``create`` (``/mnt/tailnet`` itself private to the desktop user)."""
        descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
                    )
                except OSError as error:
                    raise SyncError(
                        f"{part!r} on the way to {MOUNT_BASE} is not a real directory ({error.strerror})"
                    ) from None
                os.close(descriptor)
                descriptor = child
                if create and part == "tailnet":
                    self._private(child)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _private(self, descriptor: int) -> None:
        info = os.fstat(descriptor)
        if info.st_uid != self._config.uid or info.st_gid != self._config.gid:
            os.fchown(descriptor, self._config.uid, self._config.gid)
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)

    def _make_mountpoint(self, host: str, name: str) -> None:
        """``/mnt/tailnet/<host>/<name>``: plain directories; the last one is only ``lstat``-ed when it exists, never
        opened (opening the directory of an active automount would mount the peer's disk)."""
        parent = self._chain([*MOUNT_BASE.strip("/").split("/"), host], create=True)
        try:
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
            except FileExistsError:
                if not stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise SyncError(f"{MOUNT_BASE}/{host}/{name} exists and is not a directory") from None
        finally:
            os.close(parent)

    def _rmdir_in(self, parts: Sequence[str], name: str) -> None:
        """``rmdir`` of ``name`` below the directory ``parts``, without following links; busy or missing is fine."""
        try:
            parent = self._chain(parts, create=False)
        except (SyncError, OSError):
            return
        try:
            os.rmdir(name, dir_fd=parent)
        except OSError:
            pass  # still mounted, not empty (the user put something there), or already gone
        finally:
            os.close(parent)

    def _remove_mountpoint(self, host: str, name: str) -> None:
        base = MOUNT_BASE.strip("/").split("/")
        self._rmdir_in([*base, host], name)
        self._rmdir_in(base, host)

    def _remove(self, entry: Existing) -> None:
        stem = unit_stem(entry.host, entry.name)
        units = [f"{stem}.automount", f"{stem}.mount"]
        point = f"{MOUNT_BASE}/{entry.host}/{entry.name}"
        self._run(["systemctl", "stop", *units], 60)
        self._run(["systemctl", "disable", units[0]], 30)
        if self._run(["findmnt", "-rn", "-M", point], 15).returncode == 0:
            self._run(["fusermount3", "-uz", point], 30)
        for file in entry.files:
            (self._units() / file).unlink(missing_ok=True)
        self._remove_mountpoint(entry.host, entry.name)

    def apply(self, changes: Changes) -> SyncReport:
        failures: list[str] = []
        for entry in changes.removals:
            self._remove(entry)
        for host, name in changes.mountpoints:
            self._make_mountpoint(host, name)
        for file, text in changes.writes.items():
            write_if_changed(self._units() / file, text)
        if changes.writes or changes.removals:
            self._run(["systemctl", "daemon-reload"], 60)
        if changes.enable:
            done = self._run(["systemctl", "enable", "--now", *changes.enable], 120)
            if done.returncode != 0:
                failures.append(f"systemctl enable --now failed: {_one_line(done.stderr) or done.returncode}")
        write_if_changed(self._root / STATE_FILE, json.dumps(changes.state, indent=2, sort_keys=True) + "\n")
        if failures:
            raise SyncError("; ".join(failures))
        return SyncReport(changes.lines(), list(changes.warnings), changes.count)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One sync at a time: the timer's run and an ``install`` must not write the same units together."""
        directory = self._root / STATE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def sync(self) -> SyncReport:
        with self._locked():
            return self.apply(self.plan(self.observe()))

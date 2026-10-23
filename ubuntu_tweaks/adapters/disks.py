"""disk adapter: the filesystems of the machine's block devices and the ``/etc/fstab`` lines nobody manages (spec, *Coverage*).

Accessed via: adapter discovery (``adapters/__init__.py``): ``discover``, ``classify``, ``owned``, ``capture``.

Assumptions:

- Items are ``disk:uuid:<UUID>`` (``disk:label:<label>`` / ``disk:dev:<path>`` for a filesystem without a UUID): every
  block device ``lsblk --json`` reports with a filesystem, mounted or not, plugged in now. ``disk:loops`` is one item for
  all loop devices (snap images). ``disk:fstab:<mount point or source>`` is an ``/etc/fstab`` line outside the
  ``ubuntu-tweaks disks`` block whose source is not a block device found above (swap files, tmpfs, network mounts); a
  line that names a found device is folded into that device's ``detail``. Without ``lsblk`` there are no items.
- ``kind`` carries the opinion ``classify`` turns into a reason: ``system`` (mounted at ``/``, ``/boot``, ``/boot/efi``,
  ``/home``, ``/var``, ...), ``loop``, ``swap``, ``pseudo`` (tmpfs, proc, ...), ``efi`` (an EFI System Partition that is
  not the booted one: boot loader files), all ``excluded`` with that reason; ``filesystem`` (a data disk) and ``network``
  stay for the generic rules, so they are ``untracked`` until declared or excluded by a rule of the profile.
- ``owned``: the disks declared as ``[modules.disks.mounts.<name>]`` in the profile (shared or machine overlay): a
  declaration by ``uuid``, ``label`` or ``partuuid`` owns the plugged-in device that carries it; an unplugged declared
  disk has no item and so nothing to track.
- ``capture`` proposes the ``[modules.disks.mounts.<name>]`` text for the untracked ``filesystem`` items the ``disks``
  module can mount (``ubuntu_tweaks.disks.FILESYSTEMS``), for the machine overlay. Filesystems another OS owns (ntfs,
  apfs, hfsplus, udf, iso9660) are proposed ``read_only = true`` with no forcing option. A **Windows** NTFS partition is a
  proposal only: its text is commented out, so writing it into the profile enables nothing until the user uncomments it.
  Containers the module cannot open (LUKS, LVM, RAID members) get no proposal.

Public API: ``NAME``, ``discover(ctx)``, ``classify(ctx, item, rules)``, ``owned(ctx)``, ``capture(ctx, items)``,
``parse_lsblk(text)``, ``Device``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks import disks as lib
from ubuntu_tweaks.adapters._system import system
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Finding, Item, Layer, Proposal, Rules

NAME = "disk"
LSBLK = (
    "lsblk",
    "--json",
    "-o",
    "NAME,PATH,TYPE,UUID,PARTUUID,LABEL,PARTLABEL,PARTTYPENAME,FSTYPE,MOUNTPOINTS,SIZE,TRAN,HOTPLUG,PKNAME",
)
SYSTEM_MOUNTS = frozenset(
    {"/", "/boot", "/boot/efi", "/boot/firmware", "/home", "/var", "/usr", "/opt", "/srv", "/tmp"}
)
PSEUDO_FSTYPES = frozenset(
    {
        "tmpfs", "ramfs", "proc", "sysfs", "devpts", "devtmpfs", "cgroup", "cgroup2", "securityfs", "pstore", "debugfs",
        "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "bpf", "efivarfs", "autofs", "overlay",
    }
)  # fmt: skip
REASONS = {
    "loop": "read-only loop images (snap packages)",
    "swap": "swap space",
    "pseudo": "kernel pseudo or temporary filesystem",
    "efi": "an EFI System Partition that is not the booted one: boot loader files only",
}
_NAME = re.compile(r"[^a-z0-9._-]+")
_MICROSOFT_RESERVED = "Microsoft reserved"


@dataclass(frozen=True)
class Device:
    name: str
    path: str
    type: str
    uuid: str
    partuuid: str
    label: str
    partlabel: str
    parttype: str
    fstype: str
    mountpoints: tuple[str, ...]
    size: str
    transport: str
    hotplug: bool
    parent: str


def _text(node: Mapping[str, object], key: str) -> str:
    value = node.get(key)
    return value if isinstance(value, str) else ""


def parse_lsblk(text: str) -> list[Device]:
    """The devices of ``lsblk --json`` output, parents before children; a child inherits its disk's transport."""
    document = json.loads(text)
    found: list[Device] = []

    def walk(nodes: Sequence[Mapping[str, object]], transport: str) -> None:
        for node in nodes:
            mounts = node.get("mountpoints")
            tran = _text(node, "tran") or transport
            found.append(
                Device(
                    name=_text(node, "name"),
                    path=_text(node, "path"),
                    type=_text(node, "type"),
                    uuid=_text(node, "uuid"),
                    partuuid=_text(node, "partuuid"),
                    label=_text(node, "label"),
                    partlabel=_text(node, "partlabel"),
                    parttype=_text(node, "parttypename"),
                    fstype=_text(node, "fstype"),
                    mountpoints=tuple(m for m in mounts if isinstance(m, str)) if isinstance(mounts, list) else (),
                    size=_text(node, "size"),
                    transport=tran,
                    hotplug=node.get("hotplug") is True,
                    parent=_text(node, "pkname"),
                )
            )
            children = node.get("children")
            if isinstance(children, list):
                walk([child for child in children if isinstance(child, dict)], tran)

    blockdevices = document.get("blockdevices") if isinstance(document, dict) else None
    if not isinstance(blockdevices, list):
        raise ValueError("lsblk printed no 'blockdevices' list")
    walk([node for node in blockdevices if isinstance(node, dict)], "")
    return found


def _devices() -> list[Device] | None:
    """Every block device with its filesystem facts, or ``None`` when ``lsblk`` cannot be read."""
    result = system().process.query(list(LSBLK), timeout=30.0)
    if not result.ok:
        return None
    try:
        return parse_lsblk(result.stdout)
    except (ValueError, TypeError):
        return None


def _device_id(device: Device) -> str:
    if device.uuid:
        return f"disk:uuid:{device.uuid}"
    if device.label:
        return f"disk:label:{device.label}"
    return f"disk:dev:{device.path}"


def _system_mount(device: Device) -> str | None:
    for mountpoint in device.mountpoints:
        if mountpoint in SYSTEM_MOUNTS or mountpoint.startswith("/snap/"):
            return mountpoint
    return None


def _kind(device: Device) -> str:
    if device.type == "loop":
        return "loop"
    if device.fstype == "swap":
        return "swap"
    if _system_mount(device) is not None:
        return "system"
    if device.parttype == "EFI System":
        return "efi"
    return "filesystem"


def _state(device: Device) -> str:
    mounted = [m for m in device.mountpoints if m]
    return "mounted at " + ", ".join(mounted) if mounted else "not mounted"


def _fstab_lines() -> list[tuple[int, list[str]]]:
    """The unmanaged ``/etc/fstab`` lines as ``(line number, fields)`` with the source and mount point unescaped."""
    try:
        text = (system().root / "etc/fstab").read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for number, line in lib.unmanaged_lines(text):
        fields = line.split()
        if len(fields) >= 2:
            found.append((number, [lib.fstab_unescape(fields[0]), lib.fstab_unescape(fields[1]), *fields[2:]]))
    return found


def _udev_decode(text: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda found: chr(int(found.group(1), 16)), text)


def _named_by(source: str, devices: Sequence[Device]) -> Device | None:
    """The device an fstab source (``UUID=``, ``LABEL=``, ``PARTUUID=``, a ``/dev/disk/by-*`` link or ``/dev/...``) names."""
    scheme, separator, value = source.partition("=")
    if separator and scheme in ("UUID", "LABEL", "PARTUUID"):
        key, value = scheme.lower(), value.strip("\"'")
    elif source.startswith("/dev/disk/"):
        link, _, value = source.removeprefix("/dev/disk/").partition("/")
        key = {"by-uuid": "uuid", "by-label": "label", "by-partuuid": "partuuid"}.get(link)
        if key is None:
            return None
        value = _udev_decode(value) if key == "label" else value
    else:
        return next((device for device in devices if device.path == source), None)
    return next((device for device in devices if value and getattr(device, key).lower() == value.lower()), None)


def _fstab_kind(fields: Sequence[str]) -> str:
    source, target = fields[0], fields[1]
    fstype = fields[2] if len(fields) > 2 else ""
    if fstype == "swap":
        return "swap"
    if fstype in PSEUDO_FSTYPES:
        return "pseudo"
    if target in SYSTEM_MOUNTS or target.startswith("/snap/"):
        return "system"
    if source.startswith("/dev/") or "=" in source:
        return "filesystem"
    return "network"


def discover(ctx: Context) -> Iterable[Item]:
    devices = _devices()
    if (
        devices is None
    ):  # no lsblk (or unreadable output): nothing to scan, like a machine without systemd for ``units``
        return
    lines = _fstab_lines()
    listed: dict[str, int] = {}
    unresolved = []
    for number, fields in lines:
        device = _named_by(fields[0], devices)
        if device is None:
            unresolved.append((number, fields))
        else:
            listed.setdefault(device.path, number)
    loops = [device for device in devices if device.type == "loop" and device.fstype]
    if loops:
        yield Item(NAME, "disk:loops", "loop", f"{len(loops)} loop devices with a filesystem (snap images)")
    for device in devices:
        if not device.fstype or device.type == "loop":
            continue
        facts = [device.fstype, device.size]
        if device.label:
            facts.append(f"label '{device.label}'")
        facts += [f"on {device.name}", _state(device)]
        if device.hotplug or device.transport == "usb":
            facts.append("removable")
        if device.path in listed:
            facts.append(f"/etc/fstab line {listed[device.path]}")
        yield Item(NAME, _device_id(device), _kind(device), ", ".join(part for part in facts if part))
    for number, fields in unresolved:
        key = fields[1] if fields[1] != "none" else fields[0]
        fstype = fields[2] if len(fields) > 2 else "?"
        yield Item(
            NAME,
            f"disk:fstab:{key}",
            _fstab_kind(fields),
            f"{fstype} {fields[0]} -> {fields[1]}, /etc/fstab line {number}",
        )


def classify(ctx: Context, item: Item, rules: Rules) -> Finding | None:
    """The system's own filesystems, loop images, swap, pseudo filesystems and other disks' EFI partitions are excluded
    with a reason; a rule of the module, profile or machine overlay keeps the last word."""
    rule = rules.classify(item)
    if rule is not None and rule.layer > Layer.ENGINE:
        return None
    if item.kind == "system":
        return Finding(item, Coverage.EXCLUDED, f"the system's own filesystem ({item.detail})")
    reason = REASONS.get(item.kind)
    return Finding(item, Coverage.EXCLUDED, reason) if reason is not None else None


def _declared(ctx: Context) -> list[tuple[str, str, str]]:
    """``(declaration name, selector, value)`` of every ``[modules.disks.mounts.*]`` table that names its device."""
    mounts = ctx.profile.load_config().table("modules", "disks", "mounts")
    found = []
    for name in sorted(mounts):
        table = mounts[name]
        if not isinstance(table, dict):
            continue
        for selector in lib.SELECTORS:
            value = table.get(selector)
            if isinstance(value, str) and value:
                found.append((name, selector, value))
                break
    return found


def owned(ctx: Context) -> Mapping[str, str]:
    declared = _declared(ctx)
    if not declared:
        return {}
    devices = _devices() or []
    found: dict[str, str] = {}
    for name, selector, value in declared:
        for device in devices:
            if device.fstype and getattr(device, selector).lower() == value.lower():
                found.setdefault(_device_id(device), f"[modules.disks.mounts.{name}]")
    return found


def _slug(device: Device, taken: set[str]) -> str:
    base = (
        _NAME.sub("-", device.label.lower()).strip("-.")
        or f"{device.fstype}-{(device.uuid or device.partuuid or device.name)[:8].lower()}"
    )
    base = _NAME.sub("-", base).strip("-.") or "disk"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    taken.add(name)
    return name


def _is_windows(device: Device, devices: Sequence[Device]) -> bool:
    if device.fstype != "ntfs":
        return False
    if device.label.lower().startswith("windows"):
        return True
    return any(
        other.parent == device.parent and other.parttype == _MICROSOFT_RESERVED for other in devices if device.parent
    )


def _selector(device: Device) -> tuple[str, str]:
    if device.uuid:
        return "uuid", device.uuid
    if device.partuuid:
        return "partuuid", device.partuuid
    return "label", device.label


def _declaration(device: Device, name: str, windows: bool) -> str:
    selector, value = _selector(device)
    where = f"{device.size} on {device.name}" + (", USB" if device.transport == "usb" else "")
    description = f"{device.label} ({device.fstype}, {where})" if device.label else f"{device.fstype} ({where})"
    body = [f"[modules.disks.mounts.{name}]", f'{selector} = "{value}"', f'fstype = "{device.fstype}"']
    if lib.FILESYSTEMS[device.fstype].read_only_default:
        body.append("read_only = true  # another OS owns this filesystem: read-only unless you decide otherwise")
    body.append(f'description = "{description}"')
    if not windows:
        return "\n".join(body)
    note = (
        f"# {description}: a proposal only, nothing is enabled by writing it. Windows Fast Startup and hibernation leave\n"
        "# NTFS marked dirty, so this stays read-only and is never mounted by force. Uncomment it to mount the volume,\n"
        "# then `ubuntu-tweak apply --commit`."
    )
    return note + "\n" + "\n".join(f"# {line}" for line in body)


def capture(ctx: Context, items: Sequence[Item]) -> Iterable[Proposal]:
    wanted = {item.id: item for item in items if item.kind == "filesystem"}
    devices = _devices() or []
    taken = {name for name, _, _ in _declared(ctx)}
    blocks: list[str] = []
    members: list[Item] = []
    for device in devices:
        item = wanted.get(_device_id(device))
        if item is None or device.fstype not in lib.FILESYSTEMS:
            continue
        blocks.append(_declaration(device, _slug(device, taken), _is_windows(device, devices)))
        members.append(item)
    if members:
        title = Path("machines") / ctx.profile.machine / "machine.toml"
        yield Proposal(title.as_posix(), "\n\n".join(blocks) + "\n", tuple(members))

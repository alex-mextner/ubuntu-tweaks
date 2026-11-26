"""A stateful fake machine for the tailnet-mounts tests: tailscale, ssh to peers, systemctl, findmnt, dpkg, apt-get.

It answers ``query(argv, timeout=...)`` and ``execute(argv, input_text=...)`` the way ``ubuntu_tweaks.process.Process``
does, records every command in ``log`` and keeps the little state the module reads back (which units are
enabled and active, which sshfs mounts exist, which packages are installed).
"""

import json
from pathlib import Path

from ubuntu_tweaks.process import CommandResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MUTATING = ("daemon-reload", "enable", "disable", "stop", "start")


def status_text(online=None, drop=(), backend="Running"):
    """The recorded `tailscale status --json` of the real tailnet, with ``online`` overriding Online per host."""
    data = json.loads((FIXTURES / "tailscale-status.json").read_text())
    data["BackendState"] = backend
    for key in list(data["Peer"]):
        peer = data["Peer"][key]
        if peer["HostName"] in drop:
            del data["Peer"][key]
        elif online and peer["HostName"] in online:
            peer["Online"] = online[peer["HostName"]]
    return json.dumps(data)


def remote_output(name):
    return (FIXTURES / f"{name}.txt").read_text()


class FakeHost:
    def __init__(self, *, status=None, remotes=None, packages=("sshfs",)):
        self.status = status if status is not None else status_text()
        self.remotes = (
            remotes
            if remotes is not None
            else {
                "odroidn2": remote_output("odroidn2"),
                "ubuntu-pc": remote_output("ubuntu-pc"),
                "clvm": '{"blockdevices": []}\nHOME=/home/ultra\n',
                "bananapi-m4": '{"blockdevices": []}\nHOME=/home/pi\n',
            }
        )
        self.ssh_errors = {"ubuntu-hyper": "ultra@ubuntu-hyper: Permission denied (publickey)."}
        self.packages = set(packages)
        self.enabled = set()
        self.active = set()
        self.mounted = set()  # mount points with an sshfs filesystem
        self.hung = set()  # mount points whose stat never returns
        self.log = []
        self.sync_result = "success"
        self.tailscale_down = False

    # -- the Process seam -----------------------------------------------------------------------

    def query(self, argv, *, timeout=60.0, cwd=None):
        self.log.append(list(argv))
        code, out, err = self._answer(list(argv))
        return CommandResult(code, out, err)

    def execute(self, argv, *, input_text=None, cwd=None):
        self.log.append(list(argv))
        if argv[0] == "apt-get" and argv[1] == "install":
            self.packages.update(word for word in argv[2:] if not word.startswith("-"))
            return
        code, out, err = self._answer(list(argv))
        if code != 0:
            raise AssertionError(f"{argv} failed: {err}")

    # -- commands ---------------------------------------------------------------------------------

    def _answer(self, argv):
        program = argv[0]
        handler = getattr(self, "_" + program.replace("-", "_"), None)
        if handler is None:
            return 127, "", f"{program}: not faked"
        return handler(argv[1:])

    def _tailscale(self, args):
        if self.tailscale_down:
            return 1, "", "failed to connect to local tailscaled"
        return 0, self.status, ""

    def _ssh(self, args):
        target = next(word for word in args if "@" in word and not word.startswith("-"))
        address = target.split("@", 1)[1]
        if address in self.ssh_errors:
            return 255, "", self.ssh_errors[address]
        if address not in self.remotes:
            return 255, "", f"ssh: connect to host {address} port 22: Connection timed out"
        return 0, self.remotes[address], ""

    def _dpkg_query(self, args):
        package = args[-1]
        return (0, "install ok installed", "") if package in self.packages else (1, "", "no packages found")

    def _systemctl(self, args):
        verb, rest = args[0], args[1:]
        if verb == "daemon-reload":
            return 0, "", ""
        if verb == "enable":
            units = [unit for unit in rest if not unit.startswith("-")]
            self.enabled.update(units)
            if "--now" in rest:
                self.active.update(units)
            return 0, "", ""
        if verb in ("disable", "stop"):
            for unit in rest:
                self.active.discard(unit)
                if verb == "disable":
                    self.enabled.discard(unit)
            return 0, "", ""
        if verb in ("is-active", "is-enabled"):
            pool = self.active if verb == "is-active" else self.enabled
            states = [unit in pool for unit in rest]
            word = ("active", "inactive") if verb == "is-active" else ("enabled", "disabled")
            return (0 if all(states) else 3), "\n".join(word[0] if state else word[1] for state in states) + "\n", ""
        if verb == "show":
            return 0, self.sync_result + "\n", ""
        return 1, "", f"unfaked systemctl {verb}"

    def _findmnt(self, args):
        if "-M" in args:
            point = args[args.index("-M") + 1]
            return (0, "fuse.sshfs\n", "") if point in self.mounted else (1, "", "")
        return 0, "".join(f"{point}\n" for point in sorted(self.mounted)), ""

    def _timeout(self, args):
        point = args[-1]
        return (124, "", "") if point in self.hung else (0, "1\n", "")

    def _fusermount3(self, args):
        self.mounted.discard(args[-1])
        return 0, "", ""

    # -- helpers for tests ------------------------------------------------------------------------

    def mutations(self):
        return [
            argv
            for argv in self.log
            if argv[0] == "fusermount3" or (argv[0] == "systemctl" and argv[1] in MUTATING)
        ]

    def ssh_calls(self):
        return [argv for argv in self.log if argv[0] == "ssh"]


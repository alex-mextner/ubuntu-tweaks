#!/usr/bin/env python3
"""Run a system module's root installer inside a disposable docker container (real root, no host system paths).

    scripts/root-container-check.sh MODULE [--image IMG] [--apt PKG]... [--stub CMD]... [--show FILE]...
                                    [--installer NAME] [--install-args WORDS] [--check-args WORDS] [--user NAME]
                                    [--no-network] [--print-command]

Steps inside the container, each with its exit code printed as ``exit=N``: install the ``--apt`` packages,
create the unprivileged ``--user`` (default ``tester``, also exported as SUDO_USER), put a stub for every
``--stub`` command first on PATH (a container has no systemd: ``--stub systemctl`` logs the call and answers from enable/start markers),
run ``modules/MODULE/<installer> <install-args>`` as root twice (the second run must change nothing: no ``--show``
file differs and nothing under /etc, /usr/local, /opt, /srv is newer than the start of the second run), run
``<installer> <check-args>`` as the unprivileged user (defaults: no install argument, ``--check``; a ``module_script``
program takes ``--installer NAME --install-args install --check-args check``), then ``ls -l`` and ``sha256sum`` every ``--show`` file. The repository is mounted read-only
at /repo; no other host path is mounted; the container is ``--rm`` and never ``--privileged``. Exit status: the
first install's, else the second install's, else 4 when the second run changed something, else the check's. ``--print-command`` prints the docker command instead of running it.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_IMAGE = "ubuntu:26.04"
DEFAULT_USER = "tester"
STUB_DIR = "/stub"
ALWAYS_APT = ("python3",)


# A stateful systemctl: ``enable``/``start`` leave a marker per unit and ``is-enabled``/``is-active`` answer from it,
# so an installer that forgets to enable its unit fails its own check instead of being told it is enabled already.
SYSTEMCTL_STATE = [
    'state="${STUB_STATE:-/tmp/stub-state}"; mkdir -p "$state"',
    'cmd=""; units=""',
    'for a in "$@"; do case "$a" in -*) ;; *) if [ -z "$cmd" ]; then cmd=$a; else units="$units $a"; fi;; esac; done',
    'for unit in $units; do case "$cmd" in',
    '  enable) touch "$state/$unit.enabled"; case " $* " in *" --now "*) touch "$state/$unit.active";; esac;;',
    '  start|restart|reload-or-restart) touch "$state/$unit.active";;',
    '  disable) rm -f "$state/$unit.enabled"; case " $* " in *" --now "*) rm -f "$state/$unit.active";; esac;;',
    '  stop) rm -f "$state/$unit.active";;',
    '  is-enabled) [ -e "$state/$unit.enabled" ] && echo enabled || { echo disabled; exit 1; };;',
    '  is-active) [ -e "$state/$unit.active" ] && echo active || { echo inactive; exit 3; };;',
    "esac; done",
]

STUB_NAME = re.compile(r"[A-Za-z0-9._+-]+")


def stub_lines(commands: list[str], stub_dir: str = STUB_DIR) -> list[str]:
    """Shell lines creating a logging stub for each command (a container has no systemd or kernel to talk to)."""
    if not commands:
        return []
    lines = [f"mkdir -p {shlex.quote(stub_dir)}"]
    for command in commands:
        if not STUB_NAME.fullmatch(command):
            raise ValueError(f"--stub takes a bare command name, not {command!r}")
        body = ["#!/bin/sh", f'echo {shlex.quote(f"[stub {command}] ")}"$*" >&2']
        if command == "systemctl":
            body += SYSTEMCTL_STATE
        body.append("exit 0")
        path = shlex.quote(f"{stub_dir}/{command}")
        lines += [f"printf '%s\\n' {' '.join(shlex.quote(line) for line in body)} > {path}", f"chmod +x {path}"]
    return [*lines, f"export PATH={shlex.quote(stub_dir)}:$PATH"]


def inner_script(
    module: str,
    *,
    installer: str,
    user: str,
    apt: list[str],
    stubs: list[str],
    show: list[str],
    network: bool = True,
    install_args: list[str] | None = None,
    check_args: list[str] | None = None,
) -> str:
    if "/" in module or module in {"", ".", ".."}:
        raise ValueError(f"module is a directory name under modules/, not {module!r}")
    if user == "root":
        raise ValueError("--user must be unprivileged: the check would run as root")
    if apt and not network:
        raise ValueError("--apt needs the network; drop --no-network or use an --image that already has the packages")
    packages = " ".join(shlex.quote(p) for p in dict.fromkeys([*ALWAYS_APT, *apt]))
    target = f"./{shlex.quote(installer)}"
    install_cmd = " ".join([target, *(shlex.quote(a) for a in install_args or [])])
    check_cmd = " ".join([target, *(shlex.quote(a) for a in (["--check"] if check_args is None else check_args))])
    digest = f'"$(sha256sum -- {" ".join(shlex.quote(f) for f in show)} 2>&1)"' if show else '""'
    lines = ["set -u", "export DEBIAN_FRONTEND=noninteractive"]
    if network:
        install = f"apt-get update -qq >/dev/null && apt-get install -y -qq {packages} >/dev/null"
        lines.append(f"{install} || {{ echo apt failed; exit 1; }}")
    lines += [
        f"useradd -m {shlex.quote(user)} || {{ echo useradd failed; exit 1; }}",
        *stub_lines(stubs),
        f"export SUDO_USER={shlex.quote(user)}",
        f"cd /repo/modules/{shlex.quote(module)} || exit 2",
        'echo "== root install #1"',
        f'{install_cmd}; first=$?; echo "exit=$first"',
        'echo "== root install #2 (must change nothing)"',
        f"mark=$(mktemp) || exit 1; before={digest}; sleep 1",
        f'{install_cmd}; second=$?; echo "exit=$second"',
        'changed=$(find /etc /usr/local /opt /srv -xdev -newer "$mark" 2>/dev/null)',
        f'[ "$before" = {digest} ] || changed="$changed show-files"',
        'noop=0; [ -e "$mark" ] || changed="$changed no-marker"; [ -z "$changed" ] || { echo "second install changed:$changed"; noop=4; }',
        f"echo {shlex.quote(f'== --check as unprivileged {user}')}",
        f'runuser -u {shlex.quote(user)} -- {check_cmd}; checked=$?; echo "exit=$checked"',
    ]
    for file in show:
        quoted = shlex.quote(file)
        lines += [f"echo {shlex.quote(f'== {file}')}", f"ls -l -- {quoted}; sha256sum -- {quoted}"]
    lines.append('[ "$first" = 0 ] || exit "$first"')
    lines.append('[ "$second" = 0 ] || exit "$second"')
    lines.append('[ "$noop" = 0 ] || exit "$noop"')
    lines.append('exit "$checked"')
    return "\n".join(lines)


def docker_command(repo: Path, image: str, script: str, *, network: bool) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--security-opt", "no-new-privileges",
        "--memory", "2g", "--pids-limit", "512",
        *([] if network else ["--network", "none"]),
        "-v", f"{repo}:/repo:ro",
        image, "bash", "-c", script,
    ]  # fmt: skip


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="root-container-check.sh", description=__doc__.split("\n\n")[0])
    parser.add_argument("module")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--installer", default="install.sh")
    parser.add_argument("--install-args", default="")
    parser.add_argument("--check-args", default="--check")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--apt", action="append", default=[])
    parser.add_argument("--stub", action="append", default=[])
    parser.add_argument("--show", action="append", default=[])
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--print-command", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse(argv)
    repo = Path(__file__).resolve().parents[1]
    if not (repo / "modules" / args.module / args.installer).is_file():
        sys.stderr.write(f"root-container-check: modules/{args.module}/{args.installer} not found\n")
        return 2
    if args.no_network and args.image == DEFAULT_IMAGE:
        sys.stderr.write(f"root-container-check: --no-network needs an --image that already has {ALWAYS_APT[0]}\n")
        return 2
    try:
        script = inner_script(
            args.module,
            installer=args.installer,
            user=args.user,
            apt=args.apt,
            stubs=args.stub,
            show=args.show,
            network=not args.no_network,
            install_args=shlex.split(args.install_args),
            check_args=shlex.split(args.check_args),
        )
    except ValueError as error:
        sys.stderr.write(f"root-container-check: {error}\n")
        return 2
    command = docker_command(repo, args.image, script, network=not args.no_network)
    if args.print_command:
        sys.stdout.write(shlex.join(command) + "\n")
        return 0
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

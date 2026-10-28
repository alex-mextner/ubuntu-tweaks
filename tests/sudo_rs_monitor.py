"""A sandbox model of sudo-rs 0.2.13's pty monitor, the cause of the frozen ``finish-setup`` (ticket #126).

Run as ``python tests/sudo_rs_monitor.py <stdin: tty|devnull>``; prints one JSON object. Never runs sudo or apt.

Why a model: Ubuntu 26.04's ``sudo`` is sudo-rs. When its stdout is a pipe (``process.run_streamed`` always gives
children pipes) it starts the command in the *background* of its own pty. A grandchild that changes the pty's settings
(``apt-get`` does, at the end of an install: ``tcsetattr(TCSAFLUSH)`` in ``StopPtyMagic``) is sent SIGTTOU together
with its whole group; the monitor sees only its direct child stop, grants the foreground and resumes *that pid*
(``kill(command_pid, SIGCONT)``, "FIXME: We should call killpg"), so the grandchild stays stopped for ever. A child
whose stdin is not a terminal never touches the pty's settings and cannot be stopped this way.

Layout, as seen with ``ps`` on the machine that froze: monitor (session leader of a fresh pty) -> command (own process
group, the module script) -> grandchild (same group, ``apt-get``).
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import signal
import sys
import tempfile
import termios
import time

GRANDCHILD = r"""
import sys, termios, time
time.sleep(0.3)  # dpkg running for a while: the monitor has settled in the background state
if sys.stdin.isatty():
    termios.tcsetattr(0, termios.TCSAFLUSH, termios.tcgetattr(0))  # apt's StopPtyMagic: SIGTTOU from the background
open(sys.argv[1], "w").write("finished\n")
"""

COMMAND = r"""
import os, subprocess, sys
stdin = None if sys.argv[2] == "tty" else subprocess.DEVNULL
grandchild = subprocess.Popen([sys.executable, "-c", sys.argv[3], sys.argv[1]], stdin=stdin)
open(sys.argv[1] + ".pid", "w").write(str(grandchild.pid))
grandchild.wait()
"""


def state_of(pid: int) -> str:
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return "gone"
    return stat[stat.rindex(")") + 2 :].split()[0]


def main(stdin_kind: str) -> dict[str, object]:
    marker = os.path.join(tempfile.mkdtemp(prefix="sudo-rs-model-"), "marker")
    leader, follower = pty.openpty()
    reader, writer = os.pipe()
    monitor = os.fork()
    if monitor == 0:
        os.close(leader)
        os.close(reader)
        os.setsid()
        fcntl.ioctl(follower, termios.TIOCSCTTY, 0)
        monitor_group = os.getpgrp()
        command = os.fork()
        if command == 0:
            os.setpgid(0, 0)
            os.dup2(follower, 0)
            os.execvp(sys.executable, [sys.executable, "-c", COMMAND, marker, stdin_kind, GRANDCHILD])
        os.setpgid(command, command)
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(follower, monitor_group)  # the command starts in the background: the monitor keeps the tty
        bounces = 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            finished, status = os.waitpid(command, os.WUNTRACED | os.WNOHANG)
            if finished == 0:
                time.sleep(0.02)
            elif os.WIFSTOPPED(status):
                bounces += 1
                os.tcsetpgrp(follower, command)  # sudo-rs: SIGCONT_FG ...
                os.kill(command, signal.SIGCONT)  # ... resumes the command's pid only
            else:
                break
        time.sleep(0.4)
        grandchild = int(open(marker + ".pid").read())
        report = {
            "bounces": bounces,
            "command_state": state_of(command),
            "grandchild_state": state_of(grandchild),
            "grandchild_finished": os.path.exists(marker),
        }
        if report["grandchild_state"] == "T":
            os.kill(grandchild, signal.SIGKILL)  # the sandbox's own stopped child: never leave it behind
        os.kill(command, signal.SIGKILL) if state_of(command) not in ("gone", "Z") else None
        os.write(writer, json.dumps(report).encode())
        os._exit(0)
    os.close(follower)
    os.close(writer)
    answer = os.read(reader, 4096)
    os.waitpid(monitor, 0)
    return json.loads(answer)


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1])))

"""Shared fixtures of the error-intercept tests: a synthetic apport report, fake publishers, a temp home.

The report is modelled on the real one of this machine: a nested test shell (``gnome-shell --headless
--virtual-monitor``) that crashed with SIGSEGV in libmutter-mtk's ``mtk_anonymous_file_open_fd``, apport refusing
to report it (obsolete packages), the monitors.xml Errno 40 line in two places, and, around them, everything the
sanitizer must never pass on: a process environment with a token, the memory maps, a core dump.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
sys.dont_write_bytecode = True

from error_intercept.audit import Audit  # noqa: E402
from error_intercept.paths import Paths  # noqa: E402
from error_intercept.publisher import PublishError  # noqa: E402

FAKE_TOKEN = "ghp_" + "Zk3QxP9vLm2NwR7tYb5CdF8gHj1sAe4UoI6V"
BEARER = "Bearer " + "abcDEF0123456789abcDEF0123456789"
USER_HOME = "/home/ultra"
MONITORS_LINE = f"monitors.xml: Error: [Errno 40] Too many levels of symbolic links: '{USER_HOME}/.config/monitors.xml'"
NESTED_CMDLINE = "gnome-shell --headless --no-x11 --wayland-display=vtest-wl --virtual-monitor 1600x1000"
REAL_CMDLINE = "/usr/bin/gnome-shell"


def crash_text(
    *, cmdline=NESTED_CMDLINE, exe="/usr/bin/gnome-shell", signal="11", signal_name="SIGSEGV", top=None, extra=""
):
    top = top or (
        " mtk_anonymous_file_open_fd () at /usr/lib/x86_64-linux-gnu/mutter-18/libmutter-mtk-18.so.0\n"
        " ??? () at /usr/lib/x86_64-linux-gnu/libmutter-18.so.0\n"
        " ??? () at /usr/lib/x86_64-linux-gnu/libffi.so.8\n"
    )
    maps = "".join(
        f" 7f{i:010x}-7f{i + 1:010x} r-xp 00000000 08:01 {i} {USER_HOME}/.cache/lib{i}.so\n" for i in range(200)
    )
    core = " H4sIAAAAAAAAA" + "QUFBQUFBQUFBQUFB" * 14000 + "\n" + " QUJD\n"  # one 224 KB line, then more
    shell_journal = "".join(f" ноя 01 16:07:{i % 60:02d} host gnome-shell[4242]: message {i}\n" for i in range(400))
    return (
        "ProblemType: Crash\n"
        "Architecture: amd64\n"
        "Date: Sun Nov  1 16:08:56 2026\n"
        "Dependencies:\n libc6 2.43-0ubuntu1\n libmutter-18-0 18.0-1\n"
        "ExecutablePath: " + exe + "\n"
        "Package: gnome-shell 50.1-0ubuntu1.4\n"
        "PackageArchitecture: amd64\n"
        "ProcCmdline: " + cmdline + f" --token={FAKE_TOKEN}\n"
        "ProcEnviron:\n"
        f" HOME={USER_HOME}\n"
        f" GITHUB_TOKEN={FAKE_TOKEN}\n"
        " SECRET_KEY=hunter2hunter2\n"
        " LANG=ru_RU.UTF-8\n"
        "ProcMaps:\n" + maps + "ProcStatus:\n Name:\tgnome-shell\n Uid:\t1000\t1000\t1000\t1000\n"
        "Signal: " + signal + "\n"
        "SignalName: " + signal_name + "\n"
        "CoreDump: base64\n"
        + core
        + "Disassembly:\n => 0x794088f0ba1e <mtk_anonymous_file_open_fd+30>:\tmov 0x8(%rdi),%edi\n"
        "JournalErrors:\n"
        f" ноя 01 16:07:46 host gnome-shell[4242]: {MONITORS_LINE}\n"
        f" ноя 01 16:07:47 host gnome-shell[4242]: {MONITORS_LINE}\n"
        f" ноя 01 16:07:48 host app[1]: Authorization: {BEARER}\n"
        f" ноя 01 16:07:49 host app[1]: cannot open {USER_HOME}/.local/share/x.db\n"
        "Registers:\n rax 0x0 0\n"
        "SegvAnalysis:\n"
        " Segfault happened at: 0x794088f0ba1e <mtk_anonymous_file_open_fd+30>:\tmov    0x8(%rdi),%edi\n"
        " PC (0x794088f0ba1e) ok\n"
        ' source "0x8(%rdi)" (0x00000008) not located in a known VMA region (needed readable region)!\n'
        "SegvReason: reading NULL VMA\n"
        "ShellJournal:\n" + shell_journal + "StacktraceTop:\n" + top + "Tags: resolute wayland-session\n"
        "Title: gnome-shell crashed with SIGSEGV in mtk_anonymous_file_open_fd()\n"
        "UnreportableReason:\n"
        " You have some obsolete package versions installed. Please upgrade the following packages and check if the problem still occurs:\n"
        " \n"
        " gir1.2-mutter-18, libinsights0, libmutter-18-0, mutter-common, mutter-common-bin, rtkit\n"
        "_MarkForUpload: True\n" + MONITORS_LINE + "\n" + extra
    )


class FakePublish:
    """The publisher seam: records events, answers ``ev-N``."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)
        return f"ev-{len(self.events)}"


class FailingPublish:
    def __init__(self, message="agent-sub not found"):
        self.message = message
        self.calls = 0

    def __call__(self, event):
        self.calls += 1
        raise PublishError(self.message)


class Case(unittest.TestCase):
    """A temp HOME with state, config and crash directories; nothing outside it is touched."""

    now = 1_800_000_000.0

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ei-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.crash = self.root / "var-crash"
        self.home.mkdir()
        self.crash.mkdir()
        self.env = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "ERROR_INTERCEPT_CRASH_DIR": str(self.crash),
            "PATH": "/usr/bin:/bin",
        }
        self.paths = Paths.from_env(self.env, self.home)
        self.audit = Audit(self.paths.audit, lambda: self.now)

    def write_crash(self, name="_usr_bin_gnome-shell.1000.crash", text=None, age_s=60.0, seen=False, mode=0o640):
        path = self.crash / name
        path.write_text(text if text is not None else crash_text(), encoding="utf-8")
        path.chmod(mode)
        mtime = self.now - age_s
        os.utime(path, (mtime + 5 if seen else mtime - 5, mtime))  # apport: seen means atime > mtime
        return path

    def crash_files(self):
        """What is left in the crash directory: the reports, not the private ``.ei-claims`` directory."""
        return sorted(name for name in os.listdir(self.crash) if name != ".ei-claims")

    def clock(self):
        return self.now

    def audit_decisions(self):
        return [record.get("decision") for record in self.audit.records()]

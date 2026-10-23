"""The test package.

Pins ``ubuntu_tweaks.ui`` to "stdout is not a terminal, nothing in the environment asks for color or a
language" for every test: the renderers choose the explained or the compact layout by asking the stream, and a
developer running the suite in a terminal (or with ``CLICOLOR_FORCE`` / ``UBUNTU_TWEAK_LANG`` exported) must get
the same bytes as CI. ``UBUNTU_TWEAKS_DIR`` is pinned to the checkout under test, so a ``cli.main`` test that runs from
a git worktree never resolves to the developer's main checkout (``ubuntu_tweaks.checkout``; ``tests/test_checkout.py``
removes it where resolution is the subject). A test that wants a terminal passes a stream with ``fake_tty = True``
(``tests.test_ui.Tty``) or selects the layout with ``ui.configure(color="always")``.

Also points the session log of ``cli.main`` at a temporary directory (``UBUNTU_TWEAK_LOG_DIR``): every ``main`` call
writes a log, and none may land in the developer's real ``~/.local/state``.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import TextIO

from ubuntu_tweaks import ui


def _only_fake_ttys(stream: TextIO) -> bool:
    return bool(getattr(stream, "fake_tty", False))


ui._is_tty = _only_fake_ttys
for _name in ("CLICOLOR_FORCE", "NO_COLOR", "UBUNTU_TWEAK_LANG"):
    os.environ.pop(_name, None)
os.environ["UBUNTU_TWEAKS_DIR"] = str(Path(__file__).resolve().parent.parent)

_LOG_DIR = tempfile.mkdtemp(prefix="ubuntu-tweak-test-logs-")
os.environ["UBUNTU_TWEAK_LOG_DIR"] = _LOG_DIR
os.environ.pop("UBUNTU_TWEAK_SESSION_LOG", None)
atexit.register(shutil.rmtree, _LOG_DIR, ignore_errors=True)

# Nothing a test does by accident may reach the developer's machine: the run journal and every other state file
# go to a temporary directory, and an accidental Gio write lands in GSettings' in-memory backend instead of the
# live dconf database. The scratch-dconf scenarios (test_settings, test_planner_gio) run their children under
# their own dbus-run-session, HOME and GSETTINGS_BACKEND=dconf (UBUNTU_TWEAKS_SCRATCH=1): those keep them.
if os.environ.get("UBUNTU_TWEAKS_SCRATCH") != "1":
    _STATE_DIR = tempfile.mkdtemp(prefix="ubuntu-tweak-test-state-")
    os.environ["XDG_STATE_HOME"] = _STATE_DIR
    os.environ["GSETTINGS_BACKEND"] = "memory"
    atexit.register(shutil.rmtree, _STATE_DIR, ignore_errors=True)

# No test may ask the developer's running GNOME Shell anything (`gnome-extensions list` goes over the live bus).
os.environ["UBUNTU_TWEAK_SHELL_LOADED"] = "1"

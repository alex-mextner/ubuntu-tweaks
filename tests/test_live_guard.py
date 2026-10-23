"""Ticket #71: no test may reach the developer's live dconf database or state directory."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

try:
    from gi.repository import Gio, GLib

    from ubuntu_tweaks.settings import GioBackend
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

KEY = "/org/gnome/shell/enabled-extensions"


def live_value() -> str | None:
    """The live user's value, read with the real dconf CLI against the developer's own profile (read-only)."""
    if shutil.which("dconf") is None:
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("GSETTINGS_BACKEND", "XDG_STATE_HOME")}
    done = subprocess.run(["dconf", "read", KEY], capture_output=True, text=True, env=env, check=False)
    return done.stdout if done.returncode == 0 else None


class LiveGuardTests(unittest.TestCase):
    def test_the_default_gio_backend_of_the_test_run_is_the_in_memory_one(self) -> None:
        self.assertEqual(os.environ.get("GSETTINGS_BACKEND"), "memory")
        self.assertIn("Memory", type(Gio.SettingsBackend.get_default()).__name__)

    def test_the_developers_session_bus_is_not_reachable_from_the_test_process(self) -> None:
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", os.environ)

    def test_the_state_directory_of_the_test_run_is_temporary(self) -> None:
        state = Path(os.environ["XDG_STATE_HOME"])
        self.assertNotEqual(state, Path.home() / ".local/state")
        self.assertTrue(str(state).startswith(("/tmp/", os.environ.get("TMPDIR", "/tmp"))))

    def test_a_real_gio_write_leaves_the_live_database_alone(self) -> None:
        schemas = Gio.SettingsSchemaSource.get_default()
        if schemas is None or schemas.lookup("org.gnome.shell", True) is None:
            self.skipTest("org.gnome.shell schema is not installed")
        before = live_value()
        backend = GioBackend()
        backend.write("org.gnome.shell", None, "enabled-extensions", GLib.Variant.new_strv(["fixture@test"]))
        self.assertEqual(live_value(), before)


if __name__ == "__main__":
    unittest.main()

"""The settings planner against the real GioBackend, in a scratch dbus session (never the live dconf).
Needs PyGObject; skipped without it or without dbus-run-session, glib-compile-schemas and dconf."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.settings import GioBackend

SCHEMA_XML = """<schemalist>
  <schema id="test.ubuntu.tweaks.ext" path="/test/ubuntu/tweaks/ext/">
    <key name="size" type="d"><default>0.5</default></key>
  </schema>
</schemalist>
"""
DEFAULTS = "[org/gnome/desktop/interface]\nclock-format='12h'\n"


def find_tool(name: str) -> str | None:
    """Prefer the distro binary: a brew dbus-daemon cannot activate the distro's dconf service."""
    return shutil.which(name, path="/usr/bin:/bin") or shutil.which(name)


class GioPlannerTests(unittest.TestCase):
    def test_planner_against_gio_backend_in_a_scratch_session(self) -> None:
        tools = {name: find_tool(name) for name in ("dbus-run-session", "glib-compile-schemas", "dconf")}
        if None in tools.values():
            self.skipTest("dbus-run-session, glib-compile-schemas and dconf are required")
        system = GioBackend()
        for schema in ("org.gnome.desktop.interface", "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"):
            if not system.has_schema(schema):
                self.skipTest(f"the system schema {schema} is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schemas = root / "repo/gnome-extensions/ext@x/schemas"
            schemas.mkdir(parents=True)
            (schemas / "test.ubuntu.tweaks.ext.gschema.xml").write_text(SCHEMA_XML, encoding="utf-8")
            subprocess.run([tools["glib-compile-schemas"], str(schemas)], check=True, capture_output=True)

            keyfiles = root / "defaults.d"
            keyfiles.mkdir()
            (keyfiles / "00-defaults").write_text(DEFAULTS, encoding="utf-8")
            subprocess.run([tools["dconf"], "compile", str(root / "defaults.db"), str(keyfiles)], check=True)

            profile = root / "dconf-profile"
            profile.write_text(f"user-db:user\nfile-db:{root / 'defaults.db'}\n", encoding="utf-8")
            runtime = root / "run"
            runtime.mkdir(mode=0o700)
            env = {
                key: os.environ[key]
                for key in ("PATH", "LANG", "XDG_DATA_DIRS", "LD_LIBRARY_PATH", "GI_TYPELIB_PATH")
                if key in os.environ
            }
            env.update(
                HOME=str(root / "home"),
                XDG_CONFIG_HOME=str(root / "config"),
                XDG_DATA_HOME=str(root / "data"),
                XDG_CACHE_HOME=str(root / "cache"),
                XDG_STATE_HOME=str(root / "state"),
                XDG_RUNTIME_DIR=str(runtime),
                DCONF_PROFILE=str(profile),
                GSETTINGS_BACKEND="dconf",
                PYTHONPATH=str(REPO_ROOT),
                UBUNTU_TWEAKS_SCRATCH="1",
            )
            result = subprocess.run(
                [
                    tools["dbus-run-session"],
                    "--",
                    sys.executable,
                    str(REPO_ROOT / "tests/gio_planner_scenario.py"),
                    str(root),
                ],
                env=env,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )

        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()

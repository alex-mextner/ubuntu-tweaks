"""Scenario run by test_planner_gio inside ``dbus-run-session`` with a scratch ``DCONF_PROFILE``
(user db plus a read-only defaults db that plays the session override), temporary HOME and XDG_*.

Usage: ``gio_planner_scenario.py <root>`` where ``<root>/repo/gnome-extensions/ext@x/schemas`` holds
the compiled schema of the module's extension; exits 0 when every check holds.
"""

import os
import subprocess
import sys
from pathlib import Path

from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.planners import settings as planner
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import GioBackend

INTERFACE = "/org/gnome/desktop/interface/"
KEYBINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def dconf_read(path: str) -> str:
    return subprocess.run(["dconf", "read", path], capture_output=True, text=True, check=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def scratch_problem(root: Path) -> str | None:
    """Why this process must not touch dconf, or None when it is provably inside the scratch session.

    Everything dconf-related must be rooted in the temp ``root`` and the bus must be the private one
    ``dbus-run-session`` started: a bus-activated dconf-service inherits the *bus daemon's*
    environment, so a bus address equal to the parent's would reach the live user's dconf."""
    if os.environ.get("UBUNTU_TWEAKS_SCRATCH") != "1":
        return "UBUNTU_TWEAKS_SCRATCH is not set"
    resolved = os.path.realpath(root)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        value = os.environ.get(name, "")
        if not value or not os.path.realpath(value).startswith(resolved + os.sep):
            return f"{name}={value!r} is not under the scratch root {resolved}"
    profile = os.environ.get("DCONF_PROFILE", "")
    if not profile or not os.path.realpath(profile).startswith(resolved + os.sep):
        return f"DCONF_PROFILE={profile!r} is not under the scratch root"
    if os.environ.get("GSETTINGS_BACKEND") != "dconf":
        return "GSETTINGS_BACKEND is not dconf"
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not bus or bus == os.environ.get("UBUNTU_TWEAKS_PARENT_BUS"):
        return "the session bus is missing or is the parent's own bus"
    return None


def main(root: Path) -> int:
    problem = scratch_problem(root)
    if problem:
        print(f"refusing to run: {problem}")
        return 2
    repo, profile_root = root / "repo", root / "profile"
    extension = repo / "gnome-extensions/ext@x"
    write(extension / "metadata.json", '{"uuid": "ext@x", "settings-schema": "test.ubuntu.tweaks.ext"}')
    write(
        repo / "modules/ext/module.toml",
        'description = "x"\n[source]\nkind = "local"\npath = "gnome-extensions/ext@x"\n'
        '[[settings]]\nfile = "ext.ini"\n',
    )
    write(repo / "modules/ext/ext.ini", "[/]\nsize=0.75\n")
    write(
        profile_root / "settings/appearance.ini",
        "[org/gnome/desktop/interface]\n"
        "gtk-theme='Scratch-Theme'\n"
        "clock-format='12h'\n"  # the defaults db already holds this: effective == wanted
        "[org/nowhere/known]\nkey=1\n",
    )
    write(
        profile_root / "settings/keyboard.ini",
        "[org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0]\n"
        "name='Terminal'\nbinding='<Super>t'\ncommand='true'\n",
    )
    write(
        profile_root / "machines/home/settings/appearance.ini", "[org/gnome/desktop/interface]\ngtk-theme='Machine'\n"
    )

    backend = GioBackend([extension / "schemas"])
    context = Context(repo, Profile(profile_root, "home"))

    check(
        backend.effective("org.gnome.desktop.interface", None, "clock-format").unpack() == "12h",
        "the defaults db changes the effective value without a user value",
    )
    check(backend.user_value("org.gnome.desktop.interface", None, "clock-format") is None, "no user value yet")

    plan = Plan.build(planner.build(context, backend))
    keys = sorted(action.target.full for action in plan.actions)  # type: ignore[attr-defined]
    expected = sorted(
        [
            INTERFACE + "gtk-theme",
            KEYBINDING_PATH + "name",
            KEYBINDING_PATH + "binding",
            KEYBINDING_PATH + "command",
            "/test/ubuntu/tweaks/ext/size",
        ]
    )
    check(keys == expected, f"plan keys {keys} != {expected}")
    check(
        any("= 'Machine'" in action.describe() for action in plan.actions),
        "the machine overlay value wins over the profile panel file",
    )
    warnings = planner.warnings(context, backend)
    check(
        any("no installed schema at /org/nowhere/known/" in line for line in warnings), f"warning missing: {warnings}"
    )

    plan.execute()
    check(dconf_read(INTERFACE + "gtk-theme") == "'Machine'", "gtk-theme written")
    check(dconf_read(KEYBINDING_PATH + "binding") == "'<Super>t'", "relocatable key written at its path")
    check(dconf_read("/test/ubuntu/tweaks/ext/size") == "0.75", "extension schema key written")
    fresh = GioBackend([extension / "schemas"])
    check(
        fresh.user_value("org.gnome.desktop.interface", None, "clock-format") is None,
        "an override-provided value is never frozen into the dconf user db",
    )

    again = Plan.build(planner.build(context, fresh))
    check(len(again) == 0, f"second plan is not empty: {[a.describe() for a in again.actions]}")

    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <root>")
    sys.exit(main(Path(sys.argv[1])))

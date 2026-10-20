"""The refusal every test scenario that writes through Gio/dconf runs before touching anything.

Accessed via: ``gio_scenario.py`` and ``gio_planner_scenario.py`` (child processes started by
``test_settings`` / ``test_planner_gio`` under ``dbus-run-session``).

Why: a bus-activated dconf-service inherits the *bus daemon's* environment, so a scenario that
runs with the live user's bus (or HOME, or DCONF_PROFILE) writes the live user's dconf database.
The parent test therefore exports the scratch HOME/XDG_*/DCONF_PROFILE *before* starting
``dbus-run-session`` and passes its own bus address as ``UBUNTU_TWEAKS_PARENT_BUS``; the child calls
``scratch_problem`` first and exits 2 on any answer other than ``None``.
"""

import os
from pathlib import Path


def scratch_problem(root: Path) -> str | None:
    """Why this process must not touch dconf, or None when it is provably inside the scratch session."""
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

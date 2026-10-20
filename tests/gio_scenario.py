"""Scenario run by test_settings.GioBackendTests inside ``dbus-run-session`` with a scratch
``DCONF_PROFILE``, temporary HOME and temporary XDG_* (never the real user's dconf).

Usage: ``gio_scenario.py <schema-dir>``; exits 0 when every check holds, prints the failing
check and exits 1 otherwise.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from gi.repository import GLib

from ubuntu_tweaks.settings import GioBackend, SettingsError, is_satisfied, parse_value

FIXED = "test.ubuntu.tweaks"
ITEM = "test.ubuntu.tweaks.item"
FIXED_PATH = "/test/ubuntu/tweaks/"
ITEM_PATH = "/test/ubuntu/tweaks/items/one/"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def raises(function: Callable[..., object], *args: object) -> bool:
    try:
        function(*args)
    except SettingsError:
        return True
    return False


def main(schema_dir: Path) -> int:
    if os.environ.get("UBUNTU_TWEAKS_SCRATCH") != "1" or "DCONF_PROFILE" not in os.environ:
        print("refusing to write dconf outside a scratch session (see tests/test_settings.py)")
        return 2
    backend = GioBackend([schema_dir])
    check(backend.schema_dirs == (schema_dir,), "schema_dirs lists the extra directory")
    check(backend.has_schema(FIXED) and not backend.has_schema("no.such.schema"), "has_schema")
    check(backend.has_key(FIXED, "count") and not backend.has_key(FIXED, "nope"), "has_key")
    check(backend.key_type(FIXED, "count") == "u", "key_type")

    # Unset key: effective == default, no user value; a typed 5 equals the parsed text "5".
    check(backend.user_value(FIXED, None, "count") is None, "unset key has no user value")
    check(backend.effective(FIXED, None, "count").equal(backend.default(FIXED, None, "count")), "effective == default")
    wanted_three = parse_value(backend, FIXED, "count", "3")
    check(is_satisfied(backend, FIXED, None, "count", wanted_three), "default-valued key is satisfied")
    check(not GLib.Variant("i", 3).equal(wanted_three), "int32 3 is not uint32 3 (typed compare)")

    # Write, read back, drift check, reset.
    backend.write(FIXED, None, "count", parse_value(backend, FIXED, "count", "uint32 5"))
    check(backend.user_value(FIXED, None, "count").equal(GLib.Variant("u", 5)), "user value after write")
    check(is_satisfied(backend, FIXED, None, "count", parse_value(backend, FIXED, "count", "5")), "5 == uint32 5")
    check(not is_satisfied(backend, FIXED, None, "count", wanted_three), "changed key is not satisfied")
    backend.write(FIXED, FIXED_PATH, "names", parse_value(backend, FIXED, "names", "['x', 'y']"))
    check(backend.effective(FIXED, None, "names").unpack() == ["x", "y"], "list key round trip")

    # Wrong type and out-of-range values are refused, not stored.
    check(raises(backend.write, FIXED, None, "count", GLib.Variant("s", "5")), "wrong type refused")
    check(raises(backend.write, FIXED, None, "limited", GLib.Variant("i", 99)), "out of range refused")
    check(backend.user_value(FIXED, None, "limited") is None, "refused write stored nothing")
    check(raises(backend.write, FIXED, "/other/path/", "count", GLib.Variant("u", 1)), "fixed path mismatch refused")
    check(raises(backend.effective, "no.such.schema", None, "k"), "missing schema raises")
    check(raises(backend.effective, FIXED, None, "nope"), "missing key raises")

    # Relocatable schema through new_with_path semantics; the path is required.
    check(raises(backend.effective, ITEM, None, "name"), "relocatable schema needs a path")
    check(raises(backend.effective, ITEM, "/bad//path/", "name"), "malformed relocatable path refused")
    check(raises(backend.effective, ITEM, "no/slashes", "name"), "relative relocatable path refused")
    backend.write(ITEM, ITEM_PATH, "name", GLib.Variant("s", "one"))
    check(backend.effective(ITEM, ITEM_PATH, "name").unpack() == "one", "relocatable write at its path")
    check(backend.effective(ITEM, "/test/ubuntu/tweaks/items/two/", "name").unpack() == "x", "other path keeps default")

    listed = {key.full for key in backend.list_user_keys()}
    check(
        {FIXED_PATH + "count", FIXED_PATH + "names", ITEM_PATH + "name"} <= listed,
        f"list_user_keys has the written keys, got {sorted(listed)}",
    )

    backend.reset(FIXED, None, "count")
    check(backend.user_value(FIXED, None, "count") is None, "reset drops the user value")
    check(FIXED_PATH + "count" not in {key.full for key in backend.list_user_keys()}, "reset key is not listed")

    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <schema-dir>")
    sys.exit(main(Path(sys.argv[1])))

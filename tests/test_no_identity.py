"""The engine names nobody: no occurrence of the author's identifiers outside ``tests/identity_allowlist.txt``.

Every allowed file is listed with the reason it may keep its hits (the project's own repository URL, the
identifier of a tool the project integrates, a verbatim user request). An entry whose file no longer has a
hit is stale and fails too, so the list can only shrink or be argued for. ``LICENSE`` is exempt.
"""

import re
import subprocess
import unittest
from pathlib import Path

from ubuntu_tweaks.context import REPO_ROOT

ALLOWLIST = Path(__file__).with_name("identity_allowlist.txt")
EXEMPT = {"LICENSE", "tests/identity_allowlist.txt", "tests/test_no_identity.py"}
IDENTIFIERS = re.compile(r"alex-mextner|mextner|\bultra\b|hyperide|65\.21\.40\.51|ubuntu-pc|invntrm", re.IGNORECASE)


def tracked_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return sorted(name for name in listed.decode("utf-8").split("\0") if name and name not in EXEMPT)


def hits() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for name in tracked_files():
        path = REPO_ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary files (images) carry no identifiers
        lines = [number for number, line in enumerate(text.splitlines(), 1) if IDENTIFIERS.search(line)]
        if lines:
            found[name] = lines
    return found


def allowed() -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, separator, reason = line.partition(" | ")
        if not separator or not reason.strip():
            raise AssertionError(f"identity_allowlist.txt: {line!r} needs `path | reason`")
        entries[path.strip()] = reason.strip()
    return entries


class NoIdentityTests(unittest.TestCase):
    def test_no_new_hit_of_the_authors_identifiers(self) -> None:
        permitted = allowed()
        new = {name: lines for name, lines in hits().items() if name not in permitted}
        self.assertEqual(
            new,
            {},
            "the engine must not name its author: make the value a config option or a neutral fixture, or add the "
            "file to tests/identity_allowlist.txt with the reason it is a project/tool reference",
        )

    def test_no_stale_allowlist_entry(self) -> None:
        found = hits()
        stale = sorted(name for name in allowed() if name not in found)
        self.assertEqual(stale, [], "these allowlisted files no longer contain an identifier: remove the entries")

    def test_every_allowlist_entry_names_a_tracked_file(self) -> None:
        files = set(tracked_files())
        self.assertEqual(sorted(name for name in allowed() if name not in files), [])

    def test_the_scan_sees_identifiers(self) -> None:
        self.assertTrue(IDENTIFIERS.search("alex-mextner/.files"))
        self.assertTrue(IDENTIFIERS.search("/home/ultra/x"))
        self.assertFalse(IDENTIFIERS.search("cultural result"))

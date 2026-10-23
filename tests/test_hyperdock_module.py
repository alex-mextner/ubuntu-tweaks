"""The shipped ``modules/hyperdock`` manifest through the real planners.

The hyperdock extension's ``metadata.json`` names its schema with ``schema-id`` (not the standard
``settings-schema``), so the manifest has to declare the schema itself; without that the preset,
the seed and the migration's value copy are all silently skipped.
"""

import json
import shutil
import unittest

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.planners import migrations, seeds
from ubuntu_tweaks.planners import settings as settings_planner
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema, parse_keyfile

from gi.repository import GLib  # noqa: E402  (ubuntu_tweaks.settings has pinned the GLib typelib version)

SCHEMA = "org.gnome.shell.extensions.dash2dock-lite"
DIRECTORY = "/org/gnome/shell/extensions/dash2dock-lite/"
OLD_UUID = "dash2dock-lite@icedman.github.com"


def key_type(text: str) -> str:
    return GLib.Variant.parse(None, text).get_type_string()


class HyperdockModuleTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        shutil.copytree(REPO_ROOT / "modules/hyperdock", self.repo / "modules/hyperdock")
        write(
            self.repo / "gnome-extensions/hyperdock/metadata.json",
            json.dumps({"uuid": "hyperdock@example", "schema-id": SCHEMA}),
        )
        self.preset = parse_keyfile((REPO_ROOT / "modules/hyperdock/dock.ini").read_text(encoding="utf-8"))["/"]
        keys = {name: FakeKey(key_type(text), text) for name, text in self.preset.items()}
        keys["folder-stacks"] = FakeKey("s", "''")
        self.backend = FakeBackend(
            [
                FakeSchema(SCHEMA, keys, path=DIRECTORY),
                FakeSchema(
                    "org.gnome.shell", {"enabled-extensions": FakeKey("as", "@as []")}, path="/org/gnome/shell/"
                ),
            ]
        )
        self.profile_root = self.tmp / "profile"
        self.profile_root.mkdir()
        self.ctx = Context(self.repo, Profile(self.profile_root, "home"), frozenset({"hyperdock"}))

    def test_every_key_of_the_preset_becomes_a_set_key_action(self) -> None:
        actions = settings_planner.build(self.ctx, self.backend)

        self.assertEqual({action.kind for action in actions}, {"set-key"})
        self.assertEqual({action.target.full for action in actions}, {DIRECTORY + name for name in self.preset})
        self.assertEqual(settings_planner.warnings(self.ctx, self.backend), [])

    def test_the_screenshots_stack_seed_resolves_to_the_hyperdock_schema(self) -> None:
        (seed,) = seeds.build(self.ctx, self.backend, home=self.tmp / "home", env={})

        self.assertEqual(seed.kind, "seed-key")
        self.assertEqual(seed.target.full, DIRECTORY + "folder-stacks")

    def test_the_migration_drops_the_old_uuid_from_enabled_extensions(self) -> None:
        self.backend.set_user("org.gnome.shell", None, "enabled-extensions", f"['{OLD_UUID}', 'ding@rastersoft.com']")
        process = FakeProcess({("dconf", "dump", "/org/gnome/shell/extensions/dash2dock-lite/"): CommandResult(0, "")})

        (action,) = migrations.build(
            self.ctx, self.backend, process=process, home=self.tmp / "home", extension_root=Path(self.tmp / "ext")
        )

        self.assertFalse(action.check())
        self.assertIn(f"drop {OLD_UUID} from enabled-extensions", action.describe())


if __name__ == "__main__":
    unittest.main()

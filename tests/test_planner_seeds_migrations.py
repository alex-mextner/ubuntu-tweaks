"""Seed providers and the seeds / migrations planners."""

import json
import unittest
from collections.abc import Sequence
from pathlib import Path

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from gi.repository import GLib

from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.planners import migrations as migrations_planner
from ubuntu_tweaks.planners import seeds as seeds_planner
from ubuntu_tweaks.process import CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.seeds import SeedContext, get_provider, pictures_dir, screenshots_stack
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema, SettingsError, dump_keyfile, parse_keyfile
from tests.util import TempDirCase, write

SCHEMA = "org.example.hyper"
DIRECTORY = "/org/example/hyper/"
OLD_PATH = "/org/example/old/"
OLD_UUID = "old@example"
XML = """<schemalist>
  <schema id="org.example.hyper" path="/org/example/hyper/">
    <key name="icon-size" type="d"><default>0.5</default></key>
    <key name="folder-stacks" type="s"><default>''</default></key>
  </schema>
</schemalist>
"""
SHELL = FakeSchema(
    "org.gnome.shell",
    {"enabled-extensions": FakeKey("as", "@as []"), "disabled-extensions": FakeKey("as", "@as []")},
    path="/org/gnome/shell/",
)
INSTALLED = FakeSchema(SCHEMA, {"icon-size": FakeKey("d", "0.5"), "folder-stacks": FakeKey("s", "''")}, path=DIRECTORY)


class DconfMachine(FakeProcess):
    """``dconf dump`` / ``dconf load`` over an in-memory ``{directory: {key: text}}``."""

    def __init__(self, dconf: dict[str, dict[str, str]]) -> None:
        super().__init__()
        self.dconf = dconf
        self.failing: set[str] = set()

    def query(self, argv: Sequence[str], *, timeout: float = 60.0) -> CommandResult:
        super().query(argv, timeout=timeout)
        if argv[2] in self.failing:
            return CommandResult(124, "", "timed out")
        values = self.dconf.get(argv[2], {})
        return CommandResult(0, dump_keyfile({"/": values}) if values else "")

    def execute(self, argv: Sequence[str], *, input_text: str | None = None) -> None:
        super().execute(argv, input_text=input_text)
        self.dconf.setdefault(argv[2], {}).update(parse_keyfile(input_text or "").get("/", {}))


class RepoCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.home = self.tmp / "home"
        (self.tmp / "profile").mkdir()
        self.ctx = Context(self.repo, Profile(self.tmp / "profile", "home"))
        self.extensions = self.home / ".local/share/gnome-shell/extensions"
        self.extensions.mkdir(parents=True)

    def module(self, extra: str, *, enabled: bool = True) -> None:
        write(
            self.repo / "modules/hyper/module.toml",
            f'description = "x"\nenabled = {str(enabled).lower()}\n[source]\nkind = "local"\n'
            f'path = "gnome-extensions/hyper@x"\n{extra}',
        )
        write(
            self.repo / "gnome-extensions/hyper@x/metadata.json",
            json.dumps({"uuid": "hyper@x", "settings-schema": SCHEMA}),
        )
        write(self.repo / "gnome-extensions/hyper@x/schemas/org.example.hyper.gschema.xml", XML)


class SeedProviderTests(TempDirCase):
    def context(self, **env: str) -> SeedContext:
        return SeedContext(self.tmp, env, lambda domain, message: {"Screenshots": "Schermate"}.get(message, message))

    def test_screenshots_stack_uses_the_localized_folder_under_the_pictures_dir(self) -> None:
        write(self.tmp / ".config/user-dirs.dirs", '# x\nXDG_PICTURES_DIR="$HOME/Immagini"\n')
        value = screenshots_stack(self.context())
        folder = self.tmp / "Immagini/Schermate"

        self.assertEqual(value.ensure_dirs, (folder,))
        stacks = json.loads(GLib.Variant.parse(GLib.VariantType.new("s"), value.text).get_string())
        self.assertEqual(
            stacks,
            [
                {
                    "path": str(folder),
                    "icon": "folder-pictures",
                    "view": "automatic",
                    "sort": "date-added",
                    "display": "stack",
                }
            ],
        )

    def test_without_user_dirs_it_falls_back_to_pictures(self) -> None:
        self.assertEqual(pictures_dir(self.tmp, {}), self.tmp / "Pictures")
        self.assertEqual(screenshots_stack(self.context()).ensure_dirs, (self.tmp / "Pictures/Schermate",))

    def test_a_relative_pictures_dir_is_ignored(self) -> None:
        write(self.tmp / ".config/user-dirs.dirs", 'XDG_PICTURES_DIR="Pics"\n')

        self.assertEqual(pictures_dir(self.tmp, {}), self.tmp / "Pictures")

    def test_unknown_provider_is_invalid_config(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown seed provider 'nope'; known: screenshots-stack"):
            get_provider("nope")


class SeedPlannerTests(RepoCase):
    SEEDS = '[[seeds]]\nkey = "folder-stacks"\nprovider = "screenshots-stack"\n'

    def build(self, backend: FakeBackend) -> list:
        return list(seeds_planner.build(self.ctx, backend, home=self.home, env={}, localize=lambda d, m: m))

    def test_seed_is_written_once_while_unset_and_creates_its_folder(self) -> None:
        self.module(self.SEEDS)
        backend = FakeBackend([INSTALLED])
        plan = Plan.build(self.build(backend))

        self.assertEqual([a.kind for a in plan.actions], ["seed-key"])
        self.assertIn("only while unset", plan.actions[0].describe())
        self.assertFalse((self.home / "Pictures/Screenshots").exists())
        plan.execute()

        stacks = json.loads(backend.effective(SCHEMA, None, "folder-stacks").unpack())
        self.assertEqual(stacks[0]["path"], str(self.home / "Pictures/Screenshots"))
        self.assertTrue((self.home / "Pictures/Screenshots").is_dir())
        self.assertEqual(len(Plan.build(self.build(backend))), 0)

    def test_an_existing_user_value_is_never_overwritten_or_drift_checked(self) -> None:
        self.module(self.SEEDS)
        backend = FakeBackend([INSTALLED])
        backend.set_user(SCHEMA, None, "folder-stacks", "'[]'")

        self.assertEqual(len(Plan.build(self.build(backend))), 0)

    def test_path_with_quotes_survives_the_gvariant_and_json_layers(self) -> None:
        self.module(self.SEEDS)
        backend = FakeBackend([INSTALLED])
        awkward = self.tmp / "a \"q\" 's' \\b"
        Plan.build(seeds_planner.build(self.ctx, backend, home=awkward, env={}, localize=lambda d, m: m)).execute()

        stacks = json.loads(backend.effective(SCHEMA, None, "folder-stacks").unpack())
        self.assertEqual(stacks[0]["path"], str(awkward / "Pictures/Screenshots"))

    def test_schema_not_installed_yet_is_planned_unsatisfied(self) -> None:
        self.module(self.SEEDS)
        plan = Plan.build(self.build(FakeBackend([])))

        self.assertEqual(len(plan), 1)
        self.assertIn("not installed yet", plan.actions[0].describe())

    def test_unknown_provider_fails_the_build(self) -> None:
        self.module('[[seeds]]\nkey = "folder-stacks"\nprovider = "nope"\n')
        with self.assertRaises(ConfigError):
            self.build(FakeBackend([INSTALLED]))

    def test_unknown_key_is_a_warning_not_an_action(self) -> None:
        self.module('[[seeds]]\nkey = "no-such"\nprovider = "screenshots-stack"\n')
        backend = FakeBackend([INSTALLED])

        self.assertEqual(self.build(backend), [])
        self.assertIn("no key no-such", seeds_planner.warnings(self.ctx, backend)[0])


class MigrationTests(RepoCase):
    MIGRATION = f'[[migrations]]\nfrom_uuid = "{OLD_UUID}"\nfrom_path = "{OLD_PATH}"\n'

    def setUp(self) -> None:
        super().setUp()
        self.dconf: dict[str, dict[str, str]] = {OLD_PATH: {"icon-size": "0.17", "legacy-only": "1"}}
        self.process = DconfMachine(self.dconf)
        self.backend = FakeBackend([SHELL])
        self.backend.set_user("org.gnome.shell", None, "enabled-extensions", f"['{OLD_UUID}', 'keep@x']")

    def build(self) -> list:
        return list(
            migrations_planner.build(
                self.ctx, self.backend, process=self.process, home=self.home, extension_root=self.extensions
            )
        )

    def link_old_extension(self, target: Path) -> Path:
        link = self.extensions / OLD_UUID
        link.symlink_to(target)
        return link

    def test_values_are_copied_the_old_uuid_dropped_and_the_repo_link_removed(self) -> None:
        self.module(self.MIGRATION)
        link = self.link_old_extension(self.repo / "gnome-extensions/old@example")
        plan = Plan.build(self.build())

        self.assertEqual([a.kind for a in plan.actions], ["migrate"])
        self.assertEqual(plan.actions[0].scope, "hyper")
        text = plan.actions[0].describe()
        self.assertIn(f"copy 1 value(s) {OLD_PATH} -> {DIRECTORY} (icon-size)", text)
        self.assertIn(f"drop {OLD_UUID} from enabled-extensions", text)
        self.assertIn("remove symlink", text)
        self.assertEqual(self.process.executed, [])  # the preview changed nothing
        self.assertTrue(link.is_symlink())

        plan.execute()
        self.assertEqual(self.dconf[DIRECTORY], {"icon-size": "0.17"})  # unknown key not copied
        self.assertEqual(self.dconf[OLD_PATH], {"icon-size": "0.17", "legacy-only": "1"})  # old values stay
        self.assertEqual(self.backend.user_value("org.gnome.shell", None, "enabled-extensions").unpack(), ["keep@x"])
        self.assertFalse(link.is_symlink())
        self.assertEqual(len(Plan.build(self.build())), 0)

    def test_nothing_is_copied_when_the_new_path_already_has_user_values(self) -> None:
        self.module(self.MIGRATION)
        self.dconf[DIRECTORY] = {"icon-size": "0.9"}
        plan = Plan.build(self.build())
        plan.execute()

        self.assertEqual(self.dconf[DIRECTORY], {"icon-size": "0.9"})
        self.assertEqual(self.process.executed, [])
        self.assertEqual(self.backend.user_value("org.gnome.shell", None, "enabled-extensions").unpack(), ["keep@x"])

    def test_a_link_that_does_not_point_into_the_repo_is_not_ours_to_remove(self) -> None:
        self.module(self.MIGRATION)
        link = self.link_old_extension(self.tmp / "somebody-elses")
        Plan.build(self.build()).execute()

        self.assertTrue(link.is_symlink())

    def test_an_enabled_list_that_comes_from_an_override_is_never_frozen_into_dconf(self) -> None:
        self.module(self.MIGRATION)
        backend = FakeBackend([SHELL])  # no user value: the effective list may come from the session override
        self.backend = backend
        Plan.build(self.build()).execute()

        self.assertEqual(backend.writes, [])

    def test_uuid_only_migration_has_no_copy_step(self) -> None:
        self.module(f'[[migrations]]\nfrom_uuid = "{OLD_UUID}"\n')
        plan = Plan.build(self.build())

        self.assertEqual(len(plan), 1)
        self.assertNotIn("copy", plan.actions[0].describe())

    def test_a_disabled_module_does_not_migrate(self) -> None:
        self.module(self.MIGRATION, enabled=False)

        self.assertEqual(self.build(), [])

    def test_already_migrated_machine_plans_nothing(self) -> None:
        self.module(self.MIGRATION)
        self.backend.set_user("org.gnome.shell", None, "enabled-extensions", "['keep@x']")
        self.dconf[DIRECTORY] = {"icon-size": "0.17"}

        self.assertEqual(len(Plan.build(self.build())), 0)

    def test_a_failing_dconf_dump_is_an_error_not_an_empty_path(self) -> None:
        self.module(self.MIGRATION)
        for failing in (OLD_PATH, DIRECTORY):
            self.process.failing = {failing}
            with self.subTest(failing=failing), self.assertRaisesRegex(SettingsError, "dconf dump .* failed"):
                Plan.build(self.build())
        self.assertEqual(self.process.executed, [])


if __name__ == "__main__":
    unittest.main()

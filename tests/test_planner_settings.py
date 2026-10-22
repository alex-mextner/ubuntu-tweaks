"""The settings planner: layering, typed comparison, skipped keys, seeds, overrides, pending schemas."""

import unittest

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from ubuntu_tweaks.actions import Phase, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.planners import settings as planner
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema
from tests.util import TempDirCase, write

INTERFACE = "org.gnome.desktop.interface"
INTERFACE_PATH = "/org/gnome/desktop/interface/"
SHELL = "org.gnome.shell"
KEYBINDING = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
KEYBINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
EXTENSION_SCHEMA = "org.example.ext"

SCHEMAS = [
    FakeSchema(
        INTERFACE,
        {
            "gtk-theme": FakeKey("s", "'Yaru'"),
            "text-scaling-factor": FakeKey("d", "1.0"),
            "clock-show-seconds": FakeKey("b", "false"),
        },
        path=INTERFACE_PATH,
    ),
    FakeSchema(
        SHELL,
        {"enabled-extensions": FakeKey("as", "@as []"), "disabled-extensions": FakeKey("as", "@as []")},
        path="/org/gnome/shell/",
    ),
    FakeSchema(KEYBINDING, {"name": FakeKey("s", "''"), "binding": FakeKey("s", "''")}),
]

EXTENSION_XML = """<schemalist>
  <schema id="org.example.ext" path="/org/example/ext/">
    <key name="size" type="d"><default>1.0</default></key>
    <key name="folders" type="s"><default>''</default></key>
  </schema>
</schemalist>
"""


def extension_schema() -> FakeSchema:
    return FakeSchema(
        EXTENSION_SCHEMA,
        {"size": FakeKey("d", "1.0"), "folders": FakeKey("s", "''")},
        path="/org/example/ext/",
    )


class SettingsPlannerCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.profile_root = self.tmp / "profile"
        (self.repo / "modules").mkdir(parents=True)
        self.profile_root.mkdir()

    def context(self, only: frozenset[str] = frozenset()) -> Context:
        return Context(self.repo, Profile(self.profile_root, "home"), only)

    def builtin_module(self, name: str, preset: str, *, mode: str = "enforce", schema: str = INTERFACE) -> None:
        write(
            self.repo / "modules" / name / "module.toml",
            f'description = "x"\n[source]\nkind = "builtin"\n[[settings]]\nfile = "preset.ini"\nschema = "{schema}"\n'
            f'mode = "{mode}"\n',
        )
        write(self.repo / "modules" / name / "preset.ini", preset)

    def plan(self, backend: FakeBackend | None = None, ctx: Context | None = None) -> tuple[Plan, FakeBackend]:
        backend = backend or FakeBackend(SCHEMAS)
        return Plan.build(planner.build(ctx or self.context(), backend)), backend


class PanelFileTests(SettingsPlannerCase):
    def test_only_keys_that_differ_from_the_effective_value_are_planned(self) -> None:
        write(
            self.profile_root / "settings/appearance.ini",
            "[org/gnome/desktop/interface]\ngtk-theme='Yaru-dark'\ntext-scaling-factor=1\nclock-show-seconds=false\n",
        )
        plan, backend = self.plan()

        self.assertEqual([a.kind for a in plan.actions], ["set-key"])
        self.assertIn("/org/gnome/desktop/interface/gtk-theme = 'Yaru-dark' (now 'Yaru')", plan.actions[0].describe())
        self.assertIn("settings/appearance.ini", plan.actions[0].describe())
        self.assertEqual(plan.satisfied, 2)  # 1 and 1.0 are the same typed double; false is the default
        plan.execute()
        self.assertEqual(backend.effective(INTERFACE, None, "gtk-theme").unpack(), "Yaru-dark")

    def test_machine_overlay_beats_profile_and_collapses_to_one_action(self) -> None:
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/desktop/interface]\ngtk-theme='A'\n")
        write(
            self.profile_root / "machines/home/settings/appearance.ini",
            "[org/gnome/desktop/interface]\ngtk-theme='B'\n",
        )
        actions = planner.build(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].phase, Phase.SETTINGS_MACHINE)
        self.assertIn("'B'", actions[0].describe())

    def test_a_satisfied_lower_layer_is_not_undone_by_the_higher_one(self) -> None:
        # The lower layer already holds; only the higher value is planned, so the plan converges.
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/desktop/interface]\ngtk-theme='Yaru'\n")
        write(
            self.profile_root / "machines/home/settings/appearance.ini",
            "[org/gnome/desktop/interface]\ngtk-theme='B'\n",
        )
        plan, backend = self.plan()

        self.assertEqual(len(plan), 1)
        plan.execute()
        self.assertEqual(backend.effective(INTERFACE, None, "gtk-theme").unpack(), "B")
        self.assertEqual(len(Plan.build(planner.build(self.context(), backend))), 0)

    def test_keys_without_a_schema_are_skipped_with_a_warning_never_guessed(self) -> None:
        write(
            self.profile_root / "settings/keyboard.ini",
            "[org/nowhere/known]\na=1\nb=2\n[org/gnome/desktop/interface]\nno-such-key=1\ngtk-theme='X'\n",
        )
        backend = FakeBackend(SCHEMAS)
        actions = planner.build(self.context(), backend)
        lines = planner.warnings(self.context(), backend)

        self.assertEqual(len(actions), 1)
        self.assertIn(
            "skipped settings/keyboard.ini [org/nowhere/known] a, b: no installed schema at /org/nowhere/known/", lines
        )
        self.assertIn(
            "skipped settings/keyboard.ini [org/gnome/desktop/interface] no-such-key: "
            "no key no-such-key in org.gnome.desktop.interface",
            lines,
        )

    def test_a_value_of_the_wrong_type_is_invalid_config(self) -> None:
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/desktop/interface]\nclock-show-seconds='x'\n")
        with self.assertRaisesRegex(ConfigError, r"settings/appearance.ini|appearance.ini"):
            planner.build(self.context(), FakeBackend(SCHEMAS))

    def test_extension_lists_in_panel_files_are_left_to_the_toggle_planner(self) -> None:
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/shell]\nenabled-extensions=['a@b']\n")
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(planner.build(self.context(), backend), [])
        self.assertIn("owned by the extension toggles", planner.warnings(self.context(), backend)[0])

    def test_relocatable_schema_is_addressed_through_its_path(self) -> None:
        write(
            self.profile_root / "settings/keyboard.ini",
            f"[{KEYBINDING_PATH.strip('/')}]\nname='Terminal'\nbinding='<Super>t'\n",
        )
        plan, backend = self.plan()

        self.assertEqual(len(plan), 2)
        plan.execute()
        self.assertEqual(backend.effective(KEYBINDING, KEYBINDING_PATH, "binding").unpack(), "<Super>t")

    def test_scope_is_the_panel_id_so_only_filters_by_it(self) -> None:
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/desktop/interface]\ngtk-theme='A'\n")
        write(self.profile_root / "settings/power.ini", "[org/gnome/desktop/interface]\ntext-scaling-factor=2.0\n")
        actions = planner.build(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual([a.scope for a in actions], ["appearance", "power"])
        self.assertEqual(len(Plan.build(actions, only={"power"})), 1)

    def test_no_profile_means_no_actions(self) -> None:
        self.assertEqual(planner.build(self.context(), FakeBackend(SCHEMAS)), [])


class ModulePresetTests(SettingsPlannerCase):
    def test_module_preset_is_the_lowest_layer(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='Preset'\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/desktop/interface]\ngtk-theme='Mine'\n")
        plan, backend = self.plan()

        by_key = {a.target.key: a for a in plan.actions}  # type: ignore[attr-defined]
        self.assertEqual(set(by_key), {"gtk-theme", "text-scaling-factor"})
        self.assertEqual(by_key["gtk-theme"].phase, Phase.SETTINGS_PROFILE)
        self.assertEqual(by_key["text-scaling-factor"].scope, "tune")

    def test_override_in_profile_toml_replaces_one_preset_key(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='Preset'\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "profile.toml", '[modules.tune.settings]\ntext-scaling-factor = "2.0"\n')
        plan, _ = self.plan()

        text = {a.target.key: a.describe() for a in plan.actions}  # type: ignore[attr-defined]
        self.assertIn("text-scaling-factor = 2.0", text["text-scaling-factor"])
        self.assertIn("profile.toml [modules.tune.settings]", text["text-scaling-factor"])
        self.assertIn("gtk-theme = 'Preset'", text["gtk-theme"])

    def test_machine_toml_override_wins_over_profile_toml(self) -> None:
        self.builtin_module("tune", "[/]\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "profile.toml", '[modules.tune.settings]\ntext-scaling-factor = "2.0"\n')
        write(
            self.profile_root / "machines/home/machine.toml", '[modules.tune.settings]\ntext-scaling-factor = "3.0"\n'
        )
        plan, _ = self.plan()

        self.assertIn("text-scaling-factor = 3.0", plan.actions[0].describe())
        self.assertIn("machines/home/machine.toml", plan.actions[0].describe())

    def test_override_must_be_gvariant_text(self) -> None:
        self.builtin_module("tune", "[/]\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "profile.toml", "[modules.tune.settings]\ntext-scaling-factor = 2\n")
        with self.assertRaisesRegex(ConfigError, "must be a string of GVariant text"):
            planner.build(self.context(), FakeBackend(SCHEMAS))

    def test_override_of_an_unknown_key_is_a_warning(self) -> None:
        self.builtin_module("tune", "[/]\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "profile.toml", '[modules.tune.settings]\nnope = "1"\n')
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(len(planner.build(self.context(), backend)), 1)
        self.assertIn("no key nope", planner.warnings(self.context(), backend)[0])

    def test_disabled_module_contributes_nothing(self) -> None:
        self.builtin_module("tune", "[/]\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "machines/home/machine.toml", "[modules.tune]\nenabled = false\n")

        self.assertEqual(planner.build(self.context(), FakeBackend(SCHEMAS)), [])

    def test_seed_mode_writes_only_while_unset_and_is_never_drift_checked(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='Seeded'\n", mode="seed")
        plan, backend = self.plan()

        self.assertEqual([a.kind for a in plan.actions], ["seed-key"])
        backend.set_user(INTERFACE, None, "gtk-theme", "'Mine'")
        self.assertTrue(plan.actions[0].check())
        self.assertEqual(len(Plan.build(planner.build(self.context(), backend))), 0)

    def test_a_profile_panel_beats_a_seed_for_the_same_key(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='Seeded'\n", mode="seed")
        write(self.profile_root / "settings/appearance.ini", "[org/gnome/desktop/interface]\ngtk-theme='Mine'\n")
        actions = planner.build(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual([a.kind for a in actions], ["set-key"])

    def test_root_section_of_a_relocatable_module_schema_is_skipped(self) -> None:
        self.builtin_module("tune", "[/]\nname='x'\n", schema=KEYBINDING)
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(planner.build(self.context(), backend), [])
        self.assertIn("relocatable", planner.warnings(self.context(), backend)[0])

    def test_override_replaces_the_preset_entry_with_that_key_even_in_another_section(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='A'\n[org/example/ext]\nsize=1.0\n", schema=INTERFACE)
        write(self.profile_root / "profile.toml", '[modules.tune.settings]\nsize = "2.0"\n')
        actions = planner.build(self.context(), FakeBackend([*SCHEMAS, extension_schema()]))

        self.assertEqual(len(actions), 2)
        by_key = {a.target.key: a.describe() for a in actions}  # type: ignore[attr-defined]
        self.assertIn("/org/example/ext/size = 2.0", by_key["size"])

    def test_an_explicit_override_beats_another_modules_preset_whatever_the_discovery_order(self) -> None:
        self.builtin_module("a", "[/]\ngtk-theme='A'\n")
        self.builtin_module("b", "[/]\ngtk-theme='B'\n")
        write(self.profile_root / "profile.toml", "[modules.a.settings]\ngtk-theme = \"'Mine'\"\n")
        actions = planner.build(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual(len(actions), 1)
        self.assertIn("'Mine'", actions[0].describe())

    def test_override_of_a_key_set_in_two_preset_directories_is_ambiguous(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='A'\n[org/example/ext]\ngtk-theme='B'\n", schema=INTERFACE)
        write(self.profile_root / "profile.toml", "[modules.tune.settings]\ngtk-theme = \"'C'\"\n")
        extra = FakeSchema("org.example.ext", {"gtk-theme": FakeKey("s", "''")}, path="/org/example/ext/")
        backend = FakeBackend([*SCHEMAS, extra])

        self.assertEqual(len(planner.build(self.context(), backend)), 2)
        self.assertIn("ambiguous", " ".join(planner.warnings(self.context(), backend)))

    def test_override_takes_the_mode_of_the_winning_preset_file_not_of_an_earlier_one(self) -> None:
        write(
            self.repo / "modules/tune/module.toml",
            'description = "x"\n[source]\nkind = "builtin"\n'
            f'[[settings]]\nfile = "a.ini"\nschema = "{INTERFACE}"\nmode = "seed"\n'
            f'[[settings]]\nfile = "b.ini"\nschema = "{INTERFACE}"\n',
        )
        write(self.repo / "modules/tune/a.ini", "[/]\ngtk-theme='A'\n")
        write(self.repo / "modules/tune/b.ini", "[/]\ngtk-theme='B'\n")
        write(self.profile_root / "profile.toml", "[modules.tune.settings]\ngtk-theme = \"'C'\"\n")
        actions = planner.build(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual([a.kind for a in actions], ["set-key"])
        self.assertIn("'C'", actions[0].describe())

    def test_override_for_a_module_without_settings_files_uses_its_metadata_schema(self) -> None:
        write(
            self.repo / "modules/ext/module.toml",
            'description = "x"\n[source]\nkind = "local"\npath = "gnome-extensions/ext@x"\n',
        )
        write(
            self.repo / "gnome-extensions/ext@x/metadata.json",
            '{"uuid": "ext@x", "settings-schema": "org.example.ext"}',
        )
        write(self.profile_root / "profile.toml", '[modules.ext.settings]\nsize = "0.8"\nnope = "1"\n')
        backend = FakeBackend([*SCHEMAS, extension_schema()])

        self.assertEqual(len(planner.build(self.context(), backend)), 1)
        self.assertIn("no key nope", " ".join(planner.warnings(self.context(), backend)))

    def test_extension_lists_in_a_module_preset_are_skipped_like_in_panel_files(self) -> None:
        self.builtin_module("tune", "[org/gnome/shell]\nenabled-extensions=['x@y']\n", schema=INTERFACE)
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(planner.build(self.context(), backend), [])
        self.assertIn("owned by the extension toggles", planner.warnings(self.context(), backend)[0])

    def test_a_seed_planned_unset_is_not_written_if_the_user_value_appeared_meanwhile(self) -> None:
        self.builtin_module("tune", "[/]\ngtk-theme='Seeded'\n", mode="seed")
        plan, backend = self.plan()

        backend.set_user(INTERFACE, None, "gtk-theme", "'Theirs'")
        plan.actions[0].run()

        self.assertEqual(backend.writes, [])
        self.assertEqual(backend.effective(INTERFACE, None, "gtk-theme").unpack(), "Theirs")

    def test_override_warnings_name_the_toml_table_once(self) -> None:
        self.builtin_module("tune", "[/]\ntext-scaling-factor=1.5\n")
        write(self.profile_root / "profile.toml", '[modules.tune.settings]\nnope = "1"\n')
        lines = planner.warnings(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual(
            lines, ["skipped profile.toml [modules.tune.settings] nope: no key nope in org.gnome.desktop.interface"]
        )

    def test_override_also_looks_in_the_metadata_schema_when_settings_files_name_another(self) -> None:
        write(
            self.repo / "modules/ext/module.toml",
            'description = "x"\n[source]\nkind = "local"\npath = "gnome-extensions/ext@x"\n'
            f'[[settings]]\nfile = "x.ini"\nschema = "{INTERFACE}"\n',
        )
        write(self.repo / "modules/ext/x.ini", "[/]\ngtk-theme='A'\n")
        write(
            self.repo / "gnome-extensions/ext@x/metadata.json",
            '{"uuid": "ext@x", "settings-schema": "org.example.ext"}',
        )
        write(self.profile_root / "profile.toml", '[modules.ext.settings]\nsize = "2.0"\n')
        actions = planner.build(self.context(), FakeBackend([*SCHEMAS, extension_schema()]))

        self.assertEqual(sorted(a.target.key for a in actions), ["gtk-theme", "size"])  # type: ignore[attr-defined]


class PendingExtensionSchemaTests(SettingsPlannerCase):
    def extension_module(self, *, with_xml: bool = True) -> None:
        write(
            self.repo / "modules/ext/module.toml",
            'description = "x"\n[source]\nkind = "local"\npath = "gnome-extensions/ext@x"\n'
            '[[settings]]\nfile = "ext.ini"\n',
        )
        write(self.repo / "modules/ext/ext.ini", "[/]\nsize=0.5\nfolders='a'\nbogus=1\n")
        write(
            self.repo / "gnome-extensions/ext@x/metadata.json",
            '{"uuid": "ext@x", "settings-schema": "org.example.ext"}',
        )
        if with_xml:
            write(self.repo / "gnome-extensions/ext@x/schemas/org.example.ext.gschema.xml", EXTENSION_XML)

    def test_schema_not_installed_yet_is_planned_unsatisfied_and_resolved_at_run_time(self) -> None:
        self.extension_module()
        backend = FakeBackend(SCHEMAS)
        plan, _ = self.plan(backend)

        self.assertEqual(len(plan), 2)
        self.assertIn("(schema org.example.ext is not installed yet)", plan.actions[0].describe())
        self.assertIn("no key bogus", planner.warnings(self.context(), backend)[0])

        installed = FakeBackend([*SCHEMAS, extension_schema()])
        Plan.build(planner.build(self.context(), installed)).execute()
        self.assertEqual(installed.effective(EXTENSION_SCHEMA, None, "size").unpack(), 0.5)

    def test_a_plan_built_before_the_schema_appears_runs_on_the_same_backend_once_it_has(self) -> None:
        self.extension_module()
        backend = FakeBackend(SCHEMAS)
        plan, _ = self.plan(backend)

        backend._schemas[EXTENSION_SCHEMA] = extension_schema()  # what link + compile-schemas do to a refreshed backend
        plan.execute()

        self.assertEqual(backend.effective(EXTENSION_SCHEMA, None, "size").unpack(), 0.5)

    def test_a_wrong_typed_preset_value_for_a_pending_schema_is_invalid_config_at_plan_time(self) -> None:
        self.extension_module()
        write(self.repo / "modules/ext/ext.ini", "[/]\nsize=abc\n")

        with self.assertRaisesRegex(ConfigError, "ext.ini.*size"):
            planner.build(self.context(), FakeBackend(SCHEMAS))

    def submodule_module(self, *, checked_out: bool) -> None:
        write(
            self.repo / "modules/ext/module.toml",
            'description = "x"\n[source]\nkind = "submodule"\npath = "gnome-extensions/ext@x"\n'
            '[[settings]]\nfile = "ext.ini"\nschema = "org.example.ext"\n',
        )
        write(self.repo / "modules/ext/ext.ini", "[/]\nsize=0.5\n")
        (self.repo / "gnome-extensions/ext@x").mkdir(parents=True)
        if checked_out:
            write(self.repo / "gnome-extensions/ext@x/metadata.json", '{"uuid": "ext@x"}')

    def test_settings_of_a_submodule_that_is_not_checked_out_are_staged_not_skipped(self) -> None:
        self.submodule_module(checked_out=False)
        (line,) = planner.warnings(self.context(), FakeBackend(SCHEMAS))
        self.assertTrue(line.startswith("staged modules/ext/ext.ini [/] size:"), line)
        self.assertIn("arrives with the extension code this plan installs", line)
        self.assertIn("applied by the same `apply --commit`", line)
        self.assertNotIn("skipped", line)

    def test_a_checked_out_extension_without_the_schema_is_still_a_skip(self) -> None:
        self.submodule_module(checked_out=True)
        (line,) = planner.warnings(self.context(), FakeBackend(SCHEMAS))
        self.assertTrue(line.startswith("skipped modules/ext/ext.ini"), line)

    def test_panel_key_of_a_schema_that_is_not_installed_yet_waits_for_the_next_pass(self) -> None:
        self.extension_module()
        write(self.profile_root / "settings/appearance.ini", "[org/example/ext]\nsize=0.9\n")
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(len(planner.build(self.context(), backend)), 2)
        self.assertIn("no installed schema at /org/example/ext/", " ".join(planner.warnings(self.context(), backend)))

    def test_once_installed_the_panel_file_wins_over_the_module_preset(self) -> None:
        self.extension_module()
        write(self.profile_root / "settings/appearance.ini", "[org/example/ext]\nsize=0.9\n")
        actions = planner.build(self.context(), FakeBackend([*SCHEMAS, extension_schema()]))

        by_key = {a.target.key: a for a in actions}  # type: ignore[attr-defined]
        self.assertEqual(by_key["size"].phase, Phase.SETTINGS_PROFILE)
        self.assertEqual(len(actions), 2)

    def test_without_metadata_the_module_is_skipped_with_a_warning(self) -> None:
        self.extension_module()
        (self.repo / "gnome-extensions/ext@x/metadata.json").unlink()
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(planner.build(self.context(), backend), [])
        self.assertIn("no settings schema", planner.warnings(self.context(), backend)[0])

    def test_a_broken_metadata_file_skips_the_module_like_a_missing_one(self) -> None:
        for text in ("{not json", '["a list"]', '{"uuid": "ext@x", "settings-schema": 123}', '{"uuid": "ext@x"}'):
            with self.subTest(metadata=text):
                self.extension_module()
                write(self.repo / "gnome-extensions/ext@x/metadata.json", text)
                backend = FakeBackend(SCHEMAS)

                self.assertEqual(planner.build(self.context(), backend), [])
                self.assertIn("no settings schema", planner.warnings(self.context(), backend)[0])

    def test_a_metadata_file_that_is_not_utf8_skips_the_module(self) -> None:
        self.extension_module()
        (self.repo / "gnome-extensions/ext@x/metadata.json").write_bytes(b'{"uuid": "ext@x", "name": "caf\xe9"}')
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(planner.build(self.context(), backend), [])
        self.assertIn("no settings schema", planner.warnings(self.context(), backend)[0])


RANGED = FakeSchema(
    "org.example.range", {"limited": FakeKey("i", "1", minimum="0", maximum="10")}, path="/org/example/range/"
)


class BundleTests(SettingsPlannerCase):
    def test_a_bundle_may_mix_sections_from_any_schema_and_its_header_never_reaches_the_plan(self) -> None:
        write(
            self.profile_root / "settings/dev/mixed.ini",
            "[ubuntu-tweak]\ndescription = \"dev\"\n\n[org/gnome/desktop/interface]\ngtk-theme='X'\n"
            "[org/gnome/shell]\ndisabled-extensions=@as []\n",
        )
        backend = FakeBackend(SCHEMAS)
        actions = planner.build(self.context(), backend)

        self.assertEqual([a.target.full for a in actions], ["/org/gnome/desktop/interface/gtk-theme"])  # type: ignore[attr-defined]
        self.assertEqual([a.scope for a in actions], ["dev/mixed"])
        warnings = planner.warnings(self.context(), backend)
        self.assertFalse(any("ubuntu-tweak" in line and "no installed schema" in line for line in warnings))

    def test_the_reason_is_shown_next_to_the_key_in_the_preview_and_truncated(self) -> None:
        write(
            self.profile_root / "settings/a.ini",
            "[org/gnome/desktop/interface]\n# dark for the night shift\ngtk-theme='X'\n"
            f"# {'very long ' * 20}\nclock-show-seconds=true\n",
        )
        plan, _backend = self.plan()
        lines = {a.target.key: a.describe() for a in plan.actions}  # type: ignore[attr-defined]

        self.assertIn("<- settings/a.ini  # dark for the night shift", lines["gtk-theme"])
        long_line = lines["clock-show-seconds"]
        self.assertIn("  # very long", long_line)
        self.assertTrue(long_line.endswith("…"))
        self.assertNotIn("\n", long_line)

    def test_status_shows_the_reason_of_a_modified_key(self) -> None:
        from ubuntu_tweaks.commands import status

        write(
            self.profile_root / "settings/a.ini",
            "[org/gnome/desktop/interface]\n# the OLED panel hates white\ngtk-theme='X'\n",
        )
        plan, _backend = self.plan()
        _missing, modified = status.classify(plan)
        self.assertEqual(len(modified), 1)
        self.assertIn("# the OLED panel hates white", f"[{modified[0].kind}] {modified[0].describe()}")

    def test_two_shared_bundles_with_different_values_for_a_key_fail_planning_naming_both(self) -> None:
        write(self.profile_root / "settings/a.ini", "[org/gnome/desktop/interface]\ngtk-theme='A'\n")
        write(self.profile_root / "settings/b.ini", "[org/gnome/desktop/interface]\ngtk-theme='B'\n")
        with self.assertRaises(ConfigError) as caught:
            planner.build(self.context(), FakeBackend(SCHEMAS))
        self.assertIn("settings/a.ini:2", str(caught.exception))
        self.assertIn("settings/b.ini:2", str(caught.exception))

    def test_identical_duplicates_plan_once_and_warn(self) -> None:
        for name in ("a", "b"):
            write(self.profile_root / f"settings/{name}.ini", "[org/gnome/desktop/interface]\ngtk-theme='A'\n")
        backend = FakeBackend(SCHEMAS)

        self.assertEqual(len(planner.build(self.context(), backend)), 1)
        self.assertTrue(
            any(
                line.startswith("duplicate key /org/gnome/desktop/interface/gtk-theme")
                for line in planner.warnings(self.context(), backend)
            )
        )

    def test_an_overlay_bundle_beats_a_shared_bundle_of_another_name(self) -> None:
        write(self.profile_root / "settings/a.ini", "[org/gnome/desktop/interface]\ngtk-theme='A'\n")
        write(self.profile_root / "machines/home/settings/other.ini", "[org/gnome/desktop/interface]\ngtk-theme='B'\n")
        actions = planner.build(self.context(), FakeBackend(SCHEMAS))

        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0].phase, actions[0].scope), (Phase.SETTINGS_MACHINE, "other"))
        self.assertIn("'B'", actions[0].describe())

    def test_machines_filter_and_requires_module_decide_whether_a_bundle_applies(self) -> None:
        self.builtin_module("m", "[/]\nclock-show-seconds=true\n")
        write(
            self.profile_root / "settings/laptop.ini",
            "[ubuntu-tweak]\nmachines = [\"laptop\"]\n[org/gnome/desktop/interface]\ngtk-theme='L'\n",
        )
        write(
            self.profile_root / "settings/needs-m.ini",
            '[ubuntu-tweak]\nrequires-module = "m"\n[org/gnome/desktop/interface]\ntext-scaling-factor=1.5\n',
        )
        backend = FakeBackend(SCHEMAS)
        self.assertEqual(
            sorted(a.target.key for a in planner.build(self.context(), backend)),  # type: ignore[attr-defined]
            ["clock-show-seconds", "text-scaling-factor"],
        )
        write(self.profile_root / "profile.toml", "[modules.m]\nenabled = false\n")
        self.assertEqual(planner.build(self.context(), backend), [])

    def test_a_value_outside_the_schema_range_is_invalid_config_at_plan_time(self) -> None:
        write(self.profile_root / "settings/r.ini", "[org/example/range]\nlimited=11\n")
        backend = FakeBackend([*SCHEMAS, RANGED])
        with self.assertRaisesRegex(
            ConfigError, r"settings/r\.ini \[org/example/range\] limited: 11 is outside the allowed range"
        ):
            planner.build(self.context(), backend)
        self.assertEqual(backend.writes, [])

    def test_a_module_preset_outside_the_range_is_invalid_config_too(self) -> None:
        self.builtin_module("r", "[/]\nlimited=-3\n", schema="org.example.range")
        with self.assertRaisesRegex(ConfigError, "outside the allowed range"):
            planner.build(self.context(), FakeBackend([*SCHEMAS, RANGED]))

    def test_a_value_inside_the_range_is_planned(self) -> None:
        write(self.profile_root / "settings/r.ini", "[org/example/range]\nlimited=10\n")
        self.assertEqual(len(planner.build(self.context(), FakeBackend([*SCHEMAS, RANGED]))), 1)


if __name__ == "__main__":
    unittest.main()

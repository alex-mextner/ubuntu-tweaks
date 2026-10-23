"""The submodule, schemas, extension_links and toggle planners (they need PyGObject)."""

import os
import subprocess
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.util import write

try:
    from tests.actions_scenario import Scenario
    from ubuntu_tweaks.actions import ManualStepRequired, Plan
    from ubuntu_tweaks.context import Context
    from ubuntu_tweaks.errors import ConfigError
    from ubuntu_tweaks.extension import extension_modules
    from ubuntu_tweaks.profile import Profile
    from ubuntu_tweaks.planners import extension_links, schemas, submodule, toggle
    from ubuntu_tweaks.planners import build_actions as discovered_build_actions
    from ubuntu_tweaks.settings import FakeBackend
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

SCHEMA_XML = """<schemalist><schema id="org.example.ext" path="/org/example/ext/">
<key name="flag" type="b"><default>false</default></key></schema></schemalist>"""
EXTENSION_MODULE = '[extension]\nconflicts = ["dock@x"]\nretire = ["lamp@x"]\n'


class ExtensionLinkTests(Scenario):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.add_extension("gnome-extensions/eng@example", "eng@example")
        self.add_module("eng", path="gnome-extensions/eng@example")
        self.link = self.extensions_dir / "eng@example"

    def plan(self) -> Plan:
        return Plan.build(extension_links.build(self.context(), self.backend()))

    def test_missing_link_is_planned_as_link_and_converges(self) -> None:
        plan = self.plan()
        self.assertEqual([action.kind for action in plan.actions], ["link"])
        self.assertIn("~/.local/share/gnome-shell/extensions/eng@example -> ", plan.actions[0].describe())
        plan.execute()
        self.assertEqual(os.path.realpath(self.link), str(self.source.resolve()))
        self.assertEqual(len(self.plan()), 0)

    def test_the_uuid_comes_from_metadata_not_the_directory_name(self) -> None:
        write(self.source / "metadata.json", '{"uuid": "renamed@example"}')
        self.plan().execute()
        self.assertTrue((self.extensions_dir / "renamed@example").is_symlink())
        self.assertFalse(self.link.exists())

    def test_real_directory_in_the_way_is_backed_up_and_flagged_link_replace(self) -> None:
        write(self.link / "extension.js", "old")
        (action,) = self.plan().actions
        self.assertEqual(action.kind, "link-replace")
        self.assertIn("backup+replace", action.describe())
        self.assertIn("directory, content differs", action.describe())
        stamp = datetime(2026, 10, 20, 12, 30, 5, tzinfo=UTC)
        (module,) = extension_modules(self.context())
        extension_links.ExtensionLink(module, self.extensions_dir, now=lambda: stamp).run()
        self.assertTrue(self.link.is_symlink())
        saved = self.home / ".local/state/ubuntu-tweaks/backup/20261020T123005Z" / str(self.link).lstrip("/")
        self.assertEqual((saved / "extension.js").read_text(), "old")

    def test_identical_directory_is_reported_as_identical(self) -> None:
        write(self.source / "extension.js", "same")
        write(self.link / "extension.js", "same")
        write(self.link / "metadata.json", (self.source / "metadata.json").read_text())
        (action,) = self.plan().actions
        self.assertIn("directory, identical content", action.describe())

    def test_symlink_elsewhere_is_replaced_after_backup(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.extensions_dir.mkdir(parents=True)
        self.link.symlink_to(elsewhere)
        (action,) = self.plan().actions
        self.assertEqual(action.kind, "link-replace")
        self.assertIn(f"symlink to {elsewhere}", action.describe())
        action.run()
        self.assertEqual(os.path.realpath(self.link), str(self.source.resolve()))

    def test_uninitialized_submodule_is_planned_lazily_and_resolves_later(self) -> None:
        (self.repo / "gnome-extensions/dock").mkdir()
        self.add_module("dock", kind="submodule", path="gnome-extensions/dock")
        pending = [a for a in self.plan().actions if a.scope == "dock"]
        self.assertEqual(len(pending), 1)
        self.assertIn("known once the submodule is initialized", pending[0].describe())
        self.assertEqual(pending[0].kind, "link")
        with self.assertRaisesRegex(ManualStepRequired, "its sources are missing"):
            pending[0].run()
        self.add_extension("gnome-extensions/dock", "dock@real")
        pending[0].run()
        self.assertTrue((self.extensions_dir / "dock@real").is_symlink())

    def test_local_module_without_metadata_is_invalid_config(self) -> None:
        (self.source / "metadata.json").unlink()
        with self.assertRaisesRegex(ConfigError, "metadata.json not found"):
            extension_links.build(self.context(), self.backend())

    def test_disabled_module_is_not_managed(self) -> None:
        write(self.profile_root / "profile.toml", "[modules.eng]\nenabled = false\n")
        self.assertEqual(extension_links.build(self.context(), self.backend()), [])

    def test_extras_name_repo_symlinks_of_undeclared_uuids_only(self) -> None:
        self.extensions_dir.mkdir(parents=True)
        (self.extensions_dir / "old@uuid").symlink_to(self.repo / "gnome-extensions/gone")  # dangling, into this repo
        (self.extensions_dir / "foreign@uuid").symlink_to(self.tmp)  # not into this repo
        self.link.symlink_to(self.source)  # declared
        extras = extension_links.extras(self.context(), self.backend())
        self.assertEqual(len(extras), 1)
        self.assertIn("old@uuid", extras[0])


class SchemasTests(Scenario):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.add_extension("gnome-extensions/ext", "ext@x", schema="org.example.ext")
        self.add_module("ext", path="gnome-extensions/ext")
        self.xml = write(self.source / "schemas/org.example.ext.gschema.xml", SCHEMA_XML)
        self.compiled = self.source / "schemas/gschemas.compiled"

    def pending(self) -> list[str]:
        return [a.kind for a in Plan.build(schemas.build(self.context(), self.backend())).actions]

    def test_missing_compiled_file_is_pending_and_compiling_satisfies_it(self) -> None:
        self.assertEqual(self.pending(), ["compile-schemas"])
        Plan.build(schemas.build(self.context(), self.backend())).execute()
        self.assertTrue(self.compiled.is_file())
        self.assertEqual(self.pending(), [])

    def test_xml_newer_than_compiled_is_pending(self) -> None:
        write(self.compiled, "x")
        now = time.time()
        os.utime(self.compiled, (now - 100, now - 100))
        os.utime(self.xml, (now, now))
        self.assertEqual(self.pending(), ["compile-schemas"])

    def test_compiled_newer_than_xml_is_satisfied(self) -> None:
        write(self.compiled, "x")
        now = time.time()
        os.utime(self.xml, (now - 100, now - 100))
        os.utime(self.compiled, (now, now))
        self.assertEqual(self.pending(), [])

    def test_extension_without_schemas_is_satisfied(self) -> None:
        self.xml.unlink()
        self.assertEqual(self.pending(), [])

    def test_uninitialized_submodule_is_pending_until_it_proves_schema_free(self) -> None:
        (self.repo / "gnome-extensions/dock").mkdir()
        self.add_module("dock", kind="submodule", path="gnome-extensions/dock")
        before = [a for a in Plan.build(schemas.build(self.context(), self.backend())).actions if a.scope == "dock"]
        self.assertEqual(len(before), 1)
        self.add_extension("gnome-extensions/dock", "dock@x")  # initialized, no schemas dir
        self.assertTrue(before[0].check())

    def test_execute_does_not_run_the_compiler_for_a_schema_free_extension(self) -> None:
        (self.repo / "gnome-extensions/dock").mkdir()
        self.add_module("dock", kind="submodule", path="gnome-extensions/dock")
        plan = Plan.build(schemas.build(self.context(), self.backend()), only={"dock"})
        self.add_extension("gnome-extensions/dock", "dock@x")  # the submodule got initialized meanwhile
        with mock.patch.object(schemas, "run") as compiler:
            result = plan.execute()
        compiler.assert_not_called()
        self.assertEqual(result.changed, 0)


class SubmoduleTests(Scenario):
    def git(self, cwd: Path, *argv: str) -> str:
        return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout

    def setUp(self) -> None:
        super().setUp()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "protocol.file.allow",
            "GIT_CONFIG_VALUE_0": "always",
            "GIT_CONFIG_KEY_1": "init.defaultBranch",
            "GIT_CONFIG_VALUE_1": "main",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        upstream = self.tmp / "upstream"
        upstream.mkdir()
        self.git(upstream, "init")
        write(upstream / "metadata.json", '{"uuid": "dock@x"}')
        self.git(upstream, "add", ".")
        self.git(upstream, "commit", "-m", "first")
        origin = self.tmp / "origin"
        origin.mkdir()
        self.git(origin, "init")
        write(origin / "modules/dock/module.toml", '[source]\nkind = "submodule"\npath = "gnome-extensions/dock"\n')
        self.git(origin, "submodule", "add", str(upstream), "gnome-extensions/dock")
        self.git(origin, "add", ".")
        self.git(origin, "commit", "-m", "umbrella")
        subprocess.run(["git", "clone", str(origin), str(self.repo / "clone")], check=True, capture_output=True)
        self.clone = self.repo / "clone"

    def context(self, only: frozenset[str] = frozenset()) -> Context:
        return Context(self.clone, Profile(self.profile_root, "home"), only)

    def test_init_runs_git_and_a_second_plan_is_empty(self) -> None:
        plan = Plan.build(submodule.build(self.context(), self.backend()))
        self.assertEqual([a.kind for a in plan.actions], ["submodule"])
        self.assertIn("gnome-extensions/dock", plan.actions[0].describe())
        plan.execute()
        self.assertTrue((self.clone / "gnome-extensions/dock/.git").exists())
        self.assertEqual(len(Plan.build(submodule.build(self.context(), self.backend()))), 0)
        self.assertEqual(submodule.extras(self.context(), self.backend()), [])

    def test_submodule_at_another_commit_is_reported_not_reset(self) -> None:
        Plan.build(submodule.build(self.context(), self.backend())).execute()
        inside = self.clone / "gnome-extensions/dock"
        write(inside / "dev.txt", "work")
        self.git(inside, "add", ".")
        self.git(inside, "commit", "-m", "dev work")
        head = self.git(inside, "rev-parse", "HEAD").strip()
        self.assertEqual(len(Plan.build(submodule.build(self.context(), self.backend()))), 0)
        (line,) = submodule.extras(self.context(), self.backend())
        self.assertIn(head[:12], line)
        self.assertIn("never resets", line)
        self.assertEqual(self.git(inside, "rev-parse", "HEAD").strip(), head)

    def test_an_unpacked_archive_has_no_git_to_ask(self) -> None:
        archive = self.tmp / "archive"
        write(archive / "modules/dock/module.toml", '[source]\nkind = "submodule"\npath = "gnome-extensions/dock"\n')
        (archive / "gnome-extensions/dock").mkdir(parents=True)
        context = Context(archive, Profile(self.profile_root, "home"))
        (action,) = Plan.build(submodule.build(context, self.backend())).actions
        with self.assertRaisesRegex(ManualStepRequired, "is not a git checkout"):
            action.run()
        write(archive / "gnome-extensions/dock/metadata.json", "{}")
        self.assertEqual(len(Plan.build(submodule.build(context, self.backend()))), 0)


class ToggleTests(Scenario):
    def setUp(self) -> None:
        super().setUp()
        self.add_extension("gnome-extensions/dock", "hyper@x")
        self.add_module("dock", path="gnome-extensions/dock", extension=EXTENSION_MODULE)

    def plan(self, backend: FakeBackend) -> Plan:
        return Plan.build(toggle.build(self.context(), backend))

    def user(self, backend: FakeBackend, key: str) -> list[str]:
        return toggle.user_list(backend, key)

    def test_conflict_disabled_retire_dropped_extension_enabled_as_single_item_edits(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['keep@x', 'dock@x', 'lamp@x']")
        plan = self.plan(backend)
        self.assertEqual(sorted(a.kind for a in plan.actions), ["disable", "disable", "enable"])
        plan.execute()
        self.assertEqual(self.user(backend, "enabled-extensions"), ["keep@x", "hyper@x"])
        self.assertEqual(self.user(backend, "disabled-extensions"), ["dock@x"])
        self.assertEqual(len(self.plan(backend)), 0)

    def test_an_unset_user_value_is_treated_as_empty_and_defaults_are_never_written_back(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "disabled-extensions", "['other@x']")
        self.plan(backend).execute()
        self.assertEqual(self.user(backend, "enabled-extensions"), ["hyper@x"])
        self.assertEqual(self.user(backend, "disabled-extensions"), ["other@x", "dock@x"])
        written = [(key, value.unpack()) for _, _, key, value in backend.writes]
        self.assertIn(("enabled-extensions", ["hyper@x"]), written)

    def test_enable_removes_the_uuid_from_disabled(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['hyper@x']")
        backend.set_user("org.gnome.shell", None, "disabled-extensions", "['hyper@x', 'dock@x']")
        (action,) = [a for a in self.plan(backend).actions if a.kind == "enable"]
        self.assertIn("enable hyper@x", action.describe())
        action.run()
        self.assertEqual(self.user(backend, "disabled-extensions"), ["dock@x"])

    def test_nothing_is_written_when_already_satisfied(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['hyper@x']")
        backend.set_user("org.gnome.shell", None, "disabled-extensions", "['dock@x']")
        self.assertEqual(len(self.plan(backend)), 0)
        self.assertEqual(backend.writes, [])

    def test_uninitialized_submodule_enable_is_planned_with_a_placeholder_uuid(self) -> None:
        (self.repo / "gnome-extensions/later").mkdir()
        self.add_module("later", kind="submodule", path="gnome-extensions/later")
        backend = self.backend()
        (action,) = [a for a in self.plan(backend).actions if a.scope == "later"]
        self.assertIn("uuid known once the submodule is initialized", action.describe())
        with self.assertRaisesRegex(ManualStepRequired, "its sources are missing"):
            action.run()

    def test_extras_list_enabled_extensions_no_module_declares(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['hyper@x', 'gsconnect@x', 'lamp@x']")
        self.assertEqual(
            toggle.extras(self.context(), backend), ["extension gsconnect@x is enabled but no module declares it"]
        )

    def test_scope_selects_by_module_name(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['lamp@x']")
        actions = toggle.build(self.context(), backend)
        self.assertEqual(len(Plan.build(actions, only={"nope"})), 0)
        self.assertEqual(len(Plan.build(actions, only={"dock"})), 3)


class SystemExtensionToggleTests(Scenario):
    """A builtin module whose apt package ships an extension: ``[extension] enable`` is the uuid."""

    def setUp(self) -> None:
        super().setUp()
        self.add_builtin(enabled=True)

    def add_builtin(self, *, enabled: bool) -> None:
        text = f'enabled = {str(enabled).lower()}\n[source]\nkind = "builtin"\n[extension]\nenable = ["gsc@x"]\n'
        write(self.repo / "modules" / "gsc" / "module.toml", text)

    def plan(self, backend: FakeBackend) -> Plan:
        return Plan.build(toggle.build(self.context(), backend))

    def test_the_uuid_is_enabled_without_reading_any_metadata(self) -> None:
        backend = self.backend()
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['keep@x']")
        backend.set_user("org.gnome.shell", None, "disabled-extensions", "['gsc@x']")
        plan = self.plan(backend)
        self.assertEqual([a.describe() for a in plan.actions], ["enable gsc@x"])
        plan.execute()
        self.assertEqual(toggle.user_list(backend, "enabled-extensions"), ["keep@x", "gsc@x"])
        self.assertEqual(toggle.user_list(backend, "disabled-extensions"), [])
        self.assertEqual(len(self.plan(backend)), 0)

    def test_a_disabled_module_plans_nothing_and_its_uuid_is_not_an_extra(self) -> None:
        self.add_builtin(enabled=False)
        backend = self.backend()
        self.assertEqual(len(self.plan(backend)), 0)
        backend.set_user("org.gnome.shell", None, "enabled-extensions", "['gsc@x']")
        self.assertEqual(toggle.extras(self.context(), backend), [])

    def test_enable_is_refused_for_a_module_whose_code_is_in_the_repo(self) -> None:
        self.add_extension("gnome-extensions/dock", "hyper@x")
        self.add_module("dock", path="gnome-extensions/dock", extension='[extension]\nenable = ["other@x"]\n')
        with self.assertRaisesRegex(Exception, "'enable' is for builtin modules"):
            self.context().manifests()


class DiscoveryTests(Scenario):
    def test_all_four_planners_contribute_to_the_discovered_plan(self) -> None:
        self.add_extension("gnome-extensions/eng@example", "eng@example")
        self.add_module("eng", path="gnome-extensions/eng@example")
        kinds = {a.kind for a in discovered_build_actions(self.context(), self.backend())}
        self.assertTrue({"link", "enable", "compile-schemas"} <= kinds)


if __name__ == "__main__":
    unittest.main()

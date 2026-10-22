"""Modules supplied by the profile: ``<profile>/modules/<name>/module.toml``, found next to the engine's own."""

from collections.abc import Sequence
from pathlib import Path

from tests.test_system_modules import Recorder
from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.commands import list as list_command
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import SourceKind, discover_modules
from ubuntu_tweaks.planners import system_modules
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend

SYSTEM = """
description = "{description}"
enabled = {enabled}

[source]
kind = "system"
root = false
install = "install.sh"
check = "install.sh --check"
"""


class Installing(Recorder):
    """The checker says "drift" until the installer ran."""

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        super().execute(argv, input_text=input_text, cwd=cwd)
        self.check_exit = 0


class ProfileModuleTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.profile = Profile(self.tmp / "profile", "box")
        self.profile.root.mkdir()

    def engine(self, name: str, description: str = "from the engine", enabled: bool = True) -> None:
        write(
            self.repo / "modules" / name / "module.toml",
            SYSTEM.format(description=description, enabled=str(enabled).lower()),
        )

    def mine(self, name: str, description: str = "from the profile", enabled: bool = True) -> Path:
        path = self.profile.modules_dir / name
        write(path / "module.toml", SYSTEM.format(description=description, enabled=str(enabled).lower()))
        return path

    def context(self) -> Context:
        return Context(self.repo, self.profile)

    def test_the_profiles_modules_are_listed_with_the_engines_and_marked_by_origin(self) -> None:
        self.engine("alpha")
        directory = self.mine("beta")
        found = {m.name: m for m in self.context().manifests()}
        self.assertEqual(sorted(found), ["alpha", "beta"])
        self.assertEqual((found["alpha"].origin, found["beta"].origin), ("engine", "profile"))
        self.assertEqual(found["beta"].directory, directory)

    def test_a_profile_module_replaces_an_engine_module_of_the_same_name(self) -> None:
        self.engine("alpha")
        self.mine("alpha", "mine instead")
        (found,) = self.context().manifests()
        self.assertEqual((found.description, found.origin), ("mine instead", "profile"))

    def test_a_profile_without_a_modules_directory_adds_nothing(self) -> None:
        self.engine("alpha")
        self.assertEqual([m.name for m in self.context().manifests()], ["alpha"])

    def test_a_profile_that_does_not_exist_supplies_no_modules(self) -> None:
        self.engine("alpha")
        absent = Profile(self.tmp / "no-profile", "box")
        self.assertEqual([m.name for m in discover_modules(self.repo / "modules", absent)], ["alpha"])

    def test_a_broken_profile_manifest_is_invalid_config_naming_its_file(self) -> None:
        write(self.profile.modules_dir / "bad/module.toml", 'enabled = "maybe"\n[source]\nkind = "system"\n')
        with self.assertRaisesRegex(ConfigError, r"profile/modules/bad/module.toml"):
            self.context().manifests()

    def test_a_profile_module_cannot_be_a_submodule_of_the_engine(self) -> None:
        write(self.profile.modules_dir / "sub/module.toml", '[source]\nkind = "submodule"\npath = "x"\n')
        with self.assertRaisesRegex(ConfigError, "cannot be a git submodule of the engine"):
            self.context().manifests()

    def test_a_local_extension_path_is_relative_to_the_module_directory(self) -> None:
        write(self.profile.modules_dir / "ext/module.toml", '[source]\nkind = "local"\npath = "code"\n')
        (found,) = self.context().manifests()
        self.assertEqual(found.source.path, str(self.profile.modules_dir / "ext/code"))
        self.assertIs(found.source.kind, SourceKind.LOCAL)

    def test_the_profile_can_switch_its_own_module_off_like_any_other(self) -> None:
        self.mine("tool")
        write(self.profile.profile_toml, "[modules.tool]\nenabled = false\n")
        runner = Recorder(check_exit=3)
        self.assertEqual(system_modules.build(self.context(), FakeBackend(), process=runner), [])
        self.assertEqual(runner.calls, [])

    def test_a_profile_module_goes_through_the_normal_plan_and_runs_only_at_commit(self) -> None:
        directory = self.mine("tool")
        runner = Installing(check_exit=3)
        plan = Plan.build(system_modules.build(self.context(), FakeBackend(), process=runner))
        (action,) = plan.actions
        self.assertEqual((action.kind, action.scope, action.needs_root), ("system-module", "tool", False))
        self.assertIn(f"run tool: {directory / 'install.sh'}", action.describe())
        self.assertEqual([call[3] for call in runner.calls], [True])  # a read-only check; nothing was executed
        plan.execute(no_sudo=True)
        executed = [call[0] for call in runner.calls if not call[3]]
        self.assertEqual(executed, [[str(directory / "install.sh")]])

    def test_list_names_the_profile_as_the_source_and_runs_nothing(self) -> None:
        self.mine("tool")
        text = list_command.render(self.context())
        self.assertIn("tool", text)
        self.assertIn("profile module", text)

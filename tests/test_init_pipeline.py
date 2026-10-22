"""``init`` on an empty machine (golden), the pipeline order, and ``doctor`` with planned installs."""

import argparse
import contextlib
import dataclasses
import io
import os
import shutil
import types
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from tests.actions_scenario import SHELL_SCHEMA
from tests.util import TempDirCase, write

try:
    from ubuntu_tweaks import planners, reconcile
    from ubuntu_tweaks.actions import DEFAULT_ORDER, Action, ManualStepRequired, Phase
    from ubuntu_tweaks.commands import doctor, init
    from ubuntu_tweaks.context import REPO_ROOT, Context
    from ubuntu_tweaks.manifest import parse_manifest
    from ubuntu_tweaks.planners import base, extension_links, files, schemas, submodule, system_modules, toggle
    from ubuntu_tweaks.process import CommandResult, FakeProcess
    from ubuntu_tweaks.profile import Profile
    from ubuntu_tweaks.settings import FakeBackend
    from ubuntu_tweaks.steps import StepEnv
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

GOLDEN = Path(__file__).parent / "golden" / "init_empty_machine.txt"
PIPELINE_MODULES = (
    "base-tools",
    "zsh",
    "clipboard",
    "git-gh",
    "brew",
    "ssh-server",
    "hyperdock",
    "dotfiles",
)
OPTIONAL_MODULES = ()  # nothing in the engine is off by default among the pipeline modules


class ScriptedProcess(FakeProcess):
    """Module installers on an empty machine: every ``check`` reports drift (3), every ``preview`` one line."""

    def query(self, argv: Sequence[str], *, timeout: float = 60.0, cwd: Path | None = None) -> CommandResult:
        if "/repo/modules/" not in argv[0]:
            return super().query(argv, timeout=timeout, cwd=cwd)
        if argv[-1] in ("check", "--check"):
            return CommandResult(3, "", "")
        return CommandResult(0, f"would install {Path(argv[0]).parent.name}\n", "")


class EmptyMachine(TempDirCase):
    """A repo with every init-pipeline module, a profile with one linked file, and a machine where nothing is installed."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        for name in PIPELINE_MODULES:
            shutil.copytree(REPO_ROOT / "modules" / name, self.repo / "modules" / name)
        (self.repo / "bin").mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.profile = Profile(self.tmp / "profile", "home")
        self.profile.root.mkdir()
        write(self.profile.files_dir / ".config/ubuntu-tweaks-demo/config.toml", "demo = true\n")
        write(self.profile.machine_toml, "".join(f"[modules.{name}]\nenabled = true\n" for name in OPTIONAL_MODULES))
        self.runner = ScriptedProcess()

        def overrides(env: StepEnv) -> StepEnv:
            return dataclasses.replace(
                env, root=self.root, home=self.home, user="alice", group="alice", uid=os.getuid(), which=lambda _: None
            )

        def build(ctx: Context, backend: FakeBackend) -> list[Action]:
            return [
                *base.build(ctx, backend, process=self.runner, env_overrides=overrides),
                *system_modules.build(ctx, backend, process=self.runner),
                *files.build(ctx, backend, home=self.home),
                *submodule.build(ctx, backend),
                *extension_links.build(ctx, backend),
                *schemas.build(ctx, backend),
                *toggle.build(ctx, backend),
            ]

        shim = types.SimpleNamespace(build=build, warnings=base.warnings)
        for patcher in (
            mock.patch.object(planners, "discover_planners", return_value=[shim]),
            # `apply` binds its default backend factory at definition time: patch the default, not the module name
            mock.patch.dict(
                reconcile.apply.__kwdefaults__, {"backend_factory": lambda _ctx: FakeBackend([SHELL_SCHEMA])}
            ),
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(doctor.shutil, "which", return_value=None),
            mock.patch.object(doctor, "has_gi", return_value=True),
            mock.patch.object(doctor, "apt_installed", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_init(self, **flags: bool) -> tuple[int, str]:
        args = argparse.Namespace(**{"commit": False, "yes": False, "no_sudo": False, **flags})
        out = io.StringIO()
        ctx = Context(self.repo, self.profile)
        with contextlib.redirect_stdout(out):
            code = init.run(ctx, args)
        text = out.getvalue()
        for real, shown in ((str(self.tmp), "<tmp>"),):
            text = text.replace(real, shown)
        return code, text


class InitPreviewTests(EmptyMachine):
    def test_init_preview_on_an_empty_machine_matches_the_golden_file(self) -> None:
        code, text = self.run_init()
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(text, encoding="utf-8")
        self.assertEqual(text, GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.executed, [], "a preview runs nothing")
        self.assertEqual(list(self.home.iterdir()), [], "a preview writes nothing")

    def test_the_pipeline_runs_in_the_order_the_user_asked_for(self) -> None:
        _, text = self.run_init()
        order = [
            "apt: install",
            "chsh: set the login shell",
            "create <tmp>/home/.zprofile",
            "create <tmp>/home/.zshrc",
            "needs manual: gh auth login",
            "gh auth setup-git",
            "create /home/linuxbrew/.linuxbrew owned by",
            "brew: run the Homebrew installer",
        ]
        text = text.replace("~/.zprofile", "<tmp>/home/.zprofile").replace("~/.zshrc", "<tmp>/home/.zshrc")
        positions = [text.find(marker) for marker in order]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_a_machine_can_switch_modules_off(self) -> None:
        write(self.profile.machine_toml, "[modules.brew]\nenabled = false\n[modules.clipboard]\nenabled = false\n")
        _, text = self.run_init()
        self.assertNotIn("brew-", text)
        self.assertNotIn("xclip", text)
        self.assertIn("chsh", text)


class DoctorPlannedTests(EmptyMachine):
    def test_git_is_planned_not_missing_while_a_module_installs_it(self) -> None:
        needed = doctor.requirements(Context(self.repo, self.profile))
        git = next(item for item in needed if item.label == "git")
        self.assertEqual(git.planned_by, "base-tools")
        self.assertNotIn("git", [item.label for item in doctor.missing(needed)])
        self.assertIn("dconf", [item.label for item in doctor.missing(needed)])

    def test_git_is_missing_again_when_no_module_installs_it(self) -> None:
        write(self.profile.profile_toml, "[modules.base-tools]\nenabled = false\n[modules.git-gh]\nenabled = false\n")
        needed = doctor.requirements(Context(self.repo, self.profile))
        self.assertIn("git", [item.label for item in doctor.missing(needed)])

    def test_git_stays_hard_when_the_root_actions_will_be_skipped(self) -> None:
        needed = doctor.requirements(Context(self.repo, self.profile), honor_planned=False)
        self.assertIn("git", [item.label for item in doctor.missing(needed)])

    def test_python3_gi_is_never_planned(self) -> None:
        write(self.repo / "modules/gi/module.toml", '[source]\nkind = "builtin"\n[install]\napt = ["python3-gi"]\n')
        gi = next(item for item in doctor.requirements(Context(self.repo, self.profile)) if "python3-gi" in item.label)
        self.assertIsNone(gi.planned_by)

    def test_report_says_planned(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.report(doctor.requirements(Context(self.repo, self.profile)))
        self.assertIn(
            "planned git [apt: git] (submodules, update) -> `init`/`apply` installs it (module base-tools)",
            out.getvalue(),
        )


class StagedProbe(Action):
    phase = Phase.SUBMODULE
    kind = "submodule"

    def __init__(self) -> None:
        self.done = False

    def describe(self) -> str:
        return "stage"

    def check(self) -> bool:
        return self.done

    def run(self) -> None:
        self.done = True


class ManualProbe(Action):
    phase = Phase.BASE
    kind = "gh-login"

    def __init__(self) -> None:
        self.runs = 0

    def describe(self) -> str:
        return "login"

    def check(self) -> bool:
        return False

    def run(self) -> None:
        self.runs += 1
        raise ManualStepRequired("gh auth login (no token)")


class ManualStepsAcrossPassesTests(TempDirCase):
    def test_a_manual_step_runs_once_and_is_reported_once_even_when_the_plan_is_staged(self) -> None:
        manual, staged = ManualProbe(), StagedProbe()
        shim = types.SimpleNamespace(build=lambda ctx, backend: [manual, staged])
        (self.tmp / "repo").mkdir()
        ctx = Context(self.tmp / "repo", Profile(self.tmp / "profile", "home"))
        out = io.StringIO()
        with (
            mock.patch.object(planners, "discover_planners", return_value=[shim]),
            contextlib.redirect_stdout(out),
        ):
            code = reconcile.apply(ctx, commit=True, no_sudo=False, backend_factory=lambda _ctx: FakeBackend())

        self.assertEqual(code, 0)
        self.assertEqual(manual.runs, 1)
        text = out.getvalue()
        self.assertEqual(text.count("[gh-login] gh auth login (no token)"), 1)
        self.assertIn("1 changed", text)
        self.assertTrue(text.rstrip().splitlines()[-1].startswith("  1. [gh-login] gh auth login (no token)"))
        self.assertIn("Manual steps (1)", text)


class ModuleOrderingTests(unittest.TestCase):
    def test_phase_moves_install_time_actions_only_and_order_is_not_overwritten(self) -> None:
        class Probe(Action):
            kind = "probe"

            def __init__(self, phase: Phase, order: float = DEFAULT_ORDER) -> None:
                self.phase = phase
                self.order = order
                self.scope = "m"

            def describe(self) -> str:
                return ""

            def check(self) -> bool:
                return True

            def run(self) -> None:
                return None

        manifest = parse_manifest("m", 'order = 5\nphase = "base"\n[source]\nkind = "builtin"\n', Path("/x"))
        actions = [
            Probe(Phase.MIGRATE),
            Probe(Phase.SCHEMAS),
            Probe(Phase.PACKAGES),
            Probe(Phase.SYSTEM, 40.5),
            Probe(Phase.LINK),
        ]
        planners.apply_module_ordering(actions, [manifest])
        self.assertEqual(
            [(a.phase, a.order) for a in actions],
            [
                (Phase.MIGRATE, 5),
                (Phase.SCHEMAS, 5),
                (Phase.BASE, 5),
                (Phase.BASE, 40.5),
                (Phase.LINK, 5),
            ],
        )


if __name__ == "__main__":
    unittest.main()

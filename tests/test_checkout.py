"""The stable checkout (spec *Checkout*): where links, submodules and extension code point.

Builds real temporary git repositories with linked worktrees, so the resolution is the one git does. Nothing
here touches the developer's own checkout: ``cli.main`` tests patch ``cli.REPO_ROOT`` to a temporary tree.
"""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.util import TempDirCase, no_dotfiles_discovery, write
from ubuntu_tweaks import cli, commands
from ubuntu_tweaks.checkout import ENV_VAR, stable_checkout, warning_line
from ubuntu_tweaks.errors import ConfigError, ExitCode


def git(cwd: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd, check=True, capture_output=True
    )


def engine(root: Path) -> Path:
    """Make ``root`` look like an engine checkout: a ``modules/`` directory and an ``ubuntu_tweaks/`` package."""
    write(root / "modules" / ".keep")
    write(root / "ubuntu_tweaks" / "__init__.py")
    return root


class GitCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.main_tree = self.tmp / "main"
        self.main_tree.mkdir()
        git(self.main_tree, "init", "-q", "-b", "main")
        write(self.main_tree / "README", "x")
        git(self.main_tree, "add", "README")
        git(self.main_tree, "commit", "-q", "-m", "init")
        self.linked = self.main_tree / ".worktrees" / "feature"
        git(self.main_tree, "worktree", "add", "-q", str(self.linked), "-b", "feature")


class ResolutionTests(GitCase):
    def test_a_linked_worktree_resolves_to_the_main_tree(self) -> None:
        found = stable_checkout(self.linked, {})
        self.assertEqual((found.root, found.running, found.reason), (self.main_tree.resolve(), self.linked, "git"))
        self.assertFalse(found.is_stable)

    def test_the_main_tree_is_its_own_stable_checkout(self) -> None:
        found = stable_checkout(self.main_tree, {})
        self.assertEqual(found.root, self.main_tree.resolve())
        self.assertTrue(found.is_stable)

    def test_the_env_variable_wins_over_git(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        found = stable_checkout(self.linked, {ENV_VAR: str(other)})
        self.assertEqual((found.root, found.reason), (other.resolve(), "env"))

    def test_an_empty_env_variable_is_ignored(self) -> None:
        self.assertEqual(stable_checkout(self.linked, {ENV_VAR: ""}).reason, "git")

    def test_an_env_path_that_is_no_directory_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            stable_checkout(self.linked, {ENV_VAR: str(self.tmp / "missing")})
        self.assertIn(ENV_VAR, str(caught.exception))
        self.assertIn(str(self.tmp / "missing"), str(caught.exception))

    def test_a_directory_that_is_not_a_git_checkout_is_its_own_stable_checkout(self) -> None:
        copy = self.tmp / "copy"
        copy.mkdir()
        found = stable_checkout(copy, {})
        self.assertEqual((found.root, found.reason), (copy.resolve(), "running"))
        self.assertTrue(found.is_stable)

    def test_a_subdirectory_of_a_larger_repository_is_not_taken_for_the_repository(self) -> None:
        vendored = self.main_tree / "vendor" / "engine"
        vendored.mkdir(parents=True)
        self.assertEqual(stable_checkout(vendored, {}).root, vendored.resolve())

    def test_without_git_the_running_checkout_is_used(self) -> None:
        with mock.patch("ubuntu_tweaks.checkout.subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertEqual(stable_checkout(self.linked, {}).root, self.linked.resolve())

    def test_usable_needs_modules_and_the_package(self) -> None:
        self.assertFalse(stable_checkout(self.main_tree, {}).usable)
        engine(self.main_tree)
        self.assertTrue(stable_checkout(self.main_tree, {}).usable)


class WarningTests(GitCase):
    def test_the_line_names_both_trees_and_the_override(self) -> None:
        line = warning_line(self.main_tree, self.linked)
        self.assertEqual(
            line,
            f"warning: running from {self.linked}, a git worktree; links, submodules and extension code target "
            f"the stable checkout {self.main_tree} (set UBUNTU_TWEAKS_DIR to change)",
        )

    def test_a_plain_copy_is_not_called_a_worktree(self) -> None:
        copy = self.tmp / "copy"
        copy.mkdir()
        self.assertIn(f"running from {copy}, a copy;", warning_line(self.main_tree, copy))


DROPPED_COMMANDS = """
def register(subparsers):
    subparsers.add_parser("plans").set_defaults(handler=show, plans_links=True)
    subparsers.add_parser("reads").set_defaults(handler=show)


def show(ctx, args):
    print(f"{ctx.repo_root}|{ctx.running_root}")
"""


class MainTests(GitCase):
    """``cli.main`` from a linked worktree, with two throwaway commands: ``plans`` declares ``plans_links``, ``reads``
    does not. ``cli.REPO_ROOT`` is patched to the worktree: the real one is the developer's checkout."""

    def setUp(self) -> None:
        super().setUp()
        directory = self.tmp / "drop"
        write(directory / "dropped.py", DROPPED_COMMANDS)
        commands.__path__.append(str(directory))
        self.addCleanup(commands.__path__.remove, str(directory))
        self.addCleanup(sys.modules.pop, "ubuntu_tweaks.commands.dropped", None)
        self.running = self.linked.resolve()
        for patcher in (
            mock.patch.object(cli, "REPO_ROOT", self.running),
            mock.patch.dict(os.environ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(ENV_VAR, None)

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), no_dotfiles_discovery():
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_usable_stable_checkout_is_used_and_the_warning_printed_once(self) -> None:
        engine(self.main_tree)
        code, out, err = self.run_main("plans")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"{self.main_tree.resolve()}|{self.running}")
        self.assertEqual(err.count("warning: running from"), 1)
        self.assertIn(f"the stable checkout {self.main_tree.resolve()} (set UBUNTU_TWEAKS_DIR to change)", err)

    def test_an_unusable_stable_checkout_stops_a_link_planning_command(self) -> None:
        code, out, err = self.run_main("plans")
        self.assertEqual(code, int(ExitCode.INVALID_CONFIG))
        self.assertEqual(out, "")
        self.assertIn(str(self.main_tree.resolve()), err)
        self.assertIn("modules/", err)
        self.assertIn(f"{ENV_VAR}=<checkout> ubuntu-tweak plans", err)

    def test_a_read_only_command_keeps_running_from_the_running_checkout(self) -> None:
        code, out, err = self.run_main("reads")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"{self.running}|{self.running}")
        self.assertEqual(err.count("warning: running from"), 1)

    def test_the_env_override_replaces_an_unusable_default(self) -> None:
        chosen = engine(self.tmp / "chosen")
        os.environ[ENV_VAR] = str(chosen)
        code, out, _ = self.run_main("plans")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"{chosen.resolve()}|{self.running}")

    def test_an_invalid_env_path_fails_even_a_read_only_command(self) -> None:
        os.environ[ENV_VAR] = str(self.tmp / "missing")
        self.assertEqual(self.run_main("reads")[0], int(ExitCode.INVALID_CONFIG))

    def test_no_warning_when_the_running_tree_is_the_stable_one(self) -> None:
        engine(self.main_tree)
        with mock.patch.object(cli, "REPO_ROOT", self.main_tree.resolve()):
            code, _, err = self.run_main("plans")
        self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main()

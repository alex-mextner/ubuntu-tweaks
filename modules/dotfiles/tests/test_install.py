"""modules/dotfiles/install.sh against a throwaway HOME, a local git repo standing in for the user's dotfiles
repository and a fake ``gh`` that plays the user's GitHub account.

The repo holds a mac-era ``.zshrc`` and an ``ubuntu/`` profile directory; the tests prove the module only
clones and fast-forwards, writes nothing else in $HOME and never touches a file of the checkout, and that it
finds the repository through gh (the user's own ``.files``, ``dotfiles``, ...) instead of a built-in name.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(MODULE, "install.sh")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# a gh that knows one account: FAKE_GH_LOGIN (empty = logged out), FAKE_GH_REPOS (names), FAKE_GH_ORIGIN (the
# git repository every "clone" is taken from); every call is appended to FAKE_GH_LOG
FAKE_GH = r"""#!/bin/sh
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "api user")
    [ -n "$FAKE_GH_LOGIN" ] || { echo "To get started with GitHub CLI, please run:  gh auth login" >&2; exit 4; }
    echo "$FAKE_GH_LOGIN" ;;
  "auth status") [ -n "$FAKE_GH_LOGIN" ] || exit 1 ;;
  "repo list")
    printf '['
    sep=
    for name in $(echo "$FAKE_GH_REPOS" | tr ',' ' '); do
      printf '%s{"name":"%s","nameWithOwner":"%s/%s","isPrivate":true,"isArchived":false,"defaultBranchRef":{"name":"main"}}' \
        "$sep" "$name" "$FAKE_GH_LOGIN" "$name"
      sep=,
    done
    printf ']\n' ;;
  "repo clone") exec git clone -q -- "$FAKE_GH_ORIGIN" "$4" ;;
  *) exit 1 ;;
esac
"""


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True, capture_output=True, text=True
    ).stdout


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, dirs, files in os.walk(root) for f in files + dirs)


def read(path):
    with open(path) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(self.work, "ubuntu"))
        with open(os.path.join(self.work, ".zshrc"), "w") as f:
            f.write("# mac-era zsh\n")
        with open(os.path.join(self.work, "ubuntu", "profile.toml"), "w") as f:
            f.write("# profile\n")
        git("init", "-q", "-b", "main", self.work)
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "dotfiles", cwd=self.work)
        self.origin = os.path.join(self.tmp, "dotfiles.git")
        git("clone", "-q", "--bare", self.work, self.origin)
        self.dir = os.path.join(self.home, "dotfiles")  # a repository named dotfiles is cloned to ~/dotfiles
        self.state = os.path.join(self.tmp, "state")
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        open(self.gh_log, "w").close()

    def install_gh(self):
        path = os.path.join(self.bin, "gh")
        with open(path, "w") as f:
            f.write(FAKE_GH)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def run_install(self, *args, options=None, gh=None, **env):
        """``options`` are the module's [modules.dotfiles] keys (the default names this test's own repository);
        ``gh`` = {"login": ..., "repos": [...]} puts a fake gh on PATH, and then no repo option is given."""
        config = dict(options or {})
        environment = {
            **os.environ,
            **GIT_ENV,
            "HOME": self.home,
            "XDG_STATE_HOME": self.state,
            "FAKE_GH_LOG": self.gh_log,
            "FAKE_GH_ORIGIN": self.origin,
        }
        environment.pop("UBUNTU_TWEAK_DOTFILES_REPO", None)
        if gh is not None:
            self.install_gh()
            environment["PATH"] = self.bin + os.pathsep + environment["PATH"]
            environment["FAKE_GH_LOGIN"] = gh.get("login", "")
            environment["FAKE_GH_REPOS"] = ",".join(gh.get("repos", []))
        else:
            config.setdefault("repo", self.origin)
        environment["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps(config)
        environment.update(env)
        return subprocess.run(["bash", INSTALL, *args], env=environment, capture_output=True, text=True, cwd=MODULE)

    def push_upstream(self, name="NEWS"):
        with open(os.path.join(self.work, name), "w") as f:
            f.write("x\n")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", f"add {name}", cwd=self.work)
        git("push", "-q", self.origin, "HEAD:main", cwd=self.work)

    def gh_calls(self):
        return read(self.gh_log).splitlines()


class DotfilesInstall(Base):
    # -- clone ---------------------------------------------------------------------------------
    def test_plan_is_read_only_and_names_the_clone(self):
        before = tree(self.home)
        res = self.run_install("--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"git clone {self.origin} {self.dir}", res.stdout)
        self.assertEqual(tree(self.home), before)

    def test_install_clones_and_writes_nothing_else_in_home(self):
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(read(os.path.join(self.dir, ".zshrc")), "# mac-era zsh\n")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "ubuntu", "profile.toml")))
        top = sorted(os.listdir(self.home))
        self.assertEqual(top, ["dotfiles"])  # no ~/.zshrc, no symlinks, nothing linked out of the repo
        self.assertEqual(git("status", "--porcelain", cwd=self.dir), "")
        self.assertIn(f"profile: {self.dir}/ubuntu", res.stdout)

    def test_the_checkout_is_named_after_the_repository_and_a_tilde_dir_expands(self):
        self.assertEqual(self.run_install(options={"dir": "~/elsewhere/files"}).returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "elsewhere", "files", ".git")))
        self.assertFalse(os.path.exists(self.dir))

    def test_a_repository_named_dot_files_is_cloned_to_dot_files(self):
        origin = os.path.join(self.tmp, ".files.git")
        git("clone", "-q", "--bare", self.work, origin)
        self.assertEqual(self.run_install(options={"repo": origin}).returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".files", ".git")))

    def test_an_unreachable_repo_is_a_manual_step(self):
        res = self.run_install(options={"repo": os.path.join(self.tmp, "nope.git")})
        self.assertEqual(res.returncode, 10)
        self.assertIn("needs manual: cannot clone", res.stderr)

    def test_a_dash_leading_repo_is_refused(self):
        res = self.run_install(options={"repo": "--upload-pack=evil"})
        self.assertEqual(res.returncode, 2)
        self.assertFalse(os.path.exists(self.dir))

    def test_a_directory_that_is_not_a_checkout_is_never_touched(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "mine"), "w") as f:
            f.write("keep\n")
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        self.assertEqual(os.listdir(self.dir), ["mine"])

    def test_the_checkout_is_remembered_so_a_custom_dir_is_found_next_time(self):
        self.assertEqual(self.run_install(options={"dir": "~/src/mine", "path": "ubuntu"}).returncode, 0)
        remembered = json.loads(read(os.path.join(self.state, "ubuntu-tweaks", "dotfiles.json")))["checkout"]
        self.assertEqual(remembered["dir"], os.path.join(self.home, "src", "mine"))
        self.assertEqual(remembered["profile_dir"], os.path.join(self.home, "src", "mine", "ubuntu"))
        self.assertEqual(remembered["repo"], self.origin)

    # -- fast-forward ----------------------------------------------------------------------------
    def test_a_clean_checkout_is_fast_forwarded_and_a_second_run_changes_nothing(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.push_upstream()
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("fast-forwarded by 1 commit", res.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "NEWS")))
        before = tree(self.dir)
        again = self.run_install()
        self.assertEqual(again.returncode, 0)
        self.assertIn("up to date", again.stdout)
        self.assertEqual(tree(self.dir), before)

    def test_a_dirty_checkout_that_is_behind_is_a_manual_step_and_keeps_the_edit(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.dir, ".zshrc"), "w") as f:
            f.write("# my edit\n")
        self.push_upstream()
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        self.assertIn("uncommitted changes", res.stderr)
        self.assertEqual(read(os.path.join(self.dir, ".zshrc")), "# my edit\n")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "NEWS")))

    def test_a_dirty_checkout_that_is_up_to_date_is_fine(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.dir, ".zshrc"), "w") as f:
            f.write("# my edit\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(read(os.path.join(self.dir, ".zshrc")), "# my edit\n")

    def test_a_diverged_checkout_is_a_manual_step(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.dir, "LOCAL"), "w") as f:
            f.write("l\n")
        git("add", "-A", cwd=self.dir)
        git("commit", "-q", "-m", "local", cwd=self.dir)
        self.push_upstream()
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        self.assertIn("diverged", res.stderr)

    def test_unpushed_local_commits_are_left_alone(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.dir, "LOCAL"), "w") as f:
            f.write("l\n")
        git("add", "-A", cwd=self.dir)
        git("commit", "-q", "-m", "local", cwd=self.dir)
        head = git("rev-parse", "HEAD", cwd=self.dir)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.dir), head)

    # -- --check -------------------------------------------------------------------------------
    def test_check_is_drift_when_missing_then_ok_and_read_only(self):
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("is missing", res.stdout)
        self.assertEqual(self.run_install().returncode, 0)
        before = tree(self.dir)
        ok = self.run_install("--check")
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertEqual(tree(self.dir), before)

    def test_check_sees_behind_from_the_last_fetched_state(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.push_upstream()
        git("fetch", "-q", "origin", cwd=self.dir)  # a fetch elsewhere updated the tracking ref
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("1 commit(s) behind", res.stdout)

    def test_check_sees_a_diverged_checkout_and_accepts_one_that_is_only_ahead(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.dir, "LOCAL"), "w") as f:
            f.write("l\n")
        git("add", "-A", cwd=self.dir)
        git("commit", "-q", "-m", "local", cwd=self.dir)
        self.assertEqual(self.run_install("--check").returncode, 0)  # ahead only
        self.push_upstream()
        git("fetch", "-q", "origin", cwd=self.dir)
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("diverged", res.stdout)

    def test_check_sees_a_checkout_cloned_from_somewhere_else(self):
        self.assertEqual(self.run_install().returncode, 0)
        other = os.path.join(self.tmp, "x", "dotfiles.git")  # the same name elsewhere: still another repository
        res = self.run_install("--check", options={"repo": other})
        self.assertEqual(res.returncode, 3)
        self.assertIn("not " + other, res.stdout)

    def test_a_plain_directory_inside_another_work_tree_is_not_a_checkout(self):
        git("init", "-q", "-b", "main", self.home)  # a git-managed $HOME
        os.makedirs(self.dir)
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("not a git checkout", res.stdout)
        self.assertEqual(self.run_install().returncode, 10)

    def test_an_untracked_file_in_the_way_of_the_fast_forward_is_a_manual_step(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.dir, "NEWS"), "w") as f:
            f.write("mine\n")
        self.push_upstream()
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        self.assertIn("could not be fast-forwarded", res.stderr)
        self.assertEqual(read(os.path.join(self.dir, "NEWS")), "mine\n")

    def test_a_relative_dir_option_is_refused_and_nothing_is_created(self):
        res = self.run_install(options={"dir": ".files"})
        self.assertEqual(res.returncode, 2)
        self.assertIn("must be absolute", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(MODULE, ".files")))

    def test_a_regular_file_at_the_checkout_path_is_not_a_checkout(self):
        with open(self.dir, "w") as f:
            f.write("x\n")
        self.assertEqual(self.run_install("--check").returncode, 3)
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        with open(self.dir) as f:
            self.assertEqual(f.read(), "x\n")

    def test_install_refuses_a_checkout_whose_origin_is_not_the_configured_repo(self):
        self.assertEqual(self.run_install().returncode, 0)
        other = os.path.join(self.tmp, "x", "dotfiles.git")
        res = self.run_install(options={"repo": other})
        self.assertEqual(res.returncode, 10)
        self.assertIn("remote set-url origin", res.stderr)

    def test_check_is_drift_for_a_directory_that_is_not_a_checkout(self):
        os.makedirs(self.dir)
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("not a git checkout", res.stdout)

    def test_usage(self):
        self.assertEqual(self.run_install("--bogus").returncode, 2)


class DotfilesDiscovery(Base):
    """No repo option: the repository comes from the user's gh account, whoever that is."""

    def test_the_users_own_repository_is_found_and_cloned_with_gh(self):
        gh = {"login": "alice", "repos": ["notes", "dotfiles"]}
        plan = self.run_install("--plan", gh=gh)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("dotfiles repository: alice/dotfiles (gh, gh login alice)", plan.stdout)
        self.assertIn(f"gh repo clone alice/dotfiles {self.dir}", plan.stdout)
        self.assertFalse(os.path.exists(self.dir))
        res = self.run_install(gh=gh)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "ubuntu", "profile.toml")))
        self.assertIn("repo clone alice/dotfiles " + self.dir, self.gh_calls())

    def test_dot_files_comes_first_and_is_cloned_to_dot_files(self):
        gh = {"login": "alice", "repos": ["dotfiles", ".files"]}
        res = self.run_install(gh=gh)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("note: several dotfiles repositories found (alice/.files, alice/dotfiles)", res.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".files", ".git")))
        self.assertFalse(os.path.exists(self.dir))

    def test_no_such_repository_is_a_plain_no_op(self):
        gh = {"login": "alice", "repos": ["notes"]}
        for args in (("--plan",), ("--check",), ()):
            with self.subTest(args=args):
                res = self.run_install(*args, gh=gh)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("no dotfiles repository found for alice", res.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_a_logged_out_gh_is_a_manual_step_with_the_exact_command(self):
        gh = {"login": ""}
        res = self.run_install(gh=gh)
        self.assertEqual(res.returncode, 10)
        self.assertIn("needs manual: not logged in to GitHub: run `gh auth login`", res.stderr)
        check = self.run_install("--check", gh=gh)
        self.assertEqual(check.returncode, 3)
        self.assertIn("gh auth login", check.stdout)
        plan = self.run_install("--plan", gh=gh)
        self.assertEqual(plan.returncode, 0)
        self.assertIn("needs manual: not logged in to GitHub: run `gh auth login`", plan.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_a_checkout_that_is_already_on_disk_needs_no_gh_at_all(self):
        self.assertEqual(self.run_install().returncode, 0)  # cloned with an explicit repo
        os.rename(self.dir, os.path.join(self.home, ".files"))
        res = self.run_install("--check", gh={"login": ""})  # logged out, nothing to ask
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.gh_calls(), [])

    def test_the_flag_variable_selects_the_repository_and_skips_discovery(self):
        gh = {"login": "alice", "repos": [".files"]}
        res = self.run_install("--plan", gh=gh, UBUNTU_TWEAK_DOTFILES_REPO="bob/setup")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"gh repo clone bob/setup {os.path.join(self.home, 'setup')}", res.stdout)
        self.assertNotIn("alice", res.stdout)
        self.assertNotIn("repo list alice", " ".join(self.gh_calls()))

    def test_a_slug_without_a_gh_login_falls_back_to_https_for_public_repositories(self):
        res = self.run_install("--plan", gh={"login": ""}, UBUNTU_TWEAK_DOTFILES_REPO="bob/setup")
        self.assertIn(f"git clone https://github.com/bob/setup {os.path.join(self.home, 'setup')}", res.stdout)

    def test_a_checkout_of_another_repository_is_not_taken_for_the_requested_one(self):
        self.assertEqual(self.run_install().returncode, 0)  # ~/dotfiles cloned from this test's own repo
        res = self.run_install("--check", gh={"login": "alice"}, UBUNTU_TWEAK_DOTFILES_REPO="bob/dotfiles")
        self.assertEqual(res.returncode, 3)
        self.assertIn("not bob/dotfiles", res.stdout)


if __name__ == "__main__":
    unittest.main()

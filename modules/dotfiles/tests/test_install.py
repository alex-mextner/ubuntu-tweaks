"""modules/dotfiles/install.sh against a throwaway HOME and a local git repo standing in for .files.

The repo holds a mac-era ``.zshrc`` and an ``ubuntu/`` profile directory; the tests prove the module
only clones and fast-forwards, writes nothing else in $HOME and never touches a file of the checkout.
"""

import json
import os
import shutil
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


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True, capture_output=True, text=True
    ).stdout


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, dirs, files in os.walk(root) for f in files + dirs)


def read(path):
    with open(path) as f:
        return f.read()


class DotfilesInstall(unittest.TestCase):
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
        self.dir = os.path.join(self.home, ".files")

    def run_install(self, *args, options=None):
        env = {
            **os.environ,
            **GIT_ENV,
            "HOME": self.home,
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({"repo": self.origin, **(options or {})}),
        }
        return subprocess.run(["bash", INSTALL, *args], env=env, capture_output=True, text=True, cwd=MODULE)

    def push_upstream(self, name="NEWS"):
        with open(os.path.join(self.work, name), "w") as f:
            f.write("x\n")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", f"add {name}", cwd=self.work)
        git("push", "-q", self.origin, "HEAD:main", cwd=self.work)

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
        self.assertEqual(top, [".files"])  # no ~/.zshrc, no symlinks, nothing linked out of the repo
        self.assertEqual(git("status", "--porcelain", cwd=self.dir), "")

    def test_the_default_checkout_is_dot_files_and_a_tilde_dir_expands(self):
        self.assertEqual(self.run_install(options={"dir": "~/elsewhere/files"}).returncode, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "elsewhere", "files", ".git")))

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

    def test_check_sees_behind_and_diverged_from_the_last_fetched_state(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.push_upstream()
        git("fetch", "-q", "origin", cwd=self.dir)  # a fetch elsewhere updated the tracking ref
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("1 commit(s) behind", res.stdout)

    def test_check_is_drift_for_a_directory_that_is_not_a_checkout(self):
        os.makedirs(self.dir)
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)
        self.assertIn("not a git checkout", res.stdout)

    def test_usage(self):
        self.assertEqual(self.run_install("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()

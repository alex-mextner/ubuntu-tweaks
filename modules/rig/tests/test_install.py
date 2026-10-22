"""modules/rig/install.sh against a throwaway HOME and a local git repo standing in for rig-cli.

The "rig" in that repo is a shell stub that records every call in $RIG_LOG, so the tests see the
order of the steps (clone, link, profile, activate) and which calls are read-only. Nothing here
touches the real $HOME, the network or the real rig.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(MODULE, "install.sh")

FAKE_RIG = """#!/bin/bash
echo "$*" >>"$RIG_LOG"
case "$1 ${2:-}" in
  "profile status") exit "${FAKE_PROFILE_STATUS_RC:-0}" ;;
  "profile sync"|"profile init") exit "${FAKE_PROFILE_RC:-0}" ;;
  "activate --json") echo "{\\"config_changes\\": [${FAKE_ACTIVATE_CHANGES:-}]}" ;;
  "activate commit") exit "${FAKE_ACTIVATE_RC:-0}" ;;
  "activate ") echo "rig activate - PREVIEW (nothing was changed)" ;;
  "status ") exit "${FAKE_STATUS_RC:-0}" ;;
esac
exit 0
"""

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args, cwd=None):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, dirs, files in os.walk(root) for f in files + dirs)


class RigInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.log = os.path.join(self.tmp, "rig.log")
        open(self.log, "w").close()
        work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(work, "bin"))
        os.makedirs(os.path.join(work, "riglib"))
        with open(os.path.join(work, "bin", "rig"), "w") as f:
            f.write(FAKE_RIG)
        os.chmod(os.path.join(work, "bin", "rig"), 0o755)
        open(os.path.join(work, "riglib", "cli.py"), "w").close()
        git("init", "-q", "-b", "main", work)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "rig stub", cwd=work)
        self.origin = os.path.join(self.tmp, "rig-cli.git")
        git("clone", "-q", "--bare", work, self.origin)
        self.work = work
        self.dir = os.path.join(self.home, "dev", "rig-cli")
        self.link = os.path.join(self.home, ".local", "bin", "rig")

    def run_install(self, *args, options=None, **env):
        cfg = {"repo": self.origin, **(options or {})}
        full = {
            **os.environ,
            **GIT_ENV,
            "HOME": self.home,
            "RIG_LOG": self.log,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(cfg),
            **env,
        }
        return subprocess.run(["bash", INSTALL, *args], env=full, capture_output=True, text=True, cwd=MODULE)

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    # -- --plan --------------------------------------------------------------------------------
    def test_plan_shows_every_step_and_changes_nothing(self):
        before = tree(self.home)
        res = self.run_install("--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"git clone {self.origin} {self.dir}", res.stdout)
        self.assertIn(f"ln -s {self.dir}/bin/rig {self.link}", res.stdout)
        self.assertIn("rig profile: none set up", res.stdout)
        self.assertIn("rig activate commit (its preview is shown once rig-cli is cloned)", res.stdout)
        self.assertEqual(tree(self.home), before)
        self.assertEqual(self.calls(), [])

    def test_plan_after_install_shows_the_activate_preview(self):
        self.assertEqual(self.run_install().returncode, 0)
        open(self.log, "w").close()
        res = self.run_install("--plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("PREVIEW (nothing was changed)", res.stdout)
        self.assertIn("link: ", res.stdout)
        self.assertEqual(self.calls(), ["activate"])

    def test_plan_names_the_profile_step_by_what_is_configured(self):
        res = self.run_install("--plan", options={"profile_url": "https://forge.example/u/.rig-profile"})
        self.assertIn("rig profile init https://forge.example/u/.rig-profile", res.stdout)
        res = self.run_install("--plan", options={"profile_sync": False, "activate": False})
        self.assertIn("not touched (profile_sync = false)", res.stdout)
        self.assertIn("skipped (activate = false)", res.stdout)

    # -- install -------------------------------------------------------------------------------
    def test_install_clones_links_syncs_nothing_without_a_profile_and_activates(self):
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "riglib", "cli.py")))
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(os.path.join(self.dir, "bin", "rig")))
        self.assertEqual(self.calls(), ["activate commit"])
        self.assertIn("rig profile: none set up", res.stdout)

    def test_install_runs_profile_sync_before_activate_when_a_profile_exists(self):
        os.makedirs(os.path.join(self.home, ".config", "rig", "profile", "repo", ".git"))
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.calls(), ["profile sync", "activate commit"])

    def test_install_inits_the_profile_from_profile_url(self):
        url = "https://forge.example/u/.rig-profile"
        res = self.run_install(options={"profile_url": url})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.calls(), [f"profile init {url}", "activate commit"])

    def test_options_can_switch_the_profile_and_activation_off(self):
        os.makedirs(os.path.join(self.home, ".config", "rig", "profile", "repo", ".git"))
        res = self.run_install(options={"profile_sync": False, "activate": False})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.islink(self.link))

    def test_a_second_install_clones_nothing_new_and_leaves_the_link(self):
        self.assertEqual(self.run_install().returncode, 0)
        target = os.readlink(self.link)
        inode = os.stat(self.dir).st_ino
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.readlink(self.link), target)
        self.assertEqual(os.stat(self.dir).st_ino, inode)
        self.assertEqual(self.calls(), ["activate commit", "activate commit"])

    def test_a_clean_checkout_is_fast_forwarded(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.work, "NEWS"), "w") as f:
            f.write("x\n")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "more", cwd=self.work)
        git("push", "-q", self.origin, "HEAD:main", cwd=self.work)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "NEWS")))

    def test_a_dirty_checkout_is_not_touched(self):
        self.assertEqual(self.run_install().returncode, 0)
        mine = os.path.join(self.dir, "riglib", "cli.py")
        with open(mine, "w") as f:
            f.write("# my change\n")
        with open(os.path.join(self.work, "NEWS"), "w") as f:
            f.write("x\n")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "more", cwd=self.work)
        git("push", "-q", self.origin, "HEAD:main", cwd=self.work)
        res = self.run_install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("working tree has changes, not updated", res.stdout)
        with open(mine) as f:
            self.assertEqual(f.read(), "# my change\n")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "NEWS")))

    # -- needs manual (exit 10) and failures -----------------------------------------------------
    def test_an_unreachable_repo_is_a_manual_step(self):
        res = self.run_install(options={"repo": os.path.join(self.tmp, "nope.git")})
        self.assertEqual(res.returncode, 10, res.stdout)
        self.assertIn("needs manual: cannot clone", res.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_foreign_directory_at_the_checkout_path_is_a_manual_step(self):
        os.makedirs(self.dir)
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        self.assertIn("is not a rig-cli checkout", res.stderr)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_regular_file_at_the_link_path_is_never_replaced(self):
        os.makedirs(os.path.dirname(self.link))
        with open(self.link, "w") as f:
            f.write("mine\n")
        res = self.run_install()
        self.assertEqual(res.returncode, 10)
        self.assertIn("is a regular file", res.stderr)
        with open(self.link) as f:
            self.assertEqual(f.read(), "mine\n")

    def test_a_stale_symlink_is_repointed(self):
        os.makedirs(os.path.dirname(self.link))
        os.symlink("/nonexistent/rig", self.link)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(os.path.join(self.dir, "bin", "rig")))

    def test_a_profile_conflict_is_a_manual_step_and_stops_before_activate(self):
        os.makedirs(os.path.join(self.home, ".config", "rig", "profile", "repo", ".git"))
        res = self.run_install(FAKE_PROFILE_RC="3")
        self.assertEqual(res.returncode, 10)
        self.assertIn("--take local|remote", res.stderr)
        self.assertEqual(self.calls(), ["profile sync"])

    def test_a_failing_activate_fails_the_install(self):
        res = self.run_install(FAKE_ACTIVATE_RC="1")
        self.assertEqual(res.returncode, 1)

    def test_a_dash_leading_option_is_refused(self):
        res = self.run_install(options={"repo": "--upload-pack=evil"})
        self.assertEqual(res.returncode, 2)
        self.assertFalse(os.path.exists(self.dir))

    def test_a_tilde_dir_option_expands_to_home(self):
        res = self.run_install(options={"dir": "~/src/rig"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "src", "rig", "riglib", "cli.py")))

    # -- --check -------------------------------------------------------------------------------
    def test_check_reports_drift_before_install_and_is_read_only(self):
        before = tree(self.home)
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("rig checkout missing", res.stdout)
        self.assertEqual(tree(self.home), before)

    def test_check_passes_after_install_and_only_reads(self):
        self.assertEqual(self.run_install().returncode, 0)
        open(self.log, "w").close()
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.calls(), ["activate --json", "status"])
        self.assertTrue(all(call.split()[0] in ("activate", "status", "profile") for call in self.calls()))
        self.assertFalse([c for c in self.calls() if c in ("activate commit", "profile sync")])

    def test_check_sees_pending_activation_a_drifting_status_and_an_unsynced_profile(self):
        self.assertEqual(self.run_install().returncode, 0)
        res = self.run_install("--check", FAKE_ACTIVATE_CHANGES='"tmux.enabled: true"')
        self.assertEqual(res.returncode, 3)
        self.assertIn("would write 1 config switch", res.stdout)
        res = self.run_install("--check", FAKE_STATUS_RC="3")
        self.assertEqual(res.returncode, 3)
        self.assertIn("rig status reports drift", res.stdout)
        os.makedirs(os.path.join(self.home, ".config", "rig", "profile", "repo", ".git"))
        res = self.run_install("--check", FAKE_PROFILE_STATUS_RC="3")
        self.assertEqual(res.returncode, 3)
        self.assertIn("profile is not in sync", res.stdout)

    def test_check_with_activation_off_ignores_activation_and_status(self):
        self.assertEqual(self.run_install().returncode, 0)
        res = self.run_install("--check", options={"activate": False}, FAKE_STATUS_RC="3", FAKE_ACTIVATE_CHANGES='"x"')
        self.assertEqual(res.returncode, 0, res.stdout)

    def test_a_checkout_without_an_executable_rig_counts_as_missing(self):
        self.assertEqual(self.run_install().returncode, 0)
        os.chmod(os.path.join(self.dir, "bin", "rig"), 0o644)
        res = self.run_install("--check")
        self.assertEqual(res.returncode, 3)

    def test_usage(self):
        res = self.run_install("--bogus")
        self.assertEqual(res.returncode, 2)


if __name__ == "__main__":
    unittest.main()

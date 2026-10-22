"""`install.sh --check` against a scratch MEMGUARD_ROOT: exit 0 only when every installed file
equals its repo copy (polkit rule with the user substituted) and has the right mode; read-only."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(MODULE, "install.sh")

# source : target : mode, as in install.sh
FILES = [
    ("memguard_swap.py", "usr/local/lib/memguard/memguard_swap.py", 0o644),
    ("swap-drain", "usr/local/sbin/swap-drain", 0o755),
    ("memguard", "usr/local/bin/memguard", 0o755),
    ("memguard.toml", "etc/memguard/memguard.toml", 0o644),
    ("swap-drain.service", "etc/systemd/system/swap-drain.service", 0o644),
    ("swap-drain.timer", "etc/systemd/system/swap-drain.timer", 0o644),
    ("50-memguard-swap-drain.rules", "etc/polkit-1/rules.d/50-memguard-swap-drain.rules", 0o644),
]
USER = "alice"


def snapshot(root):
    return sorted(
        (os.path.relpath(os.path.join(d, f), root), os.stat(os.path.join(d, f)).st_mtime_ns)
        for d, _, files in os.walk(root)
        for f in files
    )


class InstallCheck(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def check(self, *args, root=True):
        env = dict(os.environ, MEMGUARD_USER=USER)
        if root:
            env["MEMGUARD_ROOT"] = self.root
        return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=60)

    def install_all(self):
        for src, dst, mode in FILES:
            target = os.path.join(self.root, dst)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(os.path.join(MODULE, src)) as f:
                text = f.read()
            if src.endswith(".rules"):
                text = text.replace("@USER@", USER)
            if src == "memguard.toml":
                text = text.replace('user = ""', f'user = "{USER}"')
            with open(target, "w") as f:
                f.write(text)
            os.chmod(target, mode)
        return USER

    def test_empty_root_is_drift(self):
        r = self.check("--check")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("+ " + self.root + "/etc/memguard/memguard.toml (new", r.stdout)
        self.assertNotIn("apply with", r.stdout)

    def test_matching_installation_is_ok_and_leaves_the_root_untouched(self):
        self.install_all()
        before = snapshot(self.root)
        r = self.check("--check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "memguard: nothing to change"), r.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_changed_content_is_drift_with_a_diff(self):
        self.install_all()
        with open(os.path.join(self.root, "usr/local/sbin/swap-drain"), "a") as f:
            f.write("# local edit\n")
        r = self.check("--check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("-# local edit", r.stdout)

    def test_wrong_mode_is_drift(self):
        self.install_all()
        os.chmod(os.path.join(self.root, "etc/memguard/memguard.toml"), 0o600)
        r = self.check("--check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("mode 600 -> 644", r.stdout)

    def write_stamp(self, tamper=False):
        lines = []
        for _, dst, mode in FILES:
            with open(os.path.join(self.root, dst), "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            if tamper and dst.endswith(".rules"):
                digest = "0" * 64
            lines.append(f"{mode:o} {digest} {self.root}/{dst}")
        os.makedirs(os.path.join(self.root, "etc/memguard"), exist_ok=True)
        with open(os.path.join(self.root, "etc/memguard/installed.sha256"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def hide_rules_dir(self):
        rules_dir = os.path.join(self.root, "etc/polkit-1/rules.d")
        os.chmod(rules_dir, 0)
        self.addCleanup(os.chmod, rules_dir, 0o755)

    @unittest.skipIf(os.geteuid() == 0, "root can read the hidden directory")
    def test_a_directory_the_user_cannot_read_is_vouched_for_by_the_install_stamp(self):
        self.install_all()
        self.write_stamp()
        self.hide_rules_dir()
        r = self.check("--check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "memguard: nothing to change"), r.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can read the hidden directory")
    def test_an_unreadable_directory_without_a_matching_stamp_is_drift(self):
        self.install_all()
        self.write_stamp(tamper=True)
        self.hide_rules_dir()
        stale = self.check("--check")
        os.remove(os.path.join(self.root, "etc/memguard/installed.sha256"))
        missing = self.check("--check")
        for r in (missing, stale):
            self.assertEqual(r.returncode, 3)
            self.assertIn("install stamp does not vouch for it", r.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root can read mode-000 files")
    def test_the_stamp_does_not_mask_an_unreadable_runtime_file(self):
        self.install_all()
        self.write_stamp()
        runtime = os.path.join(self.root, "usr/local/bin/memguard")
        os.chmod(runtime, 0)
        r = self.check("--check")
        self.assertEqual(r.returncode, 3)
        self.assertIn(runtime, r.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root can read mode-000 files")
    def test_the_stamp_does_not_mask_an_unreadable_polkit_rule_in_a_traversable_directory(self):
        self.install_all()
        self.write_stamp()
        rule = os.path.join(self.root, "etc/polkit-1/rules.d/50-memguard-swap-drain.rules")
        os.chmod(rule, 0)
        r = self.check("--check")
        self.assertEqual(r.returncode, 3)
        self.assertIn(rule, r.stdout)

    def stage(self, **extra_env):
        """A real (non-dry) install into the scratch root, with systemctl/runuser that fail loudly."""
        bin_dir = self.root.rstrip("/") + "-bin"
        os.makedirs(bin_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        for tool in ("systemctl", "runuser"):
            path = os.path.join(bin_dir, tool)
            with open(path, "w") as f:
                f.write(f"#!/bin/sh\necho {tool} called >&2\nexit 99\n")
            os.chmod(path, 0o755)
        extra_env.setdefault("MEMGUARD_USER", USER)
        env = dict(os.environ, MEMGUARD_ROOT=self.root, PATH=bin_dir + os.pathsep + os.environ["PATH"], **extra_env)
        return subprocess.run([INSTALL], env=env, capture_output=True, text=True, timeout=60)

    def test_a_staged_install_writes_every_file_with_its_mode_and_the_stamp(self):
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("called", r.stderr)  # systemctl/runuser are real-root only
        self.assertIn("installed " + self.root + "/etc/memguard/installed.sha256", r.stdout)
        for src, dst, mode in FILES:
            target = os.path.join(self.root, dst)
            self.assertEqual(os.stat(target).st_mode & 0o777, mode, dst)
        stamp = os.path.join(self.root, "etc/memguard/installed.sha256")
        with open(stamp) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), len(FILES))
        for line, (_, dst, mode) in zip(lines, FILES):
            recorded_mode, digest, path = line.split(" ", 2)
            self.assertEqual((recorded_mode, path), (f"{mode:o}", os.path.join(self.root, dst)))
            with open(path, "rb") as f:
                self.assertEqual(digest, hashlib.sha256(f.read()).hexdigest(), dst)
        with open(os.path.join(self.root, FILES[-1][1])) as f:
            self.assertNotIn("@USER@", f.read())
        with open(os.path.join(self.root, "etc/memguard/memguard.toml")) as f:
            self.assertNotIn('user = ""', f.read())

    def test_check_passes_after_a_staged_install_also_through_the_stamp(self):
        self.assertEqual(self.stage().returncode, 0)
        r = self.check("--check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "memguard: nothing to change"), r.stderr)
        if os.geteuid() != 0:
            self.hide_rules_dir()  # the desktop user cannot read /etc/polkit-1/rules.d: the stamp vouches
            r = self.check("--check")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "memguard: nothing to change"), r.stderr)

    def test_a_second_staged_install_changes_nothing(self):
        self.assertEqual(self.stage().returncode, 0)
        before = snapshot(self.root)
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to change", r.stdout)
        self.assertNotIn("installed ", r.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_a_staged_install_repairs_drift(self):
        self.assertEqual(self.stage().returncode, 0)
        target = os.path.join(self.root, "usr/local/sbin/swap-drain")
        with open(target, "a") as f:
            f.write("# local edit\n")
        self.assertEqual(self.check("--check").returncode, 3)
        self.assertEqual(self.stage().returncode, 0)
        self.assertEqual(self.check("--check").returncode, 0)

    @unittest.skipIf(os.geteuid() == 0, "root may install")
    def test_a_real_install_without_a_root_override_needs_root(self):
        r = subprocess.run(
            [INSTALL],
            env={k: v for k, v in os.environ.items() if k != "MEMGUARD_ROOT"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("run with sudo", r.stderr)

    def fake_bin(self, **scripts):
        bin_dir = self.root.rstrip("/") + "-fakebin"
        os.makedirs(bin_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        for name, body in scripts.items():
            path = os.path.join(bin_dir, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
        return bin_dir

    def real_dry_run(self, bin_dir, **env):
        """A non-staged --dry-run (reads the real /etc, writes nothing) with fake getent/id."""
        base = {k: v for k, v in os.environ.items() if k not in ("MEMGUARD_ROOT", "SUDO_USER", "PKEXEC_UID")}
        base["PATH"] = bin_dir + os.pathsep + base["PATH"]
        return subprocess.run([INSTALL, "--dry-run"], env={**base, **env}, capture_output=True, text=True, timeout=60)

    GETENT = (
        'case "$2" in 1234) echo "alice:x:1234:1234::/home/alice:/bin/sh" ;; *) echo "$2:x:1:1::/h:/bin/sh" ;; esac\n'
    )

    def test_sudo_user_is_the_desktop_user(self):
        r = self.real_dry_run(self.fake_bin(getent=self.GETENT), SUDO_USER="alice", PKEXEC_UID="1234")
        self.assertIn("memguard: desktop user: alice", r.stdout, r.stderr)

    def test_pkexec_uid_is_resolved_when_there_is_no_sudo_user(self):
        r = self.real_dry_run(self.fake_bin(getent=self.GETENT), PKEXEC_UID="1234")
        self.assertIn("memguard: desktop user: alice", r.stdout, r.stderr)

    def test_root_without_sudo_user_or_pkexec_uid_or_configured_user_fails(self):
        fake_id = 'case "$1" in -u) echo 0 ;; -un) echo root ;; *) exec /usr/bin/id "$@" ;; esac\n'
        r = self.real_dry_run(self.fake_bin(id=fake_id, getent=self.GETENT), SUDO_USER="root")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("memguard.toml: user is not set and no SUDO_USER/PKEXEC_UID", r.stderr)

    def test_a_staged_install_writes_the_resolved_user_into_the_installed_config_and_the_rule(self):
        self.assertEqual(self.stage(MEMGUARD_USER="alice").returncode, 0)
        with open(os.path.join(self.root, "etc/memguard/memguard.toml")) as f:
            self.assertIn('\nuser = "alice"', f.read())
        with open(os.path.join(self.root, FILES[-1][1])) as f:
            self.assertIn('subject.user === "alice"', f.read())
        self.assertEqual(self.check("--check").returncode, 0)

    def test_check_compares_against_the_rendering_for_the_resolved_user(self):
        self.assertEqual(self.stage(MEMGUARD_USER="alice").returncode, 0)
        r = subprocess.run(
            [INSTALL, "--check"],
            env=dict(os.environ, MEMGUARD_ROOT=self.root, MEMGUARD_USER="bob"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 3)
        self.assertIn('+user = "bob"', r.stdout)

    def test_unknown_flag_is_a_usage_error(self):
        self.assertEqual(self.check("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()

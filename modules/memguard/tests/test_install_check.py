"""`install.sh --check` against a scratch MEMGUARD_ROOT: exit 0 only when every installed file
equals its repo copy (polkit rule with the user substituted) and has the right mode; read-only."""

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
        env = dict(os.environ)
        if root:
            env["MEMGUARD_ROOT"] = self.root
        return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=60)

    def install_all(self):
        user = subprocess.run(
            [
                "python3",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import memguard_swap as m;"
                "print(m.load_config(sys.argv[1] + '/memguard.toml')['user'])",
                MODULE,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for src, dst, mode in FILES:
            target = os.path.join(self.root, dst)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(os.path.join(MODULE, src)) as f:
                text = f.read()
            if src.endswith(".rules"):
                text = text.replace("@USER@", user)
            with open(target, "w") as f:
                f.write(text)
            os.chmod(target, mode)
        return user

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

    def test_root_override_is_refused_for_a_real_install(self):
        r = self.check()
        self.assertEqual(r.returncode, 2)
        self.assertIn("only valid with --dry-run / --check", r.stderr)

    def test_unknown_flag_is_a_usage_error(self):
        self.assertEqual(self.check("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()

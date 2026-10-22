"""passwords-app-polkit against a fake root: the policy file is copied only when it differs."""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.module_script import ModuleError, NeedsManual  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("passwords_app_polkit_script", str(MODULE / "passwords-app-polkit"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


polkit = load_script()
POLICY = "<policyconfig><action id='io.github.alex_mextner.Passwords.unlock'/></policyconfig>\n"


class PolkitModule(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/alice"
        self.checkout = self.home / "dev/passwords-app"
        (self.checkout / "data/polkit").mkdir(parents=True)
        (self.checkout / "data/polkit/io.github.alex_mextner.Passwords.policy").write_text(POLICY)
        self.target = self.root / "usr/share/polkit-1/actions/io.github.alex_mextner.Passwords.policy"

    def module(self, options=None, *, is_root=True):
        return polkit.PasswordsAppPolkit(self.root, self.home, os.getuid(), options or {}, is_root=is_root)

    def test_missing_policy_is_unsatisfied_then_installed_with_mode_644(self):
        self.assertFalse(self.module().findings()[0].ok)
        self.module().apply()
        self.assertEqual(self.target.read_text(), POLICY)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertTrue(all(f.ok for f in self.module().findings()))

    def test_a_different_installed_policy_is_replaced_an_equal_one_is_not_touched(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("old\n")
        self.assertFalse(self.module().findings()[0].ok)
        self.module().apply()
        self.assertEqual(self.target.read_text(), POLICY)
        before = self.target.stat().st_mtime_ns
        self.module().apply()
        self.assertEqual(self.target.stat().st_mtime_ns, before)

    def test_check_does_not_write(self):
        self.module(is_root=False).findings()
        self.assertFalse(self.target.exists())

    def test_requires_root(self):
        with self.assertRaises(ModuleError):
            self.module(is_root=False).apply()

    def test_no_checkout_is_a_manual_step_and_an_unsatisfied_check(self):
        shutil.rmtree(self.checkout)
        self.assertFalse(self.module().findings()[0].ok)
        with self.assertRaisesRegex(NeedsManual, "passwords-app module first"):
            self.module().apply()

    def test_foreign_actions_symlinks_and_other_owners_are_refused(self):
        source = self.checkout / "data/polkit/io.github.alex_mextner.Passwords.policy"
        source.write_text("<policyconfig><action id='org.freedesktop.policykit.exec'/></policyconfig>")
        with self.assertRaisesRegex(ModuleError, "actions must be in"):
            self.module().apply()
        source.unlink()
        source.symlink_to(self.root / "elsewhere")
        (self.root / "elsewhere").write_text(POLICY)
        with self.assertRaises(ModuleError):
            self.module().apply()
        self.assertFalse(self.target.exists())
        source.unlink()
        source.write_text(POLICY)
        other = polkit.PasswordsAppPolkit(self.root, self.home, os.getuid() + 1, {}, is_root=True)
        with self.assertRaises(ModuleError):
            other.apply()

    def test_spaced_attributes_pkexec_rules_and_foreign_file_names_are_refused(self):
        source = self.checkout / "data/polkit/io.github.alex_mextner.Passwords.policy"
        ok = "<action id='io.github.alex_mextner.Passwords.unlock'/>"
        for text in (
            ok + '<action id = "org.freedesktop.evil"/>',
            ok + '<annotate key="org.freedesktop.policykit&#46;exec.path">/bin/bash</annotate>',
            ok + '<annotate key="org.freedesktop.policykit.imply">org.freedesktop.systemd1.manage-units</annotate>',
            '<!DOCTYPE x [<!ENTITY e "y">]>' + ok,
            ok + '<annotate key="org.freedesktop.policykit.exec.path">/bin/bash</annotate>',
        ):
            source.write_text(f"<policyconfig>{text}</policyconfig>")
            with self.assertRaises(ModuleError, msg=text):
                self.module().apply()
        source.write_text(POLICY)
        (self.checkout / "data/polkit/org.freedesktop.systemd1.policy").write_text(POLICY)
        with self.assertRaisesRegex(ModuleError, "file name"):
            self.module().apply()

    def test_options(self):
        with self.assertRaises(ModuleError):
            self.module({"nope": 1})
        with self.assertRaises(ModuleError):
            self.module({"checkout": "relative"})
        self.assertEqual(self.module({"checkout": "~/x"})._checkout, self.home / "x")


if __name__ == "__main__":
    unittest.main()

"""firefox-policy module against a fake root: policy JSON golden, merge, XPI copy, snap plug."""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.module_script import Account, ModuleError, NeedsManual  # noqa: E402
from ubuntu_tweaks.process import CommandResult, Process  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("firefox_policy_script", str(MODULE / "firefox-policy"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


fp = load_script()

def signed_zip(tag="x"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", tag)
        archive.writestr("META-INF/mozilla.rsa", "sig")
    return buffer.getvalue()


GOLDEN = """\
{
  "policies": {
    "ExtensionSettings": {
      "passwords-app@alex-mextner.github.io": {
        "installation_mode": "force_installed",
        "install_url": "file:///etc/firefox/extensions/passwords-app@alex-mextner.github.io.xpi"
      }
    }
  }
}
"""
GOLDEN_WITH_HELPER = """\
{
  "policies": {
    "ExtensionSettings": {
      "passwords-app@alex-mextner.github.io": {
        "installation_mode": "force_installed",
        "install_url": "file:///etc/firefox/extensions/passwords-app@alex-mextner.github.io.xpi"
      },
      "credentialsd-helper@iinuwa.xyz": {
        "installation_mode": "force_installed",
        "install_url": "https://github.com/linux-credentials/credentialsd/releases/download/v0.3.1/credentialsd-helper@iinuwa.xyz-0.2.0.xpi"
      }
    }
  }
}
"""
CONNECTED = "Interface Plug Slot Notes\nsystem-files firefox:etc-firefox :system-files -\n"
DISCONNECTED = "Interface Plug Slot Notes\nsystem-files firefox:etc-firefox - -\n"


class FakeSnap(Process):
    def __init__(self, listing=CONNECTED, snap=True):
        self.listing = listing
        self.snap = snap
        self.executed = []

    def query(self, argv, *, timeout=60.0, cwd=None):
        if list(argv) == ["snap", "connections", "firefox"]:
            return CommandResult(0, self.listing, "") if self.snap else CommandResult(1, "", "no such snap")
        return CommandResult(127, "", "not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        self.executed.append(tuple(argv))
        if list(argv)[:2] == ["snap", "connect"]:
            self.listing = CONNECTED


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/ultra"
        self.stage = self.home / ".local/state/ubuntu-tweaks/firefox"
        self.stage.mkdir(parents=True)
        self.xpi = self.stage / "passwords-app.xpi"
        self.xpi_bytes = signed_zip()
        self.xpi.write_bytes(self.xpi_bytes)
        self.policy = self.root / "etc/firefox/policies/policies.json"
        self.installed = self.root / "etc/firefox/extensions/passwords-app@alex-mextner.github.io.xpi"

    def module(self, machine=None, options=None, *, is_root=True):
        return fp.FirefoxPolicy(
            machine or FakeSnap(), self.root, Account("ultra", self.home, os.getuid()), options or {}, is_root=is_root
        )


class PolicyGeneration(Base):
    def test_golden_policy_on_a_fresh_machine(self):
        self.module().apply()
        self.assertEqual(self.policy.read_text(), GOLDEN)
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o644)

    def test_golden_policy_with_the_credentialsd_helper(self):
        self.module(options={"credentialsd_helper": True}).apply()
        self.assertEqual(self.policy.read_text(), GOLDEN_WITH_HELPER)

    def test_other_policies_and_extension_settings_are_kept(self):
        self.policy.parent.mkdir(parents=True)
        self.policy.write_text(
            json.dumps(
                {
                    "policies": {
                        "DisableTelemetry": True,
                        "ExtensionSettings": {
                            "*": {"blocked_install_message": "no"},
                            "other@x": {"installation_mode": "blocked"},
                        },
                    },
                    "extra": 1,
                }
            )
        )
        self.module().apply()
        document = json.loads(self.policy.read_text())
        self.assertTrue(document["policies"]["DisableTelemetry"])
        self.assertEqual(document["extra"], 1)
        settings = document["policies"]["ExtensionSettings"]
        self.assertEqual(settings["*"], {"blocked_install_message": "no"})
        self.assertEqual(settings["other@x"], {"installation_mode": "blocked"})
        self.assertEqual(settings[fp.EXTENSION_ID]["installation_mode"], "force_installed")

    def test_merge_is_idempotent(self):
        self.module().apply()
        before = self.policy.stat().st_mtime_ns
        self.module().apply()
        self.assertEqual(self.policy.stat().st_mtime_ns, before)

    def test_a_compact_but_equal_file_is_not_rewritten(self):
        self.policy.parent.mkdir(parents=True)
        self.policy.write_text(json.dumps(json.loads(GOLDEN)))
        before = self.policy.read_text()
        self.module().apply()
        self.assertEqual(self.policy.read_text(), before)

    def test_malformed_or_foreign_json_is_refused_not_overwritten(self):
        self.policy.parent.mkdir(parents=True)
        for text in ("{not json", "[1, 2]", '{"policies": []}', '{"policies": {"ExtensionSettings": 3}}'):
            self.policy.write_text(text)
            with self.assertRaises(ModuleError, msg=text):
                self.module().apply()
            self.assertEqual(self.policy.read_text(), text)


class ExtensionCopy(Base):
    def test_the_signed_xpi_is_copied_world_readable(self):
        self.module().apply()
        self.assertEqual(self.installed.read_bytes(), self.xpi_bytes)
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o644)

    def test_a_rebuilt_xpi_replaces_the_copy(self):
        self.module().apply()
        newer = signed_zip("changed")
        self.xpi.write_bytes(newer)
        self.module().apply()
        self.assertEqual(self.installed.read_bytes(), newer)

    def test_the_xpi_is_copied_before_the_policy_names_it(self):
        order = []
        original = fp.write_file
        fp.write_file = lambda path, data, mode=0o644: (order.append(Path(path).name), original(path, data, mode))[1]
        try:
            self.module().apply()
        finally:
            fp.write_file = original
        self.assertEqual(order, [self.installed.name, "policies.json"])

    def test_a_symlink_or_a_foreign_file_in_the_stage_is_refused_not_read_as_root(self):
        secret = self.root / "secret"
        secret.write_text("not for everyone")
        self.xpi.unlink()
        self.xpi.symlink_to(secret)
        with self.assertRaises(ModuleError):
            self.module().apply()
        self.assertFalse(self.installed.exists())
        self.xpi.unlink()
        self.xpi.write_bytes(b"plain text, not a zip")
        with self.assertRaisesRegex(ModuleError, "not a Mozilla-signed"):
            self.module().apply()
        self.xpi.write_bytes(signed_zip())
        with self.assertRaises(ModuleError):
            fp.FirefoxPolicy(FakeSnap(), self.root, Account("ultra", self.home, os.getuid() + 1), {}, is_root=True).apply()

    def test_nothing_staged_is_a_manual_step_and_writes_nothing(self):
        self.xpi.unlink()
        with self.assertRaisesRegex(NeedsManual, "firefox module first"):
            self.module().apply()
        self.assertFalse(self.policy.exists())
        findings = self.module().findings()
        self.assertTrue([f for f in findings if f.manual])


class Check(Base):
    def test_fresh_machine_is_unsatisfied_and_converges(self):
        module = self.module()
        self.assertFalse(all(f.ok for f in module.findings()))
        module.apply()
        self.assertEqual([f.line() for f in module.findings() if not f.ok], [])

    def test_a_different_installed_copy_is_drift(self):
        module = self.module()
        module.apply()
        self.installed.write_bytes(b"tampered")
        self.assertFalse(all(f.ok for f in module.findings()))

    def test_a_policy_entry_with_another_url_is_drift(self):
        module = self.module()
        module.apply()
        document = json.loads(self.policy.read_text())
        document["policies"]["ExtensionSettings"][fp.EXTENSION_ID]["install_url"] = "file:///tmp/other.xpi"
        self.policy.write_text(json.dumps(document))
        self.assertFalse(all(f.ok for f in module.findings()))

    def test_check_never_writes(self):
        self.module(is_root=False).findings()
        self.assertFalse(self.policy.exists() or self.installed.exists())


class SnapPlug(Base):
    def test_a_disconnected_plug_is_drift_and_install_connects_it(self):
        machine = FakeSnap(DISCONNECTED)
        module = self.module(machine)
        self.assertTrue([f for f in module.findings() if not f.ok and "etc-firefox" in f.detail])
        module.apply()
        self.assertEqual(machine.executed, [("snap", "connect", "firefox:etc-firefox", ":system-files")])
        self.assertTrue(all(f.ok for f in module.findings()))

    def test_no_snap_means_nothing_to_connect(self):
        machine = FakeSnap(snap=False)
        self.module(machine).apply()
        self.assertEqual(machine.executed, [])


class Options(Base):
    def test_validation(self):
        for options in ({"nope": 1}, {"credentialsd_helper": "yes"}):
            with self.assertRaises(ModuleError, msg=options):
                self.module(options=options)

    def test_requires_root(self):
        with self.assertRaises(ModuleError):
            self.module(is_root=False).apply()


if __name__ == "__main__":
    unittest.main()

"""firefox-autologin against a temporary HOME with a fake Firefox that activates the extension on start."""

import importlib.machinery
import importlib.util
import json
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
    loader = importlib.machinery.SourceFileLoader("firefox_autologin_script", str(MODULE / "firefox-autologin"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


fa = load_script()
URL = "https://accounts.firefox.com/signin"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += 1


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.spawned = []
        self.clock = Clock()
        self.activate_on_start = True
        self.env = {"WAYLAND_DISPLAY": "wayland-0"}
        self.tools = {"firefox": "/usr/bin/firefox"}
        self.policy = self.home / "policies.json"
        self.policy.write_text('{"policies": {"ExtensionSettings": {"%s": {}}}}' % fa.EXTENSION_ID)

    def registry(self, root=".mozilla/firefox", *, active=True, addon=fa.EXTENSION_ID):
        path = self.home / root / "abc.default/extensions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"addons": [{"id": "other@x", "active": True}, {"id": addon, "active": active}]}))

    def spawn(self, argv):
        self.spawned.append(list(argv))
        if len(argv) == 1 and self.activate_on_start:
            self.registry()  # Firefox applied the policy while starting

    def module(self, options=None, *, is_root=False):
        return fa.FirefoxAutologin(
            self.home,
            self.env,
            options or {},
            is_root=is_root,
            which=self.tools.get,
            spawn=self.spawn,
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
            policy_file=self.policy,
        )

    @property
    def marker(self):
        return self.home / ".local/state/ubuntu-tweaks/firefox-signin-opened"


class Detection(Base):
    def test_active_extension_in_the_deb_or_the_snap_profile(self):
        self.assertFalse(fa.extension_active(self.home))
        self.registry(".mozilla/firefox")
        self.assertTrue(fa.extension_active(self.home))
        shutil.rmtree(self.home / ".mozilla")
        self.registry("snap/firefox/common/.mozilla/firefox")
        self.assertTrue(fa.extension_active(self.home))

    def test_inactive_other_or_unreadable_registries_do_not_count(self):
        self.registry(active=False)
        self.assertFalse(fa.extension_active(self.home))
        self.registry(addon="someone-else@x")
        self.assertFalse(fa.extension_active(self.home))
        (self.home / ".mozilla/firefox/abc.default/extensions.json").write_text("{broken")
        self.assertFalse(fa.extension_active(self.home))


class Check(Base):
    def test_fresh_machine_is_unsatisfied_and_says_what_automatic_means(self):
        findings = self.module().findings()
        self.assertEqual([f.ok for f in findings], [True, False, False, True])
        self.assertIn("never stored by ubuntu-tweaks", findings[3].detail)

    def test_satisfied_with_extension_and_marker(self):
        self.registry()
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("{}")
        self.assertTrue(all(f.ok for f in self.module().findings()))

    def test_check_starts_nothing(self):
        self.module().findings()
        self.assertEqual(self.spawned, [])


class Install(Base):
    def test_starts_firefox_waits_for_the_extension_then_opens_the_page_once(self):
        self.module().apply()
        self.assertEqual(self.spawned, [["/usr/bin/firefox"], ["/usr/bin/firefox", "--new-tab", URL]])
        self.assertEqual(json.loads(self.marker.read_text()), {"url": URL, "extension": fa.EXTENSION_ID})
        self.assertTrue(all(f.ok for f in self.module().findings()))

    def test_with_the_extension_already_active_only_the_page_is_opened(self):
        self.registry()
        self.module().apply()
        self.assertEqual(self.spawned, [["/usr/bin/firefox", "--new-tab", URL]])

    def test_an_extension_that_never_activates_is_a_manual_step_and_leaves_no_marker(self):
        self.activate_on_start = False
        with self.assertRaisesRegex(NeedsManual, "did not activate.*firefox-policy"):
            self.module({"wait_seconds": 10}).apply()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.spawned, [["/usr/bin/firefox"]])  # the sign-in page was not opened
        self.assertEqual(self.clock.slept, 5)
        self.assertFalse(self.module().findings()[2].ok)

    def test_without_the_policy_nothing_is_launched(self):
        self.policy.write_text("{}")
        with self.assertRaisesRegex(NeedsManual, "firefox-policy module first"):
            self.module().apply()
        self.assertEqual(self.spawned, [])

    def test_no_graphical_session_no_firefox_and_root_are_refused(self):
        self.env.clear()
        with self.assertRaisesRegex(NeedsManual, "graphical"):
            self.module().apply()
        self.env["DISPLAY"] = ":0"
        del self.tools["firefox"]
        with self.assertRaisesRegex(NeedsManual, "firefox is not installed"):
            self.module().apply()
        self.tools["firefox"] = "/usr/bin/firefox"
        with self.assertRaises(ModuleError):
            self.module(is_root=True).apply()
        self.assertEqual(self.spawned, [])

    def test_a_second_run_changes_nothing(self):
        module = self.module()
        module.apply()
        self.spawned.clear()
        module.apply()
        self.assertEqual(self.spawned, [])

    def test_a_custom_url_is_used(self):
        self.registry()
        self.module({"url": "https://example.org/login"}).apply()
        self.assertEqual(self.spawned[-1][-1], "https://example.org/login")


class Options(Base):
    def test_validation(self):
        for options in (
            {"nope": 1},
            {"url": "http://insecure"},
            {"wait_seconds": 0},
            {"wait_seconds": True},
            {"wait_seconds": "9"},
        ):
            with self.assertRaises(ModuleError, msg=options):
                self.module(options)


if __name__ == "__main__":
    unittest.main()

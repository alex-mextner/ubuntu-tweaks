"""install.sh --check / install against a scratch APPLE_HID_ROOT with fake make/strip/depmod/modprobe."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(MODULE, "install.sh")
NAME = "livepatch_hid_battery_offset"
OLD = "7.0.0-38-generic"
FIXED = "7.3.1-1-generic"

FAKE_MAKE = f"""#!/bin/sh
echo "make $*" >>"$FAKE_LOG"
while [ "$1" != -C ]; do shift; done
echo fake-ko >"$2/{NAME}.ko"
"""
FAKE_STRIP = '#!/bin/sh\necho "strip $*" >>"$FAKE_LOG"\n'
FAKE_DEPMOD = '#!/bin/sh\necho "depmod $*" >>"$FAKE_LOG"\n'
FAKE_MODPROBE = f"""#!/bin/sh
echo "modprobe $*" >>"$FAKE_LOG"
mkdir -p "$APPLE_HID_ROOT/sys/kernel/livepatch/{NAME}"
echo 1 >"$APPLE_HID_ROOT/sys/kernel/livepatch/{NAME}/enabled"
mkdir -p "$APPLE_HID_ROOT/sys/module/{NAME}"
"""
FAKE_RMMOD = f"""#!/bin/sh
echo "rmmod $*" >>"$FAKE_LOG"
rm -rf "$APPLE_HID_ROOT/sys/kernel/livepatch/{NAME}" "$APPLE_HID_ROOT/sys/module/{NAME}"
"""


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin = os.path.join(self.root, "fakebin")
        os.makedirs(self.bin)
        for name, body in (
            ("make", FAKE_MAKE),
            ("strip", FAKE_STRIP),
            ("depmod", FAKE_DEPMOD),
            ("modprobe", FAKE_MODPROBE),
            ("rmmod", FAKE_RMMOD),
        ):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.root, "log")

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def run_install(self, *args, kver=OLD):
        env = dict(
            os.environ,
            APPLE_HID_ROOT=self.root,
            APPLE_HID_KVER=kver,
            FAKE_LOG=self.log,
            PATH=self.bin + os.pathsep + os.environ["PATH"],
        )
        return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().splitlines()

    def kernel(self, version, *, headers=True, ko=False):
        os.makedirs(self.path("lib/modules", version, "updates"))
        if headers:
            os.makedirs(self.path("lib/modules", version, "build"))
        if ko:
            with open(self.path("lib/modules", version, "updates", NAME + ".ko"), "w") as f:
                f.write("ko")

    def conf(self, text=NAME + "\n"):
        os.makedirs(self.path("etc/modules-load.d"), exist_ok=True)
        with open(self.path("etc/modules-load.d/livepatch-hid-battery-offset.conf"), "w") as f:
            f.write(text)

    def live(self, value="1"):
        os.makedirs(self.path("sys/kernel/livepatch", NAME), exist_ok=True)
        with open(self.path("sys/kernel/livepatch", NAME, "enabled"), "w") as f:
            f.write(value + "\n")

    def test_check_on_an_empty_machine_lists_every_missing_piece(self):
        self.kernel(OLD, headers=False)
        r = self.run_install("--check")
        self.assertEqual(r.returncode, 3)
        for text in ("updates/" + NAME + ".ko is missing", "modules-load.d", "is not loaded and enabled"):
            self.assertIn(text, r.stdout)

    def test_check_passes_when_installed_conf_and_enabled(self):
        self.kernel(OLD, ko=True)
        self.conf()
        self.live()
        r = self.run_install("--check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "apple-hid-battery: ok for " + OLD), r.stderr)
        self.assertEqual(self.calls(), [])

    def test_check_fails_when_the_livepatch_is_disabled_or_the_conf_names_something_else(self):
        self.kernel(OLD, ko=True)
        self.conf("other\n")
        self.live("0")
        r = self.run_install("--check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("does not name", r.stdout)
        self.assertIn("not loaded and enabled", r.stdout)

    def test_check_on_a_fixed_kernel_wants_its_module_gone(self):
        self.kernel(FIXED)
        self.assertEqual(self.run_install("--check", kver=FIXED).returncode, 0)
        self.kernel("7.0.0-15-generic", ko=True)
        self.conf()
        self.assertEqual(self.run_install("--check", kver=FIXED).returncode, 0)
        with open(self.path("lib/modules", FIXED, "updates", NAME + ".ko"), "w") as f:
            f.write("ko")
        r = self.run_install("--check", kver=FIXED)
        self.assertEqual(r.returncode, 3)
        self.assertIn("upstream fix", r.stdout)

    def test_install_builds_only_kernels_with_headers_and_missing_module_then_is_idempotent(self):
        self.kernel(OLD)
        self.kernel("7.0.0-15-generic", ko=True)
        self.kernel("7.0.0-14-generic", headers=False)

        first = self.run_install()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(os.path.exists(self.path("lib/modules", OLD, "updates", NAME + ".ko")))
        self.assertIn("skipped 7.0.0-14-generic: no kernel headers", first.stdout)
        self.assertEqual(sum(c.startswith("make ") for c in self.calls()), 1)
        self.assertIn("depmod -a " + OLD, self.calls())
        self.assertIn("modprobe " + NAME, self.calls())
        self.assertEqual(self.run_install("--check").returncode, 0)

        os.remove(self.log)
        second = self.run_install()
        self.assertEqual(second.returncode, 0)
        self.assertTrue(second.stdout.endswith("apple-hid-battery: nothing to change\n"), second.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_running_kernel_without_headers_is_an_error(self):
        self.kernel(OLD, headers=False)
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no headers", r.stderr)

    def test_install_on_a_fixed_kernel_removes_the_module_and_the_conf(self):
        self.kernel(FIXED, ko=True)
        self.conf()
        r = self.run_install(kver=FIXED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.path("lib/modules", FIXED, "updates", NAME + ".ko")))
        self.assertFalse(os.path.exists(self.path("etc/modules-load.d/livepatch-hid-battery-offset.conf")))
        self.assertNotIn("modprobe " + NAME, self.calls())
        self.assertEqual(self.run_install("--check", kver=FIXED).returncode, 0)

    def test_unknown_flag_is_a_usage_error(self):
        self.assertEqual(self.run_install("--bogus").returncode, 2)

    def test_a_loaded_but_disabled_livepatch_is_unloaded_before_modprobe(self):
        self.kernel(OLD, ko=True)
        self.conf()
        os.makedirs(self.path("sys/module", NAME))  # loaded; its livepatch directory is gone once disabled
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertLess(calls.index("rmmod " + NAME), calls.index("modprobe " + NAME))
        self.assertEqual(self.run_install("--check").returncode, 0)

    def test_writing_the_conf_refreshes_the_index_even_when_the_module_is_already_enabled(self):
        self.kernel(OLD, ko=True)
        self.live()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("depmod -a " + OLD, self.calls())
        self.assertNotIn("modprobe " + NAME, self.calls())

    def test_a_fixed_running_kernel_never_gets_the_running_kernel_index_refresh(self):
        self.kernel(FIXED)
        self.kernel(OLD, ko=True)
        r = self.run_install(kver=FIXED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("depmod -a " + FIXED, self.calls())

    def test_a_dropped_in_module_gets_a_module_index_before_modprobe(self):
        self.kernel(OLD, ko=True)
        self.conf()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertLess(calls.index("depmod -a " + OLD), calls.index("modprobe " + NAME))

    def test_the_conf_stays_while_any_kernel_still_carries_the_module(self):
        self.kernel(FIXED, ko=True)
        self.kernel(OLD, ko=True)
        self.conf()
        self.live()
        r = self.run_install(kver=FIXED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.path("lib/modules", FIXED, "updates", NAME + ".ko")))
        self.assertTrue(os.path.exists(self.path("etc/modules-load.d/livepatch-hid-battery-offset.conf")))
        self.assertIn("depmod -a " + FIXED, self.calls())

    def test_the_other_kernels_are_still_built_when_the_running_one_has_no_headers(self):
        self.kernel("7.0.0-15-generic")
        self.kernel(OLD, headers=False)
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(self.path("lib/modules/7.0.0-15-generic/updates", NAME + ".ko")))


if __name__ == "__main__":
    unittest.main()

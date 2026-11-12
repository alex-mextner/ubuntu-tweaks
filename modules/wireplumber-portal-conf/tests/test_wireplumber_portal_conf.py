"""wireplumber-portal-conf against a staged root (WIREPLUMBER_PORTAL_CONF_ROOT): no root and no real /usr/local needed."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import UTC, datetime
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "wireplumber-portal-conf"

LOCAL_CONFS = "usr/local/share/wireplumber/wireplumber.conf.d"
SYSTEM_CONFS = "usr/share/wireplumber/wireplumber.conf.d"
ETC_CONFS = "etc/wireplumber/wireplumber.conf.d"
XDG_ETC_CONFS = "etc/xdg/wireplumber/wireplumber.conf.d"
MULTIARCH = "x86_64-linux-gnu"
WP_MODULES = f"usr/lib/{MULTIARCH}/wireplumber-0.5"
PW_MODULES = f"usr/lib/{MULTIARCH}/pipewire-0.3"
PORTAL_CONF = f"{LOCAL_CONFS}/10-xdg-desktop-portal.conf"

# The file the hand-built xdg-desktop-portal install left behind (trimmed to what matters).
PORTAL_TEXT = """\
# SPDX-License-Identifier: LGPL-2.1-or-later
wireplumber.profiles = {
  base = {
    support.portal-permissionstore = disabled
    script.client.access-portal = disabled
  }
  main = {
    policy.xdg-desktop-portal = required
  }
}

wireplumber.components = [
  {
    name = libxdg-desktop-portal, type = module
    provides = support.xdg-desktop-portal
    requires = [ support.dbus ]
  }

  # Policy to enabled xdg-desktop-portal alongside the standard policy
  {
    type = virtual, provides = policy.xdg-desktop-portal
    requires = [ policy.standard,
                 support.xdg-desktop-portal ]
  }
]
"""


def component_conf(name, kind="module"):
    return f"wireplumber.components = [\n  {{ name = {name}, type = {kind} }}\n]\n"


def load_script():
    loader = importlib.machinery.SourceFileLoader("wireplumber_portal_conf", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    loader.exec_module(module)
    return module


class Staged(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.root.mkdir()
        # the module directory exists (as on a real machine), so the messages can name it
        self.put(f"{WP_MODULES}/libwireplumber-module-lua-scripting.so", "")

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_script(self, command):
        env = {
            **os.environ,
            "WIREPLUMBER_PORTAL_CONF_ROOT": str(self.root),
            "WIREPLUMBER_PORTAL_CONF_MULTIARCH": MULTIARCH,
        }
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True, timeout=60
        )

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class ConfPresentModuleMissing(Staged):
    """The incident: the conf requires libxdg-desktop-portal, wireplumber only looks in its own module directory."""

    def setUp(self):
        super().setUp()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        # where the hand-built install put the module: wireplumber never looks there
        self.put("usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so", "elf")

    def test_check_reports_drift_naming_the_conf_and_the_module(self):
        result = self.run_script("check")
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertIn("10-xdg-desktop-portal.conf", result.stdout)
        self.assertIn("libxdg-desktop-portal", result.stdout)

    def test_plan_says_it_renames_and_changes_nothing(self):
        before = self.tree()
        result = self.run_script("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("10-xdg-desktop-portal.conf.disabled", result.stdout)
        self.assertEqual(self.tree(), before)

    def test_install_retires_the_conf_and_keeps_every_byte(self):
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((self.root / PORTAL_CONF).exists())
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled").read_bytes(), PORTAL_TEXT.encode())
        self.assertTrue(
            (self.root / "usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so").exists()
        )
        self.assertEqual(self.run_script("check").returncode, 0)

    def test_second_install_changes_nothing(self):
        self.assertEqual(self.run_script("install").returncode, 0)
        after_first = self.tree()
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to change", result.stdout)
        self.assertEqual(self.tree(), after_first)
        self.assertEqual(self.run_script("plan").stdout.strip(), "nothing to change")

    def test_a_different_retired_file_already_there_is_never_overwritten(self):
        self.put(f"{PORTAL_CONF}.disabled", "an older copy\n")
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled").read_text(), "an older copy\n")
        retired = [p for p in self.tree() if p.startswith(f"{PORTAL_CONF}.disabled.")]
        self.assertEqual(len(retired), 1, self.tree())
        self.assertEqual((self.root / retired[0]).read_bytes(), PORTAL_TEXT.encode())
        self.assertFalse((self.root / PORTAL_CONF).exists())

    def test_an_identical_retired_file_already_there_is_not_duplicated(self):
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertFalse((self.root / PORTAL_CONF).exists())
        self.assertEqual([p for p in self.tree() if ".disabled" in p], [f"{PORTAL_CONF}.disabled"])


class AlreadyRetired(Staged):
    def test_only_the_disabled_copy_means_no_drift_and_no_change(self):
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertEqual(self.run_script("plan").stdout.strip(), "nothing to change")
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to change", result.stdout)
        self.assertEqual(self.tree(), before)

    def test_a_machine_without_the_conf_is_clean(self):
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertIn("nothing to change", self.run_script("install").stdout)


class RetiredModuleAppears(Staged):
    """A retired conf must not stay retired for ever: once the module it needs is where wireplumber looks, `install`
    renames it back (never over a file in the way) and `check` reports the drift until then."""

    def setUp(self):
        super().setUp()
        self.module_so = f"{WP_MODULES}/libxdg-desktop-portal.so"

    def retire_then_install_the_module(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual(self.run_script("check").returncode, 0)
        self.put(self.module_so, "elf")

    def test_retire_then_module_appears_then_restore_then_second_run_changes_nothing(self):
        self.retire_then_install_the_module()

        drift = self.run_script("check")
        self.assertEqual(drift.returncode, 3, drift.stderr + drift.stdout)
        self.assertIn("10-xdg-desktop-portal.conf", drift.stdout)
        self.assertIn("libxdg-desktop-portal", drift.stdout)
        self.assertNotIn("manual", drift.stdout)

        before = self.tree()
        plan = self.run_script("plan")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn(
            "rename /usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf.disabled", plan.stdout
        )
        self.assertEqual(self.tree(), before)

        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("restored", result.stdout)
        self.assertEqual((self.root / PORTAL_CONF).read_bytes(), PORTAL_TEXT.encode())
        self.assertEqual([p for p in self.tree() if ".disabled" in p], [])
        self.assertEqual(self.run_script("check").returncode, 0)

        after_first = self.tree()
        again = self.run_script("install")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("nothing to change", again.stdout)
        self.assertEqual(self.tree(), after_first)
        self.assertEqual(self.run_script("plan").stdout.strip(), "nothing to change")

    def test_restoring_then_losing_the_module_again_retires_again(self):
        self.retire_then_install_the_module()
        self.assertEqual(self.run_script("install").returncode, 0)
        (self.root / self.module_so).unlink()
        self.assertEqual(self.run_script("check").returncode, 3)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled").read_bytes(), PORTAL_TEXT.encode())
        self.assertFalse((self.root / PORTAL_CONF).exists())

    def test_a_conf_in_the_way_is_never_overwritten_and_ends_in_needs_manual(self):
        self.retire_then_install_the_module()
        self.put(PORTAL_CONF, "# the owner's own file\n")
        before = self.tree()

        check = self.run_script("check")
        self.assertEqual(check.returncode, 3, check.stderr + check.stdout)
        self.assertIn("manual", check.stdout)
        self.assertIn("10-xdg-desktop-portal.conf", check.stdout)
        self.assertIn("needs manual", self.run_script("plan").stdout)

        result = self.run_script("install")
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn("needs manual", result.stdout)
        self.assertIn("10-xdg-desktop-portal.conf", result.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.root / PORTAL_CONF).read_text(), "# the owner's own file\n")
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled").read_bytes(), PORTAL_TEXT.encode())

    def test_a_dangling_symlink_in_the_way_is_not_replaced(self):
        self.retire_then_install_the_module()
        (self.root / PORTAL_CONF).symlink_to("gone")
        result = self.run_script("install")
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertEqual(os.readlink(self.root / PORTAL_CONF), "gone")
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled").read_bytes(), PORTAL_TEXT.encode())

    def test_the_same_bytes_already_live_are_not_drift(self):
        self.retire_then_install_the_module()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertEqual(self.run_script("plan").stdout.strip(), "nothing to change")
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_a_stamped_copy_alone_is_restored(self):
        self.put(self.module_so, "elf")
        self.put(f"{PORTAL_CONF}.disabled.20261110T080000Z", PORTAL_TEXT)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual((self.root / PORTAL_CONF).read_bytes(), PORTAL_TEXT.encode())
        self.assertEqual([p for p in self.tree() if ".disabled" in p], [])

    def test_identical_copies_restore_one_and_keep_the_other(self):
        self.put(self.module_so, "elf")
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        self.put(f"{PORTAL_CONF}.disabled.20261110T080000Z", PORTAL_TEXT)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual((self.root / PORTAL_CONF).read_bytes(), PORTAL_TEXT.encode())
        self.assertEqual([p for p in self.tree() if ".disabled" in p], [f"{PORTAL_CONF}.disabled.20261110T080000Z"])
        self.assertEqual(self.run_script("check").returncode, 0)

    def test_different_copies_that_could_each_be_restored_are_left_for_the_owner(self):
        self.put(self.module_so, "elf")
        other = PORTAL_TEXT.replace("policy.xdg-desktop-portal = required", "policy.xdg-desktop-portal = optional")
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        self.put(f"{PORTAL_CONF}.disabled.20261110T080000Z", other)
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 3)
        result = self.run_script("install")
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn("10-xdg-desktop-portal.conf.disabled.20261110T080000Z", result.stdout)
        self.assertEqual(self.tree(), before)

    def test_a_copy_whose_module_is_still_missing_stays_retired(self):
        self.put(self.module_so, "elf")
        two = PORTAL_TEXT.replace(
            "requires = [ support.dbus ]", "requires = [ support.dbus ]\n  }\n  {\n    name = libother, type = module"
        )
        self.put(f"{PORTAL_CONF}.disabled", two)
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertIn("nothing to change", self.run_script("install").stdout)
        self.assertEqual(self.tree(), before)

    def test_a_copy_of_a_conf_that_is_not_ours_is_not_restored(self):
        self.put(self.module_so, "elf")
        self.put(f"{LOCAL_CONFS}/20-other.conf.disabled", component_conf("libxdg-desktop-portal"))
        self.put(f"{SYSTEM_CONFS}/10-xdg-desktop-portal.conf.disabled", PORTAL_TEXT)
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_a_symlink_named_like_a_copy_is_not_restored(self):
        self.put(self.module_so, "elf")
        self.put("elsewhere/portal.conf", PORTAL_TEXT)
        (self.root / LOCAL_CONFS).mkdir(parents=True)
        (self.root / f"{PORTAL_CONF}.disabled").symlink_to(self.root / "elsewhere/portal.conf")
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertFalse(os.path.lexists(self.root / PORTAL_CONF))

    def test_a_pipewire_module_that_appears_restores_too(self):
        text = component_conf("libportal-pw", "pw-module")
        self.put(f"{LOCAL_CONFS}/10-xdg-desktop-portal.conf.disabled", text)
        self.put(f"{PW_MODULES}/libportal-pw.so", "elf")
        self.assertEqual(self.run_script("check").returncode, 3)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual((self.root / PORTAL_CONF).read_text(), text)


class ConfPresentModulePresent(Staged):
    def test_a_conf_whose_module_wireplumber_finds_is_not_touched(self):
        self.put(f"{WP_MODULES}/libxdg-desktop-portal.so", "elf")  # a proper (packaged) install
        self.put(PORTAL_CONF, PORTAL_TEXT)
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertEqual(self.run_script("plan").stdout.strip(), "nothing to change")
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to change", result.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.root / PORTAL_CONF).read_text(), PORTAL_TEXT)

    def test_a_module_without_the_so_suffix_does_not_count(self):
        # wireplumber loads <dir>/<name>.so (the incident's own module is libxdg-desktop-portal.so)
        self.put(f"{WP_MODULES}/libxdg-desktop-portal", "elf")
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.assertEqual(self.run_script("check").returncode, 3)

    def test_the_pipewire_directory_serves_pw_module_components_only(self):
        self.put(f"{PW_MODULES}/libpipewire-module-rt.so", "")
        self.put(f"{ETC_CONFS}/20-rt.conf", component_conf("libpipewire-module-rt", "pw-module"))
        self.assertEqual(self.run_script("check").returncode, 0)
        # the same component with its module missing is drift: the spelling wireplumber's own wireplumber.conf uses
        self.put(f"{ETC_CONFS}/21-gone.conf", component_conf("libpipewire-module-gone", "pw-module"))
        self.assertEqual(self.run_script("check").returncode, 3)

    def test_a_module_component_is_looked_up_in_the_wireplumber_directory_only(self):
        # wireplumber loads `type = module` from its own directory only (strace-verified in the incident)
        self.put(f"{PW_MODULES}/libxdg-desktop-portal.so", "")
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.assertEqual(self.run_script("check").returncode, 3)

    def test_only_the_native_architecture_directory_counts(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        for wrong in ("usr/lib/wireplumber-0.5", "usr/lib64/wireplumber-0.5", "usr/lib/i386-linux-gnu/wireplumber-0.5"):
            with self.subTest(wrong):
                path = self.put(f"{wrong}/libxdg-desktop-portal.so", "elf")
                self.assertEqual(self.run_script("check").returncode, 3)
                path.unlink()

    def test_an_absolute_module_path_is_a_path_of_its_own(self):
        absolute = "/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so"
        self.put(PORTAL_CONF, component_conf(absolute))
        self.assertEqual(self.run_script("check").returncode, 3)  # nothing at that path yet
        self.put(absolute.lstrip("/"), "elf")  # the owner fixed it by hand: the conf is theirs now
        before = self.tree()
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertIn("nothing to change", self.run_script("install").stdout)
        self.assertEqual(self.tree(), before)

    def test_a_name_masked_by_a_higher_priority_directory_is_not_read(self):
        # an empty /etc fragment of the same name masks the packaged one: wireplumber never loads the broken one
        self.put(f"{SYSTEM_CONFS}/40-distro.conf", component_conf("libmissing"))
        self.assertEqual(self.run_script("check").returncode, 3)
        self.put(f"{ETC_CONFS}/40-distro.conf", "")
        self.assertEqual(self.run_script("check").returncode, 0)


class ConfsTheModuleMayNotRetire(Staged):
    def assert_reported_and_left_alone(self, relative):
        path = self.put(relative, component_conf("libmissing"))
        before = path.read_bytes()
        check = self.run_script("check")
        self.assertEqual(check.returncode, 3, check.stderr)
        self.assertIn(path.name, check.stdout)
        self.assertIn("libmissing", check.stdout)
        install = self.run_script("install")
        self.assertEqual(install.returncode, 10, install.stderr + install.stdout)
        self.assertIn("needs manual", install.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_etc_conf(self):
        self.assert_reported_and_left_alone(f"{ETC_CONFS}/30-mine.conf")

    def test_xdg_etc_conf(self):
        self.assert_reported_and_left_alone(f"{XDG_ETC_CONFS}/30-mine.conf")

    def test_distro_conf(self):
        self.assert_reported_and_left_alone(f"{SYSTEM_CONFS}/40-distro.conf")

    def test_other_usr_local_conf(self):
        self.assert_reported_and_left_alone(f"{LOCAL_CONFS}/50-other.conf")

    def test_the_retired_name_is_only_retired_in_the_usr_local_directory(self):
        # upstream's own install name: in a package-owned or admin directory it is not ours to rename, even broken
        for directory in (SYSTEM_CONFS, ETC_CONFS, XDG_ETC_CONFS):
            with self.subTest(directory):
                path = self.put(f"{directory}/10-xdg-desktop-portal.conf", PORTAL_TEXT)
                install = self.run_script("install")
                self.assertEqual(install.returncode, 10, install.stderr + install.stdout)
                self.assertTrue(path.exists())
                self.assertFalse(Path(f"{path}.disabled").exists())
                path.unlink()

    def test_the_retirable_conf_is_retired_before_the_manual_remainder_is_reported(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.put(f"{ETC_CONFS}/30-mine.conf", component_conf("libmissing"))
        install = self.run_script("install")
        self.assertEqual(install.returncode, 10, install.stderr + install.stdout)
        self.assertFalse((self.root / PORTAL_CONF).exists())
        self.assertTrue((self.root / f"{PORTAL_CONF}.disabled").exists())
        self.assertTrue((self.root / f"{ETC_CONFS}/30-mine.conf").exists())

    def test_files_that_are_not_conf_are_ignored(self):
        self.put(f"{ETC_CONFS}/30-mine.conf.disabled", component_conf("libmissing"))
        self.put(f"{ETC_CONFS}/README", component_conf("libmissing"))
        self.assertEqual(self.run_script("check").returncode, 0)


class Parser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def modules(self, text):
        return self.script.required_modules(text)

    def test_incident_conf(self):
        self.assertEqual(self.modules(PORTAL_TEXT), [("module", "libxdg-desktop-portal")])

    def test_nested_arguments_comments_quotes_and_separators(self):
        text = """
        # name = libcommented, type = module
        wireplumber.components = [
          { name = "libquoted", type = module, arguments = { nested = { deep = 1 } } }
          { type: pw-module, name: libpipewire-module-x }
          { name = libvirtual, type = virtual }
          { name = libscript, type = script/lua }
        ]
        """
        self.assertEqual(self.modules(text), [("module", "libquoted"), ("pw-module", "libpipewire-module-x")])

    def test_only_elements_of_the_components_array_count(self):
        text = """
        wireplumber.components.rules = [
          { matches = [ { name = "~libpipewire-module-*", type = "pw-module" } ], actions = { merge = { x = 1 } } }
        ]
        wireplumber.profiles = { main = { name = libprofile, type = module } }
        wireplumber.settings = { name = libsetting, type = module }
        wireplumber.components = [ { name = libreal, type = module } ]
        """
        self.assertEqual(self.modules(text), [("module", "libreal")])

    def test_no_components(self):
        self.assertEqual(self.modules("wireplumber.profiles = { main = { a = required } }\n"), [])
        self.assertEqual(self.modules(""), [])


class InProcess(Staged):
    """What a subprocess cannot do: pin the clock."""

    def retire_at(self, stamp):
        script = load_script()
        module = script.WireplumberPortalConf(self.root, is_root=True, multiarch=MULTIARCH, clock=stamp)
        with contextlib.redirect_stdout(io.StringIO()):
            module.apply()

    def test_a_stamped_copy_that_exists_is_never_overwritten_and_no_byte_is_lost(self):
        stamp = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.put(f"{PORTAL_CONF}.disabled", "an older copy\n")
        self.put(f"{PORTAL_CONF}.disabled.20261111T120000Z", "a copy retired this very second\n")
        self.retire_at(stamp)
        self.assertFalse((self.root / PORTAL_CONF).exists())
        kept = {
            name: (self.root / name).read_bytes() for name in self.tree() if name.startswith(f"{PORTAL_CONF}.disabled")
        }
        self.assertEqual(
            sorted(kept.values()),
            sorted([b"an older copy\n", b"a copy retired this very second\n", PORTAL_TEXT.encode()]),
        )
        self.assertEqual(self.run_script("check").returncode, 0)

    def test_a_conf_equal_to_an_earlier_stamped_copy_is_just_the_duplicate(self):
        # B was retired on day 2 (stamped, `.disabled` holds A); on day 3 B comes back: no third copy
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.put(f"{PORTAL_CONF}.disabled", "an older copy\n")
        self.put(f"{PORTAL_CONF}.disabled.20261110T080000Z", PORTAL_TEXT)
        self.retire_at(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
        self.assertFalse((self.root / PORTAL_CONF).exists())
        self.assertEqual(
            sorted(name for name in self.tree() if ".disabled" in name),
            [f"{PORTAL_CONF}.disabled", f"{PORTAL_CONF}.disabled.20261110T080000Z"],
        )

    def test_a_dangling_symlink_in_the_way_is_not_overwritten(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        (self.root / f"{PORTAL_CONF}.disabled").symlink_to("gone")
        self.retire_at(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
        self.assertEqual(os.readlink(self.root / f"{PORTAL_CONF}.disabled"), "gone")
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled.20261111T120000Z").read_bytes(), PORTAL_TEXT.encode())

    def test_a_symlinked_copy_pointing_back_at_the_live_conf_is_not_a_copy(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        (self.root / f"{PORTAL_CONF}.disabled").symlink_to("10-xdg-desktop-portal.conf")
        self.retire_at(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
        self.assertFalse((self.root / PORTAL_CONF).exists())
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled.20261111T120000Z").read_bytes(), PORTAL_TEXT.encode())

    def test_a_non_root_install_is_refused_before_anything_is_read_or_written(self):
        script = load_script()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        before = self.tree()
        with self.assertRaises(script.ModuleError):
            script.WireplumberPortalConf(self.root, is_root=False, multiarch=MULTIARCH).apply()
        self.assertEqual(self.tree(), before)

    def test_a_lower_priority_conf_exposed_by_the_retire_is_reported_as_manual_in_the_same_run(self):
        script = load_script()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.put(f"{SYSTEM_CONFS}/10-xdg-desktop-portal.conf", PORTAL_TEXT)  # masked by the /usr/local one until now
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(script.NeedsManual):
            script.WireplumberPortalConf(self.root, is_root=True, multiarch=MULTIARCH).apply()
        self.assertFalse((self.root / PORTAL_CONF).exists())

    def test_the_plan_names_the_removal_of_a_duplicate(self):
        script = load_script()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        plan = script.WireplumberPortalConf(self.root, is_root=False, multiarch=MULTIARCH).plan()
        self.assertTrue(any("remove /" in line and "retired already" in line for line in plan), plan)
        self.assertFalse(any("rename" in line for line in plan), plan)

    def test_a_live_conf_that_is_retired_frees_the_name_for_an_older_restorable_copy_in_one_run(self):
        script = load_script()
        self.put(f"{WP_MODULES}/libxdg-desktop-portal.so", "elf")
        broken = component_conf("libgone")
        self.put(PORTAL_CONF, broken)
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        module = script.WireplumberPortalConf(
            self.root, is_root=True, multiarch=MULTIARCH, clock=datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
        )
        plan = module.plan()
        self.assertTrue(any(line.lstrip().startswith("retire /") for line in plan), plan)
        self.assertTrue(any(line.lstrip().startswith("restore /") for line in plan), plan)
        self.assertFalse(any("needs manual" in line for line in plan), plan)
        with contextlib.redirect_stdout(io.StringIO()):
            module.apply()
        self.assertEqual((self.root / PORTAL_CONF).read_bytes(), PORTAL_TEXT.encode())
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled.20261111T120000Z").read_text(), broken)
        self.assertEqual(self.run_script("check").returncode, 0)

    def test_a_non_root_install_does_not_restore(self):
        script = load_script()
        self.put(f"{WP_MODULES}/libxdg-desktop-portal.so", "elf")
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        before = self.tree()
        with self.assertRaises(script.ModuleError):
            script.WireplumberPortalConf(self.root, is_root=False, multiarch=MULTIARCH).apply()
        self.assertEqual(self.tree(), before)


class Coverage(unittest.TestCase):
    """The module's `[[coverage]]` rules account for what it retires, restores and leaves in place. No coverage adapter
    scans /usr/local yet (#235), so these are declarative: the rules are checked against the ids they must match."""

    @classmethod
    def setUpClass(cls):
        load_script()  # puts the repository on sys.path
        from ubuntu_tweaks.coverage import Coverage as Class
        from ubuntu_tweaks.coverage import Item, Rules
        from ubuntu_tweaks.manifest import load_manifest

        cls.rules = Rules(load_manifest(MODULE).coverage)
        cls.item = staticmethod(lambda identifier: Item("etc", identifier, "file"))
        cls.tracked = Class.TRACKED

    def classify(self, identifier):
        return self.rules.classify(self.item(identifier))

    def test_the_conf_and_its_retired_copies_are_tracked(self):
        base = "etc:/usr/local/share/wireplumber/wireplumber.conf.d/10-xdg-desktop-portal.conf"
        for suffix in ("", ".disabled", ".disabled.20261111T120000Z"):
            rule = self.classify(base + suffix)
            self.assertIsNotNone(rule, suffix)
            self.assertEqual(rule.coverage, self.tracked)

    def test_the_module_left_in_place_is_accounted_for_with_a_reason(self):
        for kind in ("wireplumber-0.5", "pipewire-0.3"):
            rule = self.classify(f"etc:/usr/local/lib/x86_64-linux-gnu/{kind}/libxdg-desktop-portal.so")
            self.assertIsNotNone(rule, kind)
            self.assertEqual(rule.coverage, self.tracked)
            self.assertIn("libxdg-desktop-portal.so", rule.reason)
            self.assertIn("left in place", rule.reason)

    def test_nothing_broader_is_claimed(self):
        for identifier in (
            "etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libother.so",
            "etc:/usr/local/lib/x86_64-linux-gnu/pipewire-0.3/libother.so",
            "etc:/usr/local/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so.1",
            "etc:/usr/local/lib/x86_64-linux-gnu/libxdg-desktop-portal.so",
            "etc:/usr/local/lib/x86_64-linux-gnu/spa-0.2/libxdg-desktop-portal.so",
            "etc:/usr/lib/x86_64-linux-gnu/wireplumber-0.5/libxdg-desktop-portal.so",
            "etc:/usr/local/share/wireplumber/wireplumber.conf.d/20-other.conf",
        ):
            self.assertIsNone(self.classify(identifier), identifier)


class Options(Staged):
    def test_an_option_is_refused(self):
        env = {
            **os.environ,
            "WIREPLUMBER_PORTAL_CONF_ROOT": str(self.root),
            "WIREPLUMBER_PORTAL_CONF_MULTIARCH": MULTIARCH,
            "UBUNTU_TWEAK_MODULE_CONFIG": '{"retire": true}',
        }
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "check"], env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("takes no options", result.stderr)


FAKE_SYSTEMCTL = """\
#!/usr/bin/python3 -B
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("systemctl " + " ".join(args) + "\\n")
units = [a for a in args if a.endswith(".service")]
if args[:2] == ["--user", "show"]:
    print(os.environ.get("FAKE_EXIT_STATUS", ""))
    sys.exit(0)
if args[:2] == ["--user", "is-active"]:
    sys.exit(0 if units[0] in os.environ.get("FAKE_ACTIVE", "").split(",") else 3)
if args[:2] == ["--user", "is-enabled"]:
    sys.exit(0 if units[0] in os.environ.get("FAKE_ENABLED", "").split(",") else 1)
"""


class Post(unittest.TestCase):
    """The user-level `[[post]]`: its argv, run against a fake systemctl."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.tmp / "systemctl").chmod(0o755)
        self.log = self.tmp / "log"
        self.log.write_text("")
        with open(MODULE / "module.toml", "rb") as stream:
            posts = tomllib.load(stream)["post"]
        self.assertEqual([post["name"] for post in posts], ["wireplumber-start"])
        self.post = posts[0]

    def run_argv(self, argv, active=(), enabled=(), exit_status="0"):
        env = {
            **os.environ,
            "PATH": f"{self.tmp}:{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_ACTIVE": ",".join(active),
            "FAKE_ENABLED": ",".join(enabled),
            "FAKE_EXIT_STATUS": exit_status,
        }
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

    def satisfied(self, **states):
        return self.run_argv(self.post["check"], **states).returncode == 0

    def test_a_wireplumber_that_died_with_exit_78_is_unsatisfied(self):
        self.assertFalse(self.satisfied(exit_status="78", enabled=["wireplumber.service"]))

    def test_a_running_wireplumber_is_satisfied(self):
        self.assertTrue(
            self.satisfied(exit_status="78", active=["wireplumber.service"], enabled=["wireplumber.service"])
        )

    def test_a_plain_stop_or_any_other_exit_is_left_alone(self):
        for status in ("0", "15", "1", ""):
            with self.subTest(status):
                self.assertTrue(self.satisfied(exit_status=status, enabled=["wireplumber.service"]))

    def test_a_masked_or_disabled_wireplumber_is_left_alone(self):
        self.assertTrue(self.satisfied(exit_status="78", enabled=[]))

    def test_run_clears_the_failed_state_starts_and_confirms_it_stayed_up(self):
        result = self.run_argv(self.post["run"], active=["wireplumber.service"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log.read_text().splitlines(),
            [
                "systemctl --user reset-failed wireplumber.service",
                "systemctl --user start wireplumber.service",
                "systemctl --user is-active --quiet wireplumber.service",
            ],
        )

    def test_run_fails_when_the_unit_does_not_stay_up(self):
        result = self.run_argv(self.post["run"], active=[])
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not stay up", result.stderr)


class ReviewFindings(Staged):
    def test_a_restore_never_shadows_a_packaged_conf_of_the_same_name(self):
        self.put(f"{PORTAL_CONF}.disabled", PORTAL_TEXT)
        self.put(f"{SYSTEM_CONFS}/10-xdg-desktop-portal.conf", PORTAL_TEXT)
        self.put(f"{WP_MODULES}/libxdg-desktop-portal.so", "elf")
        self.assertEqual(self.run_script("check").returncode, 0)
        before = self.tree()
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertEqual(self.tree(), before)

    def test_an_editor_backup_is_not_a_retired_copy_so_the_live_conf_is_kept(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.put(f"{PORTAL_CONF}.disabled~", PORTAL_TEXT)
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled").read_text(), PORTAL_TEXT)
        self.assertEqual((self.root / f"{PORTAL_CONF}.disabled~").read_text(), PORTAL_TEXT)
        self.assertFalse((self.root / PORTAL_CONF).exists())

    def test_a_broken_packaged_conf_is_masked_not_deleted(self):
        self.put(f"{SYSTEM_CONFS}/20-other.conf", component_conf("libgone"))
        result = self.run_script("check")
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertIn("/etc/wireplumber/wireplumber.conf.d/20-other.conf", result.stdout)


class SymlinkedLiveConf(Staged):
    """A link at the live name is not retired (a retired link is never restored): the owner settles it."""

    def setUp(self):
        super().setUp()
        self.target = self.put("elsewhere/portal.conf", PORTAL_TEXT)
        (self.root / LOCAL_CONFS).mkdir(parents=True)
        (self.root / PORTAL_CONF).symlink_to(self.target)

    def test_check_reports_it_as_manual_naming_the_link(self):
        result = self.run_script("check")
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertIn("10-xdg-desktop-portal.conf", result.stdout)
        self.assertIn("symlink", result.stdout)
        self.assertIn("manual", result.stdout)

    def test_install_leaves_the_link_alone_and_ends_in_needs_manual(self):
        result = self.run_script("install")
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn("needs manual", result.stdout)
        self.assertIn("10-xdg-desktop-portal.conf", result.stdout)
        self.assertEqual(os.readlink(self.root / PORTAL_CONF), str(self.target))
        self.assertEqual([p for p in self.tree() if ".disabled" in p], [])
        self.assertEqual(self.target.read_text(), PORTAL_TEXT)

    def test_the_plan_does_not_promise_a_rename(self):
        plan = self.run_script("plan").stdout
        self.assertIn("needs manual", plan)
        self.assertNotIn("rename", plan)

    def test_a_link_to_a_conf_whose_module_exists_is_fine(self):
        self.put(f"{WP_MODULES}/libxdg-desktop-portal.so", "elf")
        self.assertEqual(self.run_script("check").returncode, 0)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertTrue((self.root / PORTAL_CONF).is_symlink())


class OverrideComponents(Staged):
    """``override.wireplumber.components`` replaces the array instead of extending it: its modules are still loaded."""

    OVERRIDE = "override.wireplumber.components = [\n  { name = libgone, type = module }\n]\n"

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def test_the_parser_reads_the_override_spelling(self):
        self.assertEqual(self.script.required_modules(self.OVERRIDE), [("module", "libgone")])
        quoted = '"override.wireplumber.components": [ { "name": "libgone", "type": "pw-module" } ]'
        self.assertEqual(self.script.required_modules(quoted), [("pw-module", "libgone")])

    def test_both_spellings_in_one_file_are_both_read(self):
        text = self.OVERRIDE + component_conf("libother")
        self.assertEqual(self.script.required_modules(text), [("module", "libgone"), ("module", "libother")])

    def test_a_nested_key_of_the_same_name_is_not_a_components_array(self):
        text = (
            "wireplumber.profiles = { main = { override.wireplumber.components = [ { name = libx, type = module } ] } }"
        )
        self.assertEqual(self.script.required_modules(text), [])

    def test_an_override_with_a_missing_module_is_drift_naming_the_conf(self):
        self.put(f"{ETC_CONFS}/30-mine.conf", self.OVERRIDE)
        check = self.run_script("check")
        self.assertEqual(check.returncode, 3, check.stderr + check.stdout)
        self.assertIn("30-mine.conf", check.stdout)
        self.assertIn("libgone", check.stdout)
        install = self.run_script("install")
        self.assertEqual(install.returncode, 10, install.stderr + install.stdout)
        self.assertIn("30-mine.conf", install.stdout)

    def test_an_override_whose_module_exists_is_clean(self):
        self.put(f"{ETC_CONFS}/30-mine.conf", self.OVERRIDE)
        self.put(f"{WP_MODULES}/libgone.so", "elf")
        self.assertEqual(self.run_script("check").returncode, 0)

    def test_the_incident_conf_written_as_an_override_is_still_retired(self):
        self.put(PORTAL_CONF, PORTAL_TEXT.replace("wireplumber.components = [", "override.wireplumber.components = ["))
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertFalse((self.root / PORTAL_CONF).exists())
        self.assertTrue((self.root / f"{PORTAL_CONF}.disabled").exists())


class RetireRace(Staged):
    """The name picked for the retired copy can be taken between the pick and the rename: nothing is overwritten."""

    def test_a_copy_that_appears_after_the_name_was_picked_is_not_overwritten(self):
        script = load_script()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        module = script.WireplumberPortalConf(
            self.root, is_root=True, multiarch=MULTIARCH, clock=datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
        )
        pick = module._destination
        raced = []

        def losing_the_race(path):
            target, duplicate = pick(path)
            if not raced:  # someone creates the very name that was just picked
                raced.append(target)
                target.write_bytes(b"created in between\n")
            return target, duplicate

        module._destination = losing_the_race
        with contextlib.redirect_stdout(io.StringIO()):
            module.apply()
        self.assertEqual(raced[0].read_bytes(), b"created in between\n")
        self.assertFalse((self.root / PORTAL_CONF).exists())
        kept = sorted(p.read_bytes() for p in (self.root / LOCAL_CONFS).iterdir())
        self.assertEqual(kept, sorted([b"created in between\n", PORTAL_TEXT.encode()]))

    def test_a_name_that_stays_taken_ends_in_needs_manual_without_losing_the_conf(self):
        script = load_script()
        self.put(PORTAL_CONF, PORTAL_TEXT)
        module = script.WireplumberPortalConf(self.root, is_root=True, multiarch=MULTIARCH)
        taken = self.root / LOCAL_CONFS / "taken"
        taken.write_bytes(b"x")
        module._destination = lambda path: (taken, False)
        with self.assertRaises(script.NeedsManual):
            module.apply()
        self.assertEqual((self.root / PORTAL_CONF).read_bytes(), PORTAL_TEXT.encode())
        self.assertEqual(taken.read_bytes(), b"x")


class DevNullIsNoMask(Staged):
    """A link to /dev/null masks nothing: wireplumber 0.5 (``lib/wp/base-dirs.c``, ``wp_base_dirs_new_files_iterator``)
    skips every entry that is not a regular file (``g_file_test (path, G_FILE_TEST_IS_REGULAR)``, which follows the
    link and sees a character device) before it shadows the lower-priority fragment of the same name. An empty regular
    file is what masks, and what the hint asks for."""

    def link_to_dev_null(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to("/dev/null")

    def test_a_link_to_dev_null_leaves_the_packaged_fragment_in_force(self):
        self.put(f"{SYSTEM_CONFS}/40-distro.conf", component_conf("libmissing"))
        self.link_to_dev_null(f"{ETC_CONFS}/40-distro.conf")
        check = self.run_script("check")
        self.assertEqual(check.returncode, 3, check.stderr + check.stdout)
        self.assertIn("/usr/share/wireplumber/wireplumber.conf.d/40-distro.conf", check.stdout)
        install = self.run_script("install")
        self.assertEqual(install.returncode, 10, install.stderr + install.stdout)

    def test_the_xdg_etc_directory_does_not_mask_through_a_link_either(self):
        self.put(f"{SYSTEM_CONFS}/40-distro.conf", component_conf("libmissing"))
        self.link_to_dev_null(f"{XDG_ETC_CONFS}/40-distro.conf")
        self.assertEqual(self.run_script("check").returncode, 3)

    def test_the_hint_names_an_empty_regular_file_and_says_a_dev_null_link_does_not_do_it(self):
        self.put(f"{SYSTEM_CONFS}/40-distro.conf", component_conf("libmissing"))
        check = self.run_script("check")
        self.assertIn("empty regular file", check.stdout)
        self.assertIn("/dev/null", check.stdout)

    def test_an_empty_regular_file_does_mask(self):
        self.put(f"{SYSTEM_CONFS}/40-distro.conf", component_conf("libmissing"))
        self.put(f"{ETC_CONFS}/40-distro.conf", "")
        self.assertEqual(self.run_script("check").returncode, 0)

    def test_the_hand_built_portal_conf_is_still_retired_beside_a_dev_null_link_of_its_name(self):
        self.put(PORTAL_CONF, PORTAL_TEXT)
        self.link_to_dev_null(f"{ETC_CONFS}/10-xdg-desktop-portal.conf")
        self.assertEqual(self.run_script("check").returncode, 3)
        self.assertEqual(self.run_script("install").returncode, 0)
        self.assertFalse((self.root / PORTAL_CONF).exists())

    def test_a_link_to_something_else_is_a_fragment_not_a_mask(self):
        self.put(f"{SYSTEM_CONFS}/40-distro.conf", component_conf("libmissing"))
        self.put("elsewhere/40-distro.conf", component_conf("libmissing"))
        (self.root / ETC_CONFS).mkdir(parents=True)
        (self.root / ETC_CONFS / "40-distro.conf").symlink_to(self.root / "elsewhere/40-distro.conf")
        check = self.run_script("check")
        self.assertEqual(check.returncode, 3, check.stderr + check.stdout)
        self.assertIn("/etc/wireplumber/wireplumber.conf.d/40-distro.conf", check.stdout)


if __name__ == "__main__":
    unittest.main()

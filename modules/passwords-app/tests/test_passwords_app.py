"""passwords-app module against a stateful fake machine and a temporary HOME (nothing real is touched)."""

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
from ubuntu_tweaks.process import CommandResult, Process  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("passwords_app_script", str(MODULE / "passwords-app"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    loader.exec_module(module)
    return module


pw = load_script()
REPO = "https://git.example.org/tools/passwords-app.git"
HEAD = "0123456789abcdef0123456789abcdef01234567"
SECRET_ID = "someone@example.com"
MUTATING = {"clone", "enable", "start", "restart", "install.sh"}


class FakeMachine(Process):
    """git, systemctl --user, busctl --user and the repository's install.sh, as one small state."""

    def __init__(self, home, checkout, *, state="idle", installs=True):
        self.home = home
        self.checkout = checkout
        self.state = state
        self.installs = installs
        self.enabled = False
        self.active = False
        self.executed = []
        self.queried = []

    def make_checkout(self):
        (self.checkout / "scripts").mkdir(parents=True, exist_ok=True)
        (self.checkout / "pyproject.toml").write_text("[project]\nname = 'passwords-app'\n")
        (self.checkout / "scripts/install.sh").write_text("#!/bin/sh\n")

    def run_installer(self):
        venv_bin = self.home / ".local/share/passwords-app/venv/bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        for name in ("passwords-appd", "passwords-app-native-host"):
            (venv_bin / name).write_text("#!/bin/sh\n")
            (venv_bin / name).chmod(0o755)
        (self.home / ".local/bin").mkdir(parents=True, exist_ok=True)
        (self.home / ".local/bin/passwords-app").symlink_to(venv_bin / "passwords-app")
        unit = self.home / ".config/systemd/user/passwords-appd.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Unit]\n")
        host = self.home / ".mozilla/native-messaging-hosts" / pw.HOST_NAME
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text(
            json.dumps({"path": str(venv_bin / "passwords-app-native-host"), "allowed_extensions": [pw.EXTENSION_ID]})
        )
        self.enabled = self.active = True  # the real installer enables and restarts the unit

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        self.queried.append(tuple(argv))
        if argv[0] == "git":
            return (
                CommandResult(0, HEAD + "\n", "")
                if (self.checkout / "pyproject.toml").exists()
                else CommandResult(128, "", "")
            )
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return CommandResult(0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n", "")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return CommandResult(0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
        if argv[0] == "busctl" and "list" in argv:
            return CommandResult(
                0, f"{pw.BUS_NAME}  123 passwords-appd alice :1.99 user@1000.service -\n" if self.active else "", ""
            )
        if argv[0] == "busctl" and "introspect" in argv:
            return CommandResult(0 if self.active else 1, "", "")
        if argv[0] == "busctl" and "call" in argv:
            reply = {
                "type": "a{sv}",
                "data": [{"state": {"type": "s", "data": self.state}, "apple_id": {"type": "s", "data": SECRET_ID}}],
            }
            return CommandResult(0, json.dumps(reply), "")
        return CommandResult(127, "", "not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        self.executed.append(tuple(argv))
        if argv[:2] == ["git", "clone"]:
            self.make_checkout()
        elif argv[0].endswith("install.sh"):
            if self.installs:
                self.run_installer()
        elif argv[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
        elif argv[:3] == ["systemctl", "--user", "start"]:
            self.active = self.installs  # without an install the unit has no daemon to start


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.checkout = self.home / "dev/passwords-app"
        self.tools = {"git": "/usr/bin/git", "uv": "/usr/bin/uv"}

    def module(self, machine, options=None, *, is_root=False):
        options = {"repo": REPO, **(options or {})}
        return pw.PasswordsApp(
            machine, self.home, {}, options, is_root=is_root, which=self.tools.get, sleep=lambda seconds: None
        )

    def machine(self, **kw):
        return FakeMachine(self.home, self.checkout, **kw)


class Check(Base):
    def test_fresh_machine_is_unsatisfied_everywhere(self):
        findings = self.module(self.machine()).findings()
        self.assertFalse(any(f.ok for f in findings[1:]))
        self.assertTrue(findings[0].ok)

    def test_partial_install_reports_only_what_is_missing(self):
        # this machine's shape: checkout, files and a running enabled daemon, but no rev stamp
        machine = self.machine()
        machine.make_checkout()
        machine.run_installer()
        findings = {f.subject: f for f in self.module(machine).findings()}
        self.assertFalse(findings["installed from the checkout's current commit"].ok)
        self.assertTrue(findings["passwords-appd.service enabled"].ok)
        self.assertTrue(findings["io.github.alex_mextner.Passwords answers on the session bus"].ok)
        self.assertTrue(findings["Firefox native-messaging manifest"].ok)

    def test_check_never_mutates(self):
        machine = self.machine()
        machine.make_checkout()
        self.module(machine).findings()
        self.assertEqual(machine.executed, [])
        self.assertFalse({word for argv in machine.queried for word in argv} & MUTATING)

    def test_introspect_is_not_attempted_when_the_name_is_not_owned(self):
        machine = self.machine()
        machine.make_checkout()
        self.module(machine).findings()
        self.assertFalse([argv for argv in machine.queried if "introspect" in argv or "call" in argv])

    def test_signed_out_is_a_manual_finding_and_the_apple_id_is_never_shown(self):
        machine = self.machine(state="signed-out")
        machine.make_checkout()
        machine.run_installer()
        findings = self.module(machine).findings()
        manual = [f for f in findings if f.manual]
        self.assertEqual([f.subject for f in manual], ["signed in to iCloud Keychain"])
        self.assertNotIn(SECRET_ID, "\n".join(f.line() for f in findings))

    def test_host_manifest_problems(self):
        machine = self.machine()
        machine.make_checkout()
        machine.run_installer()
        host = self.home / ".mozilla/native-messaging-hosts" / pw.HOST_NAME
        host.write_text(json.dumps({"path": "/nonexistent", "allowed_extensions": [pw.EXTENSION_ID]}))
        self.assertIn("not an executable", self.module(machine)._host_manifest_problem())
        host.write_text(
            json.dumps(
                {
                    "path": str(self.home / ".local/share/passwords-app/venv/bin/passwords-appd"),
                    "allowed_extensions": [],
                }
            )
        )
        self.assertIn(pw.EXTENSION_ID, self.module(machine)._host_manifest_problem())
        host.unlink()
        self.assertIn("missing", self.module(machine)._host_manifest_problem())

    def test_a_manifest_that_is_not_an_object_or_has_odd_types_is_a_finding_not_a_crash(self):
        machine = self.machine()
        machine.make_checkout()
        machine.run_installer()
        host = self.home / ".mozilla/native-messaging-hosts" / pw.HOST_NAME
        for text in (
            "[]",
            '"x"',
            "null",
            json.dumps({"path": 3, "allowed_extensions": "passwords-app@alex-mextner.github.io"}),
        ):
            host.write_text(text)
            self.assertTrue(self.module(machine)._host_manifest_problem(), text)

    def test_missing_uv_is_reported(self):
        del self.tools["uv"]
        first = self.module(self.machine()).findings()[0]
        self.assertFalse(first.ok)
        self.assertEqual(first.detail, "uv")

    def test_options_are_validated(self):
        machine = self.machine()
        for options in ({"nope": 1}, {"repo": "git@github.com:x/y"}, {"checkout": "relative/dir"}, {"checkout": ""}):
            with self.assertRaises(ModuleError, msg=options):
                self.module(machine, options)

    def test_checkout_option_expands_tilde(self):
        module = self.module(self.machine(), {"checkout": "~/src/pw"})
        self.assertEqual(module._checkout, self.home / "src/pw")

    def test_daemon_state_parsing(self):
        self.assertEqual(pw.daemon_state(json.dumps({"data": [{"state": {"data": "idle"}}]})), "idle")
        self.assertIsNone(pw.daemon_state("not json"))
        self.assertIsNone(pw.daemon_state(json.dumps({"data": []})))


class UnsetRepo(Base):
    def unset(self, machine):
        return pw.PasswordsApp(
            machine, self.home, {}, {}, is_root=False, which=self.tools.get, sleep=lambda seconds: None
        )

    def test_findings_report_a_manual_finding_and_apply_needs_manual(self):
        machine = self.machine()
        manual = [f for f in self.unset(machine).findings() if f.manual]
        self.assertEqual(len(manual), 1)
        self.assertIn("[modules.passwords-app] repo", manual[0].line())
        with self.assertRaisesRegex(NeedsManual, r"\[modules\.passwords-app\] repo"):
            self.unset(machine).apply()
        self.assertEqual(machine.executed, [])


class Install(Base):
    def test_fresh_machine_converges(self):
        machine = self.machine()
        module = self.module(machine)
        module.apply()
        self.assertEqual(machine.executed[0], ("git", "clone", REPO, str(self.checkout)))
        self.assertEqual(machine.executed[1], (str(self.checkout / "scripts/install.sh"),))
        unmet = [f.line() for f in module.findings() if not f.ok]
        self.assertEqual(unmet, [])
        self.assertEqual((self.home / ".local/state/ubuntu-tweaks/passwords-app.rev").read_text(), HEAD + "\n")

    def test_existing_checkout_is_reinstalled_not_recloned_or_pulled(self):
        machine = self.machine()
        machine.make_checkout()
        self.module(machine).apply()
        self.assertFalse([argv for argv, _ in [(a, None) for a in machine.executed] if argv[:2] == ("git", "clone")])
        self.assertFalse([argv for argv in machine.executed if "pull" in argv or "fetch" in argv])

    def test_unit_enabled_and_started_when_the_installer_did_not(self):
        machine = self.machine()
        machine.make_checkout()
        original = machine.run_installer

        def without_enabling():
            original()
            machine.enabled = machine.active = False

        machine.run_installer = without_enabling
        self.module(machine).apply()
        self.assertIn(("systemctl", "--user", "enable", pw.UNIT), machine.executed)
        self.assertIn(("systemctl", "--user", "start", pw.UNIT), machine.executed)

    def test_signed_out_after_install_needs_manual_and_never_fakes_success(self):
        machine = self.machine(state="signin-required")
        with self.assertRaisesRegex(NeedsManual, "passwords-app signin"):
            self.module(machine).apply()
        self.assertTrue(any(not f.ok and f.manual for f in self.module(machine).findings()))

    def test_refuses_root_and_missing_tools(self):
        machine = self.machine()
        with self.assertRaises(ModuleError):
            self.module(machine, is_root=True).apply()
        del self.tools["uv"]
        with self.assertRaisesRegex(NeedsManual, "uv"):
            self.module(machine).apply()
        self.assertEqual(machine.executed, [])

    def test_foreign_directory_is_not_overwritten(self):
        self.checkout.mkdir(parents=True)
        (self.checkout / "notes.txt").write_text("mine")
        machine = self.machine()
        with self.assertRaisesRegex(ModuleError, "not a passwords-app checkout"):
            self.module(machine).apply()
        self.assertEqual(machine.executed, [])

    def test_an_up_to_date_install_is_not_rerun_just_because_signin_is_pending(self):
        machine = self.machine(state="signin-required")
        with self.assertRaises(NeedsManual):
            self.module(machine).apply()
        machine.executed.clear()
        with self.assertRaises(NeedsManual):
            self.module(machine).apply()
        self.assertFalse([argv for argv, _ in [(a, None) for a in machine.executed] if argv[0].endswith("install.sh")])

    def test_daemon_that_does_not_answer_fails(self):
        machine = self.machine()
        machine.installs = False
        with self.assertRaisesRegex(ModuleError, "does not answer"):
            self.module(machine).apply()


if __name__ == "__main__":
    unittest.main()

"""firefox-passkey-qr module against stub installers that implement the passkey-qr interface.

Two installers are stubbed: the upstream one with `--yes` and `check` (feature branch feat/yes-check-plan of
alex-mextner/firefox-passkey-qr) and today's, which has neither. The stubs are real executables run by the
real Process, so argv handling, exit codes and the environment are exercised for real.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.module_script import ModuleError, NeedsManual  # noqa: E402
from ubuntu_tweaks.process import CommandError, CommandResult, Process  # noqa: E402

REPO = "https://git.example.org/tools/firefox-passkey-qr.git"


def load_script():
    loader = importlib.machinery.SourceFileLoader("firefox_passkey_qr_script", str(MODULE / "firefox-passkey-qr"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


pq = load_script()
SENTINEL = "SENTINEL_NOT_A_REAL_HA_CREDENTIAL"

# What the stub does is decided by files next to it: `state` (installed or not), `log` (argv + env facts).
STUB = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    argv = sys.argv[1:]
    HAS_YES, HAS_CHECK = {has_yes}, {has_check}
    HELP = "usage: passkey-qr [-h] [--dry-run] " + ("[--yes] [--remove-snap] " if HAS_YES else "") \\
        + "{{status," + ("check," if HAS_CHECK else "") + "install}} ...\\n"
    if "--help" in argv:
        print(HELP); sys.exit(0)
    installed = os.path.exists(os.path.join(here, "installed"))
    command = argv[-1]
    with open(os.path.join(here, "log"), "a") as log:
        log.write(json.dumps({{"argv": argv, "has_token": "HA_TOKEN" in os.environ,
                              "ha_url": os.environ.get("HA_URL")}}) + "\\n")
    if command == "check":
        print("  missing: credentialsd installed") if not installed else None
        sys.exit(0 if installed else 3)
    if command == "status":
        mark = "x" if installed else " "
        print(f"  [{{mark}}] Firefox (Mozilla deb)\\n  [x] Bluetooth adapter\\n  [{{mark}}] credentialsd installed\\n"
              f"  [{{mark}}] native-messaging manifest\\n  [x] missing apt packages")
        sys.exit(0)
    if command == "install":
        if "--dry-run" in argv:
            print("== 1. Build and runtime packages ==\\n  $ sudo apt-get install -y meson")
            sys.exit(0)
        waiting = os.path.join(here, "waiting")
        if os.path.exists(waiting):  # the file holds the exit code (default 3 = something waits for a person)
            print("Not finished:\\n  waiting for you: 3. Bluetooth: needs a long-lived admin token")
            sys.exit(int(open(waiting).read() or 3))
        open(os.path.join(here, "installed"), "w").close()
        sys.exit(0)
    sys.exit(2)
    """
)


class KeyringProcess(Process):
    """The real Process, except that secret-tool answers from a dict."""

    def __init__(self, keyring):
        self.keyring = keyring
        self.git = []

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        if argv[:2] == ["secret-tool", "lookup"]:
            value = self.keyring.get(argv[argv.index("purpose") + 1])
            return CommandResult(0 if value else 1, value or "", "")
        return super().query(argv, timeout=timeout, cwd=cwd)

    def execute(self, argv, *, input_text=None, cwd=None):
        if list(argv)[:2] == ["git", "clone"]:
            self.git.append(list(argv))
            make_stub(Path(argv[3]), has_yes=True, has_check=True)
            return
        super().execute(argv, input_text=input_text, cwd=cwd)


def make_stub(checkout, *, has_yes, has_check):
    checkout.mkdir(parents=True, exist_ok=True)
    script = checkout / "passkey-qr"
    script.write_text(STUB.format(has_yes=has_yes, has_check=has_check))
    script.chmod(0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.checkout = self.home / "dev/firefox-passkey-qr"
        self.tools = {"git": "/usr/bin/git"}
        self.process = KeyringProcess({})

    def module(self, options=None, *, is_root=False):
        options = {"repo": REPO, **(options or {})}
        return pq.PasskeyQR(self.process, self.home, options, is_root=is_root, which=self.tools.get)

    def stub(self, **kw):
        make_stub(self.checkout, **{"has_yes": True, "has_check": True, **kw})

    def log(self):
        path = self.checkout / "log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def installed(self):
        return (self.checkout / "installed").exists()


class Check(Base):
    def test_no_checkout_is_a_missing_installer(self):
        findings = self.module().findings()
        self.assertEqual([f.ok for f in findings], [False])

    def test_unsatisfied_then_satisfied_by_the_installers_own_check(self):
        self.stub()
        findings = self.module().findings()
        self.assertEqual([f.ok for f in findings], [True, False])
        self.assertIn("credentialsd installed", findings[1].detail)
        (self.checkout / "installed").touch()
        self.assertTrue(all(f.ok for f in self.module().findings()))

    def test_check_never_runs_install(self):
        self.stub()
        self.module().findings()
        self.assertFalse([entry for entry in self.log() if entry["argv"][-1] == "install"])

    def test_an_installer_without_check_is_judged_from_status_rows(self):
        self.stub(has_check=False)
        findings = self.module().findings()
        self.assertFalse(findings[1].ok)
        self.assertIn("credentialsd installed", findings[1].detail)
        (self.checkout / "installed").touch()
        self.assertTrue(all(f.ok or f.manual for f in self.module().findings()))

    def test_an_unmet_installer_without_yes_is_a_manual_fact(self):
        self.stub(has_yes=False, has_check=False)
        findings = self.module().findings()
        manual = [f for f in findings if f.manual]
        self.assertEqual(len(manual), 1)
        self.assertIn("run", manual[0].detail)

    def test_a_satisfied_installer_without_yes_needs_no_manual_fact(self):
        self.stub(has_yes=False, has_check=False)
        (self.checkout / "installed").touch()
        self.assertEqual([f for f in self.module().findings() if not f.ok], [])


class StatusRows(unittest.TestCase):
    def test_only_required_unticked_rows_count(self):
        output = "  [x] Firefox (Mozilla deb)\n  [ ] Bluetooth adapter (local or ha-ble-vhci)\n  [ ] ha-ble-vhci service\n  [ ] bluetooth.service\n"
        self.assertEqual(pq.unmet_rows(output), ["Bluetooth adapter (local or ha-ble-vhci)"])


class Robustness(Base):
    def test_help_prose_with_the_word_check_is_not_a_check_subcommand(self):
        self.stub(has_check=False)
        script = self.checkout / "passkey-qr"
        script.write_text(
            script.read_text().replace(
                'HELP = "usage: passkey-qr [-h] [--dry-run] "',
                'HELP = "please check the docs. usage: passkey-qr [-h] [--dry-run] "',
            )
        )
        self.assertFalse(self.module()._supports("check"))

    def test_a_check_that_exits_with_another_code_is_a_broken_checker(self):
        self.stub()
        script = self.checkout / "passkey-qr"
        script.write_text(script.read_text().replace("sys.exit(0 if installed else 3)", "sys.exit(2)"))
        with self.assertRaisesRegex(ModuleError, "check broke"):
            self.module().findings()

    def test_reworded_status_rows_are_not_read_as_satisfied(self):
        self.stub(has_check=False)
        script = self.checkout / "passkey-qr"
        script.write_text(script.read_text().replace("Bluetooth adapter", "BT radio"))
        (self.checkout / "installed").touch()
        findings = self.module().findings()
        self.assertFalse(findings[1].ok)
        self.assertIn("not recognised", findings[1].detail)


class Plan(Base):
    def test_the_plan_is_the_installers_dry_run_with_the_flags_the_real_run_uses(self):
        self.stub()
        lines = self.module({"remove_snap": True}).plan()
        self.assertIn("passkey-qr --dry-run --yes --remove-snap install", lines[0])
        self.assertIn("      $ sudo apt-get install -y meson", lines)
        self.assertFalse(self.installed())  # a dry run changes nothing

    def test_without_yes_the_plan_says_the_real_run_is_interactive(self):
        self.stub(has_yes=False, has_check=False)
        self.assertTrue([line for line in self.module().plan() if "no --yes" in line])

    def test_without_a_checkout_the_plan_says_it_will_clone(self):
        self.assertIn("clone", self.module().plan()[0])


class Install(Base):
    def test_runs_the_installers_yes_flag_and_converges(self):
        self.stub()
        module = self.module()
        module.apply()
        entry = self.log()[-1]
        self.assertEqual(entry["argv"], ["--yes", "install"])
        self.assertTrue(all(f.ok for f in module.findings()))

    def test_remove_snap_is_passed_only_when_the_profile_asks(self):
        self.stub()
        self.module({"remove_snap": True}).apply()
        self.assertEqual(self.log()[-1]["argv"], ["--yes", "--remove-snap", "install"])

    def test_the_home_assistant_token_reaches_the_installer_through_its_environment_only(self):
        self.stub()
        self.process.keyring["ha-token"] = SENTINEL
        before = os.environ.get("HA_TOKEN")
        self.module({"ha_url": "http://100.64.0.7:8123"}).apply()
        entry = self.log()[-1]
        self.assertTrue(entry["has_token"])
        self.assertEqual(entry["ha_url"], "http://100.64.0.7:8123")
        self.assertEqual(os.environ.get("HA_TOKEN"), before)
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL, path.read_text(errors="ignore"))

    def test_no_keyring_token_is_not_an_error(self):
        self.stub()
        with mock.patch.dict(os.environ):
            os.environ.pop("HA_TOKEN", None)
            self.module().apply()
        self.assertFalse(self.log()[-1]["has_token"])

    def test_an_installer_that_waits_for_a_person_is_needs_manual_not_a_failure(self):
        self.stub()
        (self.checkout / "waiting").touch()  # the stub exits 3 like `passkey-qr install` does
        with self.assertRaisesRegex(NeedsManual, "lines above say what waits for you"):
            self.module().apply()
        self.assertFalse(self.installed())

    def test_any_other_installer_failure_stays_an_error(self):
        self.stub()
        (self.checkout / "waiting").write_text("1")  # the installer fails for real (exit 1), not "waits"
        with self.assertRaises(CommandError) as raised:
            self.module().apply()
        self.assertEqual(raised.exception.returncode, 1)
        self.assertEqual(self.log()[-1]["argv"], ["--yes", "install"])  # it was the install that failed

    def test_an_installer_without_yes_is_reported_as_needs_manual_never_run(self):
        self.stub(has_yes=False)
        with self.assertRaisesRegex(NeedsManual, "interactively"):
            self.module().apply()
        self.assertFalse(self.installed())
        self.assertFalse([entry for entry in self.log() if entry["argv"][-1] == "install"])

    def test_a_missing_checkout_is_cloned_first(self):
        self.module().apply()
        self.assertEqual(self.process.git[0][:3], ["git", "clone", REPO])
        self.assertTrue(self.installed())

    def test_an_unset_repo_is_manual_when_there_is_nothing_to_clone(self):
        unset = pq.PasskeyQR(self.process, self.home, {}, is_root=False, which=self.tools.get)
        manual = [f for f in unset.findings() if f.manual]
        self.assertEqual(len(manual), 1)
        self.assertIn("[modules.firefox-passkey-qr] repo", manual[0].line())
        self.assertIn("needs manual", "\n".join(unset.plan()))
        with self.assertRaisesRegex(NeedsManual, r"\[modules\.firefox-passkey-qr\] repo"):
            unset.apply()
        self.assertEqual(self.process.git, [])

    def test_a_foreign_directory_is_not_touched_and_git_is_needed_to_clone(self):
        self.checkout.mkdir(parents=True)
        (self.checkout / "notes").write_text("mine")
        with self.assertRaisesRegex(ModuleError, "not a firefox-passkey-qr checkout"):
            self.module().apply()
        shutil.rmtree(self.checkout)
        del self.tools["git"]
        with self.assertRaisesRegex(NeedsManual, "git"):
            self.module().apply()

    def test_refuses_root(self):
        self.stub()
        with self.assertRaises(ModuleError):
            self.module(is_root=True).apply()

    def test_a_failing_installer_is_an_error(self):
        self.stub()
        (self.checkout / "passkey-qr").write_text(
            (self.checkout / "passkey-qr")
            .read_text()
            .replace('open(os.path.join(here, "installed"), "w").close()', "sys.exit(1)")
        )
        with self.assertRaises(Exception):
            self.module().apply()


class Options(Base):
    def test_validation(self):
        for options in (
            {"nope": 1},
            {"repo": "git@github.com:x/y"},
            {"checkout": "relative"},
            {"ha_url": "homeassistant.local"},
            {"remove_snap": "yes"},
            {"checkout": ""},
        ):
            with self.assertRaises(ModuleError, msg=options):
                self.module(options)


if __name__ == "__main__":
    unittest.main()

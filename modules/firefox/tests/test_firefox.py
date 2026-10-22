"""firefox module: build and AMO signing with credentials from the keyring, against a fake machine."""

import importlib.machinery
import importlib.util
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

from ubuntu_tweaks.module_script import ModuleError, NeedsManual  # noqa: E402
from ubuntu_tweaks.process import CommandResult, Process  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("firefox_module_script", str(MODULE / "firefox"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


ff = load_script()
SENTINEL_A = "SENTINEL_A_NOT_A_REAL_CREDENTIAL"  # the tests check where these travel, not what they are
SENTINEL_B = "SENTINEL_B_NOT_A_REAL_CREDENTIAL"


def make_xpi(path, signed):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", "{}")
        if signed:
            archive.writestr("META-INF/mozilla.rsa", "sig")
            archive.writestr("META-INF/cose.sig", "sig")


def stage_build(stage, version, signed):
    make_xpi(stage / ff.STAGED_XPI, signed)
    (stage / ff.STAGED_VERSION).write_text(version + "\n")


class FakeMachine(Process):
    """The keyring, bun and web-ext as one state; records the environment web-ext was started with."""

    def __init__(self, *, first=SENTINEL_A, second=SENTINEL_B, signs=True):
        self.keyring = {"amo-jwt-issuer": first, "amo-jwt-secret": second}
        self.signs = signs
        self.executed = []
        self.sign_env = None

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        if argv[:2] == ["secret-tool", "lookup"]:
            value = self.keyring.get(argv[argv.index("purpose") + 1])
            return CommandResult(0 if value else 1, value or "", "")
        return CommandResult(127, "", "not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        self.executed.append((tuple(argv), cwd))
        if argv[0].endswith("web-ext"):
            self.sign_env = (os.environ.get(ff.API_KEY_ENV), os.environ.get(ff.API_SECRET_ENV))
            artifacts = next(Path(a.split("=", 1)[1]) for a in argv if a.startswith("--artifacts-dir="))
            make_xpi(artifacts / "x-1.2.3.xpi", self.signs)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.extension = self.home / "dev/passwords-app/extension"
        self.extension.mkdir(parents=True)
        (self.extension / "package.json").write_text(
            json.dumps({"name": "passwords-app-extension", "version": "1.2.3"})
        )
        self.stage = self.home / ".local/state/ubuntu-tweaks/firefox"
        self.tools = {"bun": "/usr/bin/bun", "secret-tool": "/usr/bin/secret-tool"}

    def module(self, machine, options=None, *, is_root=False):
        return ff.Firefox(machine, self.home, options or {}, is_root=is_root, which=self.tools.get)


class Check(Base):
    def test_unsatisfied_until_a_signed_xpi_is_staged(self):
        findings = self.module(FakeMachine()).findings()
        self.assertEqual([f.ok for f in findings], [True, False])
        self.assertIn("version 1.2.3", findings[1].subject)

    def test_an_unsigned_xpi_does_not_count(self):
        stage_build(self.stage, "1.2.3", signed=False)
        self.assertFalse(self.module(FakeMachine()).findings()[1].ok)

    def test_a_signed_xpi_for_another_version_does_not_count(self):
        stage_build(self.stage, "1.0.0", signed=True)
        self.assertFalse(self.module(FakeMachine()).findings()[1].ok)

    def test_signed_xpi_satisfies(self):
        stage_build(self.stage, "1.2.3", signed=True)
        self.assertTrue(all(f.ok for f in self.module(FakeMachine()).findings()))

    def test_missing_checkout_is_reported(self):
        shutil.rmtree(self.extension.parent)
        findings = self.module(FakeMachine()).findings()
        self.assertFalse(findings[0].ok)
        self.assertIn("enable passwords-app", findings[0].detail)

    def test_check_runs_nothing(self):
        machine = FakeMachine()
        self.module(machine).findings()
        self.assertEqual(machine.executed, [])

    def test_options(self):
        with self.assertRaises(ModuleError):
            self.module(FakeMachine(), {"nope": 1})
        with self.assertRaises(ModuleError):
            self.module(FakeMachine(), {"checkout": "relative"})


class Install(Base):
    def test_builds_signs_with_the_keyring_credentials_and_stages(self):
        machine = FakeMachine()
        module = self.module(machine)
        module.apply()
        commands = [argv for argv, _ in machine.executed]
        self.assertEqual(commands[0], ("bun", "install", "--frozen-lockfile"))
        self.assertEqual(commands[1], ("bun", "run", "build"))
        self.assertTrue(commands[2][0].endswith("node_modules/.bin/web-ext"))
        self.assertIn("--channel=unlisted", commands[2])
        self.assertTrue(all(cwd == self.extension for _, cwd in machine.executed))
        self.assertTrue(all(f.ok for f in module.findings()))
        self.assertEqual((self.stage / ff.STAGED_XPI).stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.stage / ff.STAGED_VERSION).read_text(), "1.2.3\n")

    def test_credentials_travel_only_in_web_exts_environment(self):
        machine = FakeMachine()
        before = (os.environ.get(ff.API_KEY_ENV), os.environ.get(ff.API_SECRET_ENV))
        self.module(machine).apply()
        self.assertEqual(machine.sign_env, (SENTINEL_A, SENTINEL_B))
        self.assertEqual((os.environ.get(ff.API_KEY_ENV), os.environ.get(ff.API_SECRET_ENV)), before)
        for argv, _ in machine.executed:
            self.assertFalse([part for part in argv if SENTINEL_A in part or SENTINEL_B in part], argv)
        for path in self.home.rglob("*"):
            if path.is_file() and path.suffix != ".xpi":
                self.assertNotIn(SENTINEL_B, path.read_text(errors="ignore"))

    def test_environment_is_restored_when_signing_fails(self):
        machine = FakeMachine()
        original = machine.execute

        def failing(argv, **kw):
            original(argv, **kw)
            if list(argv)[0].endswith("web-ext"):
                raise RuntimeError("AMO rejected the upload")

        machine.execute = failing
        before = os.environ.get(ff.API_SECRET_ENV)
        with self.assertRaises(RuntimeError):
            self.module(machine).apply()
        self.assertEqual(os.environ.get(ff.API_SECRET_ENV), before)

    def test_no_signing_key_is_needs_manual_and_nothing_is_built(self):
        machine = FakeMachine(first=None, second=None)
        with self.assertRaisesRegex(NeedsManual, "AMO signing key.*secret-tool store"):
            self.module(machine).apply()
        self.assertEqual(machine.executed, [])
        self.assertFalse(self.module(machine).findings()[1].ok)  # reported as missing, never faked

    def test_half_a_key_is_no_key(self):
        with self.assertRaises(NeedsManual):
            self.module(FakeMachine(second=None)).apply()

    def test_unsigned_output_is_an_error_not_a_staged_file(self):
        with self.assertRaisesRegex(ModuleError, "no signed .xpi"):
            self.module(FakeMachine(signs=False)).apply()
        self.assertFalse((self.stage / ff.STAGED_XPI).exists())

    def test_missing_tools_and_checkout_are_manual_steps(self):
        machine = FakeMachine()
        del self.tools["bun"]
        with self.assertRaisesRegex(NeedsManual, "bun"):
            self.module(machine).apply()
        self.tools["bun"] = "/usr/bin/bun"
        del self.tools["secret-tool"]
        with self.assertRaisesRegex(NeedsManual, "secret-tool"):
            self.module(machine).apply()
        self.tools["secret-tool"] = "/usr/bin/secret-tool"
        shutil.rmtree(self.extension.parent)
        with self.assertRaisesRegex(NeedsManual, "passwords-app module first"):
            self.module(machine).apply()
        self.assertEqual(machine.executed, [])

    def test_nothing_to_do_when_already_staged(self):
        stage_build(self.stage, "1.2.3", signed=True)
        machine = FakeMachine()
        self.module(machine).apply()
        self.assertEqual(machine.executed, [])

    def test_refuses_root(self):
        with self.assertRaises(ModuleError):
            self.module(FakeMachine(), is_root=True).apply()


if __name__ == "__main__":
    unittest.main()

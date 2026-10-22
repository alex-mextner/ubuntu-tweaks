"""The shared scaffolding of Python system-module scripts: options, the desktop user, the dispatcher."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ubuntu_tweaks import module_script as kit
from ubuntu_tweaks.process import CommandResult


def passwd(name, uid, home):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_dir=home)


class FixedModule(kit.Module):
    name = "fixed"

    def __init__(self, findings, blocked=None):
        self._findings = findings
        self._blocked = blocked
        self.applied = False

    def findings(self):
        return self._findings

    def apply(self):
        if self._blocked:
            raise kit.NeedsManual(self._blocked)
        self.applied = True


class ModuleConfig(unittest.TestCase):
    def test_unset_and_blank_mean_no_options(self):
        self.assertEqual(kit.module_config({}), {})
        self.assertEqual(kit.module_config({kit.CONFIG_ENV: "  "}), {})

    def test_json_object(self):
        self.assertEqual(kit.module_config({kit.CONFIG_ENV: '{"password_auth": false}'}), {"password_auth": False})

    def test_garbage_and_non_objects_are_errors(self):
        for text in ("{", "[1]", '"x"'):
            with self.assertRaises(kit.ModuleError, msg=text):
                kit.module_config({kit.CONFIG_ENV: text})


class InvokingAccount(unittest.TestCase):
    def lookups(self):
        users = {"alex": passwd("alex", 1000, "/home/alex"), "root": passwd("root", 0, "/root")}
        by_uid = {entry.pw_uid: entry for entry in users.values()}
        return {"lookup_name": users.__getitem__, "lookup_uid": by_uid.__getitem__}

    def test_sudo_user_wins_for_root(self):
        account = kit.invoking_account({"SUDO_USER": "alex"}, euid=0, **self.lookups())
        self.assertEqual((account.name, account.home, account.uid), ("alex", Path("/home/alex"), 1000))

    def test_pkexec_uid(self):
        account = kit.invoking_account({"PKEXEC_UID": "1000"}, euid=0, **self.lookups())
        self.assertEqual(account.name, "alex")

    def test_unprivileged_caller_is_itself_whatever_the_environment_says(self):
        account = kit.invoking_account({"SUDO_USER": "root"}, euid=1000, **self.lookups())
        self.assertEqual(account.name, "alex")

    def test_plain_root_stays_root(self):
        self.assertEqual(kit.invoking_account({}, euid=0, **self.lookups()).name, "root")

    def test_unknown_user_is_an_error(self):
        with self.assertRaises(kit.ModuleError):
            kit.invoking_account({"SUDO_USER": "ghost"}, euid=0, **self.lookups())


class Dispatcher(unittest.TestCase):
    def run_main(self, module, argv):
        out, err = io.StringIO(), io.StringIO()
        return kit.main(module, argv, out=out, err=err), out.getvalue(), err.getvalue()

    def test_check_prints_only_unmet_facts(self):
        module = FixedModule(
            [kit.Finding(True, "a"), kit.Finding(False, "b", "why"), kit.Finding(False, "c", manual=True)]
        )
        code, out, _ = self.run_main(module, ["check"])
        self.assertEqual(code, kit.EXIT_DRIFT)
        self.assertEqual(out, "missing b: why\nmanual  c\n")

    def test_status_prints_all_facts(self):
        code, out, _ = self.run_main(FixedModule([kit.Finding(True, "a")]), ["status"])
        self.assertEqual((code, out), (kit.EXIT_OK, "ok      a\n"))

    def test_install_codes(self):
        module = FixedModule([])
        self.assertEqual(self.run_main(module, ["install"])[0], kit.EXIT_OK)
        self.assertTrue(module.applied)
        code, out, _ = self.run_main(FixedModule([], blocked="AMO key"), ["install"])
        self.assertEqual((code, out), (kit.EXIT_NEEDS_MANUAL, "needs manual: AMO key\n"))

    def test_usage(self):
        for argv in ([], ["nope"], ["check", "extra"]):
            code, _, err = self.run_main(FixedModule([]), argv)
            self.assertEqual(code, kit.EXIT_USAGE)
            self.assertIn("usage: fixed", err)


class Plan(unittest.TestCase):
    def test_plan_prints_the_modules_lines_and_exits_zero(self):
        class Planned(FixedModule):
            def plan(self):
                return ["  step one", "  step two"]

        out = io.StringIO()
        self.assertEqual(kit.main(Planned([]), ["plan"], out=out, err=io.StringIO()), kit.EXIT_OK)
        self.assertEqual(out.getvalue(), "  step one\n  step two\n")

    def test_default_plan_is_empty(self):
        out = io.StringIO()
        self.assertEqual(kit.main(FixedModule([]), ["plan"], out=out, err=io.StringIO()), kit.EXIT_OK)
        self.assertEqual(out.getvalue(), "")


class KeyringSecret(unittest.TestCase):
    class Fake(kit.Process):
        def __init__(self, result):
            self.result = result
            self.asked = []

        def query(self, argv, *, timeout=60.0, cwd=None):
            self.asked.append(list(argv))
            return self.result

    def test_looks_up_by_application_and_purpose(self):
        fake = self.Fake(CommandResult(0, "VALUE\n", ""))
        self.assertEqual(kit.keyring_secret(fake, "amo-jwt-secret"), "VALUE")
        self.assertEqual(
            fake.asked, [["secret-tool", "lookup", "application", "ubuntu-tweaks", "purpose", "amo-jwt-secret"]]
        )

    def test_absent_empty_or_missing_tool_is_none(self):
        for result in (CommandResult(1, "", ""), CommandResult(0, " \n", ""), CommandResult(127, "", "not found")):
            self.assertIsNone(kit.keyring_secret(self.Fake(result), "x"))


class StateDir(unittest.TestCase):
    def test_default_and_xdg(self):
        self.assertEqual(kit.state_dir(Path("/home/a"), {}), Path("/home/a/.local/state/ubuntu-tweaks"))
        self.assertEqual(kit.state_dir(Path("/home/a"), {"XDG_STATE_HOME": "/s"}), Path("/s/ubuntu-tweaks"))


class Environment(unittest.TestCase):
    def test_set_inside_restored_after_even_on_failure(self):
        import os

        os.environ.pop("UBT_X", None)
        os.environ["UBT_Y"] = "old"
        with self.assertRaises(RuntimeError), kit.environment(UBT_X="1", UBT_Y="new"):
            self.assertEqual((os.environ["UBT_X"], os.environ["UBT_Y"]), ("1", "new"))
            raise RuntimeError
        self.assertEqual((os.environ.get("UBT_X"), os.environ["UBT_Y"]), (None, "old"))
        del os.environ["UBT_Y"]


class ReadOwnedFile(unittest.TestCase):
    def test_regular_owned_small_file_only(self):
        import os

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "f").write_bytes(b"abc")
            self.assertEqual(kit.read_owned_file(root / "f", os.getuid(), 10), b"abc")
            with self.assertRaises(kit.ModuleError):
                kit.read_owned_file(root / "f", os.getuid() + 1, 10)  # another owner
            with self.assertRaises(kit.ModuleError):
                kit.read_owned_file(root / "f", os.getuid(), 2)  # too big
            (root / "link").symlink_to(root / "f")
            with self.assertRaises(kit.ModuleError):
                kit.read_owned_file(root / "link", os.getuid(), 10)  # a symlink is never followed
            with self.assertRaises(kit.ModuleError):
                kit.read_owned_file(root, os.getuid(), 10)  # a directory


class WriteFile(unittest.TestCase):
    def test_atomic_with_final_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "a/b.conf"
            kit.write_file(target, "x\n", 0o644)
            self.assertEqual((target.read_text(), target.stat().st_mode & 0o777), ("x\n", 0o644))
            kit.write_file(target, json.dumps({"k": 1}), 0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["b.conf"])


if __name__ == "__main__":
    unittest.main()

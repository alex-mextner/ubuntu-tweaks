"""The shared scaffolding of Python system-module scripts: options, the desktop user, the dispatcher."""

import contextlib
import io
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ubuntu_tweaks import module_script as kit
from ubuntu_tweaks.process import CommandError, CommandResult, Process


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

    def test_transient_install_is_tempfail(self):
        class Offline(FixedModule):
            def apply(self):
                raise kit.TempFail("cannot resolve host")

        code, out, err = self.run_main(Offline([]), ["install"])
        self.assertEqual((code, out, err), (kit.EXIT_TEMPFAIL, "", "fixed: cannot resolve host\n"))
        self.assertEqual(kit.EXIT_TEMPFAIL, 75)

    def test_check_and_dry_run_flags_are_aliases(self):
        module = FixedModule([kit.Finding(False, "b")])
        self.assertEqual(self.run_main(module, ["--check"]), (kit.EXIT_DRIFT, "missing b\n", ""))
        self.assertEqual(self.run_main(module, ["--dry-run"]), (kit.EXIT_OK, "", ""))

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

    def test_a_file_that_grows_after_the_size_check_is_refused(self):
        import os
        from unittest import mock

        real_fstat = os.fstat

        def small(descriptor):  # what fstat said before the owner appended to the file
            info = real_fstat(descriptor)
            return os.stat_result(
                (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid, info.st_gid, 2, 0, 0, 0)
            )

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "f").write_bytes(b"abcdef")
            with mock.patch("os.fstat", small), self.assertRaisesRegex(kit.ModuleError, "more than 4 bytes"):
                kit.read_owned_file(Path(raw) / "f", os.getuid(), 4)


class WriteFile(unittest.TestCase):
    def test_atomic_with_final_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "a/b.conf"
            kit.write_file(target, "x\n", 0o644)
            self.assertEqual((target.read_text(), target.stat().st_mode & 0o777), ("x\n", 0o644))
            kit.write_file(target, json.dumps({"k": 1}), 0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["b.conf"])


class PlanHelpers(unittest.TestCase):
    def test_read_text_or_none(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "f"
            self.assertIsNone(kit.read_text_or_none(path))
            path.write_text("x")
            self.assertEqual(kit.read_text_or_none(path), "x")

    def test_unified_diff_lines_for_a_new_and_a_changed_file(self):
        self.assertEqual(
            kit.unified_diff_lines("/etc/a", None, "one\n"),
            ["    --- /dev/null", "    +++ /etc/a", "    @@ -0,0 +1 @@", "    +one"],
        )
        changed = kit.unified_diff_lines("/etc/a", "one\ntwo\n", "one\n2\n")
        self.assertEqual(changed[:2], ["    --- /etc/a", "    +++ /etc/a"])
        self.assertIn("    -two", changed)
        self.assertIn("    +2", changed)
        self.assertEqual(kit.unified_diff_lines("/etc/a", "same\n", "same\n"), [])


class AptGet(unittest.TestCase):
    """Ticket #126: ``apt-get`` of a root module script must never touch the pty sudo-rs gives it."""

    class Recorder(Process):
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], str | None, object, dict[str, str | None]]] = []

        def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
            self.calls.append(
                (list(argv), input_text, cwd, {name: os.environ.get(name) for name in kit.APT_ENVIRONMENT})
            )

    def test_apt_get_runs_with_stdin_at_eof_and_the_four_noninteractive_variables(self):
        machine = self.Recorder()

        kit.apt_get(machine, "install", "-y", "earlyoom")

        argv, stdin, cwd, seen = machine.calls[0]
        self.assertEqual(argv, ["apt-get", "install", "-y", "earlyoom"])
        self.assertEqual(stdin, "")  # a pipe that is at EOF at once, never the terminal
        self.assertIsNone(cwd)
        self.assertEqual(
            seen,
            {
                "DEBIAN_FRONTEND": "noninteractive",
                "NEEDRESTART_MODE": "a",
                "NEEDRESTART_SUSPEND": "1",
                "APT_LISTCHANGES_FRONTEND": "none",
            },
        )

    def test_the_working_directory_is_passed_on(self):
        machine = self.Recorder()

        kit.apt_get(machine, "download", "earlyoom", cwd=Path("/tmp/x"))

        self.assertEqual(machine.calls[0][2], Path("/tmp/x"))

    def test_the_environment_is_restored_afterwards_and_after_a_failure(self):
        class Failing(Process):
            def execute(self, argv, *, input_text=None, cwd=None):  # type: ignore[no-untyped-def]
                raise CommandError("apt-get install: exited with status 100", 100)

        with mock.patch.dict(os.environ, {"DEBIAN_FRONTEND": "dialog"}):
            os.environ.pop("NEEDRESTART_MODE", None)
            with self.assertRaises(CommandError):
                kit.apt_get(Failing(), "install", "x")
            self.assertEqual(os.environ["DEBIAN_FRONTEND"], "dialog")
            self.assertNotIn("NEEDRESTART_MODE", os.environ)

    def test_the_real_process_hands_the_child_a_closed_stdin_and_the_variables(self):
        with tempfile.TemporaryDirectory() as raw:
            fake = Path(raw) / "apt-get"
            fake.write_text(
                "#!/usr/bin/python3\n"
                "import os, sys\n"
                "print(sys.stdin.isatty(), repr(sys.stdin.read()), os.environ['DEBIAN_FRONTEND'], "
                "os.environ['NEEDRESTART_MODE'], os.environ['NEEDRESTART_SUSPEND'], os.environ['APT_LISTCHANGES_FRONTEND'])\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            out = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"PATH": f"{raw}:{os.environ['PATH']}"}),
                contextlib.redirect_stdout(out),
            ):
                kit.apt_get(Process(), "install", "-y", "x")

        self.assertEqual(out.getvalue().strip(), "False '' noninteractive a 1 none")

    def test_no_root_module_script_runs_apt_get_around_the_helper(self):
        modules = Path(kit.__file__).resolve().parents[1] / "modules"
        offenders = []
        for path in modules.glob("*/*"):
            if not path.is_file() or path.suffix in (".md", ".toml", ".json", ".service", ".timer", ".rules"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if re.search(r"\b(?:execute|_run|run)\(\s*\[?\s*[\"']apt(?:-get)?[\"']", text):
                offenders.append(f"{path.parent.name}/{path.name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

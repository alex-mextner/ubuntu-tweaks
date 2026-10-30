"""The passwords-app finish step: sign-in state, the venv probe with its repair, and the sign-in command."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True


def load_script():
    loader = importlib.machinery.SourceFileLoader("passwords_app_finish", str(MODULE / "finish"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


finish = load_script()


class FakeRunner:
    """Answers by the program: ``status`` for passwords-app, the probe for the venv python, uv and systemctl."""

    def __init__(self, *, state="idle", probes=(finish.FIXED_CODE,), uv=(0, ""), restart=(0, "")):
        self.state = state
        self.probes = list(probes)
        self.uv = uv
        self.restart = restart
        self.calls: list[list[str]] = []

    def capture(self, argv, *, timeout):
        self.calls.append(list(argv))
        program = Path(argv[0]).name
        if program == "passwords-app":
            return 0, json.dumps({"state": self.state, "apple_id": ""})
        if program == "python":
            answer = self.probes.pop(0)
            return (0, f"RESULT ec={answer} em='x'") if answer.lstrip("-").isdigit() else (1, answer)
        if program == "uv":
            return self.uv
        if program == "systemctl":
            return self.restart
        raise AssertionError(f"unexpected {argv}")

    def interactive(self, argv):
        self.calls.append(list(argv))
        return 0


class CheckTests(unittest.TestCase):
    def test_signed_in_is_done_and_signed_out_is_not(self) -> None:
        self.assertEqual(finish.check(FakeRunner(state="idle")), 0)
        self.assertEqual(finish.check(FakeRunner(state="signed-out")), 1)
        self.assertEqual(finish.check(FakeRunner(state="signin-required")), 1)

    def test_an_unreachable_daemon_is_not_done(self) -> None:
        class Down(FakeRunner):
            def capture(self, argv, *, timeout):
                return 3, "the sync service is not reachable"

        self.assertEqual(finish.check(Down()), 1)


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.home = Path(handle.name)
        self.python = self.home / ".local/share/passwords-app/venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("")
        self.env: dict[str, str] = {}

    def prepare(self, runner, **options):
        return finish.prepare(runner, self.env, self.home, options, sleep=lambda seconds: None)

    def test_a_venv_with_the_fix_needs_nothing(self) -> None:
        runner = FakeRunner(probes=["-20970"])
        self.assertEqual(self.prepare(runner), 0)
        self.assertEqual([Path(call[0]).name for call in runner.calls], ["python"])

    def prepare_shown(self, runner, **options):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.prepare(runner, **options)
        return code, out.getvalue()

    def install_package(self, source: dict[str, str], *, installed: dict[str, str] | None = None) -> Path:
        """A checkout whose ``src/passwords_app`` holds ``source`` and a venv with ``installed`` (default: the same)."""
        checkout = self.home / "dev/passwords-app"
        for root, files in (
            (checkout / "src/passwords_app", source),
            (
                self.home / ".local/share/passwords-app/venv/lib/python3.14/site-packages/passwords_app",
                source if installed is None else installed,
            ),
        ):
            shutil.rmtree(root, ignore_errors=True)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
        return checkout

    def test_a_venv_without_the_fix_is_reinstalled_from_the_checkout_and_probed_again(self) -> None:
        runner = FakeRunner(probes=["-80045", "-20970"])
        self.assertEqual(self.prepare(runner, checkout="/srv/passwords-app"), 0)
        programs = [Path(call[0]).name for call in runner.calls]
        self.assertEqual(programs, ["python", "uv", "systemctl", "python"])
        self.assertIn("/srv/passwords-app", runner.calls[1])
        self.assertEqual(runner.calls[2], ["systemctl", "--user", "restart", finish.UNIT])

    def test_a_refusal_that_survives_a_fresh_install_is_not_blamed_on_the_checkout(self) -> None:
        # the venv was just rebuilt from the checkout, so what still runs IS the checkout: Apple refusing
        # the anisette data (Dadoum/anisette-v3-server#61) answers the same -80045 as a missing fix did
        runner = FakeRunner(probes=["-80045", "-80045"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.prepare(runner, checkout="/srv/passwords-app")
        self.assertEqual(code, 1)
        self.assertIn("anisette", out.getvalue())
        self.assertIn("/srv/passwords-app", out.getvalue())
        self.assertNotIn("does not carry the fix", out.getvalue())

    def test_an_answer_after_the_reinstall_that_is_neither_code_is_not_called_a_refusal(self) -> None:
        runner = FakeRunner(probes=["-80045", "ERROR anisette is down"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.prepare(runner)
        self.assertEqual(code, 1)
        self.assertIn("no usable answer: ERROR anisette is down", out.getvalue())

    def test_a_venv_that_already_has_the_files_of_the_checkout_is_neither_reinstalled_nor_the_daemon_restarted(
        self,
    ) -> None:
        self.install_package({"const.py": "A = 1\n", "icloud/gsa.py": "B = 2\n"})
        stale_cache = self.python.parents[1] / "lib/python3.14/site-packages/passwords_app/__pycache__/const.pyc"
        stale_cache.parent.mkdir()
        stale_cache.write_bytes(b"\0")  # bytecode is not what was installed
        runner = FakeRunner(probes=["-80045"])
        code, text = self.prepare_shown(runner)
        self.assertEqual(code, 1)
        self.assertEqual([Path(call[0]).name for call in runner.calls], ["python"])
        self.assertIn("a reinstall would change nothing", text)
        self.assertIn("Apple refuses the anisette data of the local anisette server", text.splitlines()[-1])

    def test_every_python_version_the_venv_has_lived_under_must_match(self) -> None:
        self.install_package({"const.py": "A = 1\n"})
        older = self.python.parents[1] / "lib/python3.13/site-packages/passwords_app"
        older.mkdir(parents=True)
        (older / "const.py").write_text("A = 0\n")
        runner = FakeRunner(probes=["-80045", "-20970"])
        self.assertEqual(self.prepare(runner), 0)
        self.assertIn("uv", [Path(call[0]).name for call in runner.calls])

    def test_a_venv_that_differs_from_the_checkout_is_reinstalled(self) -> None:
        differing = {
            "another content": {"const.py": "A = 0\n"},
            "an extra file": {"const.py": "A = 1\n", "extra.py": ""},
            "a missing file": {},
        }
        for name, installed in differing.items():
            with self.subTest(name):
                self.install_package({"const.py": "A = 1\n"}, installed=installed)
                runner = FakeRunner(probes=["-80045", "-20970"])
                self.assertEqual(self.prepare(runner), 0)
                self.assertIn("uv", [Path(call[0]).name for call in runner.calls])

    def test_a_package_that_cannot_be_read_is_not_proof_that_it_is_current(self) -> None:
        self.install_package({"const.py": "A = 1\n"})
        runner = FakeRunner(probes=["-80045", "-20970"])
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            self.assertEqual(self.prepare(runner), 0)
        self.assertIn("uv", [Path(call[0]).name for call in runner.calls])

    def test_an_uncommitted_edit_in_the_checkout_is_installed_not_blamed_on_apple(self) -> None:
        checkout = self.install_package({"const.py": "A = 1\n"})
        (checkout / "src/passwords_app/const.py").write_text("A = 2\n")
        runner = FakeRunner(probes=["-80045", "-20970"])
        self.assertEqual(self.prepare(runner), 0)
        self.assertIn("uv", [Path(call[0]).name for call in runner.calls])

    def test_apple_refusing_after_the_reinstall_is_blamed_on_the_anisette_data_not_the_checkout(self) -> None:
        runner = FakeRunner(probes=["-80045", "-80045"])
        code, text = self.prepare_shown(runner)
        self.assertEqual(code, 1)
        last = text.splitlines()[-1]
        self.assertIn("Apple refuses the anisette data of the local anisette server (ec=-80045)", last)
        self.assertIn("signing in cannot work until the anisette source changes", last)
        self.assertIn(finish.ANISETTE_ISSUE, last)
        self.assertIn(finish.ANISETTE_UPSTREAM, text)
        self.assertNotIn("does not carry the fix", text)

    def test_a_failed_reinstall_stops_before_the_restart(self) -> None:
        runner = FakeRunner(probes=["-80045"], uv=(1, "no network"))
        self.assertEqual(self.prepare(runner), 1)
        self.assertNotIn("systemctl", [Path(call[0]).name for call in runner.calls])

    def test_an_answer_that_is_neither_code_changes_nothing(self) -> None:
        runner = FakeRunner(probes=["ERROR anisette is down"])
        self.assertEqual(self.prepare(runner), 1)
        self.assertEqual([Path(call[0]).name for call in runner.calls], ["python"])

    def test_no_installation_is_a_pointer_to_the_apply_step(self) -> None:
        self.python.unlink()
        self.assertEqual(self.prepare(FakeRunner()), 1)

    def test_the_data_directory_follows_xdg_data_home(self) -> None:
        other = self.home / "data"
        (other / "passwords-app/venv/bin").mkdir(parents=True)
        (other / "passwords-app/venv/bin/python").write_text("")
        self.env["XDG_DATA_HOME"] = str(other)
        runner = FakeRunner(probes=["-20970"])
        self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.calls[0][0], str(other / "passwords-app/venv/bin/python"))


class RunTests(unittest.TestCase):
    def test_the_apple_id_option_prefills_the_signin_and_nothing_else_is_passed(self) -> None:
        runner = FakeRunner()
        self.assertEqual(finish.run(runner, {"apple_id": "me@example.org"}), 0)
        self.assertEqual(runner.calls, [["passwords-app", "signin", "--apple-id", "me@example.org"]])

    def test_without_the_option_it_is_a_plain_signin(self) -> None:
        runner = FakeRunner()
        finish.run(runner, {})
        self.assertEqual(runner.calls, [["passwords-app", "signin"]])

    def test_an_apple_id_that_looks_like_a_flag_is_refused(self) -> None:
        for bad in ("--evil", "", 3):
            with self.subTest(bad=bad), self.assertRaises(finish.ModuleError):
                finish.apple_id({"apple_id": bad})


class MainTests(unittest.TestCase):
    def test_an_unknown_subcommand_is_a_usage_error(self) -> None:
        self.assertEqual(finish.main(["frobnicate"], {}, FakeRunner()), 2)

    def test_the_module_options_come_from_the_json_environment(self) -> None:
        runner = FakeRunner()
        env = {"UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({"apple_id": "me@example.org"})}
        self.assertEqual(finish.main(["run"], env, runner), 0)
        self.assertIn("me@example.org", runner.calls[0])


if __name__ == "__main__":
    unittest.main()

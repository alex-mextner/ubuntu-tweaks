"""The passwords-app finish step: sign-in state, the venv probe with its repair, and the sign-in command."""

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
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

    def test_a_venv_without_the_fix_is_reinstalled_from_the_checkout_and_probed_again(self) -> None:
        runner = FakeRunner(probes=["-80045", "-20970"])
        self.assertEqual(self.prepare(runner, checkout="/srv/passwords-app"), 0)
        programs = [Path(call[0]).name for call in runner.calls]
        self.assertEqual(programs, ["python", "uv", "systemctl", "python"])
        uv = runner.calls[1]
        self.assertIn("/srv/passwords-app", uv)
        self.assertEqual(runner.calls[2], ["systemctl", "--user", "restart", finish.UNIT])

    def test_a_checkout_without_the_fix_is_reported_not_papered_over(self) -> None:
        runner = FakeRunner(probes=["-80045", "-80045"])
        self.assertEqual(self.prepare(runner), 1)

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

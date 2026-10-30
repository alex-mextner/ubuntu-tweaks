"""The passwords-app finish step: sign-in state, the venv probe with its repair, and the sign-in command."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
import unittest.mock
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
# What the installed package's own `const.DEFAULT_ANISETTE_URL` is in these tests.
DEFAULT_NAME = "DEFAULT_ANISETTE_URL"
INSTALLED_DEFAULT = "http://localhost:6970"


class FakeRunner:
    """Answers by the program: ``status`` for passwords-app, the probe for the venv python, uv and systemctl.

    ``calls`` holds what a step really runs; the ``systemctl show`` lookups of the daemon's anisette URL are in
    ``lookups``. The URL comes from ``unit_env`` (the unit's ``Environment=`` as systemd prints it) or, when
    ``main_pid`` is set, from the running daemon, see ``daemon_environment`` on the tests.
    """

    def __init__(
        self, *, state="idle", probes=(finish.FIXED_CODE,), uv=(0, ""), restart=(0, ""), unit_env="", main_pid="0"
    ):
        self.state = state
        self.unit_env = unit_env
        self.main_pid = main_pid
        self.probes = list(probes)
        self.uv = uv
        self.restart = restart
        self.calls: list[list[str]] = []
        self.lookups: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def capture(self, argv, *, timeout, env=None):
        program = Path(argv[0]).name
        if argv[:3] == ["systemctl", "--user", "show"]:
            self.lookups.append(list(argv))
            return 0, self.main_pid if "--property=MainPID" in argv else self.unit_env
        self.calls.append(list(argv))
        if program == "passwords-app":
            return 0, json.dumps({"state": self.state, "apple_id": ""})
        if program == "python":
            self.envs.append(dict(env or {}))
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
            def capture(self, argv, *, timeout, env=None):
                return 3, "the sync service is not reachable"

        self.assertEqual(finish.check(Down()), 1)


class PrepareCase(unittest.TestCase):
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


class PrepareTests(PrepareCase):
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

    def test_a_failed_reinstall_stops_before_the_restart(self) -> None:
        runner = FakeRunner(probes=["-80045"], uv=(1, "no network"))
        self.assertEqual(self.prepare(runner), 1)
        self.assertNotIn(["systemctl", "--user", "restart", finish.UNIT], runner.calls)

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


SERIALIZED = {
    "plain": ("PASSWORDS_APP_ANISETTE_URL=http://127.0.0.1:6970", "http://127.0.0.1:6970"),
    # what `systemctl show` really prints: the whole assignment is quoted when the value holds `[` or `?`
    "ipv6": ('"PASSWORDS_APP_ANISETTE_URL=http://[::1]:6970"', "http://[::1]:6970"),
    "query": (
        '"PASSWORDS_APP_ANISETTE_URL=https://anisette.example/v1?token=abc"',
        "https://anisette.example/v1?token=abc",
    ),
}
URL = "http://127.0.0.1:6970"


class AnisetteUrlTests(PrepareCase):
    """`finish prepare` must probe through the server the daemon uses, not the one its own environment names."""

    def daemon_environment(self, block: bytes | None):
        return unittest.mock.patch.object(finish, "read_daemon_environment", lambda pid: block)

    def test_the_running_daemons_environment_decides_and_covers_environment_files_and_inheritance(self) -> None:
        runner = FakeRunner(main_pid="4242", unit_env="LANG=C")
        with self.daemon_environment(b"LANG=C\0PASSWORDS_APP_ANISETTE_URL=" + URL.encode() + b"\0HOME=/h\0"):
            self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: URL}])
        self.assertEqual(
            runner.lookups, [["systemctl", "--user", "show", finish.UNIT, "--property=MainPID", "--value"]]
        )

    def test_a_running_daemon_that_sets_none_means_the_installed_default_whatever_the_unit_says(self) -> None:
        runner = FakeRunner(main_pid="4242", unit_env=SERIALIZED["plain"][0])
        with self.daemon_environment(b"LANG=C\0"):
            self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: ""}])

    def test_a_stopped_daemon_falls_back_to_the_units_environment(self) -> None:
        for name, (serialized, url) in SERIALIZED.items():
            with self.subTest(name):
                runner = FakeRunner(unit_env=f"LANG=C {serialized} X=1")
                self.assertEqual(self.prepare(runner), 0)
                self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: url}])
                self.assertEqual(runner.lookups[-1][-2:], ["--property=Environment", "--value"])

    def test_an_unreadable_process_environment_falls_back_to_the_unit(self) -> None:
        runner = FakeRunner(main_pid="4242", unit_env=SERIALIZED["ipv6"][0])
        with self.daemon_environment(None):
            self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: "http://[::1]:6970"}])

    def test_the_scripts_own_environment_is_not_consulted(self) -> None:
        self.env["PASSWORDS_APP_ANISETTE_URL"] = "http://127.0.0.1:6969"
        runner = FakeRunner(unit_env="LANG=C")
        self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: ""}])

    def test_the_same_url_is_probed_again_after_the_reinstall(self) -> None:
        runner = FakeRunner(probes=["-80045", "-20970"], unit_env=SERIALIZED["plain"][0])
        self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: URL}] * 2)

    def test_a_refusal_that_survives_the_reinstall_names_the_server_and_the_cause(self) -> None:
        runner = FakeRunner(probes=["-80045", "-80045"], unit_env="PASSWORDS_APP_ANISETTE_URL=http://127.0.0.1:6969")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.prepare(runner), 1)
        text = out.getvalue()
        self.assertIn("http://127.0.0.1:6969", text)
        self.assertIn("anisette", text)
        self.assertIn("Windows or macOS", text)

    def test_a_failed_systemctl_show_means_no_url_and_the_default(self) -> None:
        class NoSystemd(FakeRunner):
            def capture(self, argv, *, timeout, env=None):
                if argv[:3] == ["systemctl", "--user", "show"]:
                    return 1, "no user manager"
                return super().capture(argv, timeout=timeout, env=env)

        runner = NoSystemd()
        self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: ""}])


class CredentialTests(PrepareCase):
    """A URL may carry user info or a token: it must reach neither a command line nor the output."""

    SECRET_URL = "https://alice:s3cret@anisette.example/v1?token=abc"

    def run_prepare(self, probes):
        runner = FakeRunner(probes=probes, unit_env=f'"PASSWORDS_APP_ANISETTE_URL={self.SECRET_URL}"')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.prepare(runner)
        return runner, code, out.getvalue()

    def assert_hidden(self, runner, output) -> None:
        for call in runner.calls + runner.lookups:
            for argument in call:
                self.assertNotIn("s3cret", argument)
                self.assertNotIn("token=abc", argument)
        self.assertNotIn("s3cret", output)
        self.assertNotIn("token=abc", output)
        self.assertNotIn("alice", output)

    def test_the_url_travels_in_the_probes_environment_and_the_success_line_hides_it(self) -> None:
        runner, code, output = self.run_prepare(["-20970"])
        self.assertEqual(code, 0)
        self.assertEqual(runner.envs, [{finish.PROBE_URL_VARIABLE: self.SECRET_URL}])
        self.assertIn("https://anisette.example/v1", output)
        self.assert_hidden(runner, output)

    def test_a_refusal_message_hides_it(self) -> None:
        runner, code, output = self.run_prepare(["-80045", "-80045"])
        self.assertEqual(code, 1)
        self.assertIn("https://anisette.example/v1", output)
        self.assert_hidden(runner, output)

    def test_an_error_that_quotes_the_url_is_shown_without_the_credentials(self) -> None:
        failure = f"AnisetteError: Could not get anisette data from {self.SECRET_URL}: refused"
        runner, code, output = self.run_prepare([failure])
        self.assertEqual(code, 1)
        self.assertIn("https://anisette.example/v1", output)
        self.assert_hidden(runner, output)


class ScrubTests(PrepareCase):
    def test_a_normalised_url_in_an_error_is_scrubbed_too(self) -> None:
        # the installed client strips the trailing slash before it quotes the URL
        url = "https://alice:s3cret@anisette.example/v1/?token=abc"
        failure = (
            "AnisetteError: Could not get anisette data from https://alice:s3cret@anisette.example/v1?token=abc: x"
        )
        runner = FakeRunner(probes=[failure], unit_env=f'"PASSWORDS_APP_ANISETTE_URL={url}"')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.prepare(runner), 1)
        for hidden in ("s3cret", "alice", "token=abc"):
            self.assertNotIn(hidden, out.getvalue())
        self.assertIn("https://anisette.example/v1", out.getvalue())

    def test_dollar_signs_in_systemds_escaping_come_back_unchanged(self) -> None:
        runner = FakeRunner(unit_env='"PASSWORDS_APP_ANISETTE_URL=https://u:pa\\$ss@anisette.example/v1?t=a\\$b"')
        self.assertEqual(finish.daemon_anisette_url(runner), "https://u:pa$ss@anisette.example/v1?t=a$b")

    def test_the_second_probe_uses_the_url_the_restarted_daemon_has(self) -> None:
        class Switching(FakeRunner):
            def capture(self, argv, *, timeout, env=None):
                if argv[:2] == ["systemctl", "--user"] and argv[2] == "restart":
                    self.unit_env = "PASSWORDS_APP_ANISETTE_URL=http://127.0.0.1:7000"
                return super().capture(argv, timeout=timeout, env=env)

        runner = Switching(probes=["-80045", "-20970"], unit_env="PASSWORDS_APP_ANISETTE_URL=http://127.0.0.1:6000")
        self.assertEqual(self.prepare(runner), 0)
        self.assertEqual(
            [env[finish.PROBE_URL_VARIABLE] for env in runner.envs], ["http://127.0.0.1:6000", "http://127.0.0.1:7000"]
        )


class SystemRunnerTests(unittest.TestCase):
    def test_the_given_environment_is_added_to_the_inherited_one(self) -> None:
        code, text = finish.SystemRunner().capture(
            [sys.executable, "-c", "import os; print(os.environ['FINISH_TEST_VALUE'], 'PATH' in os.environ)"],
            timeout=20,
            env={"FINISH_TEST_VALUE": "yes"},
        )
        self.assertEqual((code, text), (0, "yes True"))


class ProbeScriptTests(unittest.TestCase):
    """The PROBE source itself, run against stand-ins for the installed passwords_app and srp modules."""

    def run_probe(self, url: str) -> dict[str, str]:
        seen: dict[str, str] = {}

        class Anisette:
            def __init__(self, url):
                seen["url"] = url

        class User:
            def __init__(self, name, *args, **kwargs):
                seen["name"] = name

            def start_authentication(self):
                return b"", b"A"

        class GSAClient:
            def __init__(self, device, anisette):
                pass

            def _request(self, parameters):
                seen["operation"] = parameters["o"]
                return {"Status": {"ec": -20970}}

        def module(name, **members):
            made = types.ModuleType(name)
            vars(made).update(members)
            return made

        pysrp = module("srp._pysrp", User=User, SHA256=1, NG_2048=2)
        fakes = {
            "srp": module("srp", _pysrp=pysrp),
            "srp._pysrp": pysrp,
            "passwords_app": module("passwords_app"),
            "passwords_app.icloud": module("passwords_app.icloud"),
            "passwords_app.icloud.const": module("passwords_app.icloud.const", **{DEFAULT_NAME: INSTALLED_DEFAULT}),
            "passwords_app.icloud.auth": module("passwords_app.icloud.auth"),
            "passwords_app.icloud.auth.anisette": module("passwords_app.icloud.auth.anisette", Anisette=Anisette),
            "passwords_app.icloud.auth.device": module(
                "passwords_app.icloud.auth.device", Device=types.SimpleNamespace(generate=lambda: None)
            ),
            "passwords_app.icloud.auth.gsa": module(
                "passwords_app.icloud.auth.gsa",
                GSAClient=GSAClient,
                GSAError=Exception,
                _S2K_PROTOCOLS=("s2k",),
                _status=lambda reply: "ec=-20970",
            ),
        }
        environment = {finish.PROBE_URL_VARIABLE: url, "PASSWORDS_APP_ANISETTE_URL": "http://elsewhere:1"}
        with unittest.mock.patch.dict(sys.modules, fakes), unittest.mock.patch.dict("os.environ", environment):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                exec(finish.PROBE, {})
        seen["printed"] = out.getvalue().strip()
        return seen

    def test_the_probe_url_selects_the_anisette_server(self) -> None:
        seen = self.run_probe("http://127.0.0.1:6970")
        self.assertEqual(seen["url"], "http://127.0.0.1:6970")
        self.assertEqual((seen["operation"], seen["printed"]), ("init", "RESULT ec=-20970"))

    def test_every_run_asks_about_a_fresh_apple_id_that_cannot_exist(self) -> None:
        first, second = self.run_probe("")["name"], self.run_probe("")["name"]
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("@example.invalid"))

    def test_an_empty_url_falls_back_to_the_installed_default_not_to_the_scripts_environment(self) -> None:
        self.assertEqual(self.run_probe("")["url"], INSTALLED_DEFAULT)


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

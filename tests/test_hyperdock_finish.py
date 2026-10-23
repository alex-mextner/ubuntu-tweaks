"""The hyperdock finish step: the dock counts as loaded only when GNOME Shell reports its extension ACTIVE."""

import importlib.machinery
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout

from tests.util import TempDirCase, write
from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.manifest import load_manifest

SCRIPT = REPO_ROOT / "modules/hyperdock/finish"


def load_script():
    loader = importlib.machinery.SourceFileLoader("hyperdock_finish", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


finish = load_script()
UUID = "hyperdock@example.org"


class FakeShell:
    def __init__(self, code: int, text: str) -> None:
        self.answer = (code, text)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def capture(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        return self.answer


class CheckTests(TempDirCase):
    def checked_out(self, uuid: str = UUID) -> None:
        write(self.tmp / finish.METADATA, json.dumps({"uuid": uuid}))

    def check(self, shell: FakeShell) -> tuple[int, str]:
        with redirect_stdout(io.StringIO()) as out:
            code = finish.check(shell, self.tmp, {"LANG": "ru_RU.UTF-8"})
        return code, out.getvalue()

    def test_active_is_done_and_the_uuid_comes_from_the_checkout(self) -> None:
        self.checked_out("renamed@example.org")
        shell = FakeShell(0, "renamed@example.org\n  Name: Dock\n  State: ACTIVE\n")
        self.assertEqual(self.check(shell)[0], 0)
        self.assertEqual(shell.calls[0][0], ["gnome-extensions", "info", "renamed@example.org"])

    def test_the_output_is_asked_for_in_english_whatever_the_session_language_is(self) -> None:
        self.checked_out()
        shell = FakeShell(0, "State: ACTIVE")
        self.check(shell)
        self.assertEqual(shell.calls[0][1]["LC_ALL"], "C")

    def test_any_other_state_asks_for_the_relogin(self) -> None:
        self.checked_out()
        for state in ("INITIALIZED", "ENABLED", "ERROR", "OUT OF DATE"):
            with self.subTest(state=state):
                code, text = self.check(FakeShell(0, f"State: {state.split()[0]}"))
                self.assertEqual(code, 1)
                self.assertIn("log out", text)

    def test_an_extension_the_shell_does_not_know_asks_for_the_relogin(self) -> None:
        self.checked_out()
        code, text = self.check(FakeShell(2, f"Extension “{UUID}” doesn't exist"))
        self.assertEqual(code, 1)
        self.assertIn(UUID, text)

    def test_no_checkout_points_at_the_apply_step(self) -> None:
        code, text = self.check(FakeShell(0, "State: ACTIVE"))
        self.assertEqual(code, 1)
        self.assertIn("apply", text)

    def test_unknown_arguments_are_a_usage_error(self) -> None:
        self.assertEqual(finish.main(["run"]), 2)


class ManifestTests(unittest.TestCase):
    def test_the_module_declares_the_step_after_apply_and_it_has_nothing_to_run(self) -> None:
        (step,) = load_manifest(REPO_ROOT / "modules/hyperdock").finish
        self.assertEqual((step.id, step.requires, step.run, step.prepare), ("dock-relogin", ("apply",), (), ()))


if __name__ == "__main__":
    unittest.main()

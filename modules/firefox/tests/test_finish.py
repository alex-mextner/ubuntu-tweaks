"""The firefox finish step: the AMO key lookup, the page that creates it and the hidden-input store."""

import importlib.machinery
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_script():
    loader = importlib.machinery.SourceFileLoader("firefox_finish", str(MODULE / "finish"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


finish = load_script()
FIRST_HALF = "user:12345:67890"
SECOND_HALF = "0f" * 32


class FakeKeyring:
    """secret-tool and xdg-open over a dict; ``installed=False`` makes secret-tool missing."""

    def __init__(self, *, installed=True, stored=None, opens=True):
        self.installed = installed
        self.items = dict(stored or {})
        self.opens = opens
        self.calls: list[list[str]] = []

    def open_url(self, url):
        self.calls.append(["xdg-open", url])
        return 0 if self.opens else 1

    def capture(self, argv, *, stdin=None):
        self.calls.append(list(argv))
        if not self.installed:
            return 127, "secret-tool: not found"
        purpose = argv[argv.index("purpose") + 1]
        if argv[1] == "lookup":
            return (0, self.items[purpose]) if purpose in self.items else (1, "")
        self.items[purpose] = stdin
        return 0, ""


def quiet(function, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = function(*args, **kwargs)
    return result, out.getvalue()


class CheckTests(unittest.TestCase):
    def test_both_halves_present_is_done(self) -> None:
        keyring = FakeKeyring(stored={finish.ISSUER_PURPOSE: FIRST_HALF, finish.SECRET_PURPOSE: SECOND_HALF})
        self.assertEqual(quiet(finish.check, keyring)[0], 0)

    def test_one_half_is_not_done(self) -> None:
        keyring = FakeKeyring(stored={finish.ISSUER_PURPOSE: FIRST_HALF})
        self.assertEqual(quiet(finish.check, keyring)[0], 1)

    def test_a_missing_secret_tool_says_which_step_installs_it(self) -> None:
        code, text = quiet(finish.check, FakeKeyring(installed=False))
        self.assertEqual(code, 1)
        self.assertIn("apply", text)


class PrepareTests(unittest.TestCase):
    def test_it_opens_the_key_page(self) -> None:
        keyring = FakeKeyring()
        self.assertEqual(quiet(finish.prepare, keyring)[0], 0)
        self.assertIn(["xdg-open", finish.KEY_PAGE], keyring.calls)

    def test_no_browser_is_not_a_failure_the_url_is_printed(self) -> None:
        code, text = quiet(finish.prepare, FakeKeyring(opens=False))
        self.assertEqual(code, 0)
        self.assertIn(finish.KEY_PAGE, text)

    def test_without_secret_tool_it_stops_before_opening_anything(self) -> None:
        keyring = FakeKeyring(installed=False)
        self.assertEqual(quiet(finish.prepare, keyring)[0], 1)
        self.assertNotIn("xdg-open", [call[0] for call in keyring.calls])


class RunTests(unittest.TestCase):
    def reader(self, *answers: str):
        queue = list(answers)
        return lambda prompt: queue.pop(0)

    def test_both_halves_are_stored_through_stdin_and_never_printed(self) -> None:
        keyring = FakeKeyring()
        code, text = quiet(finish.run, keyring, self.reader(FIRST_HALF, SECOND_HALF))
        self.assertEqual(code, 0)
        self.assertEqual(keyring.items, {finish.ISSUER_PURPOSE: FIRST_HALF, finish.SECRET_PURPOSE: SECOND_HALF})
        self.assertNotIn(FIRST_HALF, text)
        self.assertNotIn(SECOND_HALF, text)
        for call in keyring.calls:  # never on a command line, where `ps` would show it
            self.assertNotIn(FIRST_HALF, " ".join(call))
            self.assertNotIn(SECOND_HALF, " ".join(call))

    def test_a_malformed_value_is_asked_again_and_never_stored(self) -> None:
        keyring = FakeKeyring()
        code, _ = quiet(finish.run, keyring, self.reader("oops", FIRST_HALF, SECOND_HALF))
        self.assertEqual(code, 0)
        self.assertEqual(len(keyring.items), 2)

    def test_three_bad_answers_give_up_without_storing(self) -> None:
        keyring = FakeKeyring()
        code, _ = quiet(finish.run, keyring, self.reader("a", "b", "c"))
        self.assertEqual(code, 1)
        self.assertEqual(keyring.items, {})

    def test_a_keyring_that_refuses_is_reported(self) -> None:
        class Locked(FakeKeyring):
            def capture(self, argv, *, stdin=None):
                return (1, "locked") if argv[1] == "store" else super().capture(argv, stdin=stdin)

        code, text = quiet(finish.run, Locked(), self.reader(FIRST_HALF, SECOND_HALF))
        self.assertEqual(code, 1)
        self.assertIn("keyring", text)


class MainTests(unittest.TestCase):
    def test_unknown_subcommand_is_a_usage_error(self) -> None:
        self.assertEqual(quiet(finish.main, ["nope"], FakeKeyring())[0], 2)


if __name__ == "__main__":
    unittest.main()

"""``[[finish]]`` in module.toml: the human-only steps a module declares for ``ubuntu-tweak finish``."""

import unittest
from pathlib import Path

from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import parse_manifest

BASE = """
[source]
kind = "system"
install = "x install"
check = "x check"
"""

STEP = """
[[finish]]
id = "icloud-signin"
title = "Sign in to iCloud"
title_ru = "Войти в iCloud"
why = "Apple ID password and the 2FA code are yours to type"
action = "Type the password"
action_ru = "Введи пароль"
check = ["./finish", "icloud-signin", "check"]
prepare = ["./finish", "icloud-signin", "prepare"]
run = ["passwords-app", "signin"]
gate = ["./finish", "icloud-signin", "gate"]
requires = ["apply"]
optional = true
reapply = true
"""


def parse(text: str):
    return parse_manifest("demo", BASE + text, Path("/modules/demo"))


class FinishTableTests(unittest.TestCase):
    def test_a_module_without_finish_tables_has_none(self) -> None:
        self.assertEqual(parse("").finish, ())

    def test_every_key_is_read(self) -> None:
        (step,) = parse(STEP).finish
        self.assertEqual(step.id, "icloud-signin")
        self.assertEqual((step.title, step.title_ru), ("Sign in to iCloud", "Войти в iCloud"))
        self.assertEqual(step.why_ru, "")  # a missing translation is empty: the English text is used instead
        self.assertEqual((step.action, step.action_ru), ("Type the password", "Введи пароль"))
        self.assertEqual(step.check, ("./finish", "icloud-signin", "check"))
        self.assertEqual(step.prepare, ("./finish", "icloud-signin", "prepare"))
        self.assertEqual(step.run, ("passwords-app", "signin"))
        self.assertEqual(step.gate, ("./finish", "icloud-signin", "gate"))
        self.assertEqual(step.requires, ("apply",))
        self.assertTrue(step.optional)
        self.assertTrue(step.reapply)

    def test_the_optional_keys_default_to_nothing(self) -> None:
        (step,) = parse('[[finish]]\nid = "a"\ntitle = "T"\nwhy = "W"\naction = "A"\ncheck = ["true"]\n').finish
        self.assertEqual(
            (step.prepare, step.run, step.gate, step.requires, step.optional, step.reapply),
            ((), (), (), (), False, False),
        )

    def test_steps_keep_file_order(self) -> None:
        text = "".join(
            f'[[finish]]\nid = "{name}"\ntitle = "T"\nwhy = "W"\naction = "A"\ncheck = ["true"]\n'
            for name in ("b", "a", "c")
        )
        self.assertEqual([step.id for step in parse(text).finish], ["b", "a", "c"])

    def test_a_missing_required_key_names_the_table(self) -> None:
        for missing in ("id", "title", "why", "action", "check"):
            lines = [
                line
                for line in STEP.splitlines()
                if not line.startswith(f"{missing} =") and not line.startswith(f"{missing}_ru =")
            ]
            wording = (
                "'check' must be a non-empty argv list" if missing == "check" else f"missing required key '{missing}'"
            )
            with self.subTest(missing=missing), self.assertRaisesRegex(ConfigError, wording):
                parse("\n".join(lines))

    def test_unknown_keys_are_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            parse(STEP + 'colour = "red"\n')

    def test_duplicate_ids_in_one_module_are_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            parse(STEP + STEP)

    def test_the_id_is_a_lowercase_slug(self) -> None:
        for bad in ("Has Space", "UPPER", "a/b", ""):
            with self.subTest(id=bad), self.assertRaises(ConfigError):
                parse(STEP.replace('id = "icloud-signin"', f'id = "{bad}"'))

    def test_a_command_is_an_argv_list_and_may_not_climb_out_of_the_module(self) -> None:
        for bad in ('"not-a-list"', "[]", '["../x"]', '["sub/../x"]', '["dir/x"]', '["./"]', "[1]"):
            with self.subTest(check=bad), self.assertRaises(ConfigError):
                parse(STEP.replace('check = ["./finish", "icloud-signin", "check"]', f"check = {bad}"))

    def test_a_program_on_path_an_absolute_path_and_dot_slash_are_accepted(self) -> None:
        for good in ('["true"]', '["/usr/bin/true"]', '["./finish"]', '["./bin/finish", "x"]'):
            with self.subTest(check=good):
                text = f'[[finish]]\nid = "a"\ntitle = "T"\nwhy = "W"\naction = "A"\ncheck = {good}\n'
                self.assertEqual(len(parse(text).finish), 1)

    def test_a_finish_table_must_be_an_array_of_tables(self) -> None:
        with self.assertRaisesRegex(ConfigError, "array of tables"):
            parse_manifest("demo", 'finish = "x"\n' + BASE, Path("/modules/demo"))


if __name__ == "__main__":
    unittest.main()

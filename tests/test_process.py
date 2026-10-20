"""The real ``Process`` with harmless commands (no root, no machine state), and ``elevate`` with patched lookups."""

import unittest
from unittest import mock

from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess, Process


class QueryTests(unittest.TestCase):
    def test_success_failure_and_missing_binary(self) -> None:
        process = Process()

        self.assertEqual(process.query(["true"]), CommandResult(0))
        self.assertEqual(process.query(["false"]).returncode, 1)
        missing = process.query(["/nonexistent/ubuntu-tweak-test"])
        self.assertEqual(missing.returncode, 127)
        self.assertIn("command not found", missing.stderr)

    def test_captures_output(self) -> None:
        self.assertEqual(Process().query(["echo", "hi"]).stdout, "hi\n")

    def test_timeout_is_124(self) -> None:
        result = Process().query(["sleep", "5"], timeout=0.1)

        self.assertEqual(result.returncode, 124)
        self.assertFalse(result.ok)

    def test_a_file_that_cannot_be_executed_is_126(self) -> None:
        result = Process().query([__file__])  # a source file without the exec bit

        self.assertEqual(result.returncode, 126)
        self.assertIn("cannot run", result.stderr)


class ExecuteTests(unittest.TestCase):
    def test_non_zero_exit_and_missing_binary_raise(self) -> None:
        process = Process()
        with self.assertRaisesRegex(CommandError, "exited with status 1"):
            process.execute(["false"])
        with self.assertRaisesRegex(CommandError, "command not found"):
            process.execute(["/nonexistent/ubuntu-tweak-test"])

    def test_a_file_that_cannot_be_executed_raises(self) -> None:
        with self.assertRaisesRegex(CommandError, "cannot run"):
            Process().execute([__file__])

    def test_zero_exit_with_input_is_fine(self) -> None:
        Process().execute(["true"], input_text="ignored\n")


class ElevateTests(unittest.TestCase):
    def elevate(self, *, euid: int, tools: set[str]) -> list[str]:
        with (
            mock.patch("os.geteuid", return_value=euid),
            mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in tools else None),
        ):
            return Process().elevate(["apt-get", "install"])

    def test_root_runs_as_is(self) -> None:
        self.assertEqual(self.elevate(euid=0, tools=set()), ["apt-get", "install"])

    def test_sudo_is_preferred_then_pkexec(self) -> None:
        self.assertEqual(self.elevate(euid=1000, tools={"sudo", "pkexec"}), ["sudo", "--", "apt-get", "install"])
        self.assertEqual(self.elevate(euid=1000, tools={"pkexec"}), ["pkexec", "apt-get", "install"])

    def test_neither_is_an_error(self) -> None:
        with self.assertRaisesRegex(CommandError, "neither sudo nor pkexec"):
            self.elevate(euid=1000, tools=set())


class FakeProcessTests(unittest.TestCase):
    def test_records_calls_and_answers_127_for_unknown_commands(self) -> None:
        fake = FakeProcess({("a",): CommandResult(0, "out")})

        self.assertEqual(fake.query(["a"]).stdout, "out")
        self.assertEqual(fake.query(["b"]).returncode, 127)
        fake.execute(["c"], input_text="x")
        self.assertEqual(fake.queries, [("a",), ("b",)])
        self.assertEqual(fake.executed, [(("c",), "x")])


if __name__ == "__main__":
    unittest.main()

"""The user-level package actions (uv, npm, bun, cargo, pipx) of the packages planner, on a fake machine."""

import unittest
from collections.abc import Sequence
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks import tool_packages
from ubuntu_tweaks.actions import Plan, RetryPolicy
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.planners import packages as packages_planner
from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend

HOME = Path("/home/u")


def listing(manager: str, names: set[str]) -> str:
    rows = sorted(names)
    match manager:
        case "uv":
            return "".join(f"{n} v1\n- {n}\n" for n in rows)
        case "npm":
            return "/home/u/.local/lib\n" + "".join(f"/home/u/.local/lib/node_modules/{n}\n" for n in rows)
        case "bun":
            return "/home/u/.bun/install/global node_modules\n" + "".join(f"├── {n}@1\n" for n in rows)
        case "cargo":
            return "".join(f"{n} v1:\n    {n}\n" for n in rows)
    return "".join(f"{n} 1\n" for n in rows)


class ToolMachine(FakeProcess):
    """Binaries present and names installed per manager; installs add the parsed name unless told to fail."""

    def __init__(self, present: set[str], failures: dict[str, list[str | None]] | None = None) -> None:
        super().__init__()
        self.present = set(present)
        self.installed: dict[str, set[str]] = {manager: set() for manager in tool_packages.TOOL_MANAGERS}
        self.failures = (
            failures or {}
        )  # "manager:spec" -> messages, one per attempt, the last one repeats; None installs

    def query(self, argv: Sequence[str], *, timeout: float = 60.0, cwd: Path | None = None) -> CommandResult:
        super().query(argv, timeout=timeout)
        manager = argv[0]
        if manager not in self.present:
            return CommandResult(127)
        if list(argv) == [manager, "--version"]:
            return CommandResult(0, "1.0\n")
        if list(argv) == tool_packages.list_argv(manager, HOME):
            return CommandResult(0, listing(manager, self.installed[manager]))
        return CommandResult(1)

    def execute(self, argv: Sequence[str], *, input_text: str | None = None, cwd: Path | None = None) -> None:
        super().execute(argv, input_text=input_text)
        manager, spec = argv[0], argv[-1]
        if manager not in self.present:
            raise CommandError(f"{manager}: command not found")
        queue = self.failures.get(f"{manager}:{spec}")
        if queue:
            message = queue.pop(0) if len(queue) > 1 else queue[0]
            if message is not None:
                raise CommandError(f"{' '.join(argv)}: {message}", 1)
        self.installed[manager].add(tool_packages.parse_spec(manager, spec))


class ToolCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "profile"
        self.root.mkdir()
        (self.tmp / "repo").mkdir()
        self.ctx = Context(self.tmp / "repo", Profile(self.root, "home"))

    def plan(self, machine: ToolMachine) -> Plan:
        return Plan.build(packages_planner.build(self.ctx, FakeBackend(), process=machine, home=HOME))

    @staticmethod
    def policy(slept: list[float] | None = None) -> RetryPolicy:
        sink = slept if slept is not None else []
        return RetryPolicy(attempts=3, base=1.0, cap=1.0, sleep=sink.append, jitter=lambda low, high: high)


class PlanTests(ToolCase):
    def test_one_user_level_action_per_manager_with_the_missing_specs(self) -> None:
        write(
            self.root / "packages.toml",
            'uv = ["glances[web]", "esphome==2026.1"]\nnpm = ["@openai/codex"]\nbun = ["cf"]\n'
            'cargo = ["wasm-bindgen-cli"]\npipx = ["black"]\n',
        )
        machine = ToolMachine({"uv", "npm", "bun", "cargo", "pipx"})
        machine.installed["uv"] = {"esphome"}
        plan = self.plan(machine)

        self.assertEqual([a.kind for a in plan.actions], ["package"] * 5)
        self.assertEqual([a.needs_root for a in plan.actions], [False] * 5)
        self.assertEqual(plan.actions[0].describe(), "uv: install glances[web]")
        self.assertEqual(machine.executed, [])

    def test_exact_argv_per_manager_and_the_second_plan_is_empty(self) -> None:
        write(
            self.root / "packages.toml",
            'uv = ["glances[web]", "hyper-qa @ git+https://git.example.com/acme/qa-cli.git"]\n'
            'npm = ["@openai/codex@0.5.1"]\nbun = ["cf"]\ncargo = ["wasm-bindgen-cli"]\npipx = ["black"]\n',
        )
        machine = ToolMachine({"uv", "npm", "bun", "cargo", "pipx"})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(result.failed, ())
        self.assertEqual(
            [argv for argv, _ in machine.executed],
            [
                ("uv", "tool", "install", "glances[web]"),
                ("uv", "tool", "install", "hyper-qa @ git+https://git.example.com/acme/qa-cli.git"),
                ("npm", "install", "-g", "--prefix", "/home/u/.local", "@openai/codex@0.5.1"),
                ("bun", "add", "-g", "cf"),
                ("cargo", "install", "wasm-bindgen-cli"),
                ("pipx", "install", "black"),
            ],
        )
        self.assertEqual(len(self.plan(machine)), 0)

    def test_installs_never_go_through_sudo(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances"]\ncargo = ["x"]\n')
        machine = ToolMachine({"uv", "cargo"})
        self.plan(machine).execute(policy=self.policy())
        self.assertEqual(len(machine.executed), 2)
        self.assertTrue(all("sudo" not in argv for argv, _ in machine.executed))

    def test_overlay_entries_are_unioned_with_the_shared_ones(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances"]\n')
        write(self.root / "machines/home/packages.toml", 'uv = ["glances", "py-spy"]\n')
        machine = ToolMachine({"uv"})
        self.plan(machine).execute(policy=self.policy())
        self.assertEqual(machine.installed["uv"], {"glances", "py-spy"})

    def test_an_installed_name_matches_a_spec_with_extras_and_a_different_spelling(self) -> None:
        write(self.root / "packages.toml", 'uv = ["Git_Filter.Repo", "glances[web]"]\n')
        machine = ToolMachine({"uv"})
        machine.installed["uv"] = {"git-filter-repo", "glances"}
        self.assertEqual(len(self.plan(machine)), 0)

    def test_invalid_specs_are_config_errors(self) -> None:
        for manager, bad in (
            ("uv", "--index-url=x"),
            ("npm", "a b"),
            ("bun", "-x"),
            ("cargo", "x;id"),
            ("pipx", "$(id)"),
        ):
            with self.subTest(manager=manager):
                write(self.root / "packages.toml", f'{manager} = ["{bad}"]\n')
                with self.assertRaisesRegex(ConfigError, f"packages.toml {manager}: '.*' is not a valid package spec"):
                    self.plan(ToolMachine(set()))

    def test_two_specs_for_one_name_are_config_errors_also_across_layers(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances", "Glances[web]"]\n')
        with self.assertRaisesRegex(ConfigError, "both install glances"):
            self.plan(ToolMachine({"uv"}))
        write(self.root / "packages.toml", 'uv = ["esphome==2025.1"]\n')
        write(self.root / "machines/home/packages.toml", 'uv = ["esphome==2026.1"]\n')
        with self.assertRaisesRegex(ConfigError, r"shared or home overlay\) uv: .*both install esphome"):
            self.plan(ToolMachine({"uv"}))

    def test_a_name_under_a_manager_and_its_fallback_is_config_error(self) -> None:
        for primary, fallback in (("uv", "pipx"), ("npm", "bun")):
            with self.subTest(primary=primary):
                write(self.root / "packages.toml", f'{primary} = ["black"]\n')
                write(self.root / "machines/home/packages.toml", f'{fallback} = ["black"]\n')
                with self.assertRaisesRegex(ConfigError, f"black listed under both {primary} and {fallback}"):
                    self.plan(ToolMachine({primary}))
        write(self.root / "packages.toml", 'uv = ["black"]\npipx = ["httpie"]\n')
        (self.root / "machines/home/packages.toml").unlink()
        self.plan(ToolMachine({"uv", "pipx"}))  # different names are fine

    def test_a_non_list_value_is_config_error(self) -> None:
        write(self.root / "packages.toml", 'uv = "glances"\n')
        with self.assertRaisesRegex(ConfigError, "uv must be a list"):
            self.plan(ToolMachine({"uv"}))


class FailureTests(ToolCase):
    def test_one_failing_package_does_not_hide_the_others(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["broken", "wasm-bindgen-cli", "ok2"]\n')
        machine = ToolMachine({"cargo"}, {"cargo:broken": ["could not compile `broken`"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(machine.installed["cargo"], {"wasm-bindgen-cli", "ok2"})
        self.assertEqual(len(result.failed), 1)
        failure = result.failed[0]
        self.assertIn("broken", failure.message)
        self.assertNotIn("wasm-bindgen-cli", failure.message)
        self.assertFalse(failure.transient)
        self.assertEqual(failure.attempts, 1)

    def test_the_real_error_format_with_the_managers_last_stderr_line_is_classified(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["a", "b"]\n')
        machine = ToolMachine(
            {"cargo"},
            {
                "cargo:a": [
                    "exited with status 101: error: failed to download: spurious network error (2 tries): Temporary failure in name resolution",
                    None,
                ],
                "cargo:b": ["exited with status 101: error: could not compile `b`"],
            },
        )
        slept: list[float] = []
        result = self.plan(machine).execute(policy=self.policy(slept))

        self.assertEqual(slept, [1.0])  # `a` retried once: the network cause is in the last stderr line
        self.assertEqual(machine.installed["cargo"], {"a"})
        self.assertEqual(len(result.failed), 1)
        self.assertFalse(result.failed[0].transient)  # `b` is a compile error

    def test_a_bare_exit_status_without_stderr_is_permanent(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["a"]\n')
        machine = ToolMachine({"cargo"}, {"cargo:a": ["exited with status 101"]})
        slept: list[float] = []
        self.plan(machine).execute(policy=self.policy(slept))
        self.assertEqual(slept, [])

    def test_a_package_named_like_a_network_error_is_not_a_network_error(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["pytest-timeout"]\n')
        machine = ToolMachine({"cargo"}, {"cargo:pytest-timeout": ["exited with status 1"]})
        slept: list[float] = []
        result = self.plan(machine).execute(policy=self.policy(slept))

        self.assertEqual(slept, [])  # the argv names `timeout`, the failure does not
        self.assertEqual(len(machine.executed), 1)
        self.assertFalse(result.failed[0].transient)

    def test_a_network_error_is_transient_and_only_the_failed_package_is_retried(self) -> None:
        write(self.root / "packages.toml", 'npm = ["flaky", "stable"]\n')
        machine = ToolMachine({"npm"}, {"npm:flaky": ["getaddrinfo: Could not resolve host", None]})
        slept: list[float] = []
        result = self.plan(machine).execute(policy=self.policy(slept))

        self.assertEqual(result.failed, ())
        self.assertEqual(machine.installed["npm"], {"flaky", "stable"})
        self.assertEqual(slept, [1.0])
        installs = [argv[-1] for argv, _ in machine.executed]
        self.assertEqual(installs, ["flaky", "stable", "flaky"])

    def test_transient_failures_that_never_clear_are_reported_as_transient_after_all_attempts(self) -> None:
        write(self.root / "packages.toml", 'npm = ["flaky"]\n')
        machine = ToolMachine({"npm"}, {"npm:flaky": ["connection reset by peer"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual((len(result.failed), result.failed[0].transient, result.failed[0].attempts), (1, True, 3))

    def test_a_permanent_failure_is_not_retried_because_a_sibling_was_transient(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["broken", "flaky"]\n')
        machine = ToolMachine(
            {"cargo"}, {"cargo:broken": ["could not compile"], "cargo:flaky": ["connection reset", None]}
        )
        result = self.plan(machine).execute(policy=self.policy())

        installs = [argv[-1] for argv, _ in machine.executed]
        self.assertEqual(installs, ["broken", "flaky", "flaky"])  # broken once, flaky retried
        self.assertEqual(machine.installed["cargo"], {"flaky"})
        self.assertEqual(len(result.failed), 1)
        self.assertIn("broken", result.failed[0].message)
        self.assertFalse(result.failed[0].transient)

    def test_the_identity_of_the_action_does_not_change_while_it_runs(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["a", "b"]\n')
        machine = ToolMachine({"cargo"}, {"cargo:a": ["could not compile"]})
        plan = self.plan(machine)
        action = plan.actions[0]
        before = action.key
        plan.execute(policy=self.policy())
        self.assertEqual((before, action.key), ("tool-package:cargo", "tool-package:cargo"))

    def test_the_line_keeps_the_names_after_the_install_ran(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["a", "b"]\n')
        machine = ToolMachine({"cargo"})
        plan = self.plan(machine)
        plan.execute(policy=self.policy())
        self.assertEqual(plan.actions[0].describe(), "cargo: install a, b")

    def test_the_line_keeps_what_was_asked_when_a_retry_has_less_to_do(self) -> None:
        write(self.root / "packages.toml", 'npm = ["flaky", "stable"]\n')
        machine = ToolMachine({"npm"}, {"npm:flaky": ["connection reset", None]})
        plan = self.plan(machine)
        plan.execute(policy=self.policy())
        self.assertEqual(plan.actions[0].describe(), "npm: install flaky, stable")

    def test_the_explanation_names_the_user_level_managers(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["a"]\n')
        self.assertIn("uv/npm/bun/cargo/pipx", self.plan(ToolMachine({"cargo"})).actions[0].why())

    def test_a_manager_failure_does_not_stop_the_next_manager(self) -> None:
        write(self.root / "packages.toml", 'uv = ["bad"]\ncargo = ["wasm-bindgen-cli"]\n')
        machine = ToolMachine({"uv", "cargo"}, {"uv:bad": ["no matching distribution"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(len(result.failed), 1)
        self.assertEqual(machine.installed["cargo"], {"wasm-bindgen-cli"})


class MissingManagerTests(ToolCase):
    def test_a_missing_manager_is_blocked_with_what_installs_it_and_the_rest_runs(self) -> None:
        write(self.root / "packages.toml", 'cargo = ["wasm-bindgen-cli"]\nbun = ["cf"]\n')
        machine = ToolMachine({"bun"})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(result.failed, ())
        self.assertEqual(len(result.blocked), 1)
        self.assertEqual(machine.installed["bun"], {"cf"})
        blocked = result.render_blocked()
        self.assertIn("cargo: install wasm-bindgen-cli", blocked)
        self.assertIn("cargo is not installed", blocked)
        self.assertIn("Rust", blocked)
        self.assertEqual([argv[0] for argv, _ in machine.executed], ["bun"])

    def test_a_manager_that_an_earlier_action_installs_unblocks_the_action(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances"]\n')
        machine = ToolMachine(set())
        plan = self.plan(machine)
        machine.present.add("uv")  # installed between planning and running, e.g. by the brew module
        result = plan.execute(policy=self.policy())

        self.assertEqual((result.failed, result.blocked), ((), ()))
        self.assertEqual(machine.installed["uv"], {"glances"})

    def test_describe_says_when_the_manager_is_missing(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances"]\n')
        self.assertIn("uv is not installed", self.plan(ToolMachine(set())).actions[0].why())
        self.assertNotIn("not installed", self.plan(ToolMachine({"uv"})).actions[0].why())


class FallbackTests(ToolCase):
    def test_uv_falls_back_to_pipx_when_the_install_fails_for_good(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances", "bad"]\n')
        machine = ToolMachine({"uv", "pipx"}, {"uv:bad": ["resolution failed"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(result.failed, ())
        self.assertEqual(machine.installed["uv"], {"glances"})
        self.assertEqual(machine.installed["pipx"], {"bad"})  # only what uv could not install
        self.assertEqual(len(result.fallbacks), 1)
        self.assertIn("pipx: install bad", result.fallbacks[0][1])
        self.assertEqual(len(self.plan(machine)), 0)  # satisfied through pipx, no churn on the next run

    def test_uv_missing_falls_back_to_pipx_instead_of_blocking(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances"]\n')
        machine = ToolMachine({"pipx"})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual((result.failed, result.blocked), ((), ()))
        self.assertEqual(machine.installed["pipx"], {"glances"})
        self.assertEqual([argv[0] for argv, _ in machine.executed], ["pipx"])  # no attempt against the missing uv
        self.assertEqual(len(result.fallbacks), 1)  # reported as a fallback, not as uv's install
        self.assertIn("pipx: install glances", result.fallbacks[0][1])

    def test_a_failing_fallback_is_tried_once_when_the_primary_is_missing(self) -> None:
        write(self.root / "packages.toml", 'uv = ["bad"]\n')
        machine = ToolMachine({"pipx"}, {"pipx:bad": ["no such package"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual([argv for argv, _ in machine.executed], [("pipx", "install", "bad")])
        self.assertEqual(len(result.failed), 1)

    def test_a_fallback_retry_does_not_repeat_what_failed_for_good_there(self) -> None:
        write(self.root / "packages.toml", 'uv = ["a", "b"]\n')
        machine = ToolMachine(
            {"uv", "pipx"},
            {
                "uv:a": ["resolution failed"],
                "uv:b": ["resolution failed"],
                "pipx:a": ["no such package"],
                "pipx:b": ["connection reset", None],
            },
        )
        result = self.plan(machine).execute(policy=self.policy())

        pipx = [argv[-1] for argv, _ in machine.executed if argv[0] == "pipx"]
        self.assertEqual(pipx, ["a", "b", "b"])  # `a` failed for good on the first pass: not tried again
        self.assertEqual(machine.installed["pipx"], {"b"})
        self.assertEqual(len(result.failed), 1)

    def test_a_fallback_whose_binary_is_missing_is_not_tried(self) -> None:
        write(self.root / "packages.toml", 'npm = ["bad"]\n')
        machine = ToolMachine({"npm"}, {"npm:bad": ["EACCES"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual([argv[0] for argv, _ in machine.executed], ["npm"])
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0].tried, ())  # nothing but the primary's own failure

    def test_uv_and_pipx_both_missing_is_blocked(self) -> None:
        write(self.root / "packages.toml", 'uv = ["glances"]\n')
        result = self.plan(ToolMachine(set())).execute(policy=self.policy())
        self.assertEqual(len(result.blocked), 1)

    def test_npm_falls_back_to_bun(self) -> None:
        write(self.root / "packages.toml", 'npm = ["agent-browser"]\n')
        machine = ToolMachine({"npm", "bun"}, {"npm:agent-browser": ["EACCES"]})
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(result.failed, ())
        self.assertEqual(machine.installed["bun"], {"agent-browser"})
        self.assertEqual(machine.executed[-1][0], ("bun", "add", "-g", "agent-browser"))

    def test_a_failing_fallback_reports_both_causes(self) -> None:
        write(self.root / "packages.toml", 'uv = ["bad"]\n')
        machine = ToolMachine({"uv", "pipx"}, {"uv:bad": ["resolution failed"]})
        machine.failures["pipx:bad"] = ["no such package"]
        result = self.plan(machine).execute(policy=self.policy())

        self.assertEqual(len(result.failed), 1)
        self.assertIn("pipx", " ".join(result.failed[0].tried))


if __name__ == "__main__":
    unittest.main()

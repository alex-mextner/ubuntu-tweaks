"""privacy-crash-reporting against a fake `systemctl` on PATH and a scratch /etc (no real systemd, no root)."""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "privacy-crash-reporting"
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.module_script import main  # noqa: E402
from ubuntu_tweaks.process import CommandResult, FakeProcess  # noqa: E402

# A stateful fake: STATE_FILE is JSON {unit: [enabled-word, active-word]}; absent unit = unknown to systemd.
FAKE_SYSTEMCTL = r"""#!/usr/bin/python3
import json, os, sys
path = os.environ["FAKE_SYSTEMD"]
state = json.load(open(path))
log = open(path + ".log", "a")
log.write(" ".join(sys.argv[1:]) + "\n")
cmd, units = sys.argv[1], sys.argv[2:]
if cmd == "is-enabled":
    entry = state.get(units[0])
    if entry is None:
        sys.stderr.write(f"Failed to get unit file state for {units[0]}: No such file or directory\n")
        sys.exit(1)
    print(entry[0]); sys.exit(0 if entry[0] in ("enabled", "static") else 1)
if cmd == "is-active":
    entry = state.get(units[0])
    word = entry[1] if entry else "inactive"
    print(word); sys.exit(0 if word == "active" else 3)
if cmd == "mask" and units[0] == "--now":
    for unit in units[1:]:
        if unit in state:
            state[unit] = ["masked", "inactive"]
    json.dump(state, open(path, "w")); sys.exit(0)
sys.exit(64)
"""


def load_script():
    loader = importlib.machinery.SourceFileLoader("privacy_crash_reporting_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def answers(enabled: CommandResult, active: CommandResult) -> FakeProcess:
    responses = {}
    for unit in ("whoopsie.path", "whoopsie.service"):
        responses[("systemctl", "is-enabled", unit)] = enabled
        responses[("systemctl", "is-active", unit)] = active
    return FakeProcess(responses)


class SystemctlAnswerTests(unittest.TestCase):
    """What the module concludes from systemctl's real answers, including the failed queries."""

    def run_check(self, process: FakeProcess) -> tuple[int, str, str]:
        script = load_script()
        module = script.CrashReporting(process, Path("/nonexistent"), apport=False)
        out, err = io.StringIO(), io.StringIO()
        return main(module, ["check"], out=out, err=err), out.getvalue(), err.getvalue()

    def test_not_found_with_exit_4_is_an_absent_unit_and_satisfied(self) -> None:
        process = answers(CommandResult(4, "not-found\n", ""), CommandResult(3, "inactive\n", ""))

        self.assertEqual(self.run_check(process), (0, "", ""))

    def test_a_failed_is_enabled_query_is_an_error_not_a_pass(self) -> None:
        for failed in (
            CommandResult(1, "", "Access denied"),
            CommandResult(124, "", "systemctl: timed out after 60 s"),
            CommandResult(127, "", "systemctl: command not found"),
        ):
            with self.subTest(failed.returncode):
                status, out, err = self.run_check(answers(failed, CommandResult(3, "inactive\n", "")))
                self.assertEqual(status, 1)
                self.assertEqual(out, "")
                self.assertIn("systemctl is-enabled whoopsie.path", err)

    def test_a_failed_is_active_query_is_an_error_not_a_pass(self) -> None:
        status, _, err = self.run_check(
            answers(CommandResult(1, "masked\n", ""), CommandResult(1, "", "Access denied"))
        )

        self.assertEqual(status, 1)
        self.assertIn("systemctl is-active whoopsie.path: exit 1: Access denied", err)

    def test_a_masked_word_is_satisfied(self) -> None:
        process = answers(CommandResult(1, "masked\n", ""), CommandResult(3, "inactive\n", ""))

        self.assertEqual(self.run_check(process), (0, "", ""))

    def test_every_word_but_a_persistent_mask_is_drift_and_so_is_a_running_masked_unit(self) -> None:
        for enabled, active in (
            ("enabled", "inactive"),
            ("enabled-runtime", "inactive"),
            ("disabled", "inactive"),
            ("static", "inactive"),
            ("masked-runtime", "inactive"),
            ("masked", "activating"),
        ):
            with self.subTest(enabled=enabled, active=active):
                status, out, _ = self.run_check(
                    answers(CommandResult(0, enabled + "\n", ""), CommandResult(0, active + "\n", ""))
                )
                self.assertEqual(status, 3)
                self.assertIn("whoopsie.path", out)


class AliasTests(unittest.TestCase):
    def test_an_alias_is_an_error_not_a_pass(self) -> None:
        script = load_script()
        process = answers(CommandResult(0, "alias\n", ""), CommandResult(3, "inactive\n", ""))
        module = script.CrashReporting(process, Path("/nonexistent"), apport=False)
        out, err = io.StringIO(), io.StringIO()

        self.assertEqual(main(module, ["check"], out=out, err=err), 1)
        self.assertIn("alias", err.getvalue())


class CrashReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "bin").mkdir()
        fake = self.tmp / "bin/systemctl"
        fake.write_text(FAKE_SYSTEMCTL)
        fake.chmod(0o755)
        self.state = self.tmp / "systemd.json"
        self.root = self.tmp / "root"
        (self.root / "etc/default").mkdir(parents=True)
        self.apport = self.root / "etc/default/apport"
        self.apport.write_text("# apport\nenabled=1\n")

    def set_units(self, **units: list[str]) -> None:
        self.state.write_text(json.dumps({name.replace("_", ".", 1): value for name, value in units.items()}))

    def run_module(self, command: str, options: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            PATH=f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            FAKE_SYSTEMD=str(self.state),
            PRIVACY_CRASH_REPORTING_ROOT=str(self.root),
        )
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        if options is not None:
            env["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps(options)
        return subprocess.run([sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True)

    def calls(self) -> list[str]:
        log = Path(str(self.state) + ".log")
        return [line for line in log.read_text().splitlines() if line.startswith("mask")] if log.exists() else []

    def test_the_manifest_is_off_by_default_and_root(self) -> None:
        manifest = load_manifest(MODULE)
        self.assertFalse(manifest.enabled)
        self.assertTrue(manifest.source.root)

    def test_a_disabled_but_unmasked_path_unit_is_drift_and_plan_names_the_exact_command(self) -> None:
        self.set_units(whoopsie_path=["disabled", "inactive"], whoopsie_service=["masked", "inactive"])

        check = self.run_module("check")
        plan = self.run_module("plan")

        self.assertEqual(check.returncode, 3)
        self.assertIn("whoopsie.path is masked", check.stdout)
        self.assertEqual(plan.stdout.splitlines(), ["systemctl mask --now whoopsie.path"])

    def test_install_masks_and_stops_then_check_passes_and_a_second_install_does_nothing(self) -> None:
        self.set_units(whoopsie_path=["enabled", "active"], whoopsie_service=["static", "active"])

        self.assertEqual(self.run_module("install").returncode, 0)
        self.assertEqual(self.run_module("check").returncode, 0)
        self.assertEqual(self.run_module("install").returncode, 0)

        self.assertEqual(self.calls(), ["mask --now whoopsie.path", "mask --now whoopsie.service"])

    def test_an_absent_unit_and_masked_inactive_units_are_satisfied(self) -> None:
        self.set_units()
        self.assertEqual(self.run_module("check").returncode, 0)
        self.set_units(whoopsie_path=["masked", "inactive"], whoopsie_service=["masked", "inactive"])
        self.assertEqual(self.run_module("check").returncode, 0)
        self.assertEqual(self.run_module("plan").stdout, "")

    def test_a_running_masked_service_is_drift(self) -> None:
        self.set_units(whoopsie_path=["masked", "inactive"], whoopsie_service=["masked", "active"])

        self.assertEqual(self.run_module("check").returncode, 3)
        self.assertEqual(self.run_module("plan").stdout.splitlines(), ["systemctl mask --now whoopsie.service"])

    def test_a_unit_a_service_wants_stays_masked_after_the_apply_so_it_cannot_be_started_again(self) -> None:
        # apport-autoreport.service Wants=whoopsie.path: after the install both units answer `masked`, the state in which
        # systemd refuses the start job instead of running the watcher (#196).
        self.set_units(whoopsie_path=["disabled", "inactive"], whoopsie_service=["static", "inactive"])

        self.assertEqual(self.run_module("install").returncode, 0)

        self.assertEqual(
            json.loads(self.state.read_text()),
            {
                "whoopsie.path": ["masked", "inactive"],
                "whoopsie.service": ["masked", "inactive"],
            },
        )

    def test_apport_is_left_alone_unless_the_option_is_on(self) -> None:
        self.set_units()

        self.assertEqual(self.run_module("check").returncode, 0)
        self.run_module("install")

        self.assertEqual(self.apport.read_text(), "# apport\nenabled=1\n")

    def test_the_apport_option_sets_enabled_0_keeping_comments_and_other_lines(self) -> None:
        self.set_units()
        self.apport.write_text("# keep me\nenabled=1\nother=2\n")
        options = {"apport": True}

        self.assertEqual(self.run_module("check", options).returncode, 3)
        plan = self.run_module("plan", options).stdout
        self.assertIn("-enabled=1", plan)
        self.assertIn("+enabled=0", plan)
        self.assertEqual(self.run_module("install", options).returncode, 0)

        self.assertEqual(self.apport.read_text(), "# keep me\nenabled=0\nother=2\n")
        self.assertEqual(self.run_module("check", options).returncode, 0)
        self.assertEqual(self.apport.stat().st_mode & 0o777, 0o644)

    def test_a_missing_apport_file_gets_the_line_when_the_option_is_on(self) -> None:
        self.set_units()
        self.apport.unlink()

        self.assertEqual(self.run_module("install", {"apport": True}).returncode, 0)

        self.assertEqual(self.apport.read_text(), "enabled=0\n")

    def test_a_wrong_option_is_refused(self) -> None:
        self.assertEqual(self.run_module("check", {"apport": "yes"}).returncode, 1)
        self.assertEqual(self.run_module("check", {"nope": 1}).returncode, 1)


if __name__ == "__main__":
    unittest.main()

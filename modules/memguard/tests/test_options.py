"""[modules.memguard] options (UBUNTU_TWEAK_MODULE_CONFIG) overlay the repo's memguard.toml in install and --check."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(MODULE, "install.sh")
sys.path.insert(0, MODULE)
sys.dont_write_bytecode = True

import memguard_options  # noqa: E402

USER = "alice"
OPTIONS = {"swap": {"heavy_locks": ["/tmp/3d-cli-heavy.lock"], "heavy_gate": "auto", "drain_min_used_gb": 6}}


class Overlay(unittest.TestCase):
    def shipped(self):
        with open(os.path.join(MODULE, "memguard.toml")) as f:
            return f.read()

    def test_no_options_leave_the_file_alone(self):
        self.assertEqual(memguard_options.apply({}, self.shipped()), self.shipped())

    def test_options_replace_the_value_and_keep_the_comment_and_the_other_lines(self):
        text = memguard_options.apply(OPTIONS, self.shipped())
        swap = tomllib.loads(text)["swap"]
        self.assertEqual(swap["heavy_locks"], ["/tmp/3d-cli-heavy.lock"])
        self.assertEqual((swap["heavy_gate"], swap["drain_min_used_gb"]), ("auto", 6))
        self.assertEqual(swap["drain_headroom_gb"], 8.0)  # untouched
        self.assertIn("# ... or SwapFree below this % of SwapTotal", text)
        self.assertEqual(len(text.splitlines()), len(self.shipped().splitlines()))
        self.assertIn("drain_min_used_gb = 6 ", text)  # the trailing comment survives next to the new value

    def test_a_float_and_a_bool_free_integer_are_rendered_as_toml(self):
        text = memguard_options.apply({"swap": {"abort_avail_gb": 7.5, "fall_window_s": 20}}, self.shipped())
        swap = tomllib.loads(text)["swap"]
        self.assertEqual((swap["abort_avail_gb"], swap["fall_window_s"]), (7.5, 20))

    def test_refusals(self):
        bad = (
            {"swap": {"drain_min_usd_gb": 4}},  # unknown key
            {"swap": {"drain_min_used_gb": "4"}},  # wrong type
            {"swap": {"drain_min_used_gb": True}},
            {"swap": {"fall_window_s": 20.5}},  # a whole-number key takes no fraction
            {"swap": {"drain_headroom_gb": -3}},
            {"swap": {"heavy_hook": "\ud800"}},  # a lone surrogate is no text
            {"swap": {"heavy_locks": ["\ud800"]}},
            {"swap": {"drain_min_used_gb": 10**400}},  # an int no float can hold: a clean refusal, not a traceback
            {"swap": {"drain_min_used_gb": 10**12}},
            {"swap": {"heavy_locks": "/tmp/x"}},
            {"swap": {"heavy_locks": [1]}},
            {"swap": {"user": "mallory"}},  # the installer resolves the user
            {"swap": "x"},
            {"other": {}},
            [],
        )
        for options in bad:
            with self.subTest(options=options), self.assertRaises(memguard_options.OptionsError):
                memguard_options.apply(options, self.shipped())

    def test_text_values_cannot_break_out_of_the_toml_string(self):
        text = memguard_options.apply({"swap": {"heavy_hook": 'a"\nb = 1'}}, self.shipped())
        self.assertEqual(tomllib.loads(text)["swap"]["heavy_hook"], 'a"\nb = 1')
        self.assertNotIn("b", tomllib.loads(text))

    def test_non_ascii_and_control_characters_are_legal_toml_or_a_clean_error(self):
        locks = memguard_options.apply({"swap": {"heavy_locks": ["/tmp/a\x7fb", "/tmp/🔥"]}}, self.shipped())
        self.assertEqual(tomllib.loads(locks)["swap"]["heavy_locks"], ["/tmp/a\x7fb", "/tmp/🔥"])
        for hook in ("~/hooks/🔥.json", "~/hooks/é.json", "a\x7fb", "tab\there"):
            with self.subTest(hook=hook):
                text = memguard_options.apply({"swap": {"heavy_hook": hook}}, self.shipped())
                self.assertEqual(tomllib.loads(text)["swap"]["heavy_hook"], hook)

    def test_a_float_is_fine_for_a_float_key_and_a_whole_number_for_either(self):
        text = memguard_options.apply({"swap": {"abort_avail_gb": 7, "drain_cooldown_min": 30}}, self.shipped())
        swap = tomllib.loads(text)["swap"]
        self.assertEqual((swap["abort_avail_gb"], swap["drain_cooldown_min"]), (7, 30))

    def test_the_rendered_file_passes_the_loader_swap_drain_uses(self):
        import memguard_swap

        text = memguard_options.apply(OPTIONS, self.shipped())
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as handle:
            handle.write(text)
            handle.flush()
            self.assertEqual(memguard_swap.load_config(handle.name)["heavy_gate"], "auto")

    def test_every_key_of_the_defaults_has_exactly_one_assignment_in_the_shipped_file(self):
        import memguard_swap

        text = self.shipped()
        for key in memguard_swap.DEFAULTS:
            with self.subTest(key=key):
                lines = [line for line in text.splitlines() if line.startswith(f"{key} = ")]
                self.assertEqual(len(lines), 1)


class Install(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_install(self, *args, options=None):
        env = dict(os.environ, MEMGUARD_USER=USER, MEMGUARD_ROOT=self.root)
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        if options is not None:
            env["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps(options)
        return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=60)

    def installed(self):
        with open(os.path.join(self.root, "etc/memguard/memguard.toml"), "rb") as f:
            return tomllib.load(f)["swap"]

    def test_a_staged_install_writes_the_options_and_the_user(self):
        result = self.run_install(options=OPTIONS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        swap = self.installed()
        self.assertEqual((swap["heavy_gate"], swap["user"]), ("auto", USER))
        self.assertEqual(swap["heavy_locks"], ["/tmp/3d-cli-heavy.lock"])

    def test_check_is_clean_with_the_same_options_and_drift_with_other_ones(self):
        self.assertEqual(self.run_install(options=OPTIONS).returncode, 0)
        self.assertEqual(self.run_install("--check", options=OPTIONS).returncode, 0)
        changed = {"swap": {**OPTIONS["swap"], "drain_min_used_gb": 9}}
        result = self.run_install("--check", options=changed)
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("-drain_min_used_gb = 6", result.stdout)
        self.assertIn("+drain_min_used_gb = 9", result.stdout)
        # dropping the options is drift too: the installed file still carries them
        self.assertEqual(self.run_install("--check").returncode, 3)

    def test_a_second_install_with_options_changes_nothing(self):
        self.run_install(options=OPTIONS)
        again = self.run_install(options=OPTIONS)
        self.assertEqual((again.returncode, "nothing to change" in again.stdout), (0, True), again.stdout)

    def test_bad_options_fail_before_anything_is_written(self):
        for command in ((), ("--check",), ("--dry-run",)):
            result = self.run_install(*command, options={"swap": {"nope": 1}})
            self.assertEqual(result.returncode, 1, (command, result.stdout, result.stderr))
            self.assertIn("nope", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc")))

    def test_invalid_json_fails(self):
        env = dict(os.environ, MEMGUARD_USER=USER, MEMGUARD_ROOT=self.root, UBUNTU_TWEAK_MODULE_CONFIG="{x")
        result = subprocess.run([INSTALL, "--check"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()

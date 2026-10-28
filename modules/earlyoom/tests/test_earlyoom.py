"""earlyoom module against a staged root and a stateful fake machine (no real apt, systemd or root)."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.path.insert(0, str(MODULE))
sys.dont_write_bytecode = True

import earlyoom_lib as eo  # noqa: E402

from ubuntu_tweaks.manifest import load_manifest  # noqa: E402
from ubuntu_tweaks.module_script import APT_ENVIRONMENT, ModuleError  # noqa: E402
from ubuntu_tweaks.process import CommandError, CommandResult, Process  # noqa: E402

# The laptop's /etc/default/earlyoom (migration copy), the values these tests pin the renderer against.
LAPTOP_PREFER = "(^|/)(pytest|python3?|node|bun|cc1plus|chrome|chromium|WebKitWebProcess)$"
LAPTOP_AVOID = "(^|/)(omp|gnome-shell|ptyxis|systemd|dbus-daemon|Xwayland|pipewire|sshd|tailscaled)$"
LAPTOP_LINE = f"""EARLYOOM_ARGS="-m 4 -s 10 -r 60 --prefer '{LAPTOP_PREFER}' --avoid '{LAPTOP_AVOID}' -n\""""
LAPTOP_OPTIONS = {
    "mem_percent": 4,
    "swap_percent": 10,
    "report_interval": 60,
    "prefer": LAPTOP_PREFER,
    "avoid": LAPTOP_AVOID,
    "notify": True,
}
STOCK = '# stock\nEARLYOOM_ARGS="-r 3600"\n'


class FakeMachine(Process):
    """Answers the queries the modules make from a small state and applies the mutations they execute."""

    def __init__(self, root, home, *, installed=False, enabled=False, active=False):
        self.root = root
        self.home = home
        self.installed = installed
        self.enabled = enabled
        self.active = active
        self.user_enabled = False
        self.user_active = False
        self.crash_on_args = None  # a substring of the defaults file that makes earlyoom exit at once
        self.limit_hit = self.user_limit_hit = False
        self.fail = None  # an argv prefix whose execution raises
        self.executed = []
        self.apt_runs = []  # (stdin text, the noninteractive variables the child would see) per apt-get call

    def _stays_up(self, user=False):
        path = self.home / eo.USER_ENV if user else self.root / eo.DEFAULTS_FILE
        text = path.read_text() if path.exists() else ""
        return not (self.crash_on_args and self.crash_on_args in text)

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        if argv[0] == "dpkg-query":
            return CommandResult(0 if self.installed else 1, "install ok installed" if self.installed else "", "")
        if argv[0] == "systemctl":
            user = argv[1] == "--user"
            verb = argv[2] if user else argv[1]
            if verb == "is-enabled":
                on = self.user_enabled if user else self.enabled
                return CommandResult(0 if on else 1, "enabled\n" if on else "disabled\n", "")
            if verb == "is-active":
                on = self.user_active if user else self.active
                return CommandResult(0 if on else 3, "active\n" if on else "inactive\n", "")
        return CommandResult(127, "", f"{argv[0]}: not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        self.executed.append(tuple(argv))
        if argv[0] == "apt-get":
            self.apt_runs.append((input_text, {name: os.environ.get(name) for name in APT_ENVIRONMENT}))
        if self.fail and tuple(argv[: len(self.fail)]) == self.fail:
            raise CommandError(f"{' '.join(argv)}: exited with status 1")
        if argv[0] == "apt-get" and argv[1] == "install":
            self.installed = self.enabled = self.active = True
            (self.root / eo.DEFAULTS_FILE).parent.mkdir(parents=True, exist_ok=True)
            (self.root / eo.DEFAULTS_FILE).write_text(STOCK)  # the package's own conffile, service started with it
        elif argv[:2] == ["apt-get", "download"]:
            (Path(cwd) / "earlyoom_1.9.0-1_amd64.deb").write_text("deb")
        elif argv[0] == "dpkg-deb":
            binary = Path(argv[3]) / "usr/bin/earlyoom"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
        elif argv[:3] == ["systemctl", "--user", "--machine"]:
            self.user_enabled = self.user_active = False  # `disable --now` in the desktop user's manager
        elif argv[0] == "systemctl":
            user = argv[1] == "--user"
            verb = argv[2] if user else argv[1]
            up = self._stays_up(user)
            limit = "user_limit_hit" if user else "limit_hit"
            if verb == "enable":
                setattr(self, "user_enabled" if user else "enabled", True)
            elif verb == "reset-failed":
                setattr(self, limit, False)
            elif verb in ("start", "restart"):
                if getattr(self, limit):  # systemd's start limit: a crash-looping unit refuses starts until reset
                    raise CommandError("Start request repeated too quickly")
                setattr(self, "user_active" if user else "active", up)
                if not up:
                    setattr(self, limit, True)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/alice"
        self.home.mkdir(parents=True)
        self.machine = FakeMachine(self.root, self.home)

    def system(self, options=None, *, is_root=True, with_user=True, machine=None):
        parsed = eo.parse_options(options or {})
        machine = machine or self.machine
        user = self.user(options, machine=machine) if with_user else None
        return eo.SystemEarlyoom(machine, self.root, parsed, is_root=is_root, user=user, settle=0)

    def user(self, options=None, *, machine=None):
        return eo.UserEarlyoom(
            machine or self.machine,
            self.home,
            eo.parse_options(options or {}),
            settle=0,
            system_binary=self.root / "usr/bin/earlyoom",
            owner="alice",
        )

    def ok(self, module):
        return {finding.subject: finding.ok for finding in module.findings()}

    @property
    def defaults(self):
        return (self.root / eo.DEFAULTS_FILE).read_text()


class OptionParsing(unittest.TestCase):
    def test_defaults_are_the_packages_thresholds_and_a_desktop_avoid_list(self):
        options = eo.parse_options({})
        self.assertEqual((options.mem_percent, options.swap_percent, options.report_interval), (10, 10, 3600))
        self.assertIsNone(options.prefer)
        self.assertEqual(options.avoid, eo.DEFAULT_AVOID)
        self.assertFalse(options.notify)

    def test_unknown_option_wrong_type_and_range_are_refused(self):
        for raw in (
            {"mem": 4},
            {"mem_percent": "4"},
            {"mem_percent": True},
            {"mem_percent": 0},
            {"mem_percent": 101},
            {"swap_percent": -1},
            {"report_interval": 1.5},
            {"report_interval": -1},
            {"report_interval": True},
            {"notify": "yes"},
            {"prefer": 3},
            {"avoid": ["a"]},
            {"extra_args": "-g"},
            {"extra_args": [1]},
        ):
            with self.subTest(raw=raw), self.assertRaises(ModuleError):
                eo.parse_options(raw)

    def test_injection_is_refused_everywhere_text_is_written(self):
        poison = [
            "a'b",
            'a"b',
            "a\nb",
            "a\rb",
            "a\\.b",
            "a`b",
            "a$(b)",
            "a${b}",
            "a$HOME",
            "a$$",
            "a$?",
            "a$1",
            "a$*",
            "a\tb",
            "a\x00b",
            "é",
        ]
        for text in poison:
            with self.subTest(text=text):
                for key in ("prefer", "avoid"):
                    with self.assertRaises(ModuleError):
                        eo.parse_options({key: text})
                with self.assertRaises(ModuleError):
                    eo.parse_options({"extra_args": [text]})

    def test_a_dollar_that_ends_a_pattern_is_fine(self):
        self.assertEqual(eo.parse_options({"avoid": "(a|b)$"}).avoid, "(a|b)$")
        self.assertEqual(eo.parse_options({"avoid": "(^|/)(a$|b)"}).avoid, "(^|/)(a$|b)")

    def test_extra_args_cannot_repeat_or_split_a_managed_flag(self):
        # getopt_long abbreviates long options and groups short ones: every spelling that sets a managed flag is refused
        for word in ("--pref=x", "--prefe", "--p", "--avo=x", "--a", "--avoi", "-gm5", "-dn", "-gr1", "-dS5"):
            with self.subTest(word=word), self.assertRaises(ModuleError):
                eo.parse_options({"extra_args": [word]})
        for word in (
            "-m",
            "-m5",
            "-M",
            "-M100",
            "-S",
            "-S5",
            "-s",
            "-r",
            "-r60",
            "-n",
            "--prefer",
            "--prefer=x",
            "--avoid",
            "--avoid=x",
        ):
            with self.subTest(word=word), self.assertRaises(ModuleError):
                eo.parse_options({"extra_args": [word]})
        for word in ("two words", ""):
            with self.subTest(word=word), self.assertRaises(ModuleError):
                eo.parse_options({"extra_args": [word]})
        self.assertEqual(eo.parse_options({"extra_args": ["-g", "--sort-by-rss"]}).extra_args, ("-g", "--sort-by-rss"))
        # the long flags that look alike but are not managed stay allowed
        self.assertEqual(
            eo.parse_options({"extra_args": ["--ignore=x", "--syslog"]}).extra_args, ("--ignore=x", "--syslog")
        )

    def test_an_empty_regex_switches_the_flag_off(self):
        options = eo.parse_options({"avoid": "", "prefer": ""})
        self.assertEqual(eo.render_args(options), "-m 10 -s 10 -r 3600")


class Regex(unittest.TestCase):
    def test_invalid_regexes_are_refused_with_libcs_message(self):
        for pattern in ("(unclosed", "a[", "*a", "a{1", "+a"):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(eo.posix_ere_error(pattern))
                with self.assertRaisesRegex(ModuleError, "not a valid POSIX extended regular expression"):
                    eo.parse_options({"avoid": pattern})

    def test_the_regexes_the_laptop_runs_are_valid(self):
        for pattern in (eo.DEFAULT_AVOID, LAPTOP_PREFER, LAPTOP_AVOID, "^tmux: (server|client)$", "(^|/)(a|b)$|^c$"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(eo.posix_ere_error(pattern))

    @unittest.skipUnless(shutil.which("earlyoom"), "earlyoom is not installed here")
    def test_libc_and_the_real_earlyoom_agree_on_what_compiles(self):
        for pattern in (eo.DEFAULT_AVOID, LAPTOP_PREFER, "(unclosed", "a[", "*a"):
            with self.subTest(pattern=pattern):
                # a valid pattern makes earlyoom enter its endless loop (the timeout ends it); an invalid one is fatal at start
                try:
                    run = subprocess.run(
                        ["earlyoom", "--dryrun", "-r", "0", "--avoid", pattern],
                        capture_output=True,
                        text=True,
                        timeout=1.5,
                    )
                    fatal = "could not compile regexp" in run.stdout + run.stderr
                except subprocess.TimeoutExpired:
                    fatal = False
                self.assertEqual(fatal, eo.posix_ere_error(pattern) is not None)


class Rendering(unittest.TestCase):
    def test_laptop_options_render_the_laptops_line_byte_for_byte(self):
        text = eo.render_defaults(eo.parse_options(LAPTOP_OPTIONS))
        self.assertIn(LAPTOP_LINE + "\n", text)
        self.assertEqual(text.count("EARLYOOM_ARGS"), 1)

    def test_rendering_is_deterministic_and_order_is_fixed(self):
        options = eo.parse_options({**LAPTOP_OPTIONS, "extra_args": ["-g", "--sort-by-rss"]})
        self.assertEqual(eo.render_defaults(options), eo.render_defaults(options))
        self.assertTrue(eo.render_args(options).endswith("-n -g --sort-by-rss"))

    def test_percent_round_trips_exactly_and_whole_ones_do_not_grow_a_point(self):
        self.assertEqual(
            eo.render_args(eo.parse_options({"mem_percent": 2.5, "swap_percent": 5.0})),
            eo.render_args(eo.parse_options({"mem_percent": 2.5, "swap_percent": 5})),
        )
        self.assertTrue(eo.render_args(eo.parse_options({"mem_percent": 2.5})).startswith("-m 2.5 -s 10"))
        self.assertTrue(eo.render_args(eo.parse_options({"mem_percent": 2.1234567})).startswith("-m 2.1234567 "))

    def test_the_user_level_file_never_picks_roots_processes(self):
        options = eo.parse_options({})
        self.assertNotIn("--ignore-root-user", eo.render_args(options))
        self.assertIn("--ignore-root-user", eo.render_args(options, user_level=True))


class SystemInstall(Base):
    def test_fresh_machine_reports_drift_everywhere(self):
        self.assertEqual(set(self.ok(self.system()).values()), {False})

    def test_install_makes_every_fact_true_and_restarts_once(self):
        module = self.system(LAPTOP_OPTIONS)
        module.apply()
        self.assertEqual(set(self.ok(module).values()), {True})
        self.assertIn(LAPTOP_LINE, self.defaults)
        self.assertEqual(self.defaults.count("EARLYOOM_ARGS"), 1)
        apt = [call for call in self.machine.executed if call[0] == "apt-get"]
        self.assertEqual(apt, [("apt-get", "install", "-y", "-o", "Dpkg::Options::=--force-confold", "earlyoom")])
        self.assertEqual(
            [c for c in self.machine.executed if "restart" in c], [("systemctl", "restart", "earlyoom.service")]
        )

    def test_apt_runs_without_a_terminal_and_noninteractively(self):
        # ticket #126: a grandchild of sudo-rs that touches the pty is stopped for ever; stdin must be at EOF
        self.system(LAPTOP_OPTIONS).apply()

        self.assertEqual(self.machine.apt_runs, [("", APT_ENVIRONMENT)])

    def test_a_second_install_changes_nothing_and_restarts_nothing(self):
        module = self.system(LAPTOP_OPTIONS)
        module.apply()
        before = (self.defaults, list(self.machine.executed))
        module.apply()
        self.assertEqual((self.defaults, self.machine.executed), before)
        self.assertEqual(module.plan(), ["nothing to change"])

    def test_changed_options_are_drift_then_one_restart(self):
        self.system(LAPTOP_OPTIONS).apply()
        changed = self.system({**LAPTOP_OPTIONS, "mem_percent": 5})
        self.assertFalse(self.ok(changed)["/etc/default/earlyoom has the rendered options"])
        self.machine.executed.clear()
        changed.apply()
        self.assertEqual(self.machine.executed, [("systemctl", "restart", "earlyoom.service")])
        self.assertIn("-m 5 ", self.defaults)

    def test_a_stopped_service_is_started_without_rewriting_the_file(self):
        module = self.system(LAPTOP_OPTIONS)
        module.apply()
        self.machine.active = False
        self.assertFalse(self.ok(module)["earlyoom.service is active"])
        self.machine.executed.clear()
        module.apply()
        self.assertEqual(self.machine.executed, [("systemctl", "start", "earlyoom.service")])

    def test_a_disabled_service_is_enabled(self):
        module = self.system()
        module.apply()
        self.machine.enabled = False
        self.machine.executed.clear()
        module.apply()
        self.assertEqual(self.machine.executed, [("systemctl", "enable", "earlyoom.service")])

    def test_options_earlyoom_rejects_are_rolled_back_to_the_previous_file(self):
        module = self.system({})
        module.apply()
        good = self.defaults
        self.machine.crash_on_args = "--bogus"
        broken = self.system({"extra_args": ["--bogus"]})
        with self.assertRaisesRegex(ModuleError, "previous options were put back"):
            broken.apply()
        self.assertEqual(self.defaults, good)
        self.assertTrue(self.machine.active)  # reset-failed first: a crash-looping unit refuses a plain restart
        self.assertFalse(self.machine.limit_hit)

    def test_a_rejected_first_file_is_removed_rather_than_left_behind(self):
        machine = FakeMachine(self.root, self.home, installed=True, enabled=True, active=False)
        machine.crash_on_args = "--bogus"
        with self.assertRaises(ModuleError):
            self.system({"extra_args": ["--bogus"]}, machine=machine).apply()
        self.assertFalse((self.root / eo.DEFAULTS_FILE).exists())

    def test_a_command_failure_is_a_module_error_with_the_commands_message(self):
        self.machine.fail = ("apt-get", "install")
        with self.assertRaisesRegex(ModuleError, "apt-get install"):
            self.system().apply()

    def test_install_needs_root(self):
        with self.assertRaisesRegex(ModuleError, "must run as root"):
            self.system(is_root=False).apply()
        self.assertEqual(self.machine.executed, [])

    def test_plan_names_every_step_and_shows_the_diff(self):
        plan = self.system(LAPTOP_OPTIONS).plan()
        self.assertEqual(plan[0], "apt-get install earlyoom (keeps this file over the package's)")
        self.assertEqual(plan[1], "create /etc/default/earlyoom")
        self.assertTrue(any(line.strip().startswith("+EARLYOOM_ARGS=") for line in plan))
        # the package enables and starts the service with its stock file, so the only service step is one restart
        self.assertEqual(plan[-1], "systemctl restart earlyoom.service (the package starts it with its stock options)")
        self.assertNotIn("systemctl start earlyoom.service", plan)
        self.assertEqual(self.machine.executed, [])  # plan is read-only

    def test_plan_of_an_installed_but_stopped_service_enables_and_starts_it(self):
        machine = FakeMachine(self.root, self.home, installed=True)
        plan = self.system(LAPTOP_OPTIONS, machine=machine).plan()
        self.assertIn("systemctl enable earlyoom.service", plan)
        self.assertIn("systemctl start earlyoom.service", plan)

    def test_plan_of_a_changed_option_is_a_diff_and_a_restart(self):
        self.system(LAPTOP_OPTIONS).apply()
        plan = self.system({**LAPTOP_OPTIONS, "report_interval": 300}).plan()
        self.assertEqual(plan[0], "write /etc/default/earlyoom")
        self.assertTrue(any(line.strip().startswith("-EARLYOOM_ARGS=") for line in plan))
        self.assertEqual(plan[-1], "systemctl restart earlyoom.service (the options changed)")

    def test_a_file_that_is_not_ours_is_replaced_whole(self):
        machine = FakeMachine(self.root, self.home, installed=True, enabled=True, active=True)
        (self.root / "etc/default").mkdir(parents=True)
        (self.root / eo.DEFAULTS_FILE).write_text(STOCK)
        module = self.system(LAPTOP_OPTIONS, machine=machine)
        self.assertFalse(self.ok(module)["/etc/default/earlyoom has the rendered options"])
        module.apply()
        self.assertNotIn("# stock", self.defaults)


class Failover(Base):
    def test_user_install_extracts_the_deb_writes_a_unit_and_starts_it(self):
        user = self.user({"mem_percent": 4})
        user.apply()
        self.assertEqual(set(self.ok(user).values()), {True})
        self.assertTrue((self.home / eo.USER_PREFIX / "usr/bin/earlyoom").exists())
        text = (self.home / eo.USER_ENV).read_text()
        self.assertIn("--ignore-root-user", text)
        self.assertIn("-m 4 ", text)
        unit = (self.home / eo.USER_UNIT).read_text()
        self.assertIn("ConditionPathExists=!/usr/bin/earlyoom", unit)
        self.assertIn("ExecStart=%h/.local/opt/earlyoom/usr/bin/earlyoom $EARLYOOM_ARGS", unit)
        executed = self.machine.executed
        self.assertEqual(executed[0], ("apt-get", "download", "earlyoom"))
        self.assertNotIn("sudo", " ".join(" ".join(call) for call in executed))

    def test_user_install_is_idempotent(self):
        user = self.user()
        user.apply()
        done = list(self.machine.executed)
        user.apply()
        self.assertEqual(self.machine.executed, done)
        self.assertEqual(user.plan(), ["nothing to change"])

    def test_changed_options_restart_the_user_unit_without_downloading_again(self):
        self.user().apply()
        self.machine.executed.clear()
        self.user({"report_interval": 5}).apply()
        self.assertEqual(self.machine.executed, [("systemctl", "--user", "restart", "earlyoom.service")])

    def test_a_download_failure_is_a_module_error(self):
        self.machine.fail = ("apt-get", "download")
        with self.assertRaisesRegex(ModuleError, "apt-get download"):
            self.user().apply()

    def test_the_system_checker_judges_the_rootless_copy_when_there_is_no_package(self):
        self.user().apply()
        module = self.system()
        self.assertEqual(set(self.ok(module).values()), {True})
        self.assertEqual(self.ok(module), self.ok(self.user()))  # the copy's own facts, not the package's

    def test_once_the_package_is_installed_the_system_facts_are_required_again(self):
        self.user().apply()
        self.machine.installed = True
        module = self.system()
        self.assertFalse(self.ok(module)["/etc/default/earlyoom has the rendered options"])

    def test_a_machine_without_either_is_plain_drift(self):
        self.assertIn("the earlyoom package is installed", self.ok(self.system()))

    def test_user_install_refuses_while_the_package_is_present(self):
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/bin/earlyoom").write_text("#!/bin/sh\n")
        with self.assertRaisesRegex(ModuleError, "root install is the one to repair"):
            self.user().apply()
        self.assertEqual(self.machine.executed, [])  # nothing downloaded for a unit that could not start

    def test_options_the_user_binary_rejects_are_rolled_back_like_the_system_ones(self):
        self.user().apply()
        good = (self.home / eo.USER_ENV).read_text()
        self.machine.crash_on_args = "--bogus"
        with self.assertRaisesRegex(ModuleError, "previous options were put back"):
            self.user({"extra_args": ["--bogus"]}).apply()
        self.assertEqual((self.home / eo.USER_ENV).read_text(), good)
        self.assertTrue(self.machine.user_active)  # restarted with the old options, after reset-failed

    def test_a_first_user_install_that_never_stays_up_leaves_no_options_file(self):
        self.machine.crash_on_args = "-m 10"
        with self.assertRaises(ModuleError):
            self.user().apply()
        self.assertFalse((self.home / eo.USER_ENV).exists())

    def test_a_user_copy_that_is_still_running_when_the_package_arrives_is_drift_and_is_retired(self):
        self.user().apply()
        self.assertTrue(self.machine.user_active)
        module = self.system(LAPTOP_OPTIONS)
        self.machine.installed = True
        self.assertFalse(self.ok(module)["no rootless copy of earlyoom runs beside the service"])
        self.assertTrue(any("retire the rootless copy" in line for line in module.plan()))
        module.apply()
        self.assertFalse(self.machine.user_active)
        self.assertFalse(self.machine.user_enabled)
        retire = [call for call in self.machine.executed if "--machine" in call]
        self.assertEqual(
            retire, [("systemctl", "--user", "--machine", "alice@.host", "disable", "--now", "earlyoom.service")]
        )
        self.assertNotIn("no rootless copy of earlyoom runs beside the service", self.ok(module))

    def test_a_machine_that_never_had_the_copy_is_not_asked_to_retire_anything(self):
        self.machine.installed = True
        module = self.system(LAPTOP_OPTIONS)
        module.apply()
        self.assertEqual([call for call in self.machine.executed if "--machine" in call], [])
        self.assertNotIn("no rootless copy of earlyoom runs beside the service", self.ok(module))

    def test_the_unit_file_is_rolled_back_with_the_options(self):
        self.user().apply()
        unit_path = self.home / eo.USER_UNIT
        old_unit = unit_path.read_text() + "# an older managed unit\n"
        unit_path.write_text(old_unit)  # the module's unit text changed since: this run rewrites it too
        self.machine.crash_on_args = "--bogus"
        with self.assertRaisesRegex(ModuleError, "previous options were put back"):
            self.user({"extra_args": ["--bogus"]}).apply()
        self.assertEqual(unit_path.read_text(), old_unit)
        self.assertEqual(
            self.machine.executed.count(("systemctl", "--user", "daemon-reload")), 3
        )  # first, write, restore

    def test_retiring_the_copy_does_not_depend_on_asking_the_wrong_manager(self):
        """Root running `systemctl --user is-active` reaches root's manager, so the answer is meaningless there:
        the disable runs whenever the user's unit file exists."""
        self.user().apply()
        self.machine.user_active = False  # what a root-side query would report
        self.machine.installed = True
        self.system(LAPTOP_OPTIONS).apply()
        self.assertIn(
            ("systemctl", "--user", "--machine", "alice@.host", "disable", "--now", "earlyoom.service"),
            self.machine.executed,
        )


class Scripts(unittest.TestCase):
    """The two entry points build the right module from the environment (the engine's way of calling them)."""

    @staticmethod
    def load(name):
        path = str(MODULE / name)
        loader = importlib.machinery.SourceFileLoader(f"script_{name.replace('-', '_')}", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)
        return module

    def test_earlyoom_builds_the_system_or_the_staged_module(self):
        script = self.load("earlyoom")
        config = json.dumps({"mem_percent": 4})
        self.assertIsInstance(script.build({eo.CONFIG_ENV: config}), eo.SystemEarlyoom)
        self.assertIsInstance(script.build({eo.CONFIG_ENV: config, eo.ROOT_ENV: "/x"}), eo.StagedEarlyoom)

    def test_earlyoom_user_builds_the_user_module_and_refuses_root(self):
        script = self.load("earlyoom-user")
        config = json.dumps({"report_interval": 300})
        if os.geteuid() == 0:
            with self.assertRaisesRegex(ModuleError, "not as root"):
                script.build({eo.CONFIG_ENV: config})
        else:
            self.assertIsInstance(script.build({eo.CONFIG_ENV: config}), eo.UserEarlyoom)

    def test_bad_options_reach_the_exit_code_of_both_scripts(self):
        env = {**os.environ, eo.CONFIG_ENV: json.dumps({"avoid": "(x"})}
        for name in ("earlyoom", "earlyoom-user"):
            with self.subTest(name=name):
                result = subprocess.run([str(MODULE / name), "check"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("not a valid POSIX extended regular expression", result.stderr)


class Cli(unittest.TestCase):
    """The scripts as the engine runs them: staged root, JSON options in the environment, exit codes."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_script(self, *argv, options=None, script="earlyoom"):
        env = {**os.environ, eo.ROOT_ENV: self.root}
        env.pop("UBUNTU_TWEAK_MODULE_CONFIG", None)
        if options is not None:
            env["UBUNTU_TWEAK_MODULE_CONFIG"] = json.dumps(options)
        return subprocess.run([str(MODULE / script), *argv], env=env, capture_output=True, text=True)

    def test_check_install_check_and_idempotence(self):
        self.assertEqual(self.run_script("check", options=LAPTOP_OPTIONS).returncode, 3)
        plan = self.run_script("plan", options=LAPTOP_OPTIONS)
        self.assertEqual(plan.returncode, 0)
        self.assertIn("create /etc/default/earlyoom", plan.stdout)
        self.assertFalse(Path(self.root, eo.DEFAULTS_FILE).exists())  # plan and check wrote nothing
        self.assertEqual(self.run_script("install", options=LAPTOP_OPTIONS).returncode, 0)
        self.assertEqual(self.run_script("check", options=LAPTOP_OPTIONS).returncode, 0)
        written = Path(self.root, eo.DEFAULTS_FILE).read_bytes()
        self.assertEqual(self.run_script("install", options=LAPTOP_OPTIONS).returncode, 0)
        self.assertEqual(Path(self.root, eo.DEFAULTS_FILE).read_bytes(), written)
        self.assertEqual(self.run_script("plan", options=LAPTOP_OPTIONS).stdout, "nothing to change\n")
        self.assertEqual(self.run_script("check", options={**LAPTOP_OPTIONS, "mem_percent": 6}).returncode, 3)

    def test_bad_options_exit_1_and_write_nothing(self):
        for options in ({"avoid": "(x"}, {"prefer": "a'b"}, {"bogus": 1}, {"notify": 1}):
            with self.subTest(options=options):
                result = self.run_script("install", options=options)
                self.assertEqual(result.returncode, 1)
                self.assertIn("earlyoom:", result.stderr)
                self.assertFalse(Path(self.root, eo.DEFAULTS_FILE).exists())

    def test_bad_config_json_exits_1(self):
        env = {**os.environ, eo.ROOT_ENV: self.root, "UBUNTU_TWEAK_MODULE_CONFIG": "{nope"}
        result = subprocess.run([str(MODULE / "earlyoom"), "check"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)

    def test_usage_error(self):
        self.assertEqual(self.run_script("frobnicate").returncode, 2)


class Manifest(unittest.TestCase):
    def test_module_toml_is_a_valid_system_module_with_a_rootless_fallback(self):
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.name, "earlyoom")
        self.assertTrue(manifest.source.root)
        self.assertEqual(manifest.source.check, ("earlyoom", "check"))
        self.assertEqual(manifest.source.preview, ("earlyoom", "plan"))
        self.assertEqual([(f.install, f.root) for f in manifest.fallbacks], [(("earlyoom-user", "install"), False)])
        self.assertTrue(any(rule.coverage.value == "tracked" for rule in manifest.coverage))

    def test_scripts_named_by_the_manifest_are_executable(self):
        for name in ("earlyoom", "earlyoom-user"):
            self.assertTrue(os.access(MODULE / name, os.X_OK), name)


if __name__ == "__main__":
    unittest.main()

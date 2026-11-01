"""The module installer against a stateful fake machine (agent-sub + systemctl --user) and a temp HOME."""

import importlib.machinery
import importlib.util
import io
import json
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from ei_fixtures import MODULE

sys.path.insert(0, str(MODULE.parents[1]))

from ubuntu_tweaks.module_script import EXIT_DRIFT, EXIT_NEEDS_MANUAL, EXIT_OK, ModuleError, NeedsManual, main  # noqa: E402
from ubuntu_tweaks.process import CommandResult, Process  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("error_intercept_installer", str(MODULE / "installer"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


inst = load_script()
TOPIC_DEFAULT = {"name": "errors", "role": "", "instructions": "", "spawn_min_severity": "warning", "escalate": []}


class FakeMachine(Process):
    """agent-sub topic show/set and the user's systemctl, with unit files read from the temp home."""

    def __init__(self, units_dir, module_dir, *, topic_known=True):
        self.units_dir = units_dir
        self.module = module_dir
        self.topic = dict(TOPIC_DEFAULT)
        self.topic_known = topic_known
        self.enabled = set()
        self.active = set()
        self.loaded = {}
        self.executed = []
        self.queried = []

    def snapshot(self):
        return {str(p.relative_to(self.units_dir)): p.read_text() for p in self.units_dir.rglob("*") if p.is_file()}

    def exec_start(self):
        dropin = self.loaded.get("update-notifier-crash.service.d/10-error-intercept.conf", "")
        if "ExecStart=" in dropin:
            return f"{{ path={self.module}/error-intercept ; argv[]={self.module}/error-intercept ingest ; }}"
        return "{ path=/usr/lib/update-notifier/update-notifier-crash ; }"

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        self.queried.append(tuple(argv))
        if argv[0].endswith("agent-sub"):
            if argv[1:4] == ["topic", "show", "errors"] and self.topic_known:
                return CommandResult(0, json.dumps(self.topic), "")
            return CommandResult(2, "", "")
        if argv[:2] != ["systemctl", "--user"]:
            return CommandResult(127, "", "")
        verb, rest = argv[2], argv[3:]
        if verb == "show" and "NeedDaemonReload" in rest:
            return CommandResult(0, ("yes" if self.snapshot() != self.loaded else "no") + "\n", "")
        if verb == "show" and "ExecStart" in rest:
            return CommandResult(0, self.exec_start() + "\n", "")
        if verb == "is-enabled":
            return CommandResult(0, ("enabled" if rest[0] in self.enabled else "disabled") + "\n", "")
        if verb == "is-active":
            return CommandResult(0, ("active" if rest[0] in self.active else "inactive") + "\n", "")
        return CommandResult(1, "", "")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        self.executed.append(tuple(argv))
        if argv[0].endswith("agent-sub"):
            self.set_topic(argv)
        elif argv[2] == "daemon-reload":
            self.loaded = self.snapshot()
        elif argv[2] == "enable":
            self.enabled.add(argv[-1])
            if "--now" in argv:
                self.active.add(argv[-1])

    def set_topic(self, argv):
        words = argv[4:]
        self.topic["role"] = Path(words[words.index("--role-file") + 1]).read_text()
        self.topic["instructions"] = Path(words[words.index("--instructions-file") + 1]).read_text()
        self.topic["spawn_min_severity"] = words[words.index("--spawn-min-severity") + 1]
        self.topic["escalate"] = words[words.index("--escalate") + 1 :]


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ei-inst-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "checkout"
        self.module = self.checkout / "modules/error-intercept"
        shutil.copytree(MODULE, self.module, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        self.tools = self.root / "agent-tools"
        launcher = self.tools / "lib/agenttools_subscriptions/agent-sub"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        self.units = self.home / ".config/systemd/user"
        self.machine = FakeMachine(self.units, self.module)
        self.out = io.StringIO()

    def build(self, *, staged=True, options=None, is_root=False, has_bus=True, which=lambda name: None):
        options = {"agent_tools_dir": str(self.tools), **(options or {})}
        return inst.ErrorIntercept(
            self.machine,
            self.home,
            {},
            options,
            is_root=is_root,
            staged=staged,
            module_dir=self.module,
            has_bus=has_bus,
            which=which,
            out=self.out,
        )

    def run_main(self, module, command):
        out, err = io.StringIO(), io.StringIO()
        return main(module, [command], out=out, err=err), out.getvalue(), err.getvalue()

    def reset_out(self):
        self.out.seek(0)
        self.out.truncate(0)

    def last_summary(self):
        return [line for line in self.out.getvalue().splitlines() if line.startswith("error-intercept:")][-1]


class StagedInstallTest(InstallerCase):
    def test_install_writes_everything_and_registers_the_topic(self):
        self.build().apply()
        dropin = (self.units / "update-notifier-crash.service.d/10-error-intercept.conf").read_text()
        self.assertIn(f"ExecStart=\nExecStart={self.module}/error-intercept ingest\n", dropin)
        self.assertNotIn("@MODULE@", dropin)
        for unit in (
            "error-intercept-scan.service",
            "error-intercept-spool.service",
            "error-intercept-spool.path",
            "error-intercept-drain.service",
            "error-intercept-drain.timer",
        ):
            text = (self.units / unit).read_text()
            self.assertNotIn("@MODULE@", text)
        self.assertIn(
            f"ExecStart={self.module}/error-intercept ingest-spool",
            (self.units / "error-intercept-spool.service").read_text(),
        )
        self.assertEqual(
            (self.home / ".local/bin/error-intercept").resolve(), (self.module / "error-intercept").resolve()
        )
        self.assertEqual(
            (self.home / ".local/bin/agent-sub").resolve(),
            (self.tools / "lib/agenttools_subscriptions/agent-sub").resolve(),
        )
        for name in ("archive", "spool", "pending"):
            mode = stat.S_IMODE((self.home / ".local/state/error-intercept" / name).stat().st_mode)
            self.assertEqual(mode, 0o700)
        self.assertIn(
            f"ExecStart={self.module}/error-intercept drain", (self.units / "error-intercept-drain.service").read_text()
        )
        self.assertIn("OnUnitActiveSec=5min", (self.units / "error-intercept-drain.timer").read_text())
        self.assertIn("Persistent=false", (self.units / "error-intercept-drain.timer").read_text())
        self.assertEqual(self.machine.topic["escalate"], ["tg", "--tag", "problem"])
        self.assertIn("systematic-debugging", self.machine.topic["role"])
        (topic_call,) = [c for c in self.machine.executed if c[0].endswith("agent-sub")]
        self.assertEqual(topic_call[1:4], ("topic", "set", "errors"))
        self.assertEqual(topic_call[-4:], ("--escalate", "tg", "--tag", "problem"))

    def test_second_run_changes_nothing_and_check_passes(self):
        self.build().apply()
        self.machine.executed.clear()
        self.reset_out()
        self.build().apply()
        self.assertEqual(self.last_summary(), "error-intercept: 0 changed")
        self.assertEqual(self.machine.executed, [])
        self.assertEqual(self.run_main(self.build(), "check")[0], EXIT_OK)

    def test_staged_never_runs_systemctl(self):
        self.build().apply()
        self.assertFalse([c for c in self.machine.executed + self.machine.queried if c[0] == "systemctl"])

    def test_check_before_install_is_drift_and_names_the_files(self):
        code, out, _ = self.run_main(self.build(), "check")
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("10-error-intercept.conf", out)
        self.assertIn("agent-sub topic errors registered", out)
        self.assertFalse(self.units.exists())

    def test_plan_shows_the_diff_and_writes_nothing(self):
        lines = self.build().plan()
        text = "\n".join(lines)
        self.assertIn("write", text)
        self.assertIn(f"+ExecStart={self.module}/error-intercept ingest", text)
        self.assertIn("agent-sub topic set errors", text)
        self.assertFalse(self.units.exists())
        self.build().apply()
        self.assertEqual(self.build().plan(), ["nothing to change"])

    def test_a_changed_unit_is_repaired(self):
        self.build().apply()
        (self.units / "error-intercept-spool.service").write_text("[Service]\nExecStart=/bin/true\n")
        self.machine.topic["role"] = "something else"
        self.reset_out()
        self.build().apply()
        self.assertIn("error-intercept-spool.service", self.out.getvalue())
        self.assertIn("agent-sub topic errors registered", self.out.getvalue())
        self.assertEqual(self.last_summary(), "error-intercept: 2 changed")

    def test_the_journal_unit_is_installed_only_when_the_module_ships_it(self):
        self.build().apply()
        self.assertFalse((self.units / "error-intercept-journal.service").exists())
        (self.module / "units/error-intercept-journal.service").write_text(
            "[Service]\nExecStart=@MODULE@/error-intercept journal\n[Install]\nWantedBy=default.target\n"
        )
        module = self.build()
        module.apply()
        self.assertIn(
            f"{self.module}/error-intercept journal", (self.units / "error-intercept-journal.service").read_text()
        )
        self.assertEqual(self.run_main(self.build(), "check")[0], EXIT_OK)

    def test_a_file_where_a_link_should_be_is_never_overwritten(self):
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/error-intercept").write_text("mine\n")
        with self.assertRaisesRegex(ModuleError, "left alone"):
            self.build().apply()
        self.assertEqual((self.home / ".local/bin/error-intercept").read_text(), "mine\n")

    def test_without_agent_tools_everything_else_is_done_and_the_rest_is_manual(self):
        shutil.rmtree(self.tools)
        module = self.build()
        with self.assertRaises(NeedsManual) as raised:
            module.apply()
        self.assertIn("agent_tools_dir", str(raised.exception))
        self.assertTrue((self.units / "error-intercept-spool.path").is_file())
        self.assertTrue((self.home / ".local/bin/error-intercept").is_symlink())
        self.assertFalse((self.home / ".local/bin/agent-sub").exists())
        self.assertEqual(self.run_main(self.build(), "status")[0], EXIT_DRIFT)
        self.assertIn("manual", self.run_main(self.build(), "status")[1])

    def test_an_agent_sub_already_on_path_needs_no_link(self):
        module = self.build(which=lambda name: "/usr/bin/agent-sub" if name == "agent-sub" else None)
        module.apply()
        self.assertFalse((self.home / ".local/bin/agent-sub").exists())

    def test_root_and_bad_options_are_refused(self):
        with self.assertRaisesRegex(ModuleError, "not root"):
            self.build(is_root=True).apply()
        with self.assertRaisesRegex(ModuleError, "unknown option"):
            self.build(options={"bogus": 1})
        with self.assertRaisesRegex(ModuleError, "non-empty string"):
            self.build(options={"agent_tools_dir": ""})


class LiveInstallTest(InstallerCase):
    """Not staged: the same installer talks to systemctl --user (the fake) and checks the stable checkout."""

    def test_install_reloads_after_writing_and_enables_the_units(self):
        self.build(staged=False).apply()
        commands = [c for c in self.machine.executed if c[0] == "systemctl"]
        self.assertEqual(commands[0], ("systemctl", "--user", "daemon-reload"))
        self.assertIn(("systemctl", "--user", "enable", "error-intercept-scan.service"), commands)
        self.assertIn(("systemctl", "--user", "enable", "--now", "error-intercept-spool.path"), commands)
        self.assertIn(("systemctl", "--user", "enable", "--now", "error-intercept-drain.timer"), commands)
        self.assertEqual(self.machine.exec_start().count("error-intercept ingest"), 1)

    def test_plan_lists_the_reload_before_the_first_enable_as_apply_runs_them(self):
        lines = self.build(staged=False).plan()
        reload = lines.index("systemctl --user daemon-reload")
        first_enable = next(i for i, line in enumerate(lines) if line.startswith("systemctl --user enable"))
        self.assertLess(max(i for i, line in enumerate(lines) if line.startswith("write ")), reload)
        self.assertLess(reload, first_enable)
        self.assertEqual(lines.count("systemctl --user daemon-reload"), 1)

    def test_second_run_changes_nothing(self):
        self.build(staged=False).apply()
        self.machine.executed.clear()
        self.reset_out()
        self.build(staged=False).apply()
        self.assertEqual(self.last_summary(), "error-intercept: 0 changed")
        self.assertEqual(self.machine.executed, [])
        self.assertEqual(self.run_main(self.build(staged=False), "check")[0], EXIT_OK)

    def test_a_hand_edited_unit_is_detected_as_pending_reload_until_reloaded(self):
        self.build(staged=False).apply()
        unit = self.units / "error-intercept-spool.service"
        unit.write_text(unit.read_text() + "# edit\n")
        self.assertEqual(self.run_main(self.build(staged=False), "check")[0], EXIT_DRIFT)
        self.build(staged=False).apply()
        self.assertEqual(self.run_main(self.build(staged=False), "check")[0], EXIT_OK)

    def test_a_later_drop_in_that_overrides_ours_is_an_error_not_a_success(self):
        self.build(staged=False).apply()
        self.machine.exec_start = lambda: "{ path=/other ; argv[]=/other ; }"
        with self.assertRaisesRegex(ModuleError, "overrides ours"):
            self.build(staged=False).apply()

    def test_no_user_bus_writes_the_files_and_asks_for_the_session(self):
        with self.assertRaises(NeedsManual) as raised:
            self.build(staged=False, has_bus=False).apply()
        self.assertIn("no user systemd bus", str(raised.exception))
        self.assertTrue((self.units / "update-notifier-crash.service.d/10-error-intercept.conf").is_file())
        self.assertEqual([c for c in self.machine.executed if c[0] == "systemctl"], [])
        self.assertEqual(self.run_main(self.build(staged=False, has_bus=False), "check")[0], EXIT_DRIFT)

    def test_a_git_worktree_is_refused_before_anything_is_written(self):
        (self.checkout / ".git").write_text("gitdir: /elsewhere\n")
        with self.assertRaisesRegex(ModuleError, "git worktree"):
            self.build(staged=False).apply()
        self.assertFalse(self.units.exists())

    def test_the_exit_code_of_a_manual_step_is_ten(self):
        shutil.rmtree(self.tools)
        code, out, _ = self.run_main_install(self.build(staged=False))
        self.assertEqual(code, EXIT_NEEDS_MANUAL)
        self.assertIn("needs manual", out)

    def run_main_install(self, module):
        return self.run_main(module, "install")


if __name__ == "__main__":
    unittest.main()

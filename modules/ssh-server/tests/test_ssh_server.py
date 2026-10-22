"""ssh-server module against a fake root and a stateful fake machine (no real systemd, apt or sshd)."""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE.parents[1]))
sys.dont_write_bytecode = True

from ubuntu_tweaks.module_script import (  # noqa: E402
    EXIT_DRIFT,
    EXIT_FAILED,
    EXIT_NEEDS_MANUAL,
    EXIT_OK,
    EXIT_USAGE,
    Account,
    ModuleError,
    NeedsManual,
    main,
)
from ubuntu_tweaks.process import CommandError, CommandResult, Process  # noqa: E402


def load_script():
    path = str(MODULE / "ssh-server")
    loader = importlib.machinery.SourceFileLoader("ssh_server_script", path)
    spec = importlib.util.spec_from_loader("ssh_server_script", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    loader.exec_module(module)
    return module


ssh = load_script()

MOSHI = """\
Port 2223
ListenAddress 100.74.26.95
ListenAddress fd7a:115c:a1e0::1233:1a5f
PubkeyAuthentication yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
PermitRootLogin no
AllowUsers alice
"""
STOCK_CONFIG = "Include /etc/ssh/sshd_config.d/*.conf\n\nPort 22\n#ListenAddress 0.0.0.0\nUsePAM yes\n"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXampleEXampleEXampleEXampleEXample test@host"
MUTATING = {"apt-get", "apt", "enable", "disable", "start", "stop", "reload", "restart", "daemon-reload", "reset-failed", "mask"}


class FakeMachine(Process):
    """Answers the queries the module makes from a small state, and applies the mutations it executes."""

    def __init__(
        self, root, *, installed=True, enabled=False, active=False, sshd_ok=True, socket=False, ss_output=None
    ):
        self.root = root
        self.installed = installed
        self.enabled = enabled
        self.active = active
        self.sshd_ok = sshd_ok
        self.socket = socket  # ssh.socket enabled and active
        self.ss_output = ss_output  # a literal `ss -H -ltn` answer instead of one derived from the config
        self.loaded_dropin = None  # the unit drop-in text systemd has loaded (None: no drop-in)
        self.fail_on = None  # a (verb, unit) whose execution fails
        self.executed = []
        self.queried = []

    def listeners(self):
        if not self.active:
            return []
        settings = ssh.read_sshd_settings(self.root)
        return [(host or "*", port) for host, port in ssh.expected_listeners(settings)]

    def query(self, argv, *, timeout=60.0, cwd=None):
        argv = list(argv)
        self.queried.append(tuple(argv))
        if argv[0] == "dpkg-query":
            return CommandResult(0 if self.installed else 1, "install ok installed" if self.installed else "", "")
        if argv[:2] == ["systemctl", "is-enabled"]:
            on = (self.socket and argv[2] == "ssh.socket") or (self.enabled and argv[2] == "ssh.service")
            return CommandResult(0 if on else 1, "enabled\n" if on else "disabled\n", "")
        if argv[:2] == ["systemctl", "is-active"]:
            on = (self.socket and argv[2] == "ssh.socket") or (self.active and argv[2] == "ssh.service")
            return CommandResult(0 if on else 3, "active\n" if on else "failed\n", "")
        if argv[:2] == ["systemctl", "show"]:
            try:
                text = (self.root / ssh.UNIT_DROPIN).read_text()
            except OSError:
                text = None
            return CommandResult(0, "yes\n" if text != self.loaded_dropin else "no\n", "")
        if argv[0] == "ss":
            if self.ss_output is not None:
                return CommandResult(0, self.ss_output, "")
            lines = [f"LISTEN 0 128 {host}:{port} 0.0.0.0:*" for host, port in self.listeners()]
            return CommandResult(0, "\n".join(lines), "")
        if argv == [ssh.SSHD, "-t"]:
            return CommandResult(0 if self.sshd_ok else 255, "", "" if self.sshd_ok else "bad configuration option")
        return CommandResult(127, "", f"{argv[0]}: not faked")

    def execute(self, argv, *, input_text=None, cwd=None):
        argv = list(argv)
        self.executed.append(tuple(argv))
        if self.fail_on and argv[:2] == ["systemctl", self.fail_on]:
            if self.fail_on == "restart":
                self.active = False  # a failed restart leaves sshd stopped
            raise CommandError(f"{' '.join(argv)}: exited with status 1")
        if argv[:2] == ["systemctl", "daemon-reload"]:
            try:
                self.loaded_dropin = (self.root / ssh.UNIT_DROPIN).read_text()
            except OSError:
                self.loaded_dropin = None
        if argv[0] == "apt-get":
            self.installed = True
        elif argv[:2] == ["systemctl", "enable"]:
            self.enabled = True
        elif argv[:3] == ["systemctl", "disable", "--now"] and argv[3] == "ssh.socket":
            self.socket = False
        elif argv[:2] == ["systemctl", "start"]:
            self.active = True


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home/alice"
        (self.home / ".ssh").mkdir(parents=True)
        self.account = Account("alice", self.home, 1000)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def module(self, machine, options=None, *, is_root=True):
        return ssh.SshServer(machine, self.root, options or {}, self.account, is_root=is_root)

    def tailscale_box(self, **state):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.write("etc/ssh/sshd_config.d/99-moshi-tailscale.conf", MOSHI)
        self.write("usr/lib/systemd/system/tailscaled.service", "[Unit]\n")
        return FakeMachine(self.root, enabled=True, **state)


class ConfigParsing(unittest.TestCase):
    def test_listen_address_forms(self):
        self.assertEqual(ssh.parse_listen_address("10.0.0.1"), ("10.0.0.1", None))
        self.assertEqual(ssh.parse_listen_address("10.0.0.1:2200"), ("10.0.0.1", 2200))
        self.assertEqual(ssh.parse_listen_address("[fd7a::1]:2200"), ("fd7a::1", 2200))
        self.assertEqual(ssh.parse_listen_address("fd7a:115c:a1e0::1233:1a5f"), ("fd7a:115c:a1e0::1233:1a5f", None))

    def test_keyword_forms(self):
        self.assertEqual(ssh._split_keyword("Port 22"), ("port", "22"))
        self.assertEqual(ssh._split_keyword("port=2222  # why"), ("port", "2222"))
        self.assertEqual(ssh._split_keyword("  ListenAddress = 1.2.3.4"), ("listenaddress", "1.2.3.4"))
        self.assertIsNone(ssh._split_keyword("# Port 22"))

    def test_include_order_and_match_blocks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "etc/ssh/sshd_config.d").mkdir(parents=True)
            (root / "etc/ssh/sshd_config").write_text(STOCK_CONFIG + "Match User bob\n    Port 9999\n")
            (root / "etc/ssh/sshd_config.d/99-moshi-tailscale.conf").write_text(MOSHI)
            settings = ssh.read_sshd_settings(root)
        self.assertEqual(settings.ports, (2223, 22))  # the Include comes first, then the main file's Port
        self.assertEqual(settings.listen[0], ("100.74.26.95", None))
        self.assertTrue(settings.includes_dropins)

    def test_default_port_and_no_include(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "etc/ssh").mkdir(parents=True)
            (root / "etc/ssh/sshd_config").write_text("UsePAM yes\n")
            settings = ssh.read_sshd_settings(root)
        self.assertEqual(settings.ports, (22,))
        self.assertFalse(settings.includes_dropins)
        self.assertEqual(ssh.expected_listeners(settings), [(None, 22)])

    def test_listen_address_without_port_listens_on_every_port(self):
        settings = ssh.SshdSettings((2223, 22), (("100.64.0.1", None), ("10.0.0.1", 2200)), True, {})
        self.assertEqual(
            ssh.expected_listeners(settings),
            [("100.64.0.1", 2223), ("100.64.0.1", 22), ("10.0.0.1", 2200)],
        )

    def test_listener_matching(self):
        listeners = ssh.parse_listeners(
            "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 [::]:22 [::]:*\n"
            "LISTEN 0 128 100.74.26.95:2223 0.0.0.0:*\nLISTEN 0 128 [fd7a:115c:a1e0::1233:1a5f]:2224 [::]:*\n"
        )
        self.assertTrue(ssh.is_listening((None, 22), listeners))
        self.assertTrue(ssh.is_listening(("100.74.26.95", 2223), listeners))
        self.assertFalse(ssh.is_listening(("100.74.26.95", 2224), listeners))
        self.assertTrue(ssh.is_listening(("fd7a:115c:a1e0:0::1233:1a5f", 2224), listeners))
        self.assertFalse(ssh.is_listening((None, 2225), listeners))
        self.assertTrue(ssh.is_listening(("10.9.9.9", 22), listeners))  # a wildcard socket covers any address

    def test_tailscale_addresses(self):
        for host in ("100.74.26.95", "100.64.0.1", "fd7a:115c:a1e0::1233:1a5f"):
            self.assertTrue(ssh.is_tailscale_address(host), host)
        for host in ("192.168.1.2", "100.128.0.1", "fd00::1", "example.com"):
            self.assertFalse(ssh.is_tailscale_address(host), host)


class Check(Base):
    def test_this_machines_boot_failure_is_unsatisfied(self):
        machine = self.tailscale_box(active=False)
        findings = {f.subject: f.ok for f in self.module(machine).findings()}
        self.assertTrue(findings["package openssh-server"])
        self.assertFalse(findings[f"/{ssh.UNIT_DROPIN} (After=tailscaled.service)"])
        self.assertFalse(findings["ssh.service active"])
        self.assertFalse(findings["sshd listening on 100.74.26.95:2223"])
        self.assertFalse(findings["sshd listening on [fd7a:115c:a1e0::1233:1a5f]:2223"])

    def test_check_never_mutates(self):
        machine = self.tailscale_box()
        self.module(machine, is_root=False).findings()
        self.assertEqual(machine.executed, [])
        words = {word for argv in machine.queried for word in argv}
        self.assertFalse(words & MUTATING, words & MUTATING)

    def test_plain_ubuntu_needs_no_tailscale_ordering(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.write("usr/lib/systemd/system/tailscaled.service", "[Unit]\n")
        subjects = [f.subject for f in self.module(FakeMachine(self.root)).findings()]
        self.assertFalse([s for s in subjects if "tailscale" in s])

    def test_tailscale_listen_without_tailscaled_unit_needs_no_ordering(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.write("etc/ssh/sshd_config.d/99-moshi-tailscale.conf", MOSHI)
        subjects = [f.subject for f in self.module(FakeMachine(self.root)).findings()]
        self.assertFalse([s for s in subjects if "tailscale" in s])

    def test_missing_package_is_unsatisfied(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        findings = self.module(FakeMachine(self.root, installed=False)).findings()
        self.assertFalse(findings[0].ok)

    def test_socket_activation_is_an_unmet_fact(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        machine = FakeMachine(self.root, enabled=True, active=True, socket=True)
        findings = {f.subject: f.ok for f in self.module(machine).findings()}
        self.assertFalse([s for s, ok in findings.items() if s.startswith("ssh.socket off") and ok])
        self.assertTrue(findings["ssh.service enabled"])

    def test_the_real_ss_output_of_this_machine_satisfies_the_listener_facts(self):
        machine = self.tailscale_box(active=True)
        machine.ss_output = (
            "LISTEN 0 128 100.74.26.95:2223 0.0.0.0:*\n"
            "LISTEN 0 128 [fd7a:115c:a1e0::1233:1a5f]:2223 [::]:*\n"
            "LISTEN 0 4096 127.0.0.54:53 0.0.0.0:*\n"
        )
        findings = {f.subject: f.ok for f in self.module(machine).findings()}
        self.assertTrue(findings["sshd listening on 100.74.26.95:2223"])
        self.assertTrue(findings["sshd listening on [fd7a:115c:a1e0::1233:1a5f]:2223"])
        machine.ss_output = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"  # sshd on the default port is not the configured one
        findings = {f.subject: f.ok for f in self.module(machine).findings()}
        self.assertFalse(findings["sshd listening on 100.74.26.95:2223"])

    def test_a_drop_in_the_checker_cannot_read_is_a_failure_not_an_absence(self):
        machine = self.tailscale_box()
        path = self.root / "etc/ssh/sshd_config.d/99-moshi-tailscale.conf"
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o644)
        if os.access(path, os.R_OK):
            self.skipTest("running as root: mode 000 is still readable")
        with self.assertRaisesRegex(ModuleError, "cannot read .*99-moshi-tailscale.conf"):
            self.module(machine, is_root=False).findings()

    def test_a_wildcard_of_the_other_family_does_not_cover_a_named_address(self):
        listeners = ssh.parse_listeners("LISTEN 0 128 0.0.0.0:2223 0.0.0.0:*\n")
        self.assertFalse(ssh.is_listening(("fd7a:115c:a1e0::1233:1a5f", 2223), listeners))
        self.assertTrue(ssh.is_listening(("100.74.26.95", 2223), listeners))
        # sshd sets IPV6_V6ONLY: a `::` socket never serves a v4 address, but a `*` one does
        self.assertFalse(ssh.is_listening(("100.74.26.95", 2223), ssh.parse_listeners("LISTEN 0 128 [::]:2223 [::]:*\n")))
        self.assertTrue(ssh.is_listening(("100.74.26.95", 2223), ssh.parse_listeners("LISTEN 0 128 *:2223 *:*\n")))

    def test_a_hostname_listen_address_is_matched_by_port(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG + "ListenAddress localhost\n")
        machine = FakeMachine(self.root, enabled=True, active=True, ss_output="LISTEN 0 128 127.0.0.1:22 0.0.0.0:*\n")
        findings = {f.subject: f.ok for f in self.module(machine).findings()}
        self.assertTrue(findings["sshd listening on localhost:22"])

    def test_options_are_validated(self):
        machine = FakeMachine(self.root)
        with self.assertRaises(ModuleError):
            self.module(machine, {"password_auth": "no"})
        with self.assertRaises(ModuleError):
            self.module(machine, {"port": 22})


class Install(Base):
    def test_unit_dropin_golden(self):
        self.assertEqual(
            ssh.UNIT_DROPIN_TEXT.splitlines()[2:],
            [
                "[Unit]",
                "After=tailscaled.service",
                "Wants=tailscaled.service",
                "StartLimitIntervalSec=120",
                "StartLimitBurst=10",
                "",
                "[Service]",
                "Restart=on-failure",
                "RestartSec=5",
                "RestartPreventExitStatus=",
            ],
        )

    def test_fixes_this_machine_and_converges(self):
        machine = self.tailscale_box()
        module = self.module(machine)
        module.apply()
        self.assertTrue(all(f.ok for f in module.findings()), [f.line() for f in module.findings() if not f.ok])
        self.assertEqual((self.root / ssh.UNIT_DROPIN).read_text(), ssh.UNIT_DROPIN_TEXT)
        self.assertEqual(oct((self.root / ssh.UNIT_DROPIN).stat().st_mode & 0o777), "0o644")
        self.assertEqual(
            machine.executed,
            [
                ("systemctl", "daemon-reload"),
                ("systemctl", "reset-failed", "ssh.service"),
                ("systemctl", "start", "ssh.service"),
            ],
        )
        # the machine's own configuration is untouched
        self.assertEqual((self.root / "etc/ssh/sshd_config.d/99-moshi-tailscale.conf").read_text(), MOSHI)

    def test_second_run_changes_nothing(self):
        machine = self.tailscale_box()
        module = self.module(machine)
        module.apply()
        machine.executed.clear()
        module.apply()
        self.assertEqual(machine.executed, [])

    def test_fresh_ubuntu_installs_and_enables(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        before = os.environ.get("DEBIAN_FRONTEND")
        machine = FakeMachine(self.root, installed=False)
        module = self.module(machine)
        module.apply()
        self.assertEqual(machine.executed[0], ("apt-get", "install", "-y", "openssh-server"))
        self.assertIn(("systemctl", "enable", "ssh.service"), machine.executed)
        self.assertTrue(all(f.ok for f in module.findings()))
        self.assertEqual(sorted((self.root / "etc").rglob("*.conf")), [])  # no config written
        self.assertEqual(os.environ.get("DEBIAN_FRONTEND"), before)  # restored

    def test_refuses_without_root(self):
        machine = self.tailscale_box()
        with self.assertRaises(ModuleError):
            self.module(machine, is_root=False).apply()
        self.assertEqual(machine.executed, [])

    def test_invalid_configuration_stops_before_any_restart_and_leaves_nothing_behind(self):
        machine = self.tailscale_box(sshd_ok=False)
        with self.assertRaisesRegex(ModuleError, r"(?s)sshd -t rejects.*bad configuration option"):
            self.module(machine).apply()
        self.assertEqual(machine.executed, [])
        self.assertFalse((self.root / ssh.UNIT_DROPIN).exists())  # written only after sshd accepted the config

    def test_a_second_run_after_a_rejection_still_reloads_the_units(self):
        machine = self.tailscale_box(sshd_ok=False)
        with self.assertRaises(ModuleError):
            self.module(machine).apply()
        machine.sshd_ok = True
        self.module(machine).apply()
        self.assertEqual(machine.executed[0], ("systemctl", "daemon-reload"))

    def test_a_running_healthy_sshd_is_not_restarted_for_the_unit_dropin(self):
        machine = self.tailscale_box(active=True)
        self.module(machine).apply()
        self.assertEqual(machine.executed, [("systemctl", "daemon-reload")])

    def test_socket_activation_is_switched_off_before_the_service_starts(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        machine = FakeMachine(self.root, enabled=False, active=False, socket=True)
        module = self.module(machine)
        module.apply()
        self.assertEqual(
            machine.executed,
            [
                ("systemctl", "disable", "--now", "ssh.socket"),
                ("systemctl", "enable", "ssh.service"),
                ("systemctl", "reset-failed", "ssh.service"),
                ("systemctl", "start", "ssh.service"),
            ],
        )
        self.assertTrue(all(f.ok for f in module.findings()))

    def test_socket_activation_with_a_running_service_restarts_it_as_a_plain_service(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        (self.home / ".ssh/authorized_keys").write_text(f"{KEY}\n")
        machine = FakeMachine(self.root, enabled=True, active=True, socket=True)
        self.module(machine, {"password_auth": False}).apply()
        self.assertEqual(
            machine.executed, [("systemctl", "disable", "--now", "ssh.socket"), ("systemctl", "restart", "ssh.service")]
        )


class ReloadFailures(Base):
    def keyed(self):
        (self.home / ".ssh/authorized_keys").write_text(f"{KEY}\n")

    def test_a_failed_reload_removes_the_policy_file_so_check_cannot_claim_it_applied(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True)
        machine.fail_on = "reload"
        module = self.module(machine, {"password_auth": False})
        with self.assertRaisesRegex(ModuleError, "policy file was removed"):
            module.apply()
        self.assertFalse((self.root / ssh.AUTH_DROPIN).exists())
        self.assertFalse(all(f.ok for f in module.findings()))

    def test_a_daemon_reload_that_did_not_happen_is_drift_and_is_retried(self):
        machine = self.tailscale_box(active=True)
        machine.fail_on = "daemon-reload"
        with self.assertRaises(ModuleError):
            self.module(machine).apply()
        subjects = {f.subject: f.ok for f in self.module(machine).findings()}
        self.assertFalse(subjects["systemd has loaded the current ssh.service files"])
        machine.fail_on = None
        self.module(machine).apply()
        self.assertTrue(all(f.ok for f in self.module(machine).findings()))


class PasswordPolicy(Base):
    def keyed(self):
        (self.home / ".ssh/authorized_keys").write_text(f"# comment\n{KEY}\n")

    def test_false_without_authorized_key_refuses_and_writes_nothing(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        machine = FakeMachine(self.root, enabled=True, active=True)
        with self.assertRaisesRegex(NeedsManual, "lock you out"):
            self.module(machine, {"password_auth": False}).apply()
        self.assertFalse((self.root / ssh.AUTH_DROPIN).exists())
        self.assertEqual(machine.executed, [])

    def test_false_with_key_writes_dropin_and_reloads(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True)
        module = self.module(machine, {"password_auth": False})
        module.apply()
        text = (self.root / ssh.AUTH_DROPIN).read_text()
        self.assertIn("PasswordAuthentication no\nKbdInteractiveAuthentication no\n", text)
        self.assertEqual(machine.executed, [("systemctl", "reload", "ssh.service")])
        self.assertTrue(all(f.ok for f in module.findings()))

    def test_unsatisfied_drop_in_without_key_is_reported_as_manual(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        findings = self.module(FakeMachine(self.root), {"password_auth": False}).findings()
        manual = [f for f in findings if f.manual]
        self.assertEqual(len(manual), 1)
        self.assertIn("no usable authorized key", manual[0].detail)

    def test_rejected_dropin_is_rolled_back(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True, sshd_ok=False)
        with self.assertRaises(ModuleError):
            self.module(machine, {"password_auth": False}).apply()
        self.assertFalse((self.root / ssh.AUTH_DROPIN).exists())

    def test_true_needs_no_key(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        machine = FakeMachine(self.root, enabled=True, active=True)
        self.module(machine, {"password_auth": True}).apply()
        self.assertEqual((self.root / ssh.AUTH_DROPIN).read_text(), ssh.AUTH_DROPIN_TEXT[True])

    def test_option_absent_leaves_existing_policy_alone(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.write(ssh.AUTH_DROPIN, "PasswordAuthentication yes\n")
        machine = FakeMachine(self.root, enabled=True, active=True)
        self.module(machine).apply()
        self.assertEqual((self.root / ssh.AUTH_DROPIN).read_text(), "PasswordAuthentication yes\n")

    def test_challenge_response_above_the_include_shadows_the_kbd_interactive_line(self):
        self.write("etc/ssh/sshd_config", "ChallengeResponseAuthentication yes\n" + STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True)
        with self.assertRaisesRegex(NeedsManual, "kbdinteractiveauthentication"):
            self.module(machine, {"password_auth": False}).apply()
        self.assertFalse((self.root / ssh.AUTH_DROPIN).exists())

    def test_no_include_line_is_a_manual_step(self):
        self.write("etc/ssh/sshd_config", "UsePAM yes\n")
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True)
        with self.assertRaisesRegex(NeedsManual, "Include"):
            self.module(machine, {"password_auth": False}).apply()

    def test_a_correct_policy_file_without_the_include_line_is_still_manual(self):
        self.write("etc/ssh/sshd_config", "UsePAM yes\n")
        self.write(ssh.AUTH_DROPIN, ssh.AUTH_DROPIN_TEXT[True])
        machine = FakeMachine(self.root, enabled=True, active=True)
        module = self.module(machine, {"password_auth": True})
        manual = [f for f in module.findings() if f.manual]
        self.assertEqual(len(manual), 1)
        with self.assertRaisesRegex(NeedsManual, "Include"):
            module.apply()

    def test_a_setting_above_the_include_line_shadows_the_drop_in(self):
        self.write("etc/ssh/sshd_config", "PasswordAuthentication yes\n" + STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True)
        module = self.module(machine, {"password_auth": False})
        with self.assertRaisesRegex(NeedsManual, "above the Include"):
            module.apply()
        self.assertFalse((self.root / ssh.AUTH_DROPIN).exists())  # rolled back
        self.assertEqual(machine.executed, [])

    def test_shadowing_is_reported_by_check_once_the_file_exists(self):
        self.write("etc/ssh/sshd_config", "PasswordAuthentication yes\n" + STOCK_CONFIG)
        self.write(ssh.AUTH_DROPIN, ssh.AUTH_DROPIN_TEXT[False])
        findings = self.module(FakeMachine(self.root), {"password_auth": False}).findings()
        self.assertTrue([f for f in findings if f.manual and "reads passwordauthentication" in f.detail])

    def test_text_that_is_not_a_public_key_does_not_count(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        (self.home / ".ssh/authorized_keys").write_text("ssh-ed25519 notakey\ngarbage\n")
        machine = FakeMachine(self.root, enabled=True, active=True)
        with self.assertRaises(NeedsManual):
            self.module(machine, {"password_auth": False}).apply()

    def test_restricted_keys_do_not_count(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        (self.home / ".ssh/authorized_keys").write_text(f'command="/usr/bin/rrsync /backup",restrict {KEY}\nfrom="10.0.0.0/8" {KEY}\n')
        machine = FakeMachine(self.root, enabled=True, active=True)
        with self.assertRaises(NeedsManual):
            self.module(machine, {"password_auth": False}).apply()

    def test_an_already_written_policy_does_not_need_the_lockout_guard_again(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.write(ssh.AUTH_DROPIN, ssh.AUTH_DROPIN_TEXT[False])
        machine = FakeMachine(self.root, enabled=False, active=False)
        self.module(machine, {"password_auth": False}).apply()  # no authorized key: repairs still run
        self.assertIn(("systemctl", "start", "ssh.service"), machine.executed)

    def test_a_failure_after_the_write_removes_the_policy_file(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=False, active=False)
        machine.fail_on = "enable"
        with self.assertRaisesRegex(ModuleError, "removed again"):
            self.module(machine, {"password_auth": False}).apply()
        self.assertFalse((self.root / ssh.AUTH_DROPIN).exists())

    def test_a_failed_restart_after_leaving_socket_activation_brings_the_socket_back(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        machine = FakeMachine(self.root, enabled=True, active=True, socket=True)
        machine.fail_on = "restart"
        with self.assertRaises(ModuleError):
            self.module(machine).apply()
        self.assertIn(("systemctl", "enable", "--now", "ssh.socket"), machine.executed)

    def test_a_root_shell_cannot_say_whose_lockout_to_prevent(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        self.keyed()
        machine = FakeMachine(self.root, enabled=True, active=True)
        module = ssh.SshServer(
            machine, self.root, {"password_auth": False}, Account("root", self.home, 0), is_root=True
        )
        with self.assertRaisesRegex(NeedsManual, "desktop user"):
            module.apply()


class Dispatcher(Base):
    def invoke(self, machine, argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        code = main(self.module(machine, **kw), argv, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        machine = self.tailscale_box()
        code, out, _ = self.invoke(machine, ["check"])
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("missing", out)
        self.assertEqual(self.invoke(machine, ["install"])[0], EXIT_OK)
        code, out, _ = self.invoke(machine, ["check"])
        self.assertEqual((code, out), (EXIT_OK, ""))
        code, out, _ = self.invoke(machine, ["status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("ok      package openssh-server", out)

    def test_usage_and_failure_codes(self):
        machine = self.tailscale_box()
        self.assertEqual(self.invoke(machine, ["bogus"])[0], EXIT_USAGE)
        self.assertEqual(self.invoke(machine, ["install"], is_root=False)[0], EXIT_FAILED)

    def test_needs_manual_exit(self):
        self.write("etc/ssh/sshd_config", STOCK_CONFIG)
        machine = FakeMachine(self.root, enabled=True, active=True)
        out, err = io.StringIO(), io.StringIO()
        module = self.module(machine, {"password_auth": False})
        self.assertEqual(main(module, ["install"], out=out, err=err), EXIT_NEEDS_MANUAL)
        self.assertTrue(out.getvalue().startswith("needs manual:"))


if __name__ == "__main__":
    unittest.main()

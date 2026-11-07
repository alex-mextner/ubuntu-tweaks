"""scripts/root_container.py builds the docker command that exercises a module installer on a disposable root."""

import importlib.util
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "root_container.py"
spec = importlib.util.spec_from_file_location("root_container", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


class CommandBuilderTest(unittest.TestCase):
    def command(self, **kw):
        script = rc.inner_script("m", installer="install.sh", user="tester", apt=[], stubs=[], show=[], **kw)
        return rc.docker_command(Path("/repo-host"), "ubuntu:26.04", script, network=True), script

    def test_container_is_disposable_unprivileged_and_mounts_only_the_repo_read_only(self):
        argv, _ = self.command()
        self.assertEqual(argv[:3], ["docker", "run", "--rm"])
        self.assertNotIn("--privileged", argv)
        self.assertEqual([a for a in argv if a == "-v"], ["-v"])
        self.assertEqual(argv[argv.index("-v") + 1], "/repo-host:/repo:ro")

    def test_no_network_flag(self):
        script = rc.inner_script("m", installer="install.sh", user="u", apt=[], stubs=[], show=[])
        argv = rc.docker_command(Path("/r"), "img", script, network=False)
        self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_script_installs_twice_then_checks_as_the_unprivileged_user(self):
        script = rc.inner_script(
            "memguard",
            installer="install.sh",
            user="tester",
            apt=["gcc", "python3"],
            stubs=["systemctl"],
            show=["/etc/x"],
        )
        self.assertEqual(script.count("./install.sh;"), 2)
        self.assertIn("runuser -u tester -- ./install.sh --check", script)
        self.assertIn("apt-get install -y -qq python3 gcc", script)
        self.assertIn("/stub/systemctl", script)
        self.assertIn("sha256sum -- /etc/x", script)
        self.assertIn("cd /repo/modules/memguard", script)

    def test_values_are_shell_quoted(self):
        script = rc.inner_script("m", installer="i.sh", user="t", apt=["a; rm -rf /"], stubs=[], show=["/e tc"])
        self.assertIn(shlex.quote("a; rm -rf /"), script)
        self.assertIn("'/e tc'", script)

    def test_echo_labels_and_stub_bodies_survive_hostile_names(self):
        script = rc.inner_script("m", installer="i.sh", user="t", apt=[], stubs=[], show=['/a"b$(id)'])
        self.assertIn("echo '== /a\"b$(id)'", script)
        with self.assertRaises(ValueError):
            rc.stub_lines(["/usr/bin/foo"])
        with self.assertRaises(ValueError):
            rc.stub_lines(["it's"])

    def test_a_failing_second_install_fails_the_run(self):
        script = rc.inner_script("m", installer="i.sh", user="t", apt=[], stubs=[], show=[])
        self.assertIn('[ "$second" = 0 ] || exit "$second"', script)

    def test_apt_failure_is_fatal_and_no_network_refuses_packages(self):
        script = rc.inner_script("m", installer="i.sh", user="t", apt=[], stubs=[], show=[])
        self.assertIn("|| { echo apt failed; exit 1; }", script)
        offline = rc.inner_script("m", installer="i.sh", user="t", apt=[], stubs=[], show=[], network=False)
        self.assertNotIn("apt-get", offline)
        with self.assertRaises(ValueError):
            rc.inner_script("m", installer="i.sh", user="t", apt=["gcc"], stubs=[], show=[], network=False)

    def test_unknown_module_is_refused_without_running_docker(self):
        self.assertEqual(rc.main(["no-such-module"]), 2)

    def test_refuses_root_user_and_module_paths(self):
        with self.assertRaises(ValueError):
            rc.inner_script("m", installer="i.sh", user="root", apt=[], stubs=[], show=[])
        for module in ("/opt/x", "..", "a/b"):
            with self.assertRaises(ValueError):
                rc.inner_script(module, installer="i.sh", user="t", apt=[], stubs=[], show=[])
        script = rc.inner_script("m", installer="i.sh", user="t", apt=[], stubs=[], show=["-R"])
        self.assertIn("ls -l -- -R", script)

    def test_install_and_check_words_come_from_the_options(self):
        script = rc.inner_script(
            "m", installer="m", user="t", apt=[], stubs=[], show=[], install_args=["install"], check_args=["check"]
        )
        self.assertIn("./m install; first=$?", script)
        self.assertIn("runuser -u t -- ./m check;", script)

    def test_no_network_needs_an_image_with_python(self):
        self.assertEqual(rc.main(["memguard", "--no-network"]), 2)


class GeneratedScriptRunTest(unittest.TestCase):
    """The generated shell runs for real here (no container): the stub keeps state, the no-op check can fail."""

    def run_bash(self, script, cwd):
        return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False)

    def test_systemctl_stub_answers_from_what_was_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = rc.stub_lines(["systemctl"], stub_dir=f"{tmp}/stub")
            body = "\n".join(
                [f"export STUB_STATE={shlex.quote(tmp)}/state", *lines, "systemctl is-enabled a.timer; echo rc=$?",
                 "systemctl enable --now a.timer", "systemctl is-enabled a.timer; systemctl is-active a.timer"]
            )  # fmt: skip
            out = self.run_bash(body, tmp).stdout.split()
            self.assertEqual(out, ["disabled", "rc=1", "enabled", "active"])

    def test_second_run_that_changes_a_file_fails_with_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = rc.inner_script("m", installer="i.sh", user="t", apt=[], stubs=[], show=[], network=False)
            script = script.replace("useradd -m t || { echo useradd failed; exit 1; }", "true")
            script = script.replace("cd /repo/modules/m", f"cd {tmp}").replace("runuser -u t -- ", "")
            script = script.replace("find /etc /usr/local /opt /srv", f"find {tmp}/etc")
            (Path(tmp) / "etc").mkdir()
            installer = Path(tmp) / "i.sh"
            installer.write_text(f'#!/bin/sh\n[ "$1" = --check ] && exit 0\ndate +%N > {tmp}/etc/f\n')
            installer.chmod(0o755)
            result = self.run_bash(script, tmp)
            self.assertEqual(result.returncode, 4, result.stdout)
            self.assertIn("second install changed:", result.stdout)
            installer.write_text("#!/bin/sh\nexit 0\n")
            self.assertEqual(self.run_bash(script, tmp).returncode, 0)


if __name__ == "__main__":
    unittest.main()

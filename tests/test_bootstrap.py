"""``scripts/bootstrap.sh`` on a stock machine, with fake ``sudo``, ``git``, ``python3`` and ``id`` on ``PATH``."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks.context import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "bootstrap.sh"
REPO = "https://example.invalid/ubuntu-tweaks"


class BootstrapTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.log = self.tmp / "calls.log"
        self.checkout = self.tmp / "home/xp/ubuntu-tweaks"
        self.tool("id", "echo 1000")
        self.tool("sudo", 'echo "sudo $*" >> "$LOG"')
        # `git clone` creates the checkout with a launcher that logs its argv, like bin/ubuntu-tweak would
        self.tool(
            "git",
            'echo "git $*" >> "$LOG"\n'
            'if [ "$1" = clone ]; then\n'
            '  mkdir -p "$4/bin"; printf \'#!/bin/sh\\necho "ubuntu-tweak $*" >> "$LOG"\\n\' > "$4/bin/ubuntu-tweak"\n'
            '  chmod +x "$4/bin/ubuntu-tweak"; fi',
        )
        self.tool("python3", "exit 1")  # no `gi`
        for real in ("dirname", "mkdir", "chmod"):
            (self.bin / real).symlink_to(shutil.which(real) or f"/usr/bin/{real}")

    def tool(self, name: str, body: str) -> None:
        path = write(self.bin / name, f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def run_script(self, *args: str) -> list[str]:
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.tmp / "home"),
            "UBUNTU_TWEAKS_DIR": str(self.checkout),
            "UBUNTU_TWEAKS_REPO": REPO,
            "LOG": str(self.log),
        }
        done = subprocess.run(["/bin/sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_a_stock_machine_gets_the_prerequisites_the_checkout_and_a_preview(self) -> None:
        self.assertEqual(
            self.run_script(),
            [
                "sudo apt-get update",
                "sudo apt-get install -y git curl ca-certificates python3-gi",
                f"git clone --recurse-submodules {REPO} {self.checkout}",
                "ubuntu-tweak init",
            ],
        )

    def test_yes_is_the_only_word_that_turns_the_preview_into_a_commit(self) -> None:
        self.assertEqual(self.run_script("--yes")[-1], "ubuntu-tweak init --commit --yes")

    def test_other_arguments_go_to_init_unchanged(self) -> None:
        self.assertEqual(self.run_script("--commit", "--no-sudo")[-1], "ubuntu-tweak init --commit --no-sudo")

    def test_an_existing_checkout_is_updated_not_cloned_and_present_tools_need_no_sudo(self) -> None:
        for name in ("git", "curl"):
            self.tool(name, f'echo "{name} $*" >> "$LOG"')
        self.tool("python3", "exit 0")  # `import gi` works
        write(self.checkout / ".git" / "HEAD", "")
        write(self.checkout / "bin/ubuntu-tweak", '#!/bin/sh\necho "ubuntu-tweak $*" >> "$LOG"\n').chmod(0o755)
        if not Path("/etc/ssl/certs/ca-certificates.crt").exists():
            self.skipTest("no CA bundle on this machine")
        self.assertEqual(
            self.run_script(),
            [
                f"git -C {self.checkout} pull --ff-only",
                f"git -C {self.checkout} submodule update --init --recursive",
                "ubuntu-tweak init",
            ],
        )

    def test_the_script_is_posix_sh_and_at_most_forty_lines(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertLessEqual(len(text.splitlines()), 40)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()

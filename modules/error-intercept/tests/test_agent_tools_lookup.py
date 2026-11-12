"""The end-to-end demo finds the agent-tools library only where it is told to, never by walking the real HOME.

Each case runs ``test_end_to_end_demo`` in a child interpreter whose HOME is a scratch directory. The "checkout" there is a
decoy: it has the two files the lookup keys on, and its ``agent_sub_fakes`` fails loudly when imported, so a lookup that
reached into HOME shows up as a failed child run and one that skipped cleanly as ``skipped``.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DECOY_LOADED = "decoy agent-tools was loaded"


def make_decoy(root: Path) -> Path:
    (root / "lib/agenttools_subscriptions").mkdir(parents=True)
    (root / "lib/agenttools_subscriptions/__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/agent_sub_fakes.py").write_text(f"raise RuntimeError({DECOY_LOADED!r})\n", encoding="utf-8")
    return root


class AgentToolsLookup(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="ei-lookup-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.home = self.scratch / "home"
        self.home.mkdir()

    def run_demo(self, **extra_env):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1", **extra_env}
        return subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "test_end_to_end_demo"],
            cwd=TESTS, env=env, capture_output=True, text=True, check=False,
        )  # fmt: skip

    def test_a_checkout_in_home_is_not_searched_for(self):
        for place in ("xp/agent-tools", "xp/agent-tools/.worktrees/agent-sub-library", "dev/agent-tools"):
            make_decoy(self.home / place)
        done = self.run_demo()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(DECOY_LOADED, done.stderr)
        self.assertIn("skipped", done.stderr)

    def test_an_empty_home_skips_cleanly(self):
        done = self.run_demo()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("skipped", done.stderr)

    def test_agent_tools_dir_is_still_honoured(self):
        decoy = make_decoy(self.scratch / "elsewhere/agent-tools")
        done = self.run_demo(AGENT_TOOLS_DIR=str(decoy))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(DECOY_LOADED, done.stderr)


if __name__ == "__main__":
    unittest.main()

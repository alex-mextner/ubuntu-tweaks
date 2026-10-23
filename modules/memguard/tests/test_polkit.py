"""The polkit rule allows exactly start/stop of swap-drain.service for the desktop user.
Evaluated with node or bun against a minimal polkit stand-in (skipped when neither exists);
the live check after install is in docs/specs/memguard.md § Swap hygiene."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE = os.path.join(MODULE, "50-memguard-swap-drain.rules")

HARNESS = """
const fs = require("fs");
const rules = [];
const polkit = {Result: {YES: "yes"}, addRule: (f) => rules.push(f)};
eval(fs.readFileSync(process.argv[2], "utf8").replace(/@USER@/g, "alice"));
const out = JSON.parse(process.argv[3]).map(([user, unit, verb, id]) => {
  const action = {id: id || "org.freedesktop.systemd1.manage-units",
                  lookup: (k) => ({unit, verb})[k]};
  const r = rules.map((f) => f(action, {user})).find((x) => x !== undefined);
  return r === undefined ? null : r;
});
console.log(JSON.stringify(out));
"""


class PolkitRule(unittest.TestCase):
    def test_only_start_stop_of_swap_drain_for_the_user(self):
        js = shutil.which("node") or shutil.which("bun")
        if not js:
            self.skipTest("no node/bun to evaluate the rule")
        cases = [
            ("alice", "swap-drain.service", "start", None),
            ("alice", "swap-drain.service", "stop", None),
            ("alice", "swap-drain.service", "restart", None),
            ("alice", "swap-drain.service", "start", "org.freedesktop.systemd1.manage-unit-files"),
            ("alice", "swap-drain.timer", "stop", None),
            ("alice", "ssh.service", "stop", None),
            ("guest", "swap-drain.service", "start", None),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".js") as harness:
            harness.write(HARNESS)
            harness.flush()
            r = subprocess.run([js, harness.name, RULE, json.dumps(cases)], capture_output=True, text=True,
                               timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), ["yes", "yes", None, None, None, None, None])


if __name__ == "__main__":
    unittest.main()

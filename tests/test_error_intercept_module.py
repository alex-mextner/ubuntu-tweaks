"""The shipped ``modules/error-intercept`` through the real system-module planner, staged under a temp HOME.

The module is off by default; a profile turns it on. ``ERROR_INTERCEPT_ROOT`` stages the installer: the temp
directory is its home, it runs no ``systemctl`` and points agent-sub at the same directory, so nothing real is read or
written. A tiny fake ``agent-sub`` (a script that keeps the topic in a JSON file) stands in for agent-tools.
"""

import os
import stat
import textwrap
import unittest
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks.actions import Plan
from ubuntu_tweaks.context import REPO_ROOT, Context
from ubuntu_tweaks.manifest import SourceKind, load_manifest
from ubuntu_tweaks.planners import system_modules
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend

FAKE_AGENT_SUB = textwrap.dedent(
    """\
    #!/usr/bin/python3
    import json, os, sys
    path = os.path.join(os.environ["AGENT_SUB_CONFIG_DIR"], "topics.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    words = sys.argv[1:]
    store = json.load(open(path)) if os.path.exists(path) else {}
    if words[:3] == ["topic", "show", "errors"]:
        print(json.dumps(store.get("errors", {"role": "", "instructions": "", "spawn_min_severity": "warning", "escalate": []})))
    elif words[:3] == ["topic", "set", "errors"]:
        def value(flag):
            return words[words.index(flag) + 1]
        store["errors"] = {
            "role": open(value("--role-file")).read(),
            "instructions": open(value("--instructions-file")).read(),
            "spawn_min_severity": value("--spawn-min-severity"),
            "escalate": words[words.index("--escalate") + 1:],
        }
        json.dump(store, open(path, "w"))
    else:
        sys.exit(2)
    """
)


class ErrorInterceptModuleTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile(self.tmp / "profile", "home")
        self.profile.root.mkdir()
        self.staged = self.tmp / "home"
        self.staged.mkdir()
        launcher = write(self.tmp / "agent-tools/lib/agenttools_subscriptions/agent-sub", FAKE_AGENT_SUB)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        self.tools = self.tmp / "agent-tools"
        environment = mock.patch.dict(os.environ, {"ERROR_INTERCEPT_ROOT": str(self.staged)})
        environment.start()
        self.addCleanup(environment.stop)

    def actions(self) -> list:
        ctx = Context(REPO_ROOT, self.profile, frozenset({"error-intercept"}))
        return [action for action in system_modules.build(ctx, FakeBackend()) if action.scope == "error-intercept"]

    def enable(self) -> None:
        write(
            self.profile.machine_toml,
            f'[modules.error-intercept]\nenabled = true\nagent_tools_dir = "{self.tools}"\n',
        )

    def test_manifest_is_a_user_level_system_module_that_is_off_by_default(self) -> None:
        manifest = load_manifest(REPO_ROOT / "modules/error-intercept")
        self.assertEqual(manifest.name, "error-intercept")
        self.assertIs(manifest.source.kind, SourceKind.SYSTEM)
        self.assertFalse(manifest.source.root)
        self.assertFalse(manifest.enabled)
        self.assertEqual(manifest.source.install, ("installer", "install"))
        self.assertEqual(manifest.source.check, ("installer", "check"))
        self.assertEqual(manifest.source.preview, ("installer", "plan"))

    def test_the_planner_skips_it_until_a_profile_enables_it(self) -> None:
        self.assertEqual(self.actions(), [])
        self.enable()
        (action,) = self.actions()
        self.assertEqual((action.scope, action.needs_root), ("error-intercept", False))

    def test_check_reports_drift_on_a_clean_root_and_none_after_install(self) -> None:
        self.enable()
        (action,) = self.actions()
        self.assertFalse(action.check())  # nothing installed under the staged home: drift
        result = Plan.build([action]).execute(no_sudo=True)
        self.assertEqual(result.changed, 1)
        self.assertTrue(action.check())
        unit = self.staged / ".config/systemd/user/update-notifier-crash.service.d/10-error-intercept.conf"
        self.assertIn(f"{REPO_ROOT}/modules/error-intercept/error-intercept ingest", unit.read_text(encoding="utf-8"))
        self.assertTrue((self.staged / ".local/bin/error-intercept").is_symlink())

    def test_a_second_run_changes_nothing(self) -> None:
        self.enable()
        Plan.build(self.actions()).execute(no_sudo=True)
        (action,) = self.actions()
        self.assertTrue(action.check())
        self.assertEqual(Plan.build([action]).execute(no_sudo=True).changed, 0)


if __name__ == "__main__":
    unittest.main()

"""A conflicting extension (the Ubuntu dock) is only disabled once the shell has loaded its replacement."""

import os
from collections.abc import Sequence
from unittest import mock

try:
    from tests.actions_scenario import Scenario
    from ubuntu_tweaks.actions import Plan
    from ubuntu_tweaks.planners import toggle
    from ubuntu_tweaks.process import CommandResult, FakeProcess
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    import unittest

    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error

UUID = "hyper@example"
CONFLICT = "ubuntu-dock@ubuntu.com"


class Shell(FakeProcess):
    """``gnome-extensions list`` answers with the extensions the running shell has loaded."""

    def __init__(self, known: Sequence[str], *, working: bool = True) -> None:
        super().__init__()
        self.known = list(known)
        self.working = working

    def query(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.queries.append(tuple(argv))
        if not self.working:
            return CommandResult(1, "", "no session bus")
        return CommandResult(0, "\n".join(self.known) + "\n", "")


class DockGateTests(Scenario):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(toggle.ASSUME_LOADED_ENV, None)
        self.add_extension("gnome-extensions/hyper", UUID)
        self.add_module("hyper", path="gnome-extensions/hyper", extension=f'[extension]\nconflicts = ["{CONFLICT}"]\n')
        self.backend_ = self.backend()

    def plan(self, process: FakeProcess) -> Plan:
        return Plan.build(toggle.build(self.context(), self.backend_, process=process))

    def disable(self, plan: Plan):  # type: ignore[no-untyped-def]
        (action,) = [a for a in plan.actions if a.kind == "disable"]
        return action

    def test_the_dock_is_not_disabled_while_the_shell_has_not_loaded_the_replacement(self) -> None:
        plan = self.plan(Shell(known=["ding@rastersoft.com", CONFLICT]))
        action = self.disable(plan)
        self.assertIn("waits until the shell has loaded hyper", action.describe())
        result = plan.execute()
        self.assertEqual(len(result.manual), 2 - 1)  # the disable only; the enable ran
        self.assertIn("stays enabled until the shell has loaded hyper", result.manual[0][1])
        self.assertNotIn(CONFLICT, toggle.user_list(self.backend_, toggle.DISABLED))

    def test_the_dock_is_disabled_once_the_shell_knows_the_replacement(self) -> None:
        plan = self.plan(Shell(known=[UUID, CONFLICT]))
        self.assertNotIn("waits until", self.disable(plan).describe())
        result = plan.execute()
        self.assertEqual((result.manual, result.failed), ((), ()))
        self.assertIn(CONFLICT, toggle.user_list(self.backend_, toggle.DISABLED))
        self.assertIn(UUID, toggle.user_list(self.backend_, toggle.ENABLED))

    def test_when_the_shell_cannot_be_asked_the_old_behaviour_stays(self) -> None:
        plan = self.plan(Shell(known=[], working=False))
        self.assertEqual(plan.execute().manual, ())
        self.assertIn(CONFLICT, toggle.user_list(self.backend_, toggle.DISABLED))

    def test_the_environment_can_assume_the_shell_has_loaded_it(self) -> None:
        os.environ[toggle.ASSUME_LOADED_ENV] = "1"
        shell = Shell(known=[])
        self.plan(shell).execute()
        self.assertEqual(shell.queries, [])
        self.assertIn(CONFLICT, toggle.user_list(self.backend_, toggle.DISABLED))

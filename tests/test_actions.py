"""Plan building, preview rendering, execution and root skipping."""

import unittest

from ubuntu_tweaks.actions import Action, ActionFailed, Phase, Plan


class FakeAction(Action):
    """Satisfied from the start unless ``satisfied=False``; ``run`` flips it unless ``broken``."""

    def __init__(
        self,
        name: str,
        phase: Phase,
        *,
        satisfied: bool = False,
        needs_root: bool = False,
        scope: str | None = None,
        broken: bool = False,
        log: list[str] | None = None,
    ) -> None:
        self.name = name
        self.phase = phase
        self.kind = name.split(":")[0]
        self.needs_root = needs_root
        self.scope = scope
        self.satisfied = satisfied
        self.broken = broken
        self.log = log if log is not None else []

    def describe(self) -> str:
        return f"do {self.name}"

    def check(self) -> bool:
        return self.satisfied

    def run(self) -> None:
        self.log.append(self.name)
        self.satisfied = not self.broken


class PlanBuildTests(unittest.TestCase):
    def test_plan_holds_only_actions_whose_check_fails(self) -> None:
        plan = Plan.build(
            [
                FakeAction("done", Phase.LINK, satisfied=True),
                FakeAction("todo", Phase.LINK),
            ]
        )
        self.assertEqual([a.describe() for a in plan.actions], ["do todo"])
        self.assertEqual(plan.satisfied, 1)
        self.assertEqual(len(plan), 1)

    def test_order_follows_phases_and_is_stable_inside_a_phase(self) -> None:
        names = ["seed", "files-b", "system", "packages", "files-a", "settings-machine", "settings-module", "submodule"]
        phases = {
            "seed": Phase.SEED,
            "files-b": Phase.FILES,
            "files-a": Phase.FILES,
            "system": Phase.SYSTEM,
            "packages": Phase.PACKAGES,
            "settings-machine": Phase.SETTINGS_MACHINE,
            "settings-module": Phase.SETTINGS_MODULE,
            "submodule": Phase.SUBMODULE,
        }
        plan = Plan.build([FakeAction(name, phases[name]) for name in names])
        self.assertEqual(
            [a.describe() for a in plan.actions],
            [
                "do submodule",
                "do packages",
                "do system",
                "do files-b",
                "do files-a",
                "do settings-module",
                "do settings-machine",
                "do seed",
            ],
        )

    def test_phase_order_matches_the_spec(self) -> None:
        self.assertEqual(
            list(Phase),
            [
                Phase.SUBMODULE,
                Phase.MIGRATE,
                Phase.SCHEMAS,
                Phase.PACKAGES,
                Phase.SYSTEM,
                Phase.LINK,
                Phase.FILES,
                Phase.TOGGLE,
                Phase.SETTINGS_MODULE,
                Phase.SETTINGS_PROFILE,
                Phase.SETTINGS_MACHINE,
                Phase.SEED,
            ],
        )
        self.assertEqual(sorted(Phase), list(Phase))

    def test_only_filters_by_scope_before_checking(self) -> None:
        checked: list[str] = []

        class Spy(FakeAction):
            def check(self) -> bool:
                checked.append(self.name)
                return super().check()

        plan = Plan.build(
            [
                Spy("a", Phase.LINK, scope="hyperdock"),
                Spy("b", Phase.LINK, scope="memguard"),
                Spy("c", Phase.LINK, scope=None),
            ],
            only={"hyperdock"},
        )
        self.assertEqual([a.describe() for a in plan.actions], ["do a"])
        self.assertEqual(checked, ["a"])

    def test_empty_or_missing_only_selects_everything(self) -> None:
        candidates = [FakeAction("a", Phase.LINK, scope="hyperdock"), FakeAction("b", Phase.LINK, scope=None)]
        for only in (None, frozenset()):
            with self.subTest(only=only):
                self.assertEqual(len(Plan.build(candidates, only=only)), 2)


class RenderTests(unittest.TestCase):
    def test_preview_lists_numbered_actions_with_kind_and_sudo_marker(self) -> None:
        plan = Plan.build(
            [
                FakeAction("set-key:1", Phase.SETTINGS_PROFILE),
                FakeAction("system:tz", Phase.SYSTEM, needs_root=True),
                FakeAction("ok", Phase.LINK, satisfied=True),
            ]
        )
        self.assertEqual(
            plan.render(),
            "\n".join(
                [
                    "Plan: 2 action(s) to run, 1 already satisfied, 1 need root",
                    "  1. [sudo] [system] do system:tz",
                    "  2. [set-key] do set-key:1",
                ]
            ),
        )

    def test_preview_says_when_root_actions_will_be_skipped(self) -> None:
        plan = Plan.build([FakeAction("system:tz", Phase.SYSTEM, needs_root=True)])
        self.assertIn("[sudo, skipped: --no-sudo] [system] do system:tz", plan.render(no_sudo=True))

    def test_empty_plan(self) -> None:
        plan = Plan.build([FakeAction("a", Phase.LINK, satisfied=True)])
        self.assertEqual(plan.render(), "Nothing to do (1 already satisfied).")
        self.assertFalse(plan)


class ExecuteTests(unittest.TestCase):
    def test_execute_runs_in_order_and_counts_changed(self) -> None:
        log: list[str] = []
        plan = Plan.build(
            [
                FakeAction("late", Phase.SEED, log=log),
                FakeAction("early", Phase.SUBMODULE, log=log),
            ]
        )
        started: list[str] = []
        result = plan.execute(on_start=lambda action: started.append(action.describe()))

        self.assertEqual(log, ["early", "late"])
        self.assertEqual(started, ["do early", "do late"])
        self.assertEqual(result.changed, 2)
        self.assertEqual(result.skipped, ())

    def test_second_plan_after_commit_is_empty(self) -> None:
        actions = [FakeAction("a", Phase.LINK), FakeAction("b", Phase.FILES)]
        Plan.build(actions).execute()
        again = Plan.build(actions)
        self.assertEqual((len(again), again.satisfied), (0, 2))

    def test_no_sudo_skips_and_reports_root_actions(self) -> None:
        log: list[str] = []
        root = FakeAction("system:tz", Phase.SYSTEM, needs_root=True, log=log)
        user = FakeAction("link", Phase.LINK, log=log)
        result = Plan.build([root, user]).execute(no_sudo=True)

        self.assertEqual(log, ["link"])
        self.assertEqual(result.changed, 1)
        self.assertEqual(result.skipped, (root,))
        self.assertEqual(
            result.render_skipped(),
            "Skipped 1 root action(s) because of --no-sudo:\n  [system] do system:tz",
        )
        self.assertFalse(root.check())

    def test_nothing_skipped_renders_empty(self) -> None:
        self.assertEqual(Plan.build([]).execute().render_skipped(), "")

    def test_action_satisfied_since_build_is_skipped_not_run(self) -> None:
        log: list[str] = []
        first = FakeAction("first", Phase.LINK, log=log)
        second = FakeAction("second", Phase.FILES, log=log)
        plan = Plan.build([first, second])
        second.satisfied = True  # something else did its work between preview and commit

        result = plan.execute()

        self.assertEqual((log, result.changed), (["first"], 1))

    def test_root_action_satisfied_since_build_is_not_reported_as_skipped(self) -> None:
        root = FakeAction("system:tz", Phase.SYSTEM, needs_root=True)
        plan = Plan.build([root])
        root.satisfied = True
        self.assertEqual(plan.execute(no_sudo=True).skipped, ())

    def test_action_that_does_not_converge_fails_loudly(self) -> None:
        plan = Plan.build([FakeAction("liar", Phase.LINK, broken=True)])
        with self.assertRaisesRegex(ActionFailed, r"\[liar\] do liar: still not satisfied"):
            plan.execute()

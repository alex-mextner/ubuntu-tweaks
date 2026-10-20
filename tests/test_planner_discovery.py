"""Planner discovery: sorted, `_`-prefixed skipped, `build` required, optional hooks aggregated."""

import sys
import unittest

from tests.util import write

try:
    from tests.actions_scenario import Scenario
    from ubuntu_tweaks import planners
    from ubuntu_tweaks.context import Context
    from ubuntu_tweaks.errors import ConfigError
    from ubuntu_tweaks.settings import FakeBackend
except ImportError as error:  # pragma: no cover - PyGObject is a distro package
    raise unittest.SkipTest(f"PyGObject unavailable: {error}") from error


class DiscoveryTests(Scenario):
    def drop(self, **files: str) -> None:
        """Make extra planner modules importable as ``ubuntu_tweaks.planners.<name>``."""
        for name, text in files.items():
            write(self.tmp / f"{name}.py", text)
        planners.__path__.append(str(self.tmp))
        self.addCleanup(planners.__path__.remove, str(self.tmp))
        for name in files:
            self.addCleanup(sys.modules.pop, f"{planners.__name__}.{name}", None)

    def names(self) -> list[str]:
        return [module.__name__.rsplit(".", 1)[-1] for module in planners.discover_planners()]

    def args(self) -> tuple[Context, FakeBackend]:
        return self.context(), self.backend()

    def test_shipped_planners_are_found_in_name_order(self) -> None:
        found = self.names()
        self.assertEqual(found, sorted(found))
        self.assertTrue({"extension_links", "schemas", "submodule", "toggle"} <= set(found))

    def test_underscore_modules_are_skipped(self) -> None:
        self.drop(_helper="raise RuntimeError('must not be imported')")
        self.assertNotIn("_helper", self.names())

    def test_module_without_build_is_invalid_config(self) -> None:
        self.drop(broken="X = 1\n")
        with self.assertRaisesRegex(ConfigError, "planner module .*broken has no build"):
            planners.discover_planners()

    def test_actions_extras_and_warnings_are_aggregated_and_hooks_are_optional(self) -> None:
        self.drop(
            aaa_full=(
                "def build(ctx, backend):\n    return []\n"
                "def extras(ctx, backend):\n    return ['extra from aaa']\n"
                "def warnings(ctx, backend):\n    return ['warning from aaa']\n"
            ),
            aaa_minimal="def build(ctx, backend):\n    return []\n",
        )
        ctx, backend = self.args()
        self.assertEqual(planners.collect_extras(ctx, backend), ["extra from aaa"])
        self.assertEqual(planners.collect_warnings(ctx, backend), ["warning from aaa"])
        self.assertEqual(planners.build_actions(ctx, backend), [])


if __name__ == "__main__":
    unittest.main()

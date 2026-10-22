"""The spec's init order table and its Manual steps table agree with the ``module.toml`` files."""

import re
import unittest
from pathlib import Path

from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.manifest import discover_modules

SPEC = (REPO_ROOT / "docs" / "specs" / "ubuntu-tweak-cli.md").read_text(encoding="utf-8")
ROW = re.compile(r"^\| `([a-z0-9-]+)` \| (\d+) \|", re.MULTILINE)


def section(title: str) -> str:
    """The text of the ``###`` section called ``title``, up to the next heading of that depth."""
    body = SPEC.split(f"\n### {title}\n", 1)[1]
    return re.split(r"\n#{1,3} ", body, maxsplit=1)[0]


class InitOrderTableTests(unittest.TestCase):
    def test_every_row_of_the_order_table_is_the_orders_in_module_toml_and_no_pipeline_module_is_missing(self) -> None:
        listed = {name: int(order) for name, order in ROW.findall(section("Init pipeline"))}
        manifests = {manifest.name: manifest for manifest in discover_modules(REPO_ROOT / "modules")}
        self.assertEqual(
            listed,
            {name: manifests[name].order for name in listed},
            "docs/specs/ubuntu-tweak-cli.md, Init pipeline order table, disagrees with modules/*/module.toml",
        )
        pipeline = {name for name, manifest in manifests.items() if manifest.order != 100 or name == "dotfiles"}
        self.assertEqual(
            set(listed), pipeline, "a module has an init order but is not in the spec table (or vice versa)"
        )


class ManualStepsTableTests(unittest.TestCase):
    def test_every_module_that_can_end_with_needs_manual_is_in_the_manual_steps_table(self) -> None:
        table = section("Manual steps")
        for manifest in discover_modules(REPO_ROOT / "modules"):
            if manifest.manual:
                self.assertIn(f"`{manifest.name}`", table, f"{manifest.name} has a manual hint but no table row")

    def test_every_manual_hint_is_one_line_a_person_can_follow(self) -> None:
        for manifest in discover_modules(REPO_ROOT / "modules"):
            self.assertNotIn("\n", manifest.manual)
            self.assertLessEqual(len(manifest.manual), 260, manifest.name)

    def test_the_system_modules_that_exit_10_have_a_hint(self) -> None:
        scripts = {
            manifest.name: Path(manifest.directory / manifest.source.install[0]).read_text(encoding="utf-8")
            for manifest in discover_modules(REPO_ROOT / "modules")
            if manifest.source.install
        }
        for name, text in scripts.items():
            if "NeedsManual" in text or "exit 10" in text or "needs manual" in text:
                manifest = next(m for m in discover_modules(REPO_ROOT / "modules") if m.name == name)
                self.assertTrue(manifest.manual, f"{name}'s installer can exit 10 but its module.toml has no `manual`")


if __name__ == "__main__":
    unittest.main()

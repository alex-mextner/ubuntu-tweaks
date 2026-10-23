"""The coverage model: rule parsing, classify precedence, summary, and the four-layer rule loader."""

import unittest

from tests.util import TempDirCase, write
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.coverage import Coverage, Finding, Item, Layer, Rule, Rules, classify, parse_rules, summary
from ubuntu_tweaks.coverage_rules import load_rules
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.profile import Profile, TomlTable, TomlValue

CACHE = Item("files", "file:~/.config/Code/Cache", "dir")


def rule(match: str, coverage: Coverage, layer: Layer, reason: str = "because", owner: str = "") -> Rule:
    return Rule((match,), coverage, reason, layer, owner)


class ParseRulesTests(unittest.TestCase):
    def parse(self, *entries: TomlTable) -> list[Rule]:
        return parse_rules(list(entries), Layer.PROFILE, "coverage.toml")

    def test_an_excluded_or_secret_rule_needs_a_reason(self) -> None:
        for kind in ("excluded", "secret"):
            with self.assertRaisesRegex(ConfigError, f"an {kind} rule needs a 'reason'"):
                self.parse({"match": ["file:~/x"], "class": kind})

    def test_a_tracked_rule_needs_no_reason(self) -> None:
        (parsed,) = self.parse({"match": ["file:~/x"], "class": "tracked"})
        self.assertEqual((parsed.coverage, parsed.reason, parsed.layer), (Coverage.TRACKED, "", Layer.PROFILE))

    def test_the_error_names_the_file_and_the_entry(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"coverage\.toml \[\[coverage\]\] #2: 'class' must be"):
            self.parse({"match": ["a"], "class": "tracked"}, {"match": ["b"], "class": "bogus"})

    def test_untracked_is_not_a_class_a_rule_can_set(self) -> None:
        with self.assertRaisesRegex(ConfigError, "'class' must be tracked, excluded or secret"):
            self.parse({"match": ["a"], "class": "untracked", "reason": "r"})

    def test_match_must_be_a_non_empty_list_of_globs(self) -> None:
        cases: tuple[TomlValue, ...] = ("file:~/x", [], [""], [1])
        for bad in cases:
            with self.assertRaisesRegex(ConfigError, "'match' must be a non-empty list"):
                self.parse({"match": bad, "class": "tracked"})

    def test_unknown_keys_are_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self.parse({"match": ["a"], "class": "tracked", "colour": "red"})


class ClassifyTests(unittest.TestCase):
    def test_an_item_nobody_mentions_is_untracked(self) -> None:
        self.assertEqual(classify(CACHE, Rules(), {}), Finding(CACHE, Coverage.UNTRACKED))

    def test_what_the_profile_declares_is_tracked_with_its_owner(self) -> None:
        finding = classify(CACHE, Rules(), {CACHE.id: "files/.config/Code/settings.json"})
        self.assertEqual((finding.coverage, finding.owner), (Coverage.TRACKED, "files/.config/Code/settings.json"))

    def test_an_engine_rule_classifies_an_undeclared_item(self) -> None:
        rules = Rules((rule("file:~/.config/*/Cache", Coverage.EXCLUDED, Layer.ENGINE, "cache"),))
        finding = classify(CACHE, rules, {})
        self.assertEqual((finding.coverage, finding.reason), (Coverage.EXCLUDED, "cache"))

    def test_a_declaration_beats_an_engine_rule(self) -> None:
        rules = Rules((rule("file:~/.config/Code/*", Coverage.SECRET, Layer.ENGINE, "creds"),))
        finding = classify(CACHE, rules, {CACHE.id: "files/x"})
        self.assertEqual(finding.coverage, Coverage.TRACKED)

    def test_module_profile_and_machine_rules_beat_the_engine_and_a_declaration(self) -> None:
        for layer in (Layer.MODULE, Layer.PROFILE, Layer.MACHINE):
            rules = Rules(
                (
                    rule("file:~/.config/Code/Cache", Coverage.SECRET, Layer.ENGINE, "engine"),
                    rule("file:~/.config/Code/Cache", Coverage.EXCLUDED, layer, "mine"),
                )
            )
            finding = classify(CACHE, rules, {CACHE.id: "files/x"})
            self.assertEqual((finding.coverage, finding.reason), (Coverage.EXCLUDED, "mine"), layer.name)

    def test_the_highest_layer_wins_whatever_the_order_of_the_rules(self) -> None:
        layers = (Layer.MACHINE, Layer.ENGINE, Layer.PROFILE, Layer.MODULE)
        classes = (Coverage.SECRET, Coverage.TRACKED, Coverage.EXCLUDED, Coverage.TRACKED)
        rules = Rules(
            tuple(rule(CACHE.id, kind, layer, layer.name) for kind, layer in zip(classes, layers, strict=True))
        )
        finding = classify(CACHE, rules, {})
        self.assertEqual((finding.coverage, finding.reason), (Coverage.SECRET, "MACHINE"))

    def test_inside_one_layer_the_first_matching_rule_wins(self) -> None:
        rules = Rules(
            (
                rule("file:~/.config/Code/*", Coverage.EXCLUDED, Layer.PROFILE, "first"),
                rule("file:~/.config/Code/Cache", Coverage.SECRET, Layer.PROFILE, "second"),
            )
        )
        self.assertEqual(classify(CACHE, rules, {}).reason, "first")

    def test_a_tracked_rule_carries_its_owner(self) -> None:
        rules = Rules((rule("file:~/.config/Code/*", Coverage.TRACKED, Layer.MODULE, "", owner="vscode"),))
        finding = classify(CACHE, rules, {})
        self.assertEqual((finding.coverage, finding.owner), (Coverage.TRACKED, "vscode"))

    def test_globs_are_case_sensitive_and_match_the_whole_id(self) -> None:
        rules = Rules((rule("file:~/.config/code/*", Coverage.EXCLUDED, Layer.PROFILE),))
        self.assertEqual(classify(CACHE, rules, {}).coverage, Coverage.UNTRACKED)
        rules = Rules((rule("file:~/.config/Code", Coverage.EXCLUDED, Layer.PROFILE),))
        self.assertEqual(classify(CACHE, rules, {}).coverage, Coverage.UNTRACKED)

    def test_summary_counts_every_class_even_when_empty(self) -> None:
        findings = [
            Finding(CACHE, Coverage.EXCLUDED),
            Finding(CACHE, Coverage.EXCLUDED),
            Finding(CACHE, Coverage.SECRET),
        ]
        self.assertEqual(
            summary(findings),
            {Coverage.TRACKED: 0, Coverage.EXCLUDED: 2, Coverage.SECRET: 1, Coverage.UNTRACKED: 0},
        )


class EngineDefaultsTests(TempDirCase):
    def test_every_shipped_rule_is_valid_and_has_a_reason(self) -> None:
        ctx = Context(self.tmp / "repo", Profile(self.tmp / "profile", "box"))
        shipped = [r for r in load_rules(ctx).rules if r.layer is Layer.ENGINE]
        self.assertGreater(len(shipped), 20)
        for item in shipped:
            self.assertTrue(item.reason, item.match)
            self.assertIn(item.coverage, (Coverage.EXCLUDED, Coverage.SECRET), item.match)

    def test_credentials_are_secret_and_caches_are_excluded(self) -> None:
        ctx = Context(self.tmp / "repo", Profile(self.tmp / "profile", "box"))
        rules = load_rules(ctx)

        def kind(identifier: str) -> Coverage:
            return classify(Item("x", identifier, "file"), rules, {}).coverage

        self.assertEqual(kind("file:~/.ssh"), Coverage.SECRET)
        self.assertEqual(kind("file:~/.config/gh"), Coverage.SECRET)
        self.assertEqual(kind("file:~/.config/google-chrome"), Coverage.SECRET)
        self.assertEqual(kind("dconf:/org/x/api-key"), Coverage.SECRET)
        self.assertEqual(kind("file:~/.cache"), Coverage.EXCLUDED)
        self.assertEqual(kind("file:~/.config/dconf"), Coverage.EXCLUDED)
        self.assertEqual(kind("file:~/.config/Code"), Coverage.UNTRACKED)

    def test_a_declared_secret_directory_is_tracked(self) -> None:
        """The profile links ~/.config/gh/config.yml: the engine's secret rule yields to the declaration."""
        ctx = Context(self.tmp / "repo", Profile(self.tmp / "profile", "box"))
        item = Item("files", "file:~/.config/gh", "dir")
        self.assertEqual(
            classify(item, load_rules(ctx), {item.id: "files/.config/gh/config.yml"}).coverage, Coverage.TRACKED
        )


class LayerLoadingTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.profile = Profile(self.tmp / "profile", "box")
        self.ctx = Context(self.repo, self.profile)

    def module(self, name: str, coverage: str, *, enabled: bool = True) -> None:
        write(
            self.repo / "modules" / name / "module.toml",
            f'description = "{name}"\nenabled = {str(enabled).lower()}\n[source]\nkind = "builtin"\n{coverage}',
        )

    def classify(self, identifier: str) -> Finding:
        return classify(Item("files", identifier, "dir"), load_rules(self.ctx), {})

    def test_a_module_rule_applies_and_names_the_module_as_owner(self) -> None:
        self.module("vscode", '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "tracked"\n')
        finding = self.classify("file:~/.config/Code")
        self.assertEqual((finding.coverage, finding.owner), (Coverage.TRACKED, "vscode"))

    def test_a_disabled_module_contributes_nothing(self) -> None:
        self.module("vscode", '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "tracked"\n', enabled=False)
        self.assertEqual(self.classify("file:~/.config/Code").coverage, Coverage.UNTRACKED)

    def test_profile_beats_module_and_machine_beats_profile(self) -> None:
        self.module("vscode", '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "tracked"\n')
        write(
            self.profile.root / "coverage.toml",
            '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "excluded"\nreason = "profile says so"\n',
        )
        finding = self.classify("file:~/.config/Code")
        self.assertEqual(
            (finding.coverage, finding.reason, finding.owner), (Coverage.EXCLUDED, "profile says so", "coverage.toml")
        )
        write(
            self.profile.overlay_dir / "coverage.toml",
            '[[coverage]]\nmatch = ["file:~/.config/Code"]\nclass = "secret"\nreason = "this laptop signs in"\n',
        )
        finding = self.classify("file:~/.config/Code")
        self.assertEqual((finding.coverage, finding.reason), (Coverage.SECRET, "this laptop signs in"))
        self.assertEqual(finding.owner, "machines/box/coverage.toml")

    def test_a_malformed_profile_file_is_a_config_error_naming_it(self) -> None:
        path = write(self.profile.root / "coverage.toml", '[[coverage]]\nmatch = ["a"]\nclass = "secret"\n')
        with self.assertRaisesRegex(ConfigError, f"{path}.*needs a 'reason'"):
            load_rules(self.ctx)

    def test_anything_but_coverage_tables_in_the_file_is_refused(self) -> None:
        write(self.profile.root / "coverage.toml", "[capture]\nx = 1\n")
        with self.assertRaisesRegex(ConfigError, "unknown top-level key"):
            load_rules(self.ctx)

    def test_missing_files_are_empty_layers(self) -> None:
        self.assertEqual(self.classify("file:~/.config/Code").coverage, Coverage.UNTRACKED)


if __name__ == "__main__":
    unittest.main()

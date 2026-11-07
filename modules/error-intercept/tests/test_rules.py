"""The nested-test-shell switch: default routes the crash, ``exempt`` drops it to the audit log, bad files mean off."""

import json

from ei_fixtures import Case, FakePublish

from error_intercept.apport_source import Ingester
from error_intercept.rules import RULES_FILE, SHIPPED, load_rules


class RulesCase(Case):
    def user_rules(self, text):
        self.paths.config.mkdir(parents=True, exist_ok=True)
        (self.paths.config / RULES_FILE).write_text(text, encoding="utf-8")

    def ingest(self):
        publish = FakePublish()
        outcome = Ingester(self.paths, publish, self.audit, clock=self.clock, sleep=lambda s: None, quiet_s=0).run()
        return publish, outcome


class LoaderTest(RulesCase):
    def test_shipped_default_is_off_and_valid(self):
        rules = load_rules(self.paths)
        self.assertEqual((rules.nested_exempt, rules.problems), (False, ()))
        self.assertEqual(json.loads(SHIPPED.read_text())["nested_test_shell"]["exempt"], False)

    def test_user_override_wins(self):
        self.user_rules('{"version": 1, "nested_test_shell": {"exempt": true}}')
        rules = load_rules(self.paths)
        self.assertEqual((rules.nested_exempt, rules.problems), (True, ()))

    def test_user_override_can_turn_a_shipped_on_back_off(self):
        shipped = self.root / "shipped.json"
        shipped.write_text('{"version": 1, "nested_test_shell": {"exempt": true}}')
        self.user_rules('{"version": 1, "nested_test_shell": {"exempt": false}}')
        self.assertFalse(load_rules(self.paths, shipped).nested_exempt)

    def test_invalid_files_fall_back_to_off_and_are_reported(self):
        for text in (
            "not json",
            "[]",
            '{"version": 2, "nested_test_shell": {"exempt": true}}',
            '{"version": 1, "nested_test_shell": {"exempt": "yes"}}',
            '{"version": 1, "nested_test_shell": {"exempt": true, "mode": "ignore"}}',
            '{"version": 1, "nested_test_shell": []}',
        ):
            self.user_rules(text)
            rules = load_rules(self.paths)
            self.assertFalse(rules.nested_exempt, text)
            self.assertEqual(len(rules.problems), 1, text)

    def test_a_broken_user_file_turns_a_shipped_on_off(self):
        shipped = self.root / "shipped.json"
        shipped.write_text('{"version": 1, "nested_test_shell": {"exempt": true}}')
        for text in ("not json", "[" * 5000, '{"version": 2, "nested_test_shell": {"exempt": false}}'):
            self.user_rules(text)
            rules = load_rules(self.paths, shipped)
            self.assertFalse(rules.nested_exempt, text[:20])
            self.assertEqual(len(rules.problems), 1, text[:20])


class RoutingTest(RulesCase):
    def test_default_routes_the_nested_crash_at_critical_tagged(self):
        path = self.write_crash()
        publish, outcome = self.ingest()
        (event,) = publish.events
        self.assertEqual((event["severity"], outcome.published, outcome.exempt), ("critical", 1, 0))
        self.assertIn("nested-test-shell", event["data"]["tags"])
        self.assertFalse(path.exists())

    def test_exempt_drops_it_to_the_audit_log_and_still_archives(self):
        self.user_rules('{"version": 1, "nested_test_shell": {"exempt": true}}')
        path = self.write_crash()
        publish, outcome = self.ingest()
        self.assertEqual((publish.events, outcome.published, outcome.exempt, outcome.archived), ([], 0, 1, 1))
        self.assertFalse(path.exists())
        self.assertEqual(self.audit_decisions(), ["exempt-nested", "archived"])
        self.assertEqual(self.audit.records()[0]["severity"], "critical")

    def test_exempt_does_not_touch_a_real_shell_crash(self):
        from ei_fixtures import REAL_CMDLINE, crash_text

        self.user_rules('{"version": 1, "nested_test_shell": {"exempt": true}}')
        self.write_crash(text=crash_text(cmdline=REAL_CMDLINE))
        publish, outcome = self.ingest()
        self.assertEqual((len(publish.events), outcome.exempt), (1, 0))

    def test_invalid_user_file_keeps_routing_on(self):
        self.user_rules('{"version": 1, "nested_test_shell": {"exempt": "yes"}}')
        self.write_crash()
        publish, _ = self.ingest()
        self.assertEqual(len(publish.events), 1)

"""``ubuntu-tweak coverage``: exit codes, layouts, JSON, ``--class``, ``--propose``, and the ``status`` summary line.

Every scan runs against a scratch machine (``adapters._system.System``: FakeProcess, a FakeBackend, a temporary HOME
and root), so nothing real is read, run or written.
"""

import argparse
import contextlib
import io
import json
import unittest
from collections.abc import Iterator
from unittest import mock

try:
    import gi  # noqa: F401
except ModuleNotFoundError as error:
    raise unittest.SkipTest("PyGObject (python3-gi) is not importable in this interpreter") from error

from tests.actions_scenario import Scenario
from tests.test_apply_commands import CommandScenario
from tests.test_ui import strip
from tests.util import write
from ubuntu_tweaks import reconcile, ui
from ubuntu_tweaks.adapters._system import System, use
from ubuntu_tweaks.cli import build_parser, main
from ubuntu_tweaks.commands import coverage as coverage_command
from ubuntu_tweaks.commands import status
from ubuntu_tweaks.coverage import Coverage, Finding, Item
from ubuntu_tweaks.errors import ExitCode
from ubuntu_tweaks.process import FakeProcess
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema

REAL_SCAN = coverage_command.scan  # CommandScenario replaces it with a stub; the status tests want the real one
VAULT = FakeSchema("org.example.vault", {"master-password": FakeKey("s", "''")}, path="/org/example/vault/")
INTERFACE = FakeSchema(
    "org.gnome.desktop.interface", {"gtk-theme": FakeKey("s", "'Adwaita'")}, path="/org/gnome/desktop/interface/"
)


def arguments(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {"json": False, "coverage_class": None, "propose": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class CoverageScenario(Scenario):
    """A scratch machine with one untracked app directory in ``~/.config``."""

    def setUp(self) -> None:
        super().setUp()
        ui.configure()
        self.addCleanup(ui.configure)
        self.rootfs = self.tmp / "rootfs"
        self.rootfs.mkdir()
        self.backend = FakeBackend([VAULT, INTERFACE])
        self.process = FakeProcess()
        manager = use(System(self.process, self.home, self.rootfs, self.backend))
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        write(self.home / ".config/foo/app.conf", "x")

    def run_command(self, **overrides: object) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = coverage_command.run(self.context(), arguments(**overrides))
        return code, out.getvalue()

    def classify(self, text: str) -> None:
        write(self.profile_root / "coverage.toml", text)


class ExitCodeTests(CoverageScenario):
    def test_an_untracked_item_exits_3_and_is_listed_with_its_detail(self) -> None:
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("Coverage: 1 untracked, 0 secret, 0 excluded, 0 tracked (run `ubuntu-tweak coverage`)", out)
        self.assertIn("files: 1 untracked, 0 secret, 0 excluded, 0 tracked", out)
        self.assertRegex(out, r"untracked  file:~/\.config/foo\s+directory, 1 entry")

    def test_exit_0_once_every_item_is_classified(self) -> None:
        self.classify('[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "excluded"\nreason = "scratch data"\n')
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("Coverage: 0 untracked, 0 secret, 1 excluded, 0 tracked\n", out)
        self.assertRegex(out, r"excluded   file:~/\.config/foo\s+scratch data")

    def test_a_declaration_makes_it_tracked_and_names_the_owner(self) -> None:
        write(self.profile_root / "files/.config/foo/app.conf", "x")
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.OK)
        self.assertRegex(out, r"tracked    file:~/\.config/foo\s+files/\.config/foo/app\.conf")

    def test_an_empty_machine_is_clean(self) -> None:
        (self.home / ".config/foo/app.conf").unlink()
        (self.home / ".config/foo").rmdir()
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("Coverage: 0 untracked, 0 secret, 0 excluded, 0 tracked", out)

    def test_class_filters_the_rows_but_not_the_counts_or_the_exit_code(self) -> None:
        write(self.home / ".cache/x", "x")
        code, out = self.run_command(coverage_class="excluded")
        self.assertEqual(code, ExitCode.DRIFT)  # foo is still untracked
        self.assertIn("1 untracked, 0 secret, 1 excluded", out)
        self.assertIn("file:~/.cache", out)
        self.assertNotIn("file:~/.config/foo", out)

    def test_dry_run_is_accepted_and_changes_nothing(self) -> None:
        before = sorted(str(path) for path in self.tmp.rglob("*"))
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(reconcile, "open_backend"):
            code = main(["--profile", str(self.profile_root), "--dry-run", "--plain", "coverage"])
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertEqual(sorted(str(path) for path in self.tmp.rglob("*")), before)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.process.executed, [])

    def test_a_broken_coverage_file_is_a_config_error(self) -> None:
        from ubuntu_tweaks.errors import ConfigError

        self.classify('[[coverage]]\nmatch = ["a"]\nclass = "excluded"\n')
        with self.assertRaisesRegex(ConfigError, "needs a 'reason'"):
            self.run_command()


class SecretsTests(CoverageScenario):
    def test_a_secret_shows_its_reason_and_never_a_value(self) -> None:
        write(self.home / ".ssh/id_ed25519", "PRIVATE-KEY-MATERIAL")
        self.backend.set_user(VAULT.id, None, "master-password", "'hunter2'")
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertRegex(out, r"secret     file:~/\.ssh\s+private keys")
        self.assertRegex(out, r"secret     dconf:/org/example/vault/master-password\s+the key name says credential")
        for text in ("PRIVATE-KEY-MATERIAL", "hunter2"):
            self.assertNotIn(text, out)
        _, document = self.run_command(json=True)
        self.assertNotIn("hunter2", document)
        secrets = [f for f in json.loads(document)["findings"] if f["class"] == "secret"]
        self.assertEqual({f["id"] for f in secrets}, {"file:~/.ssh", "dconf:/org/example/vault/master-password"})
        self.assertEqual({f["detail"] for f in secrets}, {""})


class JsonTests(CoverageScenario):
    def test_the_document_shape(self) -> None:
        write(self.home / ".cache/x", "x")
        self.backend.set_user(INTERFACE.id, None, "gtk-theme", "'Yaru'")
        code, out = self.run_command(json=True)
        document = json.loads(out)
        self.assertEqual(code, 3)
        self.assertEqual(sorted(document), ["counts", "exit_code", "findings", "machine", "profile"])
        self.assertEqual(document["machine"], "home")
        self.assertEqual(document["profile"], str(self.profile_root))
        self.assertEqual(document["counts"], {"untracked": 2, "secret": 0, "excluded": 1, "tracked": 0, "ticketed": 0})
        self.assertEqual(document["exit_code"], 3)
        self.assertNotIn("proposals", document)
        by_id = {f["id"]: f for f in document["findings"]}
        self.assertEqual(
            by_id["dconf:/org/gnome/desktop/interface/gtk-theme"],
            {
                "adapter": "dconf",
                "id": "dconf:/org/gnome/desktop/interface/gtk-theme",
                "kind": "key",
                "class": "untracked",
                "detail": "'Yaru' (default 'Adwaita')",
                "reason": "",
                "owner": "",
                "ticket": "",
            },
        )
        self.assertEqual(by_id["file:~/.cache"]["class"], "excluded")
        self.assertEqual(by_id["file:~/.cache"]["reason"], "cache: rebuilt by the tool that owns it")

    def test_class_filter_applies_to_findings_only(self) -> None:
        write(self.home / ".cache/x", "x")
        _, out = self.run_command(json=True, coverage_class="excluded")
        document = json.loads(out)
        self.assertEqual([f["id"] for f in document["findings"]], ["file:~/.cache"])
        self.assertEqual(document["counts"]["untracked"], 1)
        self.assertEqual(document["exit_code"], 3)

    def test_json_wins_over_the_colored_layout(self) -> None:
        ui.configure(color="always")
        code, out = self.run_command(json=True)
        self.assertEqual((code, json.loads(out)["exit_code"]), (3, 3))


class ProposeTests(CoverageScenario):
    def test_propose_prints_what_capture_would_add_and_writes_nothing(self) -> None:
        self.backend.set_user(INTERFACE.id, None, "gtk-theme", "'Yaru'")
        before = sorted(str(path) for path in self.tmp.rglob("*"))
        code, out = self.run_command(propose=True)
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("Proposals (nothing is written", out)
        self.assertIn("## settings/appearance.ini (dconf, 1 item(s))", out)
        self.assertIn("gtk-theme='Yaru'", out)
        self.assertIn("## profile.toml [capture] include_files (files, 1 item(s))", out)
        self.assertIn('".config/foo/**"', out)
        self.assertEqual(sorted(str(path) for path in self.tmp.rglob("*")), before)
        self.assertEqual(self.backend.writes, [])

    def test_propose_in_json(self) -> None:
        _, out = self.run_command(propose=True, json=True)
        (proposal,) = json.loads(out)["proposals"]
        self.assertEqual(
            (proposal["adapter"], proposal["title"], proposal["items"]),
            ("files", "profile.toml [capture] include_files", ["file:~/.config/foo"]),
        )

    def test_nothing_to_propose_when_everything_is_classified(self) -> None:
        self.classify('[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "excluded"\nreason = "r"\n')
        _, out = self.run_command(propose=True)
        self.assertIn("Nothing to propose", out)

    def test_only_untracked_items_are_proposed(self) -> None:
        write(self.home / ".config/bar/x", "x")
        self.classify('[[coverage]]\nmatch = ["file:~/.config/bar"]\nclass = "excluded"\nreason = "r"\n')
        _, out = self.run_command(propose=True)
        self.assertIn('".config/foo/**"', out)
        self.assertNotIn(".config/bar/**", out)


class LayoutTests(CoverageScenario):
    def test_piped_output_is_the_compact_text_without_escapes(self) -> None:
        _, out = self.run_command()
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("Classes", out)

    def test_colored_output_explains_the_classes_and_groups_per_adapter(self) -> None:
        ui.configure(color="always")
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn("\x1b[", out)
        plain = strip(out)
        self.assertIn("Classes", plain)
        self.assertIn("untracked: configuration nobody declared", plain)
        self.assertIn("files (1)", plain)
        self.assertRegex(plain, r"file:~/\.config/foo\s+directory, 1 entry")

    def test_the_parser_registers_the_command_and_its_flags(self) -> None:
        args = build_parser().parse_args(["--dry-run", "coverage", "--json", "--class", "secret", "--propose"])
        self.assertEqual(
            (args.command, args.json, args.coverage_class, args.propose), ("coverage", True, "secret", True)
        )
        self.assertIs(args.handler, coverage_command.run)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["coverage", "--class", "bogus"])


class TicketedProposalAndBadgeTests(CoverageScenario):
    RULE = '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "untracked"\nticket = "#12"\nreason = "later"\n'

    def test_propose_leaves_a_ticketed_item_alone(self) -> None:
        self.classify(self.RULE)
        _, out = self.run_command(propose=True)
        self.assertIn("Nothing to propose", out)

    def test_an_adapter_with_only_ticketed_items_shows_the_ok_badge(self) -> None:
        self.classify(self.RULE)
        ui.configure(color="always")
        _, out = self.run_command()
        files_line = next(line for line in strip(out).splitlines() if "files (" in line)
        self.assertNotIn("need", files_line)
        self.assertNotIn("!", files_line)


class TicketedUntrackedTests(CoverageScenario):
    """``[[coverage]] class = "untracked"`` with a ``ticket``: still a finding, but one somebody owns."""

    RULE = (
        '[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "untracked"\nticket = "#12"\n'
        'reason = "needs a decision on tracking"\n'
    )

    def test_every_untracked_item_ticketed_exits_0_and_the_counts_line_says_so(self) -> None:
        self.classify(self.RULE)
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("Coverage: 1 untracked (1 ticketed), 0 secret, 0 excluded, 0 tracked\n", out)
        self.assertNotIn("run `ubuntu-tweak coverage`", out)
        self.assertIn("files: 1 untracked (1 ticketed), 0 secret, 0 excluded, 0 tracked", out)

    def test_one_untracked_item_without_a_ticket_still_exits_3(self) -> None:
        self.classify(self.RULE)
        write(self.home / ".config/bar/x", "x")
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.DRIFT)
        self.assertIn(
            "Coverage: 2 untracked (1 ticketed), 0 secret, 0 excluded, 0 tracked (run `ubuntu-tweak coverage`)", out
        )

    def test_the_plain_table_has_a_ticket_column_and_the_reason(self) -> None:
        self.classify(self.RULE)
        write(self.home / ".config/bar/x", "x")
        _, out = self.run_command()
        self.assertRegex(out, r"untracked  file:~/\.config/foo  #12  needs a decision on tracking")
        self.assertRegex(out, r"untracked  file:~/\.config/bar\s+directory, 1 entry")

    def test_a_table_without_any_ticket_has_no_ticket_column(self) -> None:
        _, out = self.run_command()
        self.assertRegex(out, r"untracked  file:~/\.config/foo\s+directory, 1 entry")

    def test_the_rich_table_has_a_ticket_column(self) -> None:
        self.classify(self.RULE)
        ui.configure(color="always")
        _, out = self.run_command()
        plain = strip(out)
        self.assertIn("TICKET", plain)
        self.assertRegex(plain, r"file:~/\.config/foo\s+#12\s+needs a decision on tracking")
        self.assertIn("coverage: 1 untracked (1 ticketed)", plain)

    def test_json_carries_the_ticket_per_finding_and_the_ticketed_count(self) -> None:
        self.classify(self.RULE)
        _, out = self.run_command(json=True)
        document = json.loads(out)
        self.assertEqual(document["counts"]["ticketed"], 1)
        self.assertEqual(document["exit_code"], 0)
        (finding,) = [f for f in document["findings"] if f["id"] == "file:~/.config/foo"]
        self.assertEqual(
            (finding["class"], finding["ticket"], finding["reason"]),
            ("untracked", "#12", "needs a decision on tracking"),
        )

    def test_an_untracked_rule_without_a_ticket_is_invalid_config(self) -> None:
        from ubuntu_tweaks.errors import ConfigError

        self.classify('[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "untracked"\nreason = "later"\n')
        with self.assertRaisesRegex(ConfigError, "an untracked rule needs a 'ticket'"):
            self.run_command()

    def test_a_ticket_on_another_class_is_invalid_config(self) -> None:
        from ubuntu_tweaks.errors import ConfigError

        self.classify('[[coverage]]\nmatch = ["file:~/.config/foo"]\nclass = "excluded"\nreason = "r"\nticket = "#1"\n')
        with self.assertRaisesRegex(ConfigError, "'ticket' is only for an untracked rule"):
            self.run_command()

    def test_a_declaration_makes_a_ticketed_item_tracked(self) -> None:
        write(self.profile_root / "files/.config/foo/app.conf", "x")
        self.classify(self.RULE)
        code, out = self.run_command()
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("0 untracked, 0 secret, 0 excluded, 1 tracked", out)


class StatusSummaryTests(CommandScenario):
    """``status`` ends with the coverage summary and its exit code is the drift's alone."""

    def setUp(self) -> None:
        super().setUp()
        self.run_apply(commit=True)  # in sync: status exits 0 by itself
        write(self.home / ".config/foo/app.conf", "x")
        self.scratch = System(FakeProcess(), self.home, self.tmp / "rootfs", FakeBackend([]))

    @contextlib.contextmanager
    def real_scan(self) -> Iterator[None]:
        with use(self.scratch), mock.patch.object(coverage_command, "scan", side_effect=REAL_SCAN):
            yield

    def status(self, **overrides: object) -> tuple[int, str]:
        out = io.StringIO()
        values: dict[str, object] = {"explain": False, "json": False}
        values.update(overrides)
        with (
            self.real_scan(),
            contextlib.redirect_stdout(out),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
        ):
            code = status.run(self.context(), argparse.Namespace(**values))
        return code, out.getvalue()

    def test_plain_status_ends_with_the_summary_line_and_untracked_does_not_change_the_exit_code(self) -> None:
        code, out = self.status()
        self.assertEqual(code, ExitCode.OK)
        self.assertEqual(
            out.rstrip("\n").splitlines()[-1],
            "Coverage: 1 untracked, 0 secret, 0 excluded, 1 tracked (run `ubuntu-tweak coverage`)",
        )

    def test_status_json_has_a_coverage_key(self) -> None:
        code, out = self.status(json=True)
        document = json.loads(out)
        self.assertEqual(code, ExitCode.OK)
        self.assertEqual(
            document["coverage"], {"untracked": 1, "secret": 0, "excluded": 0, "tracked": 1, "ticketed": 0}
        )
        self.assertEqual(document["exit_code"], 0)

    def test_rich_status_shows_the_summary_before_the_legend(self) -> None:
        ui.configure(color="always")
        self.addCleanup(ui.configure)
        code, out = self.status()
        plain = strip(out)
        self.assertEqual(code, ExitCode.OK)
        self.assertIn("coverage: 1 untracked, 0 secret, 0 excluded, 1 tracked", plain)
        self.assertLess(plain.index("coverage: 1 untracked"), plain.index("Legend"))

    def test_only_skips_the_scan_and_reports_null(self) -> None:
        out = io.StringIO()
        with (
            self.real_scan(),
            contextlib.redirect_stdout(out),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
        ):
            status.run(self.context(frozenset({"eng"})), argparse.Namespace(explain=False, json=True))
        self.assertIsNone(json.loads(out.getvalue())["coverage"])

    def test_a_clean_machine_prints_the_line_without_the_hint(self) -> None:
        with mock.patch.object(
            coverage_command, "scan", return_value=[Finding(Item("x", "x:y", "k"), Coverage.TRACKED)]
        ):
            code, out = self.status_with_stub()
        self.assertEqual(code, ExitCode.OK)
        self.assertEqual(out.rstrip("\n").splitlines()[-1], "Coverage: 0 untracked, 0 secret, 0 excluded, 1 tracked")

    def test_ticketed_untracked_items_show_in_the_footer_without_the_hint(self) -> None:
        ticketed = Finding(Item("x", "x:y", "k"), Coverage.UNTRACKED, "decide later", ticket="#7")
        with mock.patch.object(coverage_command, "scan", return_value=[ticketed]):
            code, out = self.status_with_stub()
        self.assertEqual(code, ExitCode.OK)
        self.assertEqual(
            out.rstrip("\n").splitlines()[-1], "Coverage: 1 untracked (1 ticketed), 0 secret, 0 excluded, 0 tracked"
        )

    def test_an_open_untracked_item_keeps_the_hint_next_to_the_ticketed_count(self) -> None:
        findings = [
            Finding(Item("x", "x:y", "k"), Coverage.UNTRACKED, "decide later", ticket="#7"),
            Finding(Item("x", "x:z", "k"), Coverage.UNTRACKED),
        ]
        with mock.patch.object(coverage_command, "scan", return_value=findings):
            _, out = self.status_with_stub()
        self.assertEqual(
            out.rstrip("\n").splitlines()[-1],
            "Coverage: 2 untracked (1 ticketed), 0 secret, 0 excluded, 0 tracked (run `ubuntu-tweak coverage`)",
        )

    def test_status_json_counts_the_ticketed_items(self) -> None:
        findings = [Finding(Item("x", "x:y", "k"), Coverage.UNTRACKED, "decide later", ticket="#7")]
        with mock.patch.object(coverage_command, "scan", return_value=findings):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(reconcile, "open_backend", return_value=self.fake):
                status.run(self.context(), argparse.Namespace(explain=False, json=True))
        self.assertEqual(
            json.loads(out.getvalue())["coverage"],
            {"untracked": 1, "secret": 0, "excluded": 0, "tracked": 0, "ticketed": 1},
        )

    def status_with_stub(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(reconcile, "open_backend", return_value=self.fake):
            code = status.run(self.context(), argparse.Namespace(explain=False, json=False))
        return code, out.getvalue()


if __name__ == "__main__":
    unittest.main()

"""The commands end to end on the scratch scenario: compact when piped, explained with ``--color always``.

A separate module from ``test_ui`` because the scenario needs PyGObject (``test_apply_commands`` skips itself
without it) while the helper tests of ``test_ui`` are stdlib only.
"""

from __future__ import annotations

import dataclasses
import argparse
import contextlib
import io
import json
import os
from unittest import mock
from ubuntu_tweaks.identity import Location

from tests.test_apply_commands import CommandScenario, RootAction
from tests.test_ui import UiCase, strip
from ubuntu_tweaks import reconcile, ui
from ubuntu_tweaks.cli import build_parser, main
from ubuntu_tweaks.commands import status


class CommandOutputTests(UiCase, CommandScenario):
    """The real commands on the scratch scenario: plain when piped, explained with ``--color always``."""

    def test_status_piped_stays_in_the_compact_layout(self) -> None:
        ui.configure()
        code, out = self.run_status(self.context())
        self.assertEqual(code, 3)
        self.assertIn("Missing (3):", out)
        self.assertNotIn("\x1b[", out)

    def test_status_colored_is_the_explained_layout(self) -> None:
        ui.configure(color="always")
        code, out = self.run_status(self.context())
        self.assertEqual(code, 3)
        plain = strip(out)
        self.assertIn("Missing (3)", plain)
        self.assertIn("Declared by a module or your profile", plain)
        self.assertIn("\x1b[", out)

    def test_status_explain_prints_the_exit_codes_even_when_piped(self) -> None:
        ui.configure()
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
        ):
            status.run(self.context(), argparse.Namespace(explain=True, json=False))
        self.assertIn("Exit codes", out.getvalue())
        self.assertNotIn("\x1b[", out.getvalue())

    def test_apply_preview_colored_ends_with_the_plain_nothing_changed_footer(self) -> None:
        ui.configure(color="always")
        code, out = self.run_apply(commit=False)
        self.assertEqual(code, 0)
        self.assertIn("What will change", strip(out))
        footer = "Nothing changed. This was a preview; run `ubuntu-tweak apply --commit` to apply it."
        self.assertIn(footer, " ".join(strip(out).split()))

    def test_apply_commit_colored_shows_numbered_progress_and_a_summary(self) -> None:
        ui.configure(color="always")
        code, out = self.run_apply(commit=True)
        self.assertEqual(code, 0)
        plain = strip(out)
        self.assertRegex(plain, r"\[1/3\] → ")
        self.assertRegex(plain, r"\[3/3\] ✓ done")
        self.assertIn("Summary\n  ✓ 3 changed", plain)

    def test_clicolor_force_colors_but_keeps_the_compact_layout_on_a_pipe(self) -> None:
        ui.configure()
        with mock.patch.dict(os.environ, {"CLICOLOR_FORCE": "1"}):
            code, out = self.run_apply(commit=False)
        self.assertEqual(code, 0)
        self.assertIn("Plan: 3 action(s) to run", out)
        self.assertNotIn("What will change", out)

    def test_status_json_wins_over_explain_and_keeps_the_exit_code(self) -> None:
        ui.configure(color="always")
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
        ):
            code = status.run(self.context(), argparse.Namespace(explain=True, json=True))
        document = json.loads(out.getvalue())
        self.assertEqual((code, document["exit_code"], document["in_sync"]), (3, 3, False))
        self.assertEqual(len(document["missing"]), 3)

    def test_the_update_child_process_gets_the_output_flags(self) -> None:
        from ubuntu_tweaks.commands import update

        ui.configure(color="always", plain=False, lang="ru")
        command = update._apply_command(self.context(), no_sudo=True)
        self.assertEqual(command[-5:], ["--no-sudo", "--color", "always", "--lang", "ru"])
        args = build_parser().parse_args(command[2:])
        self.assertEqual((args.color, args.lang, args.commit), ("always", "ru", True))

    def test_a_commit_that_left_manual_or_skipped_items_does_not_promise_a_clean_status(self) -> None:
        ui.configure(color="always")
        _, out = self.run_apply(commit=True, no_sudo=True, extra=(RootAction(),))
        text = " ".join(strip(out).split())
        self.assertIn("will still list the skipped and manual items", text)
        self.assertNotIn("it should exit 0", text)
        _, out = self.run_apply(commit=True)
        self.assertIn("it should exit 0", " ".join(strip(out).split()))

    def test_errors_go_through_the_red_formatter_on_stderr(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--color", "always", "--only", "nosuchmodule", "list"])
        self.assertEqual(code, 4)
        self.assertIn("`ubuntu-tweak list` shows the module names", strip(err.getvalue()))
        self.assertIn("\x1b[31m", err.getvalue())

    def test_plain_beats_explain(self) -> None:
        ui.configure(plain=True)
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch.object(reconcile, "open_backend", return_value=self.fake),
        ):
            code = status.run(self.context(), argparse.Namespace(explain=True, json=False))
        self.assertEqual(code, 3)
        self.assertIn("Missing (3):", out.getvalue())
        self.assertNotIn("Exit codes", out.getvalue())

    def test_the_header_has_machine_profile_and_engine_rows(self) -> None:
        ui.configure(color="always")
        header = [strip(line) for line in reconcile.header_lines(self.context(), "status")]
        self.assertEqual(header[0], "ubuntu-tweak status")
        self.assertRegex(header[1], r"^  machine  \S+ \(")
        self.assertRegex(header[2], r"^  profile  ")
        self.assertRegex(header[3], r"^  engine   ubuntu-tweaks \S+$")

    def test_the_header_of_a_missing_profile_says_what_discovery_did(self) -> None:
        ui.configure(color="always")
        context = self.context()
        gone = context.profile.root / "not-there"
        lost = dataclasses.replace(context.profile, root=gone, location=Location(gone, manual="gh auth login"))
        text = "\n".join(
            strip(line) for line in reconcile.header_lines(dataclasses.replace(context, profile=lost), "status")
        )
        self.assertIn("not found: module defaults only", text)
        self.assertIn("needs manual: gh auth login", text)

    def test_engine_version_of_a_tree_that_is_not_a_checkout_is_unknown(self) -> None:
        self.assertEqual(reconcile.engine_version(self.context()), "unknown")

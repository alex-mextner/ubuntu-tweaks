"""``ubuntu_tweaks.ui``: color gating, badges, wrapping, wording, and the explained ``status`` / ``apply`` layout."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from ubuntu_tweaks import ui
from ubuntu_tweaks.actions import Action, ExecutionResult, Phase, Plan
from ubuntu_tweaks.cli import build_parser
from ubuntu_tweaks.commands import status

GOLDEN = Path(__file__).parent / "golden"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip(text: str) -> str:
    return ANSI_RE.sub("", text)


class Tty(io.StringIO):
    fake_tty = True  # what ``tests/__init__.py`` lets through as a terminal

    def isatty(self) -> bool:
        return True


class UiCase(unittest.TestCase):
    """Starts every test from a clean environment and the default configuration."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ, {"COLUMNS": "80"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("NO_COLOR", "CLICOLOR_FORCE", "TERM", "UBUNTU_TWEAK_LANG"):
            os.environ.pop(name, None)
        ui.configure()
        self.addCleanup(ui.configure)


class ColorGatingTests(UiCase):
    def test_a_pipe_gets_no_color_and_the_plain_layout(self) -> None:
        out = io.StringIO()
        self.assertFalse(ui.color_enabled(out))
        self.assertFalse(ui.rich(out))
        self.assertEqual(ui.style("x", "green", stream=out), "x")

    def test_a_tty_gets_color_and_the_rich_layout(self) -> None:
        out = Tty()
        self.assertTrue(ui.color_enabled(out))
        self.assertTrue(ui.rich(out))

    def test_no_color_keeps_the_rich_layout_on_a_tty_but_drops_the_escapes(self) -> None:
        os.environ["NO_COLOR"] = "1"
        out = Tty()
        self.assertFalse(ui.color_enabled(out))
        self.assertTrue(ui.rich(out))

    def test_an_empty_no_color_is_ignored(self) -> None:
        os.environ["NO_COLOR"] = ""
        self.assertTrue(ui.color_enabled(Tty()))

    def test_term_dumb_drops_the_escapes(self) -> None:
        os.environ["TERM"] = "dumb"
        self.assertFalse(ui.color_enabled(Tty()))

    def test_clicolor_force_colors_a_pipe_but_no_color_wins(self) -> None:
        os.environ["CLICOLOR_FORCE"] = "1"
        self.assertTrue(ui.color_enabled(io.StringIO()))
        os.environ["NO_COLOR"] = "1"
        self.assertFalse(ui.color_enabled(io.StringIO()))
        os.environ.pop("NO_COLOR")
        os.environ["CLICOLOR_FORCE"] = "0"
        self.assertFalse(ui.color_enabled(io.StringIO()))

    def test_color_always_beats_the_environment_and_never_beats_a_tty(self) -> None:
        os.environ["NO_COLOR"] = "1"
        ui.configure(color="always")
        self.assertTrue(ui.color_enabled(io.StringIO()))
        self.assertTrue(ui.rich(io.StringIO()))
        os.environ.pop("NO_COLOR")
        ui.configure(color="never")
        self.assertFalse(ui.color_enabled(Tty()))
        self.assertTrue(ui.rich(Tty()), "a terminal still gets the explained layout without color")

    def test_plain_beats_everything(self) -> None:
        ui.configure(color="always", plain=True)
        self.assertFalse(ui.color_enabled(Tty()))
        self.assertFalse(ui.rich(Tty()))

    def test_the_flags_parse_before_and_after_the_command(self) -> None:
        for argv in (["--plain", "status"], ["status", "--plain"]):
            self.assertTrue(build_parser().parse_args(argv).plain)
        args = build_parser().parse_args(["status", "--color", "always", "--lang", "ru"])
        self.assertEqual((args.color, args.lang), ("always", "ru"))
        self.assertIsNone(build_parser().parse_args(["status"]).color)

    def test_a_child_process_gets_the_same_flags(self) -> None:
        self.assertEqual(ui.cli_flags(), [])
        ui.configure(color="always", plain=False, lang="ru")
        self.assertEqual(ui.cli_flags(), ["--color", "always", "--lang", "ru"])

    def test_a_child_of_a_coloring_terminal_is_told_color_always_because_its_stdout_is_a_pipe(self) -> None:
        with contextlib.redirect_stdout(Tty()):
            self.assertEqual(ui.cli_flags(), ["--color", "always"])
            ui.configure(color="auto", plain=True)
            self.assertEqual(ui.cli_flags(), ["--plain"])

    def test_a_forced_color_on_a_pipe_does_not_make_the_child_explain_itself(self) -> None:
        with mock.patch.dict(os.environ, {"CLICOLOR_FORCE": "1"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.cli_flags(), [])


class HelperGoldenTests(UiCase):
    def test_plain_helpers_are_byte_stable(self) -> None:
        ui.configure(color="never")
        self.assertEqual(ui.style("x", "red"), "x")
        self.assertEqual(
            [ui.badge(kind) for kind in ("ok", "need", "error", "manual", "extra")],
            ["✓ ok", "! apply", "✗ error", "… manual", "+ extra"],
        )
        self.assertEqual(ui.progress(3, 21, "ok", "installed zsh"), "[3/21] ✓ installed zsh")
        self.assertEqual(ui.section("Missing", 4, "error"), "✗ Missing (4)")
        self.assertEqual(ui.format_error("boom", 2, stream=io.StringIO()), "ubuntu-tweak: boom")

    def test_ansi_helpers_use_the_fixed_color_semantics(self) -> None:
        ui.configure(color="always")
        self.assertEqual(ui.style("x", "green"), "\x1b[32mx\x1b[0m")
        self.assertEqual(ui.badge("ok"), "\x1b[32;1m✓ ok\x1b[0m")
        self.assertEqual(ui.badge("need"), "\x1b[33;1m! apply\x1b[0m")
        self.assertEqual(ui.badge("error"), "\x1b[31;1m✗ error\x1b[0m")
        self.assertEqual(ui.badge("extra"), "\x1b[36;1m+ extra\x1b[0m")
        self.assertEqual(ui.heading("Title"), "\x1b[1mTitle\x1b[0m")
        self.assertEqual(ui.progress(3, 21, "ok", "x"), "\x1b[2m[3/21]\x1b[0m \x1b[32;1m✓\x1b[0m x")

    def test_every_badge_carries_a_symbol_and_a_word_so_color_is_never_alone(self) -> None:
        ui.configure(color="never")
        for kind in ("ok", "need", "error", "manual", "extra", "run", "info"):
            self.assertRegex(ui.badge(kind), r"^\S+ \w+$")

    def test_ascii_fallback_on_a_stream_that_cannot_encode_the_symbols(self) -> None:
        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        self.assertEqual(ui.badge("ok", stream=stream), "v ok")
        self.assertEqual(ui.badge("error", stream=stream), "x error")

    def test_error_in_a_terminal_is_red_with_a_fix_hint(self) -> None:
        ui.configure(color="always")
        text = ui.format_error("bad config", 2, stream=io.StringIO())
        self.assertEqual(
            strip(text),
            "✗ error bad config\n  fix: check the file named above; `ubuntu-tweak machine` shows which files are read",
        )
        self.assertIn("\x1b[31m", text)
        self.assertIn("\x1b[36m", text)

    def test_kv_and_table_align_on_visible_width(self) -> None:
        ui.configure(color="always")
        rows = [(ui.badge("ok"), "a", "first"), (ui.badge("error"), "bb", "second")]
        lines = ui.table(("STATE", "NAME", "NOTE"), rows)
        self.assertEqual(
            [strip(line) for line in lines],
            ["  STATE    NAME  NOTE", "  ✓ ok     a     first", "  ✗ error  bb    second"],
        )
        self.assertEqual([strip(line) for line in ui.kv([("a", "1"), ("long", "2")])], ["  a     1", "  long  2"])


class WrapTests(UiCase):
    TEXT = (
        "Declared by a module or your profile, but not on this machine yet; `ubuntu-tweak apply --commit` "
        "installs or creates it, after a preview that changes nothing and a confirmation."
    )

    def test_lines_fit_the_terminal_at_60_80_and_120_columns(self) -> None:
        for columns in (60, 80, 120):
            with self.subTest(columns=columns):
                os.environ["COLUMNS"] = str(columns)
                lines = ui.wrap(self.TEXT, first="    ! ")
                self.assertTrue(all(len(line) < columns for line in lines), lines)
                self.assertEqual(" ".join(line.strip() for line in lines).replace("! ", "", 1), self.TEXT)
                self.assertTrue(all(line.startswith("      ") for line in lines[1:]), "hanging indent")

    def test_narrower_terminals_wrap_into_more_lines(self) -> None:
        counts = []
        for columns in (60, 80, 120):
            os.environ["COLUMNS"] = str(columns)
            counts.append(len(ui.wrap(self.TEXT, indent=2)))
        self.assertGreater(counts[0], counts[1])
        self.assertGreater(counts[1], counts[2])

    def test_very_wide_and_very_narrow_terminals_are_clamped(self) -> None:
        os.environ["COLUMNS"] = "300"
        self.assertEqual(ui.width(), 120)
        os.environ["COLUMNS"] = "10"
        self.assertEqual(ui.width(), 40)

    def test_a_styled_wrap_closes_every_line(self) -> None:
        ui.configure(color="always")
        os.environ["COLUMNS"] = "60"
        for line in ui.wrap(self.TEXT, indent=2):
            self.assertTrue(line.startswith("  \x1b[2m") and line.endswith("\x1b[0m"), repr(line))

    def test_a_message_with_its_own_line_breaks_keeps_them(self) -> None:
        lines = ui.wrap("first line\nrun: sudo chown -R me /opt/x", first="  1. ", names=())
        self.assertEqual(lines, ["  1. first line", "     run: sudo chown -R me /opt/x"])

    def test_an_accent_is_measured_unstyled_and_never_split(self) -> None:
        text = "x" * 30 + " " + "y" * 30
        for columns in (60, 80):
            os.environ["COLUMNS"] = str(columns)
            ui.configure(color="never")
            plain = ui.wrap(text, first="  ! ", names=(), accent="[sudo, skipped: --no-sudo] ")
            ui.configure(color="always")
            colored = ui.wrap(text, first="  ! ", names=(), accent="[sudo, skipped: --no-sudo] ")
            self.assertEqual([strip(line) for line in colored], plain, "same breaks with and without color")
            self.assertTrue(plain[0].startswith("  ! [sudo, skipped: --no-sudo] "), plain)
            self.assertIn("\x1b[33m[sudo, skipped: --no-sudo] \x1b[0m", colored[0])
            for line in colored:
                self.assertEqual(
                    line.count("\x1b[0m"), line.count("\x1b[") - line.count("\x1b[0m"), "no color left open"
                )


class WordingTests(UiCase):
    def test_every_kind_in_the_code_has_a_reason_in_both_languages(self) -> None:
        root = Path(ui.__file__).parent
        kinds = {"link", "link-replace", "cli-link", "cli-link-replace"}  # symlink.py builds these from a prefix
        for source in root.rglob("*.py"):
            kinds |= set(re.findall(r'^\s+kind = "([a-z-]+)"', source.read_text(encoding="utf-8"), re.MULTILINE))
        self.assertGreater(len(kinds), 15)
        for language in ("en", "ru"):
            ui.configure(lang=language)
            for kind in sorted(kinds):
                self.assertTrue(ui.kind_why(kind), f"{language}: no reason for {kind}")

    def test_russian_covers_every_key_of_the_status_apply_and_init_texts(self) -> None:
        self.assertEqual(set(ui._TEXT["ru"]), set(ui._TEXT["en"]))
        self.assertEqual(set(ui._KIND_WHY["ru"]), set(ui._KIND_WHY["en"]))

    def test_language_comes_from_the_flag_then_the_environment(self) -> None:
        self.assertEqual(ui.t("section.missing"), "Missing")
        os.environ["UBUNTU_TWEAK_LANG"] = "ru"
        self.assertEqual(ui.t("section.missing"), "Отсутствует")
        ui.configure(lang="en")
        self.assertEqual(ui.t("section.missing"), "Missing")
        os.environ["UBUNTU_TWEAK_LANG"] = "xx"
        ui.configure()
        self.assertEqual(ui.t("section.missing"), "Missing")

    def test_unknown_kinds_have_no_reason(self) -> None:
        self.assertEqual(ui.kind_why("frobnicate"), "")

    def test_every_wording_key_used_in_the_code_exists(self) -> None:
        root = Path(ui.__file__).parent
        used: set[str] = set()
        for source in root.rglob("*.py"):
            used |= set(re.findall(r'\bui\.t\(\s*"([a-z0-9_.]+)"', source.read_text(encoding="utf-8")))
        used |= {
            f"section.{key}{suffix}"
            for key in ("missing", "modified", "extra", "manual")
            for suffix in ("", ".what", ".fix")
        }
        used |= {f"legend.{kind}" for kind in ("ok", "need", "manual", "extra", "error")} | {
            "exit.0",
            "exit.3",
            "exit.other",
        }
        self.assertGreater(len(used), 30)
        self.assertEqual(sorted(used - set(ui._TEXT["en"])), [])


class FakeAction(Action):
    phase = Phase.SETTINGS_MODULE

    def __init__(self, kind: str, text: str, *, scope: str | None = None, root: bool = False) -> None:
        self.kind = kind
        self.scope = scope
        self.needs_root = root
        self._text = text

    def describe(self) -> str:
        return self._text

    def check(self) -> bool:
        return False

    def run(self) -> None:
        raise AssertionError("a preview never runs an action")


def fake_plan() -> tuple[list[Action], list[Action]]:
    missing: list[Action] = [
        FakeAction("package", "apt: install zsh, 7zip, xclip [base-tools, zsh]", scope="base-tools", root=True),
        FakeAction("package", "apt: install jq [base-tools]", scope="base-tools", root=True),
        FakeAction("submodule", "init submodule gnome-extensions/hyperdock", scope="hyperdock"),
        FakeAction("system", "timezone: set Europe/Berlin (now UTC)", root=True),
    ]
    modified: list[Action] = [
        FakeAction(
            "set-key",
            "/org/gnome/desktop/interface/clock-show-weekday = true (now false) <- modules/x.ini",
            scope="dock",
        ),
    ]
    return missing, modified


HEADER = ["ubuntu-tweak status", "  machine  home (no overlay: shared profile only)", "  profile  ~/.files/ubuntu"]
EXTRAS = ["extension ding@rastersoft.com is enabled but no module declares it"]
WARNINGS = ["git: name and email not set in profile.toml: git config user.name and user.email stays as it is"]


class StatusLayoutTests(UiCase):
    def render(self, **kwargs: object) -> str:
        missing, modified = fake_plan()
        return status.render_rich(HEADER, missing, modified, EXTRAS, WARNINGS, explain=bool(kwargs.get("explain")))

    def test_uncolored_layout_matches_the_golden_file(self) -> None:
        ui.configure(color="never")
        self.assertEqual(self.render() + "\n", (GOLDEN / "status_explained.txt").read_text(encoding="utf-8"))

    def test_the_colored_layout_is_the_same_text_with_escapes_only(self) -> None:
        ui.configure(color="never")
        plain = self.render()
        ui.configure(color="always")
        colored = self.render()
        self.assertNotEqual(plain, colored)
        self.assertEqual(strip(colored), plain)

    def test_colors_follow_the_fixed_semantics(self) -> None:
        ui.configure(color="always")
        text = self.render()
        self.assertIn("\x1b[31;1m✗\x1b[0m \x1b[31;1m4 missing, 1 modified, 1 extra, 1 need your attention", text)
        self.assertIn("\x1b[31;1mMissing (4)\x1b[0m", text)
        self.assertIn("\x1b[36mrun: ubuntu-tweak apply (preview), then ubuntu-tweak apply --commit", text)
        self.assertIn("\x1b[36;1m+\x1b[0m extension ding", text)
        self.assertIn("\x1b[33mgit: name and email", text)
        self.assertIn("\x1b[2mwhy:", text)

    def test_the_verdict_names_what_is_wrong_and_the_command_that_fixes_it(self) -> None:
        ui.configure(color="never")
        lines = self.render().splitlines()
        verdict = lines[lines.index("") + 1 :][:2]
        self.assertEqual(verdict[0], "✗ 4 missing, 1 modified, 1 extra, 1 need your attention")
        self.assertEqual(verdict[1], "  run: ubuntu-tweak apply (preview), then ubuntu-tweak apply --commit")

    def test_items_are_grouped_by_module_and_the_reason_is_not_repeated_within_a_group(self) -> None:
        ui.configure(color="never")
        text = self.render()
        self.assertEqual(text.count("apt/snap/flatpak packages that"), 1)
        for group in ("  base-tools", "  hyperdock", "  profile and system", "  dock"):
            self.assertIn(group + "\n", text)
        self.assertNotIn("[package]", text, "internal action kinds are not shown")
        self.assertIn("[sudo] apt: install zsh", text)

    def test_an_in_sync_machine_says_so_in_green_and_skips_the_next_steps(self) -> None:
        text = strip("\n".join(status.verdict(0, 0, 2, 1)))
        self.assertEqual(text, "… in sync, but 1 item(s) need your attention (see Manual below) (2 extra)")
        ui.configure(color="always")
        text = status.render_rich(HEADER, [], [], EXTRAS, [], explain=False)
        self.assertIn(
            "\x1b[32;1m✓\x1b[0m \x1b[32;1min sync: nothing is missing or modified (1 extra, reported only)", text
        )
        self.assertNotIn("Next", strip(text))

    def test_explain_adds_the_definitions_of_empty_sections_and_the_exit_codes(self) -> None:
        ui.configure(color="never")
        quiet = status.render_rich(HEADER, [], [], [], [], explain=False)
        loud = status.render_rich(HEADER, [], [], [], [], explain=True)
        self.assertNotIn("Exit codes", quiet)
        self.assertNotIn("Declared by a module", quiet)
        self.assertIn("3  drift: something is missing or modified", loud)
        self.assertIn("Declared by a module", loud)

    def test_lines_fit_at_60_80_and_120_columns(self) -> None:
        ui.configure(color="never")
        for columns in (60, 80, 120):
            os.environ["COLUMNS"] = str(columns)
            for line in self.render(explain=True).splitlines():
                self.assertLessEqual(len(line), columns, f"{columns}: {line!r}")

    def test_the_russian_layout_translates_the_sections_and_the_legend(self) -> None:
        ui.configure(color="never", lang="ru")
        text = self.render()
        for word in ("Отсутствует (4)", "Изменено (1)", "Лишнее (1)", "Вручную (1)", "Обозначения", "зачем:"):
            self.assertIn(word, text)

    def test_the_json_document_keeps_the_exit_contract(self) -> None:
        missing, modified = fake_plan()
        ctx = mock.Mock()
        ctx.profile.machine = "home"
        ctx.profile.root = Path("/p")
        ctx.only = frozenset()
        document = json.loads(status._json(ctx, missing, modified, EXTRAS, WARNINGS))
        self.assertEqual((document["exit_code"], document["in_sync"]), (3, False))
        self.assertEqual(document["missing"][0]["kind"], "package")
        self.assertEqual(document["modified"][0]["kind"], "set-key")
        self.assertEqual(document["extra"], EXTRAS)
        clean = json.loads(status._json(ctx, [], [], EXTRAS, []))
        self.assertEqual((clean["exit_code"], clean["in_sync"]), (0, True))

    def test_extras_are_null_when_they_were_not_checked_and_why_is_always_english(self) -> None:
        missing, modified = fake_plan()
        ctx = mock.Mock()
        ctx.profile.machine = "home"
        ctx.profile.root = Path("/p")
        ctx.only = frozenset({"dock"})
        ui.configure(lang="ru")
        document = json.loads(status._json(ctx, missing, modified, EXTRAS, WARNINGS))
        self.assertIsNone(document["extra"])
        self.assertIn("apt/snap/flatpak packages", document["missing"][0]["why"])


class PlanRenderTests(UiCase):
    def plan(self) -> Plan:
        missing, modified = fake_plan()
        return Plan.build([*missing, *modified])

    def test_plain_render_is_unchanged(self) -> None:
        self.assertEqual(
            self.plan().render().splitlines()[:2],
            [
                "Plan: 5 action(s) to run, 0 already satisfied, 3 need root",
                "  1. [sudo] [package] apt: install zsh, 7zip, xclip [base-tools, zsh]",
            ],
        )

    def test_rich_render_has_badges_sudo_marks_and_reasons_but_no_kinds(self) -> None:
        ui.configure(color="always")
        text = strip(self.plan().render(no_sudo=True))
        lines = text.splitlines()
        self.assertEqual(lines[0], "What will change")
        self.assertEqual(lines[1], "  5 to do, 0 already satisfied, 3 need sudo")
        self.assertIn("… [sudo, skipped: --no-sudo] apt: install zsh", text)
        self.assertIn("why: apt/snap/flatpak packages that your modules or profile declare", text)
        self.assertIn("(module base-tools)", text)
        self.assertNotIn("[package]", text)

    def test_rich_render_of_an_empty_plan_says_everything_is_in_place(self) -> None:
        ui.configure(color="always")
        self.assertEqual(
            strip(Plan(satisfied=7).render()), "✓ ok Nothing to do: all 7 declared items are already in place."
        )

    def test_manual_and_skipped_blocks_say_how_to_finish(self) -> None:
        ui.configure(color="always")
        action = FakeAction("gh-login", "gh: log in")
        result = ExecutionResult(
            0, (action,), ((action, "gh auth login (no token; store a token with `keychain set x`)"),)
        )
        manual, skipped = strip(result.render_manual()), strip(result.render_skipped())
        self.assertIn("… Manual steps (1)", manual)
        self.assertIn("1. gh auth login (no token; store a token with `keychain set x`)", manual)
        self.assertIn("Run again without --no-sudo", skipped)

    def test_every_action_has_a_default_reason_derived_from_its_kind(self) -> None:
        self.assertEqual(FakeAction("link", "x").why(), ui.kind_why("link"))

    def test_a_kind_without_wording_prints_no_dangling_why_line(self) -> None:
        ui.configure(color="always")
        text = strip(Plan.build([FakeAction("frobnicate", "do a thing", scope="m")]).render())
        self.assertNotIn("why:", text)
        self.assertIn("(module m)", text)
        self.assertNotIn("why:", strip(Plan.build([FakeAction("frobnicate", "do a thing")]).render()))

    def test_execute_calls_on_done_after_each_verified_action(self) -> None:
        class Counter(FakeAction):
            def __init__(self) -> None:
                super().__init__("link", "x")
                self.done = False

            def check(self) -> bool:
                return self.done

            def run(self) -> None:
                self.done = True

        seen: list[str] = []
        plan = Plan((Counter(),))
        result = plan.execute(on_start=lambda a: seen.append("start"), on_done=lambda a: seen.append("done"))
        self.assertEqual((result.changed, seen), (1, ["start", "done"]))


class DecorateTests(UiCase):
    def test_decorate_is_the_identity_without_color(self) -> None:
        text = "## SUMMARY\nclasses: secret 1"
        self.assertEqual(ui.decorate(text, "capture"), text)

    def test_decorate_colors_headings_and_counts_with_color(self) -> None:
        ui.configure(color="always")
        out = ui.decorate("## SUMMARY\nclasses: secret 1\nplain line", "capture")
        self.assertEqual(out.splitlines()[0], "\x1b[1m## SUMMARY\x1b[0m")
        self.assertEqual(out.splitlines()[1], "\x1b[36mclasses: secret 1\x1b[0m")
        self.assertEqual(out.splitlines()[2], "plain line")
        self.assertEqual(strip(out), "## SUMMARY\nclasses: secret 1\nplain line")

    def test_capture_class_help_names_every_class(self) -> None:
        from ubuntu_tweaks.capture.rules import SKIP_CLASSES

        ui.configure(color="never")
        text = "\n".join(ui.capture_class_help())
        for name in SKIP_CLASSES:
            self.assertIn(name, text)

    def test_every_family_colors_the_lines_it_was_written_for(self) -> None:
        ui.configure(color="always")
        samples = {
            "show": "═══ Питание [power] — 1 ═══",
            "machine": "modules:",
            "bundles": "CONFLICTS (different values in two bundles of one layer; apply refuses with exit 2):",
            "bundle": "# bundle power merged for machine home: shared",
            "capture": "### captured, per destination bundle",
        }
        self.assertEqual(set(samples), set(ui._DECORATIONS))
        for family, line in samples.items():
            self.assertNotEqual(ui.decorate(line, family), line, family)


if __name__ == "__main__":
    unittest.main()

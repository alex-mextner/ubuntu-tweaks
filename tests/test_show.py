"""``ubuntu-tweak show``: value formats (golden), secret masking, a FakeBackend scenario, the renderers, the
extras and the ssh runner. No live system access: the backend is a FakeBackend, commands are fake runners."""

from __future__ import annotations

import contextlib
import io
import re
import unittest
from unittest import mock

from tests.util import TempDirCase
from ubuntu_tweaks.capture.rules import load_rules
from ubuntu_tweaks.cli import main
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema
from ubuntu_tweaks.show import ssh as ssh_module
from ubuntu_tweaks.show.catalog import KeyMeta, StaticCatalog
from ubuntu_tweaks.show.collect import collect_panels
from ubuntu_tweaks.show.descriptions import load_descriptions
from ubuntu_tweaks.show.html_page import render_html
from ubuntu_tweaks.show.model import HIDDEN, Report
from ubuntu_tweaks.show.render import render_markdown, render_text
from ubuntu_tweaks.show.system import ModuleState, collect_extras, describe_monitors, parse_mimeapps
from ubuntu_tweaks.show.values import (
    ValueSpec,
    format_accel,
    format_duration,
    format_duration_ms,
    humanize,
    normalize_color,
)

SECRET = (
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
)  # shaped like a token; built in two parts so no scanner trips on the test


class ValueFormatTests(unittest.TestCase):
    def test_booleans_are_on_off(self) -> None:
        self.assertEqual(humanize(True, ValueSpec()).lines, ("Вкл",))
        self.assertEqual(humanize(False, ValueSpec()).lines, ("Выкл",))

    def test_durations(self) -> None:
        cases = {
            0: "0 с",
            45: "45 с",
            60: "1 мин",
            90: "1 мин 30 с",
            300: "5 мин",
            3600: "1 ч",
            3900: "1 ч 5 мин",
            7200: "2 ч",
        }
        for seconds, text in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), text)
        self.assertEqual(format_duration_ms(250), "250 мс")
        self.assertEqual(format_duration_ms(1500), "1.5 с")
        self.assertEqual(format_duration_ms(120000), "2 мин")

    def test_zero_text_and_units(self) -> None:
        self.assertEqual(humanize(0, ValueSpec("seconds", zero="никогда")).lines, ("никогда",))
        self.assertEqual(humanize(900, ValueSpec("seconds")).lines, ("15 мин",))
        self.assertEqual(humanize(80, ValueSpec("percent")).lines, ("80 %",))
        self.assertEqual(humanize(1700000000, ValueSpec("unix")).lines, ("2023-11-14 22:13 UTC",))
        self.assertEqual(humanize(2048, ValueSpec("bytes")).lines, ("2.0 КБ",))
        self.assertEqual(humanize((1200, 800), ValueSpec("size")).lines, ("1200 × 800 пикс.",))

    def test_lists_are_bullets_and_empty_lists_say_so(self) -> None:
        self.assertEqual(humanize(["a", "b"], ValueSpec()).lines, ("a", "b"))
        self.assertEqual(humanize([], ValueSpec()).lines, ("(пусто)",))
        self.assertEqual(humanize([], ValueSpec(accel=True)).lines, ("не назначено",))
        self.assertEqual(humanize({"b": 1, "a": True}, ValueSpec()).lines, ("a: Вкл", "b: 1"))

    def test_enum_values_carry_their_meaning(self) -> None:
        spec = ValueSpec(enums={"prefer-dark": "тёмная"})
        self.assertEqual(humanize("prefer-dark", spec).lines, ("prefer-dark — тёмная",))
        self.assertEqual(humanize("other", spec).lines, ("other",))

    def test_keybindings_are_readable_key_combinations(self) -> None:
        cases = {
            "<Super>Left": "Super + ←",
            "<Primary><Alt>t": "Ctrl + Alt + T",
            "<Shift><Control>Page_Up": "Shift + Ctrl + PageUp",
            "<Super>space": "Super + Пробел",
            "": "не назначено",
            "disabled": "отключено",
            "F11": "F11",
        }
        for accel, text in cases.items():
            with self.subTest(accel=accel):
                self.assertEqual(format_accel(accel), text)
        self.assertEqual(humanize(["<Super>Left", "<Super>a"], ValueSpec(accel=True)).lines, ("Super + ←", "Super + A"))

    def test_colors_are_normalized_and_get_a_swatch(self) -> None:
        self.assertEqual(normalize_color("#1E1E2E"), "#1e1e2e")
        self.assertEqual(normalize_color("#1e1e2eff"), "#1e1e2e")
        self.assertEqual(normalize_color("#ffff00000000"), "#ff0000")
        self.assertEqual(normalize_color("rgb(0, 128, 255)"), "#0080ff")
        self.assertIsNone(normalize_color("rgb(0, 300, 255)"))
        self.assertIsNone(normalize_color("blue"))
        shown = humanize("#1e1e2e", ValueSpec())
        self.assertEqual((shown.lines, shown.swatch), (("#1e1e2e (цвет)",), "#1e1e2e"))

    def test_long_strings_are_truncated_and_empty_ones_named(self) -> None:
        self.assertEqual(humanize("", ValueSpec()).lines, ("(пусто)",))
        self.assertEqual(len(humanize("x" * 500, ValueSpec()).lines[0]), 160)


class DescriptionsTests(TempDirCase):
    def test_the_shipped_file_loads_and_every_panel_has_a_title(self) -> None:
        texts = load_descriptions()
        for panel_id in (*load_rules().panels, "other"):
            with self.subTest(panel=panel_id):
                self.assertIsNotNone(texts.panel(panel_id))

    def test_an_exact_entry_beats_a_rule_and_keybinding_schemas_are_detected(self) -> None:
        texts = load_descriptions()
        found = texts.lookup("org.gnome.desktop.session", "idle-delay", "/org/gnome/desktop/session/idle-delay")
        self.assertEqual((found.spec.unit, found.spec.zero is not None), ("seconds", True))
        keybinding = texts.lookup("org.gnome.desktop.wm.keybindings", "show-desktop", "/x/show-desktop")
        self.assertTrue(keybinding.spec.accel)
        family = texts.lookup(None, "window-size", "/org/example/app/window-size")
        self.assertEqual(family.spec.unit, "size")
        self.assertIsNone(texts.lookup(None, "nothing", "/org/example/nothing").ru)

    def test_a_bad_file_is_invalid_config(self) -> None:
        bad = {
            "[[rule]]\nru = 'x'\n": "paths",
            "[keys.s]\nk = { unit = 'furlongs' }\n": "unit",
            "[keys.s]\nk = { ru = 1 }\n": "ru",
            "[panels.a]\ntitle = 'x'\n": "title and about",
            "[enums.s]\nk = { a = 1 }\n": "map nick",
        }
        for text, needle in bad.items():
            with self.subTest(needle=needle):
                path = self.tmp / "d.toml"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError) as caught:
                    load_descriptions(path)
                self.assertIn(needle, str(caught.exception))


def scenario_backend() -> FakeBackend:
    backend = FakeBackend(
        [
            FakeSchema(
                "org.gnome.desktop.session", {"idle-delay": FakeKey("u", "uint32 300")}, "/org/gnome/desktop/session/"
            ),
            FakeSchema(
                "org.gnome.desktop.interface",
                {
                    "color-scheme": FakeKey("s", "'default'"),
                    "gtk-theme": FakeKey("s", "'Yaru'"),
                    "accent-color": FakeKey("s", "'blue'"),
                },
                "/org/gnome/desktop/interface/",
            ),
            FakeSchema(
                "org.gnome.desktop.wm.keybindings",
                {"show-desktop": FakeKey("as", "['<Super>d']")},
                "/org/gnome/desktop/wm/keybindings/",
            ),
            FakeSchema(
                "org.gnome.desktop.remote-desktop.rdp",
                {"tls-key": FakeKey("s", "''")},
                "/org/gnome/desktop/remote-desktop/rdp/",
            ),
            FakeSchema("org.gnome.mutter", {"edge-tiling": FakeKey("b", "false")}, "/org/gnome/mutter/"),
            FakeSchema("org.gnome.desktop.notifications.application", {"application-id": FakeKey("s", "''")}, None),
        ]
    )
    backend.set_user("org.gnome.desktop.session", None, "idle-delay", "uint32 900")
    backend.set_user("org.gnome.desktop.interface", None, "color-scheme", "'prefer-dark'")
    backend.set_user("org.gnome.desktop.interface", None, "gtk-theme", "'Yaru'")  # user value equal to the default
    return backend


USER_DUMP = {
    "org/gnome/desktop/session": {"idle-delay": "uint32 900"},
    "org/gnome/desktop/interface": {"color-scheme": "'prefer-dark'", "gtk-theme": "'Yaru'"},
    "org/gnome/desktop/wm/keybindings": {"show-desktop": "['<Super>Left']"},
    "org/gnome/desktop/remote-desktop/rdp": {"tls-key": f"'{SECRET}'"},
    "org/gnome/mutter": {"edge-tiling": "true"},
    "org/example/appwithoutschema": {"window-size": "(1200, 800)", "token": f"'{SECRET}'"},
    "org/gnome/desktop/notifications/application/a": {"application-id": "'a.desktop'"},
    "org/gnome/desktop/notifications/application/b": {"application-id": "'b.desktop'"},
    "org/gnome/desktop/notifications/application/c": {"application-id": "'c.desktop'"},
    "org/gnome/desktop/notifications/application/d": {"application-id": "'d.desktop'"},
}
OVERRIDE = {"org/gnome/desktop/interface": {"accent-color": "'red'"}}


def scenario(show_all: bool = False, only_panel: str | None = None):
    backend = scenario_backend()
    for letter in "abcd":
        backend.set_user(
            "org.gnome.desktop.notifications.application",
            f"/org/gnome/desktop/notifications/application/{letter}/",
            "application-id",
            f"'{letter}.desktop'",
        )
    catalog = StaticCatalog(
        {
            ("org.gnome.mutter", "edge-tiling"): KeyMeta("Enable edge tiling"),
            ("org.gnome.desktop.interface", "accent-color"): KeyMeta("Accent color"),
        }
    )
    return collect_panels(
        user=USER_DUMP,
        override=OVERRIDE,
        backend=backend,
        catalog=catalog,
        rules=load_rules(),
        texts=load_descriptions(),
        show_all=show_all,
        only_panel=only_panel,
    )


def rows_by_path(panels) -> dict:
    return {row.path: row for panel in panels for row in panel.rows}


class CollectTests(unittest.TestCase):
    def test_changed_mode_lists_changed_and_overridden_keys_in_their_panels(self) -> None:
        panels, untouched, counts = scenario()
        by_id = {panel.id: panel for panel in panels}
        self.assertEqual(
            {"power", "appearance", "keyboard", "multitasking", "sharing", "notifications", "other"}, set(by_id)
        )
        idle = by_id["power"].rows[0]
        self.assertEqual((idle.status, idle.value, idle.default), ("changed", ("15 мин",), ("5 мин",)))
        scheme = next(row for row in by_id["appearance"].rows if row.key == "color-scheme")
        self.assertEqual(scheme.value, ("prefer-dark — тёмная",))
        self.assertEqual(scheme.default, ("default — по умолчанию (светлая)",))
        self.assertIn("Светлая или тёмная тема", scheme.explanation)
        self.assertTrue(scheme.translated)
        self.assertIn("Многозадачность", {texts for texts in (p.title for p in panels)})
        self.assertEqual(counts["changed"] + counts["override"], counts["keys"])
        self.assertNotIn("Питание", untouched)
        self.assertIn("Принтеры", untouched)

    def test_a_user_value_equal_to_the_default_is_not_a_change(self) -> None:
        rows = rows_by_path(scenario()[0])
        self.assertNotIn("/org/gnome/desktop/interface/gtk-theme", rows)
        all_rows = rows_by_path(scenario(show_all=True)[0])
        self.assertEqual(all_rows["/org/gnome/desktop/interface/gtk-theme"].status, "default")

    def test_a_key_set_only_by_the_override_file_is_marked_as_such(self) -> None:
        row = rows_by_path(scenario()[0])["/org/gnome/desktop/interface/accent-color"]
        self.assertEqual((row.status, row.value), ("override", ("red — красный",)))
        self.assertEqual(row.default, row.value)  # the override replaces the schema default

    def test_keybindings_are_key_combinations(self) -> None:
        row = rows_by_path(scenario()[0])["/org/gnome/desktop/wm/keybindings/show-desktop"]
        self.assertEqual((row.value, row.default), (("Super + ←",), ("Super + D",)))

    def test_secret_keys_are_hidden_and_never_leave_a_value_behind(self) -> None:
        panels, _, counts = scenario()
        rows = rows_by_path(panels)
        for path in ("…/tls-key", "…/token"):  # a hidden row keeps its key name only, never its directory
            with self.subTest(path=path):
                row = rows[path]
                self.assertEqual((row.hidden, row.value, row.default), (True, (HIDDEN,), (HIDDEN,)))
        self.assertEqual(counts["hidden"], 2)
        report = Report("t", "локально", "changed", panels, (), counts)
        for text in (render_text(report), render_markdown(report), render_html(report)):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("ghp_", text)
            self.assertNotIn("appwithoutschema/token", text)

    def test_keys_without_a_schema_are_listed_as_raw_with_the_family_text(self) -> None:
        row = rows_by_path(scenario()[0])["/org/example/appwithoutschema/window-size"]
        self.assertEqual((row.schema, row.value, row.status), (None, ("1200 × 800 пикс.",), "changed"))
        self.assertIn("Размер окна", row.explanation)

    def test_instances_of_a_relocatable_schema_fold_into_one_row(self) -> None:
        panels, _, _ = scenario()
        notifications = next(panel for panel in panels if panel.id == "notifications")
        (row,) = notifications.rows
        self.assertEqual(row.value, ("a: a.desktop", "b: b.desktop", "c: c.desktop", "d: d.desktop"))
        self.assertTrue(row.path.endswith("× 4"))

    def test_a_key_without_hand_written_text_falls_back_to_the_schema_summary_with_a_marker(self) -> None:
        row = rows_by_path(scenario()[0])["/org/gnome/mutter/edge-tiling"]
        self.assertIn("Прижатие окна к краю", row.explanation)  # hand-written text exists for this one
        catalog = StaticCatalog({("org.gnome.desktop.session", "idle-delay"): KeyMeta("Idle time")})
        texts = load_descriptions()
        texts_without = type(texts)(texts.panels, {}, {}, ())
        panels, _, _ = collect_panels(
            user={"org/gnome/desktop/session": {"idle-delay": "uint32 900"}},
            override={},
            backend=scenario_backend(),
            catalog=catalog,
            rules=load_rules(),
            texts=texts_without,
        )
        (marked,) = panels[0].rows
        self.assertFalse(marked.translated)
        self.assertEqual(marked.explanation, "Idle time (описание из схемы, без перевода)")

    def test_panel_filter_and_all_mode(self) -> None:
        panels, untouched, _ = scenario(only_panel="power")
        self.assertEqual([panel.id for panel in panels], ["power"])
        self.assertEqual(untouched, ())  # a single panel asked for: nothing is "left out"
        all_panels, _, counts = scenario(show_all=True)
        self.assertGreater(counts["default"], 0)
        self.assertIn("/org/gnome/mutter/edge-tiling", rows_by_path(all_panels))


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        panels, untouched, counts = scenario()
        extras = collect_extras(
            lambda argv: None, [ModuleState("hyperdock", "submodule", True, "gnome-extensions/hyperdock")]
        )
        self.report = Report("home", "локально", "changed", panels, extras, counts, untouched)

    def test_text_has_statuses_values_defaults_and_bullets(self) -> None:
        text = render_text(self.report)
        self.assertIn("изменено вами", text)
        self.assertIn("задано файлом-переопределением", text)
        self.assertIn("Сейчас: 15 мин", text)
        self.assertIn("По умолчанию: 5 мин", text)
        self.assertIn("    • a: a.desktop", text)
        self.assertIn("Сейчас: скрыто", text)
        self.assertIn("hyperdock", text)

    def test_markdown_has_headings_and_code_values(self) -> None:
        md = render_markdown(self.report)
        self.assertIn("## Питание (`power`)", md)
        self.assertIn("- **Сейчас:** `15 мин`", md)
        self.assertIn("  - `a: a.desktop`", md)

    def test_html_is_one_self_contained_page(self) -> None:
        page = render_html(self.report)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn("<details", page)
        self.assertIn('href="#panel-power"', page)
        self.assertNotRegex(page, r"(?i)<script|<link|<img|https?://|@import|url\(")
        self.assertEqual(page.count("<details"), page.count("</details>"))

    def test_html_escapes_text(self) -> None:
        report = Report("<b>x</b>", "локально", "all", (), (), self.report.counts)
        page = render_html(report)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)


MIMEAPPS = """[Default Applications]
text/html=firefox.desktop;
image/png=org.gnome.Loupe.desktop;

[Added Associations]
text/html=firefox.desktop;

[Removed Associations]
"""
MONITORS = """<monitors version="2"><configuration><logicalmonitor><x>0</x><y>0</y><scale>2</scale><primary>yes</primary>
<monitor><monitorspec><connector>eDP-1</connector><vendor>AUO</vendor><product>0x1234</product><serial>SERIAL-SECRET</serial></monitorspec>
<mode><width>2560</width><height>1600</height><rate>60.0</rate></mode></monitor></logicalmonitor>
<disabled><monitorspec><connector>HDMI-1</connector><vendor>X</vendor><product>Y</product><serial>Z</serial></monitorspec></disabled>
</configuration></monitors>"""


def fake_runner(answers: dict[str, str]):
    def run(argv):
        return answers.get(" ".join(argv))

    return run


class ExtrasTests(unittest.TestCase):
    def test_mimeapps_parsing(self) -> None:
        parsed = parse_mimeapps(MIMEAPPS)
        self.assertEqual(parsed["Default Applications"]["text/html"], ["firefox.desktop"])
        self.assertEqual(parsed["Removed Associations"], {})

    def test_monitors_summary_has_no_serial_numbers(self) -> None:
        lines = describe_monitors(MONITORS)
        self.assertEqual(
            lines[0], "конфигурация 1: eDP-1 (AUO 0x1234), 2560×1600 @ 60.00 Гц, масштаб 2, позиция (0, 0), основной"
        )
        self.assertEqual(lines[1], "конфигурация 1: отключён HDMI-1")
        self.assertNotIn("SECRET", "".join(lines))
        self.assertEqual(describe_monitors("<oops"), ["monitors.xml не удалось разобрать"])

    def test_sections_from_a_fake_machine(self) -> None:
        run = fake_runner(
            {
                'sh -c cat "$HOME/.config/mimeapps.list"': MIMEAPPS,
                'sh -c cat "$HOME/.config/monitors.xml"': MONITORS,
                "timedatectl show": "Timezone=Europe/Belgrade\nNTP=yes\nNTPSynchronized=no\nLocalRTC=no\n",
                "localectl status": "   System Locale: LANG=en_US.UTF-8\n       VC Keymap: us\n      X11 Layout: us,ru\n",
                "apt-mark showmanual": "a\nb\nc\n",
                "snap list": "Name Version\nfirefox 1\n",
            }
        )
        files, system, packages = collect_extras(run, None)
        mime = next(entry for entry in files.entries if entry.label.startswith("Приложения по умолчанию"))
        self.assertEqual(mime.lines[0], "image/png → org.gnome.Loupe.desktop")
        autostart = next(entry for entry in files.entries if entry.label == "Автозапуск")
        self.assertEqual(autostart.lines, ("нет записей в ~/.config/autostart",))
        values = {entry.label: entry.lines for entry in system.entries}
        self.assertEqual(values["Часовой пояс"], ("Europe/Belgrade",))
        self.assertEqual(values["Синхронизация времени (NTP) включена"], ("Вкл",))
        self.assertEqual(values["Часы синхронизированы"], ("Выкл",))
        self.assertEqual(values["Язык системы (locale)"], ("LANG=en_US.UTF-8",))
        self.assertEqual(values["Раскладки X11"], ("us,ru",))
        counts = {entry.label: entry.lines for entry in packages.entries}
        self.assertEqual(counts["apt: поставлено вручную"], ("3",))
        self.assertEqual(counts["snap"], ("1",))
        self.assertEqual(counts["apt: установлено всего"], ("нет данных",))

    def test_autostart_entries_show_state_not_commands(self) -> None:
        listing = "./a.desktop:Seen=1\n./a.desktop:Name=Alpha\n./b.desktop:Seen=1\n./b.desktop:Name=Beta\n./b.desktop:Hidden=true\n./c.desktop:Seen=1\n"

        def run(argv):
            return listing if argv[:2] == ["sh", "-c"] and ".config/autostart" in argv[2] else None

        files = collect_extras(run, None)[0]
        shown = {entry.label: entry.lines for entry in files.entries}
        self.assertEqual(shown["Alpha"], ("a.desktop: включён",))
        self.assertEqual(shown["Beta"], ("b.desktop: выключен",))

    def test_modules_only_when_given(self) -> None:
        self.assertEqual(len(collect_extras(lambda argv: None, None)), 3)
        modules = collect_extras(lambda argv: None, [ModuleState("memguard", "system", False, "modules/memguard")])[3]
        self.assertEqual(modules.entries[0].lines[0], "Выкл, вид: system")


class SshRunnerTests(unittest.TestCase):
    def test_the_probe_is_one_quoted_ssh_command_in_batch_mode(self) -> None:
        done = mock.Mock(returncode=0, stdout="out")
        with mock.patch.object(ssh_module.subprocess, "run", return_value=done) as patched:
            result = ssh_module.ssh_runner("ultra@host", ["StrictHostKeyChecking=accept-new"])(["cat", "/a b"])
        self.assertEqual(result, "out")
        argv = patched.call_args.args[0]
        self.assertEqual(argv[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
        self.assertEqual(argv[5:7], ["-o", "StrictHostKeyChecking=accept-new"])
        self.assertEqual(argv[7:], ["ultra@host", "cat '/a b'"])

    def test_failures_are_no_data(self) -> None:
        with mock.patch.object(ssh_module.subprocess, "run", return_value=mock.Mock(returncode=255, stdout="")):
            self.assertIsNone(ssh_module.ssh_runner("host")(["true"]))
        with mock.patch.object(ssh_module.subprocess, "run", side_effect=OSError):
            self.assertIsNone(ssh_module.ssh_runner("host")(["true"]))

    def test_bad_targets_and_options_are_invalid_config(self) -> None:
        for target in ("", "-oProxyCommand=x", "a b"):
            with self.subTest(target=target), self.assertRaises(ConfigError):
                ssh_module.ssh_runner(target)
        with self.assertRaises(ConfigError):
            ssh_module.ssh_runner("host", ["ProxyCommand"])
        self.assertEqual(ssh_module.target_host("ultra@100.1.2.3"), "100.1.2.3")


class CommandTests(unittest.TestCase):
    def test_an_unknown_panel_is_invalid_config(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["show", "--panel", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown panel 'nope'", err.getvalue())

    def test_changed_and_all_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["show", "--changed", "--all"])
        self.assertEqual(caught.exception.code, 2)

    def test_every_key_text_names_a_russian_explanation(self) -> None:
        texts = load_descriptions()
        for pair, entry in texts.keys.items():
            with self.subTest(key=pair):
                self.assertTrue(entry.ru and re.search("[А-Яа-я]", entry.ru))


class RobustnessTests(TempDirCase):
    def test_an_out_of_range_timestamp_does_not_abort_the_report(self) -> None:
        shown = humanize(1_700_000_000_000_000, ValueSpec("unix")).lines[0]  # microseconds, not seconds
        self.assertIn("не похоже на время", shown)
        self.assertEqual(humanize(-5, ValueSpec("unix")).lines, ("никогда",))

    def test_a_malformed_monitor_rate_is_left_out(self) -> None:
        xml = MONITORS.replace("<rate>60.0</rate>", "<rate>fast</rate>")
        self.assertIn("2560×1600, масштаб 2", describe_monitors(xml)[0])

    def test_an_exact_entry_and_a_rule_merge_per_field(self) -> None:
        path = self.tmp / "d.toml"
        path.write_text(
            "[keys.s]\nk = { unit = 'seconds' }\n[[rule]]\npaths = ['/x/*']\nru = 'Текст семейства'\nzero = 'нет'\n",
            encoding="utf-8",
        )
        found = load_descriptions(path).lookup("s", "k", "/x/k")
        self.assertEqual((found.ru, found.spec.unit, found.spec.zero), ("Текст семейства", "seconds", "нет"))

    def test_tiling_assistant_shortcuts_are_key_combinations(self) -> None:
        found = load_descriptions().lookup("org.gnome.shell.extensions.tiling-assistant", "tile-left-half", "/x")
        self.assertTrue(found.spec.accel)

    def test_a_color_swatch_is_drawn_only_next_to_a_single_value(self) -> None:
        from ubuntu_tweaks.show.html_page import _lines

        self.assertIn('class="sw"', _lines(("#ff0000 (цвет)",), "#ff0000"))
        self.assertNotIn('class="sw"', _lines(("#ff0000 (цвет)", "image/png"), "#ff0000"))

    def test_removed_packages_are_not_counted_as_installed(self) -> None:
        run = fake_runner({"dpkg-query -W -f=${db:Status-Abbrev} ${binary:Package}\\n": "ii  a\nrc  b\nii  c\n"})
        packages = collect_extras(run, None)[2]
        self.assertEqual(packages.entries[0].lines, ("2",))

    def test_a_group_of_three_instances_is_not_folded(self) -> None:
        backend = scenario_backend()
        rows = {}
        for letter in "abc":
            backend.set_user(
                "org.gnome.desktop.notifications.application",
                f"/org/gnome/desktop/notifications/application/{letter}/",
                "application-id",
                f"'{letter}.desktop'",
            )
            rows[f"org/gnome/desktop/notifications/application/{letter}"] = {"application-id": f"'{letter}.desktop'"}
        panels, _, counts = collect_panels(
            user=rows,
            override={},
            backend=backend,
            catalog=StaticCatalog({}),
            rules=load_rules(),
            texts=load_descriptions(),
        )
        self.assertEqual(len(panels[0].rows), 3)
        self.assertEqual(counts["keys"], 3)

    def test_a_value_that_does_not_fit_the_local_schema_still_appears(self) -> None:
        panels, _, counts = collect_panels(
            user={"org/gnome/desktop/session": {"idle-delay": "'soon'"}},  # the local schema says uint32
            override={},
            backend=scenario_backend(),
            catalog=StaticCatalog({}),
            rules=load_rules(),
            texts=load_descriptions(),
        )
        (row,) = panels[0].rows
        self.assertEqual((row.status, row.value), ("changed", ("soon",)))
        self.assertEqual(counts["keys"], 1)

    def test_a_folded_row_takes_its_default_from_a_visible_member(self) -> None:
        backend = scenario_backend()
        user = {}
        for letter in "abcd":
            path = f"/org/gnome/desktop/notifications/application/{letter}/"
            text = f"'{SECRET}'" if letter == "a" else f"'{letter}.desktop'"
            backend.set_user("org.gnome.desktop.notifications.application", path, "application-id", text)
            user[path.strip("/")] = {"application-id": text}
        panels, _, _ = collect_panels(
            user=user,
            override={},
            backend=backend,
            catalog=StaticCatalog({}),
            rules=load_rules(),
            texts=load_descriptions(),
        )
        (row,) = panels[0].rows
        self.assertEqual(row.value[0], HIDDEN)
        self.assertNotEqual(row.default, (HIDDEN,))
        self.assertNotIn(SECRET, "".join(row.value + row.default))

    def test_the_summary_counts_the_rows_on_the_page(self) -> None:
        panels, _, counts = scenario()
        self.assertEqual(counts["keys"], sum(len(panel.rows) for panel in panels))

    def test_the_report_is_written_private_and_never_through_a_symlink(self) -> None:
        from ubuntu_tweaks.commands import show as show_command
        from ubuntu_tweaks.process import CommandError

        panels, untouched, counts = scenario()
        report = Report("t", "локально", "changed", panels, (), counts, untouched)
        args = mock.Mock(format="html", output=str(self.tmp / "out.html"))
        with mock.patch.object(show_command, "build", return_value=report), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(show_command.run(mock.Mock(), args), 0)
            self.assertEqual((self.tmp / "out.html").stat().st_mode & 0o777, 0o600)
            victim = self.tmp / "victim"
            victim.write_text("keep", encoding="utf-8")
            (self.tmp / "link.html").symlink_to(victim)
            args.output = str(self.tmp / "link.html")
            with self.assertRaises(CommandError):
                show_command.run(mock.Mock(), args)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()

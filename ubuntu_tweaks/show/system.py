"""The parts of a machine's setup that do not live in dconf, for ``ubuntu-tweak show``. Read-only.

Accessed via: ``commands/show.py``. Everything is read through a ``Runner`` (``capture.system_scan``:
argv in, stdout or ``None`` out), which is the local shell or ``ssh <host>``, so the sections are the
same for both and tests need no system tools. A missing tool or file is "нет данных", never an error.

Sections: *Файлы* (default applications from ``mimeapps.list``, monitors from ``monitors.xml`` without
serial numbers, autostart entries by name and state; their commands are not shown), *Система* (timezone,
time sync, locale and keyboard layout from ``timedatectl show`` / ``localectl status``), *Пакеты* (counts),
*Модули* (the umbrella's modules; local runs only).

Public API: ``collect_extras(run, modules) -> tuple[ExtraSection, ...]``, ``parse_mimeapps``,
``describe_monitors`` (pure, golden-tested).
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass

from ubuntu_tweaks.capture.system_scan import Runner
from ubuntu_tweaks.show.model import Entry, ExtraSection
from ubuntu_tweaks.show.values import BOOL_FALSE, BOOL_TRUE

NO_DATA = "нет данных"
_MAX_MIME_LINES = 60


@dataclass(frozen=True)
class ModuleState:
    name: str
    kind: str
    enabled: bool
    source: str


def _home_file(run: Runner, relative: str) -> str | None:
    return run(["sh", "-c", f'cat "$HOME/{relative}"'])


def parse_mimeapps(text: str) -> dict[str, dict[str, list[str]]]:
    """``{section: {mime type: [desktop ids]}}`` of a ``mimeapps.list``."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif "=" in line and current is not None:
            mime, _, apps = line.partition("=")
            current[mime.strip()] = [app for app in apps.split(";") if app]
    return sections


def _mimeapps_entries(text: str | None) -> list[Entry]:
    if not text:
        return [Entry("Приложения по умолчанию", (NO_DATA,))]
    sections = parse_mimeapps(text)
    entries: list[Entry] = []
    defaults = sections.get("Default Applications", {})
    lines = [f"{mime} → {', '.join(apps)}" for mime, apps in sorted(defaults.items())]
    if len(lines) > _MAX_MIME_LINES:
        lines = [*lines[:_MAX_MIME_LINES], f"… и ещё {len(lines) - _MAX_MIME_LINES}"]
    entries.append(Entry(f"Приложения по умолчанию ({len(defaults)})", tuple(lines) or ("(нет записей)",)))
    added = sections.get("Added Associations", {})
    removed = sections.get("Removed Associations", {})
    entries.append(Entry("Добавленные / убранные привязки", (f"добавлено: {len(added)}", f"убрано: {len(removed)}")))
    return entries


def _resolution(mode: ElementTree.Element | None) -> str:
    if mode is None:
        return "режим не задан"
    try:
        rate = f" @ {float(mode.findtext('rate') or 0):.2f} Гц"
    except ValueError:
        rate = ""
    width, height = (text if (text := mode.findtext(name) or "").isdigit() else "?" for name in ("width", "height"))
    return f"{width}×{height}{rate}"


def describe_monitors(xml_text: str) -> list[str]:
    """One line per configured monitor of a ``monitors.xml``: connector, maker, mode, scale, position."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return ["monitors.xml не удалось разобрать"]
    lines: list[str] = []
    for index, configuration in enumerate(root.iter("configuration"), start=1):
        for logical in configuration.iter("logicalmonitor"):
            primary = (logical.findtext("primary") or "") == "yes"
            for monitor in logical.iter("monitor"):
                spec = monitor.find("monitorspec")
                mode = monitor.find("mode")
                connector = spec.findtext("connector") if spec is not None else "?"
                maker = " ".join(
                    part
                    for part in ((spec.findtext("vendor"), spec.findtext("product")) if spec is not None else ())
                    if part
                )
                resolution = _resolution(mode)
                lines.append(
                    f"конфигурация {index}: {connector}"
                    + (f" ({maker})" if maker else "")
                    + f", {resolution}, масштаб {logical.findtext('scale') or '1'}, "
                    + f"позиция ({logical.findtext('x') or 0}, {logical.findtext('y') or 0})"
                    + (", основной" if primary else "")
                )
        lines.extend(
            f"конфигурация {index}: отключён {disabled.findtext('monitorspec/connector')}"
            for disabled in configuration.iter("disabled")
        )
    return lines or ["мониторы не настроены"]


def _autostart_entries(run: Runner) -> list[Entry]:
    output = run(
        [
            "sh",
            "-c",
            'cd "$HOME/.config/autostart" 2>/dev/null && for f in ./*.desktop; do [ -e "$f" ] || continue; '
            'echo "$f:Seen=1"; grep -H -E "^(Name|Hidden|X-GNOME-Autostart-enabled)=" "$f"; done; true',
        ]
    )
    if not output:
        return [Entry("Автозапуск", ("нет записей в ~/.config/autostart",))]
    apps: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        name, _, rest = line.partition(":")
        key, _, value = rest.partition("=")
        apps.setdefault(name.removeprefix("./"), {})[key] = value
    entries = []
    for file, fields in sorted(apps.items()):
        enabled = fields.get("Hidden") != "true" and fields.get("X-GNOME-Autostart-enabled") != "false"
        entries.append(Entry(fields.get("Name", file), (f"{file}: {'включён' if enabled else 'выключен'}",)))
    return entries


def _files_section(run: Runner) -> ExtraSection:
    entries = _mimeapps_entries(_home_file(run, ".config/mimeapps.list"))
    monitors = _home_file(run, ".config/monitors.xml")
    entries.append(Entry("Мониторы (monitors.xml)", tuple(describe_monitors(monitors)) if monitors else (NO_DATA,)))
    entries.extend(_autostart_entries(run))
    return ExtraSection(
        "files",
        "Файлы настроек",
        "Настройки, которые лежат не в dconf, а в файлах домашней папки.",
        tuple(entries),
    )


_YES_NO = {"yes": BOOL_TRUE, "no": BOOL_FALSE}


def _pairs(output: str | None, separator: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in (output or "").splitlines():
        key, found_separator, value = line.partition(separator)
        if found_separator:
            found[key.strip()] = value.strip()
    return found


def _system_section(run: Runner) -> ExtraSection:
    time = _pairs(run(["timedatectl", "show"]), "=")
    locale = _pairs(run(["localectl", "status"]), ":")
    entries: list[Entry] = []
    for label, key in (
        ("Часовой пояс", "Timezone"),
        ("Синхронизация времени (NTP) включена", "NTP"),
        ("Часы синхронизированы", "NTPSynchronized"),
        ("Аппаратные часы в местном времени", "LocalRTC"),
    ):
        if key in time:
            entries.append(Entry(label, (_YES_NO.get(time[key], time[key]),)))
    for label, key in (
        ("Язык системы (locale)", "System Locale"),
        ("Раскладка консоли", "VC Keymap"),
        ("Раскладки X11", "X11 Layout"),
        ("Модель клавиатуры", "X11 Model"),
        ("Вариант раскладки", "X11 Variant"),
        ("Опции клавиатуры", "X11 Options"),
    ):
        if locale.get(key):
            entries.append(Entry(label, tuple(locale[key].split()) if key == "System Locale" else (locale[key],)))
    return ExtraSection(
        "system",
        "Время, язык и клавиатура",
        "Системные настройки (читаются через timedatectl и localectl).",
        tuple(entries) or (Entry("Время и язык", (NO_DATA,)),),
    )


def _count(output: str | None, skip_header: bool = False, installed_only: bool = False) -> str:
    """Number of lines; ``installed_only`` keeps dpkg-query rows whose status is ``ii`` (not removed-with-config)."""
    if output is None:
        return NO_DATA
    lines = [line for line in output.splitlines() if line.strip()]
    if installed_only:
        lines = [line for line in lines if line.startswith("ii ")]
    return str(max(len(lines) - (1 if skip_header else 0), 0))


def _packages_section(run: Runner) -> ExtraSection:
    return ExtraSection(
        "packages",
        "Пакеты",
        "Сколько всего программ установлено (имена не перечисляются; их список ведёт `ubuntu-tweak capture`).",
        (
            Entry(
                "apt: установлено всего",
                (
                    _count(
                        run(["dpkg-query", "-W", "-f=${db:Status-Abbrev} ${binary:Package}\\n"]), installed_only=True
                    ),
                ),
            ),
            Entry("apt: поставлено вручную", (_count(run(["apt-mark", "showmanual"])),)),
            Entry("snap", (_count(run(["snap", "list"]), skip_header=True),)),
        ),
    )


def _modules_section(modules: Sequence[ModuleState]) -> ExtraSection:
    return ExtraSection(
        "modules",
        "Модули ubuntu-tweaks",
        "Что umbrella-репозиторий ставит и поддерживает; включение настраивается в machines/<машина>/machine.toml.",
        tuple(
            Entry(m.name, (f"{BOOL_TRUE if m.enabled else BOOL_FALSE}, вид: {m.kind}", f"источник: {m.source}"))
            for m in modules
        ),
    )


def collect_extras(run: Runner, modules: Sequence[ModuleState] | None) -> tuple[ExtraSection, ...]:
    sections = [_files_section(run), _system_section(run), _packages_section(run)]
    if modules is not None:
        sections.append(_modules_section(modules))
    return tuple(sections)

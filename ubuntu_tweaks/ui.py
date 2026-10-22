"""Terminal presentation: colors, badges, aligned text and every explanatory sentence, in one place.

Accessed via: ``cli.main`` calls ``configure`` from the global ``--color`` / ``--plain`` / ``--lang``
flags; the commands, ``reconcile`` and ``actions.Plan.render`` call the helpers below. Stdlib only.

Two independent questions, both asked at call time of the *stream being written* (never cached):

- ``color_enabled(stream)``: may ANSI escapes be written? ``--plain`` and ``--color never`` say no,
  ``--color always`` says yes. ``--color auto`` (default) says yes only on a tty, never with ``NO_COLOR``
  (https://no-color.org, non-empty) or ``TERM=dumb``; ``CLICOLOR_FORCE`` (not ``0``) forces it on a pipe.
- ``rich(stream)``: the explained layout (headings, sections with definitions, badges). On when the stream is
  a tty (so ``NO_COLOR`` on a terminal still gets the explained layout, uncolored) or ``--color always``;
  off for ``--plain`` and for a pipe, ``CLICOLOR_FORCE`` included (it only colors). The plain layout is the
  byte-stable machine-readable text.

Color semantics are fixed: green = satisfied/ok, yellow = needs action/warning, red = error/blocking
drift, cyan = info or a command to run, dim = explanation, bold = heading. Color is never the only
carrier: every badge has a symbol and a word (``✓ ok``, ``! apply``, ``✗ error``, ``… manual``,
``+ extra``); on a stream that cannot encode them they fall back to ASCII.

Public API: ``configure``, ``cli_flags``, ``color_enabled``, ``rich``, ``width``, ``style``, ``symbol``,
``badge``, ``heading``, ``section``, ``kv``, ``table``, ``wrap``, ``legend``, ``progress``, ``command``,
``say``, ``format_error``, ``decorate``, ``capture_class_help``, ``t`` (localized text), ``kind_why``,
``visible_len``, ``pad``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TextIO

ANSI = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MIN_WIDTH, MAX_WIDTH = 40, 120


@dataclass
class _Config:
    color: str = "auto"
    plain: bool = False
    lang: str | None = None


_config = _Config()


def configure(*, color: str = "auto", plain: bool = False, lang: str | None = None) -> None:
    _config.color = color
    _config.plain = plain
    _config.lang = lang


def cli_flags() -> list[str]:
    """The global flags that reproduce this configuration, for a child ``ubuntu-tweak`` process."""
    flags = ["--color", _config.color] if _config.color != "auto" else []
    if _config.plain:
        flags.append("--plain")
    if _config.lang:
        flags += ["--lang", _config.lang]
    return flags


def _stream(stream: TextIO | None) -> TextIO:
    return sys.stdout if stream is None else stream


def _is_tty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def color_enabled(stream: TextIO | None = None) -> bool:
    if _config.plain or _config.color == "never":
        return False
    if _config.color == "always":
        return True
    env = os.environ
    if env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    if env.get("CLICOLOR_FORCE", "0") not in ("", "0"):
        return True
    return _is_tty(_stream(stream))


def rich(stream: TextIO | None = None) -> bool:
    if _config.plain:
        return False
    return _is_tty(_stream(stream)) or _config.color == "always"


def plain() -> bool:
    """``--plain`` was given: the compact text, whatever else is asked for."""
    return _config.plain


def width() -> int:
    columns = shutil.get_terminal_size((80, 24)).columns
    return max(MIN_WIDTH, min(columns, MAX_WIDTH))


def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def style(text: str, *names: str, stream: TextIO | None = None) -> str:
    if not names or not text or not color_enabled(stream):
        return text
    codes = ";".join(ANSI[name] for name in names)
    return f"\x1b[{codes}m{text}\x1b[0m"


def _unicode(stream: TextIO | None = None) -> bool:
    encoding = getattr(_stream(stream), "encoding", None)
    if encoding is None:  # io.StringIO and friends hold text as is
        return True
    try:
        "✓✗…•—→─".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# kind of badge -> (unicode symbol, ASCII symbol, word, color)
_BADGES = {
    "ok": ("✓", "v", "ok", "green"),
    "need": ("!", "!", "apply", "yellow"),
    "run": ("→", "->", "running", "cyan"),
    "error": ("✗", "x", "error", "red"),
    "manual": ("…", "~", "manual", "yellow"),
    "extra": ("+", "+", "extra", "cyan"),
    "info": ("i", "i", "info", "cyan"),
}


def symbol(kind: str, stream: TextIO | None = None) -> str:
    glyph, ascii_glyph, _word, color = _BADGES[kind]
    return style(glyph if _unicode(stream) else ascii_glyph, color, "bold", stream=stream)


def badge(kind: str, *, word: bool = True, stream: TextIO | None = None) -> str:
    glyph, ascii_glyph, label, color = _BADGES[kind]
    text = glyph if _unicode(stream) else ascii_glyph
    return style(f"{text} {label}" if word else text, color, "bold", stream=stream)


def heading(text: str, *, stream: TextIO | None = None) -> str:
    return style(text, "bold", stream=stream)


def command(text: str, *, stream: TextIO | None = None) -> str:
    return style(text, "cyan", stream=stream)


def section(title: str, count: int | None, kind: str, *, stream: TextIO | None = None) -> str:
    color = _BADGES[kind][3]
    label = title if count is None else f"{title} ({count})"
    return f"{symbol(kind, stream)} {style(label, color, 'bold', stream=stream)}"


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - visible_len(text))


def kv(rows: Sequence[tuple[str, str]], *, indent: int = 2, stream: TextIO | None = None) -> list[str]:
    """Aligned ``key  value`` lines; the key is dim, the value as given."""
    if not rows:
        return []
    key_width = max(len(key) for key, _ in rows)
    return [" " * indent + style(pad(key, key_width), "dim", stream=stream) + "  " + value for key, value in rows]


def table(
    headers: Sequence[str], rows: Iterable[Sequence[str]], *, indent: int = 2, stream: TextIO | None = None
) -> list[str]:
    """Aligned columns (the last one is not padded); cells may carry ANSI, padding counts visible characters."""
    body = [list(row) for row in rows]
    columns = len(headers)
    sizes = [max([len(headers[i])] + [visible_len(row[i]) for row in body]) for i in range(columns)]

    def line(cells: Sequence[str]) -> str:
        parts = [pad(cell, sizes[i]) for i, cell in enumerate(cells[:-1])] + [cells[-1]]
        return " " * indent + "  ".join(parts).rstrip()

    return [line([style(h, "bold", stream=stream) for h in headers])] + [line(row) for row in body]


def wrap(
    text: str,
    *,
    indent: int = 0,
    first: str | None = None,
    names: tuple[str, ...] = ("dim",),
    accent: str = "",
    accent_names: tuple[str, ...] = ("yellow",),
    stream: TextIO | None = None,
) -> list[str]:
    """Wrap to the terminal width. ``first`` is the (possibly styled) prefix of the first line, default
    ``indent`` spaces; later lines align under it. The prefix keeps its own styling; each line of text is
    styled on its own so a break never leaves a color open. ``accent`` is plain text put in front of
    ``text`` and styled with ``accent_names`` (a ``[sudo]`` tag): it is measured unstyled and never split."""
    lead = first if first is not None else " " * indent
    blank = " " * visible_len(lead)
    glued = accent.replace(" ", "\u00a0")
    pieces: list[str] = []
    for paragraph in (glued + text).split("\n"):  # a message's own line breaks are kept
        pieces += textwrap.wrap(
            paragraph,
            width=max(MIN_WIDTH, width() - 1),
            initial_indent=blank,
            subsequent_indent=blank,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [blank]
    if not pieces:
        return [lead.rstrip()]
    out = []
    for number, piece in enumerate(pieces):
        body = piece[len(blank) :]
        if number == 0 and glued and body.startswith(glued):
            body = style(accent, *accent_names, stream=stream) + style(body[len(glued) :], *names, stream=stream)
        else:
            body = style(body, *names, stream=stream)
        out.append((lead if number == 0 else blank) + body)
    return out


def progress(number: int, total: int, kind: str, text: str, *, stream: TextIO | None = None) -> str:
    counter = style(f"[{number}/{total}]", "dim", stream=stream)
    return f"{counter} {symbol(kind, stream)} {text}"


def legend(*, stream: TextIO | None = None) -> list[str]:
    lines = [heading(t("legend.title"), stream=stream)]
    for kind in ("ok", "need", "manual", "extra", "error"):
        lines.append(f"  {pad(badge(kind, stream=stream), 10)} {style(t('legend.' + kind), 'dim', stream=stream)}")
    return lines


_FIX_BY_EXIT = {
    2: "check the file named above; `ubuntu-tweak machine` shows which files are read",
    4: "`ubuntu-tweak list` shows the module names",
    5: "nothing was written; remove the secret or the unsafe path named above and run again",
}


def format_error(message: str, exit_code: int, *, fix: str | None = None, stream: TextIO | None = None) -> str:
    """Plain: ``ubuntu-tweak: <message>`` (byte-stable). Rich: red ``error`` badge and a ``fix:`` hint line
    (``fix`` when given, else the generic hint of the exit code, if any)."""
    if not rich(stream):
        return f"ubuntu-tweak: {message}"
    lines = [f"{badge('error', stream=stream)} {style(message, 'red', stream=stream)}"]
    hint = fix or _FIX_BY_EXIT.get(exit_code)
    if hint:
        lines.append(f"  {style('fix:', 'cyan', 'bold', stream=stream)} {style(hint, 'cyan', stream=stream)}")
    return "\n".join(lines)


_DECORATIONS: dict[str, list[tuple[re.Pattern[str], tuple[str, ...]]]] = {
    "capture": [
        (re.compile(r"^(#{1,3} .*|= \d\. .*)$"), ("bold",)),
        (re.compile(r"^(mode:|profile:|machine:|only:)"), ("dim",)),
        (
            re.compile(r"^(classes:|totals:|unmapped|at schema default|dconf user keys|files:|packages:|system:)"),
            ("cyan",),
        ),
        (re.compile(r"^(secret findings in adopted items: [1-9].*)$"), ("red", "bold")),
        (re.compile(r"^(nothing was committed.*|report written.*)$"), ("dim",)),
    ],
    "show": [
        (re.compile(r"^(═══ .*)$"), ("bold",)),
        (re.compile(r"^(Настройки Ubuntu.*)$"), ("bold",)),
        (re.compile(r"^(\s+Что это:.*)$"), ("dim",)),
        (re.compile(r"^(\s+/\S+)$"), ("dim",)),
        (re.compile(r"^(\s+Сейчас:.*)$"), ("green",)),
        (re.compile(r"^(\s+По умолчанию:.*)$"), ("dim",)),
        (re.compile(r"^(\s{2}\S.*\[изменено вами\])$"), ("yellow", "bold")),
    ],
    "machine": [
        (re.compile(r"^(modules:|settings bundles:|linked files:)$"), ("bold",)),
        (re.compile(r"^(\s+)?(.*\(absent.*|.*missing.*|.*no such module.*)$"), ("yellow",)),
        (re.compile(r"^\s{2}\S.*: enabled "), ("green",)),
        (re.compile(r"^\s{2}\S.*: disabled "), ("dim",)),
        (re.compile(r"^\s{4}\S"), ("dim",)),
        (re.compile(r"^\s{2}[A-Z].*\.$"), ("dim",)),
        (re.compile(r"^\s+\(none\)$"), ("dim",)),
        (re.compile(r"^(machine|profile|overlay):"), ("cyan",)),
    ],
    "bundles": [
        (re.compile(r"^(bundles of .*)$"), ("bold",)),
        (re.compile(r"^(\s+NAME\s.*)$"), ("bold",)),
        (re.compile(r"^(\s+inactive: .*)$"), ("yellow",)),
        (re.compile(r"^(shadowed \(.*|duplicated \(.*)$"), ("yellow", "bold")),
        (re.compile(r"^(CONFLICTS .*)$"), ("red", "bold")),
    ],
    "bundle": [
        (re.compile(r"^(# bundle .*)$"), ("bold",)),
        (re.compile(r"^(# from .*|# the overlay.*|# .* description: .*)$"), ("dim",)),
        (re.compile(r"^(# .*does not apply here.*)$"), ("yellow",)),
        (re.compile(r"^(# .*)$"), ("cyan",)),
        (re.compile(r"^(\[.*\])$"), ("bold",)),
    ],
}


def decorate(text: str, family: str, *, stream: TextIO | None = None) -> str:
    """Color an already rendered text block line by line (first matching rule wins); identity when colors are off."""
    if not color_enabled(stream):
        return text
    rules = _DECORATIONS[family]
    out = []
    for line in text.split("\n"):
        for pattern, names in rules:
            if pattern.match(line):
                line = style(line, *names, stream=stream)
                break
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------
# Wording. Every explanation lives here; commands and actions ask for it by key.

_KIND_WHY: dict[str, dict[str, str]] = {
    "en": {
        "package": "apt/snap/flatpak packages that your modules or profile declare; never removes any",
        "shell-rc": "created only while the file is absent; an existing file is never overwritten",
        "login-shell": "makes the shell your terminals start with the declared one (needs sudo)",
        "git-config": "writes one global git option from your profile",
        "git-credential": "lets git use your `gh` login for https remotes",
        "gh-login": "logs gh in with a token read from the keychain at run time, never stored",
        "brew-prefix": "creates the folder Homebrew installs into (needs sudo)",
        "brew-install": "downloads the pinned installer and verifies its checksum first",
        "submodule": "downloads the extension's code that lives in a git submodule",
        "migrate": "moves a value from an old extension id to its new one",
        "compile-schemas": "GNOME can only read an extension's settings after they are compiled",
        "system-module": "runs the module's own installer/checker script",
        "system": "timezone, locale or keyboard from system.toml (needs sudo)",
        "post": "runs the module's post-install command when its check fails",
        "link": "the file or extension lives in this repo/profile and is linked into place",
        "link-replace": "something else is in the way: it is backed up first, then replaced by the link",
        "cli-link": "puts `ubuntu-tweak` on your PATH through ~/.local/bin",
        "cli-link-replace": "something else is at ~/.local/bin/ubuntu-tweak: backed up, then replaced",
        "dotfiles-link": "a link from $HOME to a file of your dotfiles repository (its .zprofile, .zshrc, .profile)",
        "dotfiles-link-replace": "another file is in $HOME: it is backed up, then replaced by a link to the dotfiles repository's file",
        "enable": "turns the extension on in GNOME Shell (loads after the next login)",
        "disable": "turns off a conflicting or retired extension",
        "set-key": "the live value differs from the one your modules/profile declare",
        "seed-key": "writes an initial value once, only while the key has no value of its own",
    },
    "ru": {
        "package": "пакеты apt/snap/flatpak, которые просят модули или профиль; ничего не удаляется",
        "shell-rc": "создаётся только если файла нет; существующий не перезаписывается",
        "login-shell": "делает объявленную оболочку оболочкой по умолчанию (нужен sudo)",
        "git-config": "записывает одну глобальную опцию git из профиля",
        "git-credential": "git использует вход `gh` для https-репозиториев",
        "gh-login": "вход gh по токену из keychain; токен нигде не сохраняется",
        "brew-prefix": "создаёт каталог для Homebrew (нужен sudo)",
        "brew-install": "скачивает установщик и сверяет контрольную сумму",
        "submodule": "скачивает код расширения из git-подмодуля",
        "migrate": "переносит значение со старого id расширения на новый",
        "compile-schemas": "GNOME читает настройки расширения только после компиляции схем",
        "system-module": "запускает установщик/проверку самого модуля",
        "system": "часовой пояс, локаль или раскладка из system.toml (нужен sudo)",
        "post": "запускает пост-установочную команду модуля, пока её проверка не проходит",
        "link": "файл или расширение лежит в репозитории/профиле и подключается ссылкой",
        "link-replace": "место занято чем-то другим: сначала бэкап, затем замена ссылкой",
        "cli-link": "кладёт `ubuntu-tweak` в PATH через ~/.local/bin",
        "cli-link-replace": "по пути ~/.local/bin/ubuntu-tweak лежит другое: бэкап и замена",
        "dotfiles-link": "ссылка из $HOME на файл вашего dotfiles-репозитория (его .zprofile, .zshrc, .profile)",
        "dotfiles-link-replace": "в $HOME лежит другой файл: бэкап, затем замена ссылкой на файл dotfiles-репозитория",
        "enable": "включает расширение в GNOME Shell (загрузится после перелогина)",
        "disable": "выключает конфликтующее или устаревшее расширение",
        "set-key": "текущее значение отличается от заданного в модулях/профиле",
        "seed-key": "пишет начальное значение один раз, пока у ключа нет своего",
    },
}

_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "header.machine": "machine",
        "header.profile": "profile",
        "header.engine": "engine",
        "header.overlay": "overlay {path}",
        "header.no_overlay": "no overlay: shared profile only",
        "header.no_profile": "{path} (not found: module defaults only)",
        "verdict.sync": "in sync: nothing is missing or modified",
        "verdict.sync_extra": "in sync: nothing is missing or modified ({extra} extra, reported only)",
        "verdict.next": "run: ubuntu-tweak apply (preview), then ubuntu-tweak apply --commit",
        "count.missing": "{n} missing",
        "count.modified": "{n} modified",
        "count.extra": "{n} extra",
        "count.manual": "{n} need your attention",
        "section.missing": "Missing",
        "section.missing.what": "Declared by a module or your profile, but not on this machine yet.",
        "section.missing.fix": "Fix: `ubuntu-tweak apply --commit` installs or creates it.",
        "section.modified": "Modified",
        "section.modified.what": "Present, but different from what is declared (a changed value, or a link replaced by a real file).",
        "section.modified.fix": "Fix: `apply --commit` overwrites it (files are backed up first). To keep your version instead, run `ubuntu-tweak capture` to copy it into the profile.",
        "section.extra": "Extra",
        "section.extra.what": "On this machine but declared nowhere. Reported only: ubuntu-tweak never removes anything and extras never make status fail.",
        "section.extra.fix": "Fix: declare it (`ubuntu-tweak capture`) or ignore it.",
        "section.manual": "Manual",
        "section.manual.what": "Things ubuntu-tweak cannot decide or do for you.",
        "section.manual.fix": "Fix: do each step yourself; the line says how.",
        "none": "none",
        "group.general": "profile and system",
        "verdict.sync_manual": "in sync, but {n} item(s) need your attention (see Manual below)",
        "why": "why:",
        "next": "Next",
        "next.apply": "preview what would change (changes nothing)",
        "next.commit": "make it so",
        "exit.title": "Exit codes",
        "exit.0": "0  in sync (extras alone never fail)",
        "exit.3": "3  drift: something is missing or modified",
        "exit.other": "1 internal error, 2 invalid config, 4 unknown module, 5 refused, 6 root not obtainable",
        "legend.title": "Legend",
        "legend.ok": "satisfied, nothing to do",
        "legend.need": "needs action: `apply --commit` does it",
        "legend.manual": "only you can finish it; the line says how",
        "legend.extra": "present but not declared; reported only",
        "legend.error": "failed or blocks the run",
        "apply.title": "What will change",
        "apply.summary": "{n} to do, {satisfied} already satisfied",
        "apply.root": "{n} need sudo",
        "apply.nothing": "Nothing to do: all {satisfied} declared items are already in place.",
        "apply.footer": "Nothing changed. This was a preview; run `ubuntu-tweak apply --commit` to apply it.",
        "apply.skipped_root": "skipped: --no-sudo",
        "apply.staged": "Note: this plan installs extension code or schemas. Settings that depend on them are planned after those actions ran; `apply --commit` does that in the same invocation.",
        "commit.summary": "Summary",
        "commit.changed": "{n} changed",
        "commit.manual": "{n} need you",
        "commit.skipped": "{n} skipped (--no-sudo)",
        "commit.rerun": "Run `ubuntu-tweak status` to confirm: it should exit 0.",
        "commit.rerun_pending": "`ubuntu-tweak status` will still list the skipped and manual items until you have done them.",
        "manual.title": "Manual steps",
        "manual.what": "What is left for you. Each line says how to finish it.",
        "skipped.title": "Skipped: needs root but --no-sudo was given",
        "skipped.fix": "Run again without --no-sudo to do these.",
        "init.step.doctor": "check required tools",
        "init.step.apply": "apply the plan",
        "init.step": "Step {n}/{total}: {text}",
        "init.preview_footer": "Nothing changed. This was a preview; run `ubuntu-tweak init --commit` to set this machine up.",
        "update.preview_footer": "Nothing changed. This was a preview; run `ubuntu-tweak update --commit` to pull and apply.",
        "warnings": "Warnings",
        "staged": "Staged until the extension code is installed (not an error, nothing to do: the same `apply --commit` applies them)",
    },
    "ru": {
        "header.machine": "машина",
        "header.profile": "профиль",
        "header.engine": "версия",
        "header.overlay": "оверлей {path}",
        "header.no_overlay": "нет оверлея: только общий профиль",
        "header.no_profile": "{path} (не найден: только настройки модулей)",
        "verdict.sync": "всё синхронно: ничего не отсутствует и не изменено",
        "verdict.sync_extra": "всё синхронно: ничего не отсутствует и не изменено ({extra} лишних, только отчёт)",
        "verdict.next": "выполните: ubuntu-tweak apply (предпросмотр), затем ubuntu-tweak apply --commit",
        "count.missing": "отсутствует: {n}",
        "count.modified": "изменено: {n}",
        "count.extra": "лишних: {n}",
        "count.manual": "требуют вашего внимания: {n}",
        "section.missing": "Отсутствует",
        "section.missing.what": "Заявлено модулем или профилем, но на этой машине ещё нет.",
        "section.missing.fix": "Исправление: `ubuntu-tweak apply --commit` установит или создаст.",
        "section.modified": "Изменено",
        "section.modified.what": "Есть, но отличается от заявленного (другое значение или ссылка заменена файлом).",
        "section.modified.fix": "Исправление: `apply --commit` перезапишет (файлы сначала копируются в бэкап). Чтобы сохранить свой вариант, выполните `ubuntu-tweak capture`.",
        "section.extra": "Лишнее",
        "section.extra.what": "Есть на машине, но нигде не заявлено. Только отчёт: ничего не удаляется, status из-за этого не падает.",
        "section.extra.fix": "Исправление: заявите это (`ubuntu-tweak capture`) или проигнорируйте.",
        "section.manual": "Вручную",
        "section.manual.what": "То, что ubuntu-tweak не может решить или сделать за вас.",
        "section.manual.fix": "Исправление: выполните шаг сами, в строке написано как.",
        "none": "нет",
        "group.general": "профиль и система",
        "verdict.sync_manual": "синхронно, но {n} пунктов требуют вашего внимания (см. «Вручную» ниже)",
        "why": "зачем:",
        "next": "Дальше",
        "next.apply": "предпросмотр изменений (ничего не меняет)",
        "next.commit": "применить",
        "exit.title": "Коды выхода",
        "exit.0": "0  всё синхронно (лишнее само по себе не ошибка)",
        "exit.3": "3  расхождение: что-то отсутствует или изменено",
        "exit.other": "1 внутренняя ошибка, 2 неверная конфигурация, 4 неизвестный модуль, 5 отказ, 6 нет root",
        "legend.title": "Обозначения",
        "legend.ok": "выполнено, делать нечего",
        "legend.need": "нужно действие: его сделает `apply --commit`",
        "legend.manual": "закончить можете только вы; в строке написано как",
        "legend.extra": "есть, но не заявлено; только отчёт",
        "legend.error": "ошибка или блокирует запуск",
        "apply.title": "Что изменится",
        "apply.summary": "сделать: {n}, уже выполнено: {satisfied}",
        "apply.root": "нужен sudo: {n}",
        "apply.nothing": "Делать нечего: все {satisfied} заявленных пунктов уже на месте.",
        "apply.footer": "Ничего не изменено. Это предпросмотр; `ubuntu-tweak apply --commit` применит план.",
        "apply.skipped_root": "пропущено: --no-sudo",
        "apply.staged": "Примечание: план ставит код или схемы расширений. Настройки, которые от них зависят, будут запланированы после этих действий; `apply --commit` сделает это за один запуск.",
        "commit.summary": "Итог",
        "commit.changed": "изменено: {n}",
        "commit.manual": "ждут вас: {n}",
        "commit.skipped": "пропущено: {n} (--no-sudo)",
        "commit.rerun": "Проверьте `ubuntu-tweak status`: должен вернуть 0.",
        "commit.rerun_pending": "`ubuntu-tweak status` всё ещё покажет пропущенные и ручные пункты, пока вы их не выполните.",
        "manual.title": "Ручные шаги",
        "manual.what": "Что осталось сделать вам. В каждой строке написано как.",
        "skipped.title": "Пропущено: нужен root, а указан --no-sudo",
        "skipped.fix": "Запустите без --no-sudo, чтобы выполнить это.",
        "init.step.doctor": "проверка нужных инструментов",
        "init.step.apply": "применение плана",
        "init.step": "Шаг {n}/{total}: {text}",
        "init.preview_footer": "Ничего не изменено. Это предпросмотр; `ubuntu-tweak init --commit` настроит эту машину.",
        "update.preview_footer": "Ничего не изменено. Это предпросмотр; `ubuntu-tweak update --commit` обновит код и применит план.",
        "warnings": "Предупреждения",
        "staged": "Отложено до установки кода расширения (не ошибка, ничего делать не нужно: тот же `apply --commit` применит)",
    },
}


def lang() -> str:
    chosen = _config.lang or os.environ.get("UBUNTU_TWEAK_LANG") or "en"
    return chosen if chosen in _TEXT else "en"


def t(key: str, **values: object) -> str:
    table = _TEXT[lang()]
    text = table.get(key) or _TEXT["en"][key]
    return text.format(**values) if values else text


_SAY_COLOR = {"ok": "green", "need": "yellow", "manual": "yellow", "error": "red"}


def say(kind: str, text: str, *, stream: TextIO | None = None) -> str:
    """One status sentence. Plain output: ``text`` as is. Interactive: a badge symbol first and the text wrapped
    and colored by the badge's meaning (cyan for info)."""
    if not rich(stream):
        return text
    color = _SAY_COLOR.get(kind, "cyan")
    return "\n".join(wrap(text, first=f"{symbol(kind, stream)} ", names=(color,), stream=stream))


CAPTURE_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("secret", "red", "looks like a credential or token: never copied into the profile"),
    (
        "identity",
        "red",
        "names, accounts and addresses of you or your network: kept out so the profile stays shareable",
    ),
    ("hardware", "red", "pairings and devices that only make sense on this machine"),
    (
        "state",
        "yellow",
        "what apps rewrite by themselves (window sizes, recents, timestamps); lift with [capture] include",
    ),
    (
        "auto",
        "yellow",
        "keys the system or an app registered on its own (ids, ordering lists); lift with [capture] include",
    ),
    ("no-schema", "dim", "set in dconf but no schema on this machine describes it"),
    (
        "excluded-by-rule",
        "dim",
        "excluded by a rule (the profile's [capture] exclude, or an extension handled by modules)",
    ),
)


def capture_class_help(*, stream: TextIO | None = None) -> list[str]:
    """What each ``not captured`` class means: red = never leaves the machine, yellow = skipped but promotable."""
    lines = [heading("Why keys are not captured", stream=stream)]
    size = max(len(name) for name, _color, _text in CAPTURE_CLASSES)
    for name, color, text in CAPTURE_CLASSES:
        lines.append(f"  {style(name.ljust(size), color, 'bold', stream=stream)}  {style(text, 'dim', stream=stream)}")
    return lines


def kind_why(kind: str, language: str | None = None) -> str:
    table = _KIND_WHY[language or lang()]
    return table.get(kind) or _KIND_WHY["en"].get(kind, "")

#!/usr/bin/python3 -B
"""memguard module options: `[modules.memguard.swap]` of the profile laid over the repo's memguard.toml.

Used by install.sh, in both the install and `--check`, so the installed file and the comparison are rendered by
the same code. The engine hands the profile's `[modules.memguard]` table to the installer as JSON in
UBUNTU_TWEAK_MODULE_CONFIG (spec: docs/specs/ubuntu-tweak-cli.md, *Service modules*); the only key is `swap`, a table
of `[swap]` keys of memguard.toml (docs/specs/memguard.md). Each value replaces the assignment of that key and keeps
the comment after it; a key the file does not know, a value of the wrong type or `user` (the installer resolves the
desktop user itself) is refused before anything is written.

    memguard_options.py < memguard.toml      # prints the file with the options applied

Public API: ``apply(options, text) -> str``, ``OptionsError``.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import memguard_swap  # noqa: E402

CONFIG_ENV = "UBUNTU_TWEAK_MODULE_CONFIG"
RESOLVED_BY_INSTALLER = ("user",)
MAX_NUMBER = 10**9


class OptionsError(Exception):
    """The `[modules.memguard]` options cannot be applied; the message is shown as is."""


def _toml_value(key: str, value: object) -> str:
    default = memguard_swap.DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise OptionsError(f"[modules.memguard.swap] {key} must be true or false, got {value!r}")
        return "true" if value else "false"
    if isinstance(default, (int, float)):
        whole = isinstance(default, int)
        if isinstance(value, bool) or not isinstance(value, int if whole else (int, float)):
            raise OptionsError(
                f"[modules.memguard.swap] {key} must be {'a whole number' if whole else 'a number'}, got {value!r}"
            )
        # a number any real setting stays far below; also keeps an int out of float overflow
        if isinstance(value, float) and not math.isfinite(value) or not 0 <= value <= MAX_NUMBER:
            raise OptionsError(f"[modules.memguard.swap] {key} must be between 0 and {MAX_NUMBER}, got {value!r}")
        return repr(value)
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise OptionsError(f"[modules.memguard.swap] {key} must be a list of strings, got {value!r}")
        return "[" + ", ".join(_toml_string(key, item) for item in value) + "]"
    if not isinstance(value, str):
        raise OptionsError(f"[modules.memguard.swap] {key} must be a string, got {value!r}")
    return _toml_string(key, value)


def _toml_string(key: str, value: str) -> str:
    try:
        value.encode("utf-8")  # a lone surrogate is no text at all
    except UnicodeEncodeError as error:
        raise OptionsError(f"[modules.memguard.swap] {key} is not valid text: {error.reason}") from error
    # JSON escapes the quote, the backslash and the control characters the way TOML does; DEL is the one it leaves raw
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def apply(options: Mapping[str, object], text: str) -> str:
    if not isinstance(options, Mapping):
        raise OptionsError("[modules.memguard] must be a table")
    unknown = sorted(set(options) - {"swap"})
    if unknown:
        raise OptionsError(f"[modules.memguard] unknown option(s): {', '.join(unknown)} (the table is `swap`)")
    swap = options.get("swap", {})
    if not isinstance(swap, Mapping):
        raise OptionsError("[modules.memguard.swap] must be a table")
    for key in swap:
        if key in RESOLVED_BY_INSTALLER:
            raise OptionsError(f"[modules.memguard.swap] {key} is resolved by the installer (SUDO_USER / PKEXEC_UID)")
        if key not in memguard_swap.DEFAULTS:
            raise OptionsError(f"[modules.memguard.swap] unknown key {key!r}")
    rendered = {key: _toml_value(key, value) for key, value in swap.items()}
    for key, value in rendered.items():
        pattern = re.compile(rf'^({re.escape(key)} = )("(?:[^"\\]|\\.)*"|\[[^\]]*\]|[^\s#]+)(.*)$', re.MULTILINE)
        if len(pattern.findall(text)) != 1:
            raise OptionsError(f"memguard.toml has no single `{key} = ...` line to set")
        text = pattern.sub(lambda found, value=value: found.group(1) + value + found.group(3), text)
    try:
        tomllib.loads(text)  # a value that produced invalid TOML must never reach /etc
    except tomllib.TOMLDecodeError as error:
        raise OptionsError(f"the options produce an invalid memguard.toml: {error}") from error
    _validate_as_memguard_does(text)
    return text


def _validate_as_memguard_does(text: str) -> None:
    """The rendered file through ``memguard_swap.load_config``, the loader swap-drain and memguard use at run time."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        try:
            memguard_swap.load_config(handle.name)
        except ValueError as error:
            raise OptionsError(str(error).replace(handle.name, "memguard.toml")) from error


def main(env: Mapping[str, str]) -> int:
    raw = env.get(CONFIG_ENV, "")
    try:
        options = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        sys.stderr.write(f"memguard: {CONFIG_ENV} is not valid JSON: {error}\n")
        return 1
    try:
        text = apply(options, sys.stdin.buffer.read().decode("utf-8"))
        sys.stdout.buffer.write(text.encode("utf-8"))
    except OptionsError as error:
        sys.stderr.write(f"memguard: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ))

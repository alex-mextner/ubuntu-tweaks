"""Does apt accept the configured sources? The check any root module that writes apt sources runs last.

apt refuses a whole configuration, for every user and every command, when two entries of one repository carry
different ``Signed-By`` values (``E: Conflicting values set for option Signed-By regarding source <uri> <suite>``,
``E: The list of sources could not be read.``). A module can write a perfectly valid file and still leave apt broken
because another file declares the same repository, so the only honest post-condition is to ask apt itself.

- ``apt_sources_error(root)``: runs the unprivileged, read-only ``apt-get indextargets`` (it parses ``sources.list`` and
  every ``sources.list.d`` file, needs neither root nor network) and returns apt's error text, or ``None`` when apt
  accepts the tree or ``apt-get`` is missing. ``root`` stages ``/etc/apt`` below a directory (tests).
- ``declarations(path, text)``: the ``(uri, suite, Signed-By)`` triples one source file declares, so a module can
  find the conflicting file before apt does and name it in the finding.

Used by ``modules/apt-sources`` (the only module that writes apt sources today); a new module that touches
``/etc/apt/sources.list*`` calls ``apt_sources_error`` after its last change and fails with the cause it returns.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from pathlib import Path

from ubuntu_tweaks.process import Process

INLINE_KEY = "<inline key>"  # an armored key block in `Signed-By:`: equal to no path, and to no other block
ARCH_NAMES = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf"}
PROBE_TIMEOUT = 60.0
_SIGNED_BY_OPTION = re.compile(r"(?:^|[\s\[])signed-by=([^\s\]]+)", re.IGNORECASE)
_STANZAS = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Declaration:
    """One ``(uri, suite)`` a source file declares, with the ``Signed-By`` apt will see for it (``None``: not set)."""

    uri: str
    suite: str
    signed_by: str | None
    active: bool  # False for a deb822 stanza with ``Enabled: no``: apt ignores it
    text: str  # the line or stanza as written, for "delete this" instructions


def normalize_uri(uri: str) -> str:
    return uri.replace("$(ARCH)", ARCH_NAMES.get(platform.machine(), platform.machine())).rstrip("/")


def _signed_by(value: str) -> str:
    return INLINE_KEY if "BEGIN PGP" in value else re.sub(r"[\s,]+", ",", value.strip()).strip(",")


def declarations(path: Path, text: str) -> list[Declaration]:
    """What a ``.list`` (one-line) or ``.sources`` (deb822) file declares; comments ignored."""
    return _deb822(text) if path.suffix == ".sources" else _one_line(text)


def _one_line(text: str) -> list[Declaration]:
    found: list[Declaration] = []
    for raw in text.splitlines():
        words = raw.split("#", 1)[0].split()
        if len(words) < 3 or words[0] not in ("deb", "deb-src"):
            continue
        index = 1
        options = ""
        if words[index].startswith("["):
            while index < len(words) and not words[index].endswith("]"):
                index += 1
            index += 1
            options = " ".join(words[1:index])
        if index + 1 < len(words):
            match = _SIGNED_BY_OPTION.search(options)
            signed_by = _signed_by(match.group(1)) if match else None
            found.append(Declaration(normalize_uri(words[index]), words[index + 1], signed_by, True, raw.strip()))
    return found


def _stanza_fields(stanza: str) -> tuple[dict[str, str], list[str]]:
    """The lower-cased fields of one deb822 stanza (continuation lines joined) and every line of it as written
    (comments and blank lines left out)."""
    fields: dict[str, str] = {}
    lines: list[str] = []
    last = ""
    for line in stanza.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        lines.append(line.strip())
        if line[0] in " \t" and last:
            fields[last] += " " + line.strip()
        elif ":" in line:
            last, _, value = line.partition(":")
            last = last.strip().lower()
            fields[last] = value.strip()
    return fields, lines


def _deb822(text: str) -> list[Declaration]:
    found: list[Declaration] = []
    for stanza in _STANZAS.split(text):
        fields, lines = _stanza_fields(stanza)
        signed_by = _signed_by(fields["signed-by"]) if fields.get("signed-by") else None
        active = fields.get("enabled", "yes").lower() not in ("no", "false", "0")
        pairs = [(uri, suite) for uri in fields.get("uris", "").split() for suite in fields.get("suites", "").split()]
        whole = "stanza [" + " | ".join(lines) + "]"  # the whole stanza, key block included: nothing orphaned
        for uri, suite in pairs:
            # a stanza that declares several repositories is edited, not deleted: the others must survive
            shown = whole if len(pairs) == 1 else f"{uri} {suite} out of the {whole} (it declares other repositories)"
            found.append(Declaration(normalize_uri(uri), suite, signed_by, active, shown))
    return found


def probe_command(root: Path | None) -> tuple[str, ...]:
    """The probe's argv: C locale (the ``E:`` prefix is translated otherwise); a staged ``root`` redirects the lists."""
    command = ("env", "LC_ALL=C", "apt-get", "indextargets", "--format", "$(SITE)")
    if root is None:
        return command
    return (
        *command,
        "-o",
        f"Dir::Etc::sourcelist={root}/etc/apt/sources.list",
        "-o",
        f"Dir::Etc::sourceparts={root}/etc/apt/sources.list.d",
    )


def apt_sources_error(root: Path | None = None, process: Process | None = None) -> str | None:
    """apt's complaint about the configured sources (``E:`` lines, each once), or ``None`` when apt accepts them.

    ``None`` also when ``apt-get`` is not installed or the probe timed out: nothing can be said then, and a slow
    disk is not an inconsistent source list."""
    result = (process or Process()).query(probe_command(root), timeout=PROBE_TIMEOUT)
    if result.returncode in (0, 124, 127):
        return None
    lines = [line.strip() for line in f"{result.stderr}\n{result.stdout}".splitlines() if line.strip()]
    errors = list(dict.fromkeys(line for line in lines if line.startswith("E:")))
    if errors:
        return "\n".join(errors)
    return lines[-1] if lines else f"apt-get indextargets exited with {result.returncode}"

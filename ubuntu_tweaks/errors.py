"""Exit codes and the errors that map to them.

Accessed via: every command returns an ``ExitCode``; ``cli.main`` turns an uncaught
``UbuntuTweakError`` into its ``exit_code`` and anything else into ``ExitCode.INTERNAL``.

Public names (other modules code against these):

- ``ExitCode``: ``OK`` 0, ``INTERNAL`` 1, ``INVALID_CONFIG`` 2, ``DRIFT`` 3,
  ``UNKNOWN_MODULE`` 4, ``REFUSED`` 5 (secret finding or unsafe path).
- ``UbuntuTweakError``: base class; ``exit_code`` is a class attribute.
- ``ConfigError`` (2), ``UnknownModuleError`` (4), ``RefusedError`` (5).
"""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INTERNAL = 1
    INVALID_CONFIG = 2
    DRIFT = 3
    UNKNOWN_MODULE = 4
    REFUSED = 5


class UbuntuTweakError(Exception):
    """An expected failure with a stable exit code; the message is shown to the user as is."""

    exit_code: ExitCode = ExitCode.INTERNAL


class ConfigError(UbuntuTweakError):
    """Invalid manifest, profile file or flag value."""

    exit_code = ExitCode.INVALID_CONFIG


class UnknownModuleError(UbuntuTweakError):
    """A module named on the command line (or in a profile) does not exist."""

    exit_code = ExitCode.UNKNOWN_MODULE


class RefusedError(UbuntuTweakError):
    """The command refused to proceed: a secret finding or an unsafe path."""

    exit_code = ExitCode.REFUSED

"""The one failure a handler reports: this file cannot be previewed (the caller exits non-zero, Files shows its icon)."""


class Unsupported(Exception):
    """The file is of a kind this package knows, but this particular file cannot be rendered."""

"""The shape of a dconf directory path, shared by the manifest parser and the settings backends.

Public API: ``is_dconf_dir_path(path) -> bool``: True for ``/a/b/`` (leading and trailing slash,
every component non-empty and not ``.`` or ``..``). The root ``/`` is not a directory a schema
or a migration may address, so it is False. Kept free of ``gi`` so ``manifest.py`` can use it.
"""


def is_dconf_dir_path(path: str) -> bool:
    if not path.startswith("/") or not path.endswith("/"):
        return False
    return all(component not in {"", ".", ".."} for component in path.strip("/").split("/"))

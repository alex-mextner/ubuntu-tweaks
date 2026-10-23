"""A throwaway repo + HOME for the action planners and commands (needs PyGObject)."""

import json
import os
from pathlib import Path
from unittest import mock

from tests.util import TempDirCase, write
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.profile import Profile
from ubuntu_tweaks.settings import FakeBackend, FakeKey, FakeSchema

SHELL_SCHEMA = FakeSchema(
    "org.gnome.shell",
    {"enabled-extensions": FakeKey("as", "[]"), "disabled-extensions": FakeKey("as", "[]")},
    path="/org/gnome/shell/",
)


class Scenario(TempDirCase):
    """``self.home`` / ``self.repo`` live under ``self.tmp``; ``HOME`` and the XDG dirs point there."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile_root = self.tmp / "profile"
        self.profile_root.mkdir()

    @property
    def extensions_dir(self) -> Path:
        return self.home / ".local/share/gnome-shell/extensions"

    def add_extension(self, path: str, uuid: str, *, schema: str | None = None) -> Path:
        directory = self.repo / path
        metadata: dict[str, str] = {"uuid": uuid, "name": uuid}
        if schema is not None:
            metadata["settings-schema"] = schema
        write(directory / "metadata.json", json.dumps(metadata))
        return directory

    def add_module(
        self, name: str, *, kind: str = "local", path: str | None = None, extension: str = "", enabled: bool = True
    ) -> None:
        source = f'[source]\nkind = "{kind}"\npath = "{path}"\n'
        text = f"enabled = {str(enabled).lower()}\n{source}{extension}"
        write(self.repo / "modules" / name / "module.toml", text)

    def context(self, only: frozenset[str] = frozenset()) -> Context:
        return Context(self.repo, Profile(self.profile_root, "home"), only)

    def backend(self) -> FakeBackend:
        return FakeBackend([SHELL_SCHEMA])

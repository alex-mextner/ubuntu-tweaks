"""Planner: compile each extension's GSettings schemas when they are stale.

Accessed via: planner discovery (``planners/__init__.py``).

Assumptions: the schema directory is ``<extension source>/schemas``. The action is satisfied when
the extension ships no ``*.gschema.xml`` or ``gschemas.compiled`` is newer than all of them. While
a submodule is not initialized its ``metadata.json`` is absent: the action is then pending, and
becomes satisfied without running when the initialized extension turns out to have no schema.
"""

from __future__ import annotations

from collections.abc import Sequence

from ubuntu_tweaks.actions import Action, Phase
from ubuntu_tweaks.context import Context
from ubuntu_tweaks.extension import ExtensionModule, extension_modules, tilde
from ubuntu_tweaks.process import run
from ubuntu_tweaks.settings import SettingsBackend

COMPILED = "gschemas.compiled"


class CompileSchemas(Action):
    phase = Phase.SCHEMAS
    kind = "compile-schemas"

    def __init__(self, module: ExtensionModule) -> None:
        self.module = module
        self.scope = module.name

    def describe(self) -> str:
        return f"compile schemas in {tilde(self.module.schemas_dir)}"

    def check(self) -> bool:
        if self.module.metadata() is None:
            return False
        sources = sorted(self.module.schemas_dir.glob("*.gschema.xml"))
        if not sources:
            return True
        compiled = self.module.schemas_dir / COMPILED
        if not compiled.is_file():
            return False
        built = compiled.stat().st_mtime
        return all(source.stat().st_mtime <= built for source in sources)

    def run(self) -> None:
        self.module.require_metadata()
        run(["glib-compile-schemas", str(self.module.schemas_dir)])


def build(ctx: Context, backend: SettingsBackend) -> Sequence[Action]:
    return [CompileSchemas(module) for module in extension_modules(ctx)]

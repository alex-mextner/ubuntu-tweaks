"""module.toml: init-pipeline fields (``order``, ``phase``), ``[install]``, ``[[step]]``, ``[[post]]``, ``root``."""

import unittest
from pathlib import Path

from ubuntu_tweaks.actions import DEFAULT_ORDER, Phase
from ubuntu_tweaks.context import REPO_ROOT
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.manifest import Install, Manifest, Post, Step, discover_modules, parse_manifest

DIRECTORY = Path("/repo/modules/x")
BUILTIN = '[source]\nkind = "builtin"\n'


def parse(text: str) -> Manifest:
    return parse_manifest("x", text, DIRECTORY)


class PipelineFieldTests(unittest.TestCase):
    def test_defaults_leave_a_module_unordered_and_in_its_natural_phase(self) -> None:
        manifest = parse(BUILTIN)
        self.assertEqual((manifest.order, manifest.phase), (DEFAULT_ORDER, None))
        self.assertEqual((manifest.install, manifest.steps, manifest.post), (Install(), (), ()))

    def test_order_and_phase_are_read(self) -> None:
        manifest = parse('order = 30\nphase = "base"\n' + BUILTIN)
        self.assertEqual((manifest.order, manifest.phase), (30, Phase.BASE))

    def test_bad_order_and_phase_are_invalid_config(self) -> None:
        for text, pattern in (
            ("order = -1\n" + BUILTIN, "'order' must be a whole number from 0 to 999"),
            ("order = 1000\n" + BUILTIN, "'order' must be a whole number from 0 to 999"),
            ('order = "first"\n' + BUILTIN, "'order' must be a whole number"),
            ("order = true\n" + BUILTIN, "'order' must be a whole number"),
            ('phase = "settings"\n' + BUILTIN, "unknown phase 'settings'; valid phases: base, packages, system"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, pattern):
                parse(text)


class InstallTests(unittest.TestCase):
    def test_apt_names_and_provides(self) -> None:
        manifest = parse(BUILTIN + '[install]\napt = ["gh", "git"]\n[install.provides]\ngh = "gh"\n')
        self.assertEqual(manifest.install, Install(("gh", "git"), (("gh", "gh"),)))

    def test_provides_must_name_a_listed_package(self) -> None:
        with self.assertRaisesRegex(ConfigError, "'zsh' is not listed in apt"):
            parse(BUILTIN + '[install]\napt = ["gh"]\n[install.provides]\nzsh = "zsh"\n')

    def test_unknown_install_key_is_invalid(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            parse(BUILTIN + '[install]\nbrew = ["x"]\n')

    def test_a_system_module_has_its_own_installer(self) -> None:
        system = '[source]\nkind = "system"\ninstall = "i.sh"\ncheck = "i.sh --check"\n'
        for extra in ('[install]\napt = ["x"]\n', '[[step]]\nname = "login-shell"\n'):
            with self.subTest(extra=extra), self.assertRaisesRegex(ConfigError, "own installer"):
                parse(system + extra)


class StepTests(unittest.TestCase):
    def test_step_keeps_its_arguments(self) -> None:
        manifest = parse(
            BUILTIN + '[[step]]\nname = "brew-prefix"\nprefix = "/opt/brew"\n[[step]]\nname = "login-shell"\n'
        )
        self.assertEqual(manifest.steps, (Step("brew-prefix", {"prefix": "/opt/brew"}), Step("login-shell", {})))

    def test_a_step_needs_a_name(self) -> None:
        with self.assertRaisesRegex(ConfigError, "missing required key 'name'"):
            parse(BUILTIN + '[[step]]\nshell = "zsh"\n')


class PostTests(unittest.TestCase):
    ENTRY = 'name = "{name}"\nrun = ["true"]\ncheck = ["true"]\n'

    def test_post_fields(self) -> None:
        manifest = parse(
            BUILTIN
            + '[[post]]\nname = "shellenv"\ndescription = "wire it"\nrun = ["/bin/sh", "-c", "x"]\n'
            + 'check = ["grep", "-q", "x"]\nneeds_root = true\nafter = "install"\n'
        )
        self.assertEqual(
            manifest.post,
            (Post("shellenv", ("/bin/sh", "-c", "x"), ("grep", "-q", "x"), True, "install", "wire it"),),
        )

    def test_after_must_name_an_earlier_post_or_install(self) -> None:
        first = "[[post]]\n" + self.ENTRY.format(name="one")
        self.assertEqual(
            parse(BUILTIN + first + "[[post]]\n" + self.ENTRY.format(name="two") + 'after = "one"\n').post[1].after,
            "one",
        )
        for text in (
            first + "[[post]]\n" + self.ENTRY.format(name="two") + 'after = "three"\n',
            "[[post]]\n" + self.ENTRY.format(name="one") + 'after = "one"\n',
        ):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ConfigError, "'after' must be 'install' or an earlier"),
            ):
                parse(BUILTIN + text)

    def test_duplicate_and_reserved_names_are_invalid(self) -> None:
        entry = "[[post]]\n" + self.ENTRY.format(name="one")
        with self.assertRaisesRegex(ConfigError, "duplicate post name 'one'"):
            parse(BUILTIN + entry + entry)
        with self.assertRaisesRegex(ConfigError, "invalid post name 'install'"):
            parse(BUILTIN + "[[post]]\n" + self.ENTRY.format(name="install"))

    def test_run_and_check_are_argv_lists_with_a_resolvable_program(self) -> None:
        for body, pattern in (
            ('name = "a"\nrun = "true"\ncheck = ["true"]\n', "'run' must be a non-empty argv list"),
            ('name = "a"\nrun = []\ncheck = ["true"]\n', "'run' must be a non-empty argv list"),
            ('name = "a"\nrun = ["true"]\n', "'check' must be a non-empty argv list"),
            ('name = "a"\nrun = ["bin/x"]\ncheck = ["true"]\n', "program on PATH or an absolute path"),
            ('name = "a"\nrun = ["true", "x{packages.brew}"]\ncheck = ["true"]\n', "whole argument"),
            ('name = "a"\nrun = ["true", "{PACKAGES.brew}"]\ncheck = ["true"]\n', "whole argument"),
            ('name = "a"\nrun = ["true", "{packages.cargo}"]\ncheck = ["true"]\n', "whole argument"),
            ('name = "a"\nrun = ["{packages.brew}", "--version"]\ncheck = ["true"]\n', "cannot be the program"),
        ):
            with self.subTest(body=body), self.assertRaisesRegex(ConfigError, pattern):
                parse(BUILTIN + "[[post]]\n" + body)


class SystemRootTests(unittest.TestCase):
    SYSTEM = '[source]\nkind = "system"\ninstall = "i.sh"\ncheck = "i.sh --check"\n'

    def test_root_defaults_to_true_and_can_be_switched_off(self) -> None:
        self.assertTrue(parse(self.SYSTEM).source.root)
        self.assertFalse(parse(self.SYSTEM.replace("kind", "root = false\nkind")).source.root)

    def test_root_must_be_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "'root' must be true or false"):
            parse(self.SYSTEM.replace("kind", 'root = "no"\nkind'))

    def test_root_is_only_for_system_sources(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            parse('[source]\nroot = false\nkind = "builtin"\n')


class RepoModulesTests(unittest.TestCase):
    def test_the_init_pipeline_modules_are_ordered_as_the_spec_says(self) -> None:
        modules = {manifest.name: manifest for manifest in discover_modules(REPO_ROOT / "modules")}
        pipeline = ["base-tools", "zsh", "clipboard", "git-gh", "brew"]
        orders = [modules[name].order for name in pipeline]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(set(orders)), len(orders))
        self.assertTrue(all(modules[name].phase is Phase.BASE for name in pipeline))

    def test_the_brew_installer_pin_is_a_commit_url_with_a_sha256(self) -> None:
        (step,) = [
            s
            for s in {m.name: m for m in discover_modules(REPO_ROOT / "modules")}["brew"].steps
            if s.name == "brew-install"
        ]
        self.assertRegex(
            str(step.args["url"]), r"^https://raw\.githubusercontent\.com/Homebrew/install/[0-9a-f]{40}/install\.sh$"
        )
        self.assertRegex(str(step.args["sha256"]), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()

"""User-level package managers of ``packages.toml`` (uv, npm, bun, cargo, pipx): spec parsing, listings, argv."""

import unittest
from pathlib import Path

from ubuntu_tweaks import tool_packages
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.process import CommandResult, FakeProcess

HOME = Path("/home/u")


class ParseSpecTests(unittest.TestCase):
    def test_uv_and_pipx_names_are_normalized_pep_508_distribution_names(self) -> None:
        cases = {
            "glances[web]": "glances",
            "esphome==2026.1": "esphome",
            "Git_Filter.Repo": "git-filter-repo",
            "py-spy>=0.3,<1": "py-spy",
            "hyper-qa @ git+https://git.example.com/acme/qa-cli.git": "hyper-qa",
            "netprint @ git+https://example.com/x/y.git@v1.2#subdirectory=cli": "netprint",
            "trafilatura": "trafilatura",
        }
        for manager in ("uv", "pipx"):
            for spec, name in cases.items():
                with self.subTest(manager=manager, spec=spec):
                    self.assertEqual(tool_packages.parse_spec(manager, spec), name)

    def test_npm_and_bun_names_keep_the_scope_and_drop_the_version(self) -> None:
        cases = {
            "agent-browser": "agent-browser",
            "@openai/codex": "@openai/codex",
            "@openai/codex@0.5.1": "@openai/codex",
            "opencode-ai@latest": "opencode-ai",
            "cf@^1.2.0": "cf",
        }
        for manager in ("npm", "bun"):
            for spec, name in cases.items():
                with self.subTest(manager=manager, spec=spec):
                    self.assertEqual(tool_packages.parse_spec(manager, spec), name)

    def test_cargo_names_drop_the_version(self) -> None:
        self.assertEqual(tool_packages.parse_spec("cargo", "wasm-bindgen-cli"), "wasm-bindgen-cli")
        self.assertEqual(tool_packages.parse_spec("cargo", "wasm-bindgen-cli@0.2.100"), "wasm-bindgen-cli")

    def test_invalid_specs_are_config_errors_for_every_manager(self) -> None:
        bad = [
            "",
            "   ",
            "-e",
            "--index-url=http://evil",
            "glances; rm -rf ~",
            "glances && id",
            "glances|cat",
            "$(id)",
            "`id`",
            "a\nb",
            "two words",
            "glances @git+https://x/y.git",  # the PEP 508 form needs the spaces around @
            "x @ git+https://x/y.git; echo",
            "x @ https://x/y z",
            "x @ git+https://x/$HOME",
            "x @ -e",
            "x @ ",
        ]
        for manager in tool_packages.TOOL_MANAGERS:
            for spec in bad:
                with self.subTest(manager=manager, spec=spec), self.assertRaises(ConfigError):
                    tool_packages.parse_spec(manager, spec)

    def test_a_pep_508_url_form_is_for_uv_and_pipx_only(self) -> None:
        for manager in ("npm", "bun", "cargo"):
            with self.subTest(manager=manager), self.assertRaises(ConfigError):
                tool_packages.parse_spec(manager, "x @ git+https://example.com/x.git")

    def test_the_error_names_the_manager_and_the_entry(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"uv: '--x' is not a valid package spec"):
            tool_packages.parse_spec("uv", "--x")


class ListingTests(unittest.TestCase):
    def test_uv_tool_list_rows_are_normalized_and_entrypoints_skipped(self) -> None:
        text = "glances v4.3 [extras: web]\n- glances\nGit_Filter.Repo v2.4\n- git-filter-repo\n"
        self.assertEqual(tool_packages.parse_listing("uv", text), ["glances", "git-filter-repo"])

    def test_npm_parseable_lists_the_packages_under_node_modules(self) -> None:
        text = "/home/u/.local/lib\n/home/u/.local/lib/node_modules/@openai/codex\n/home/u/.local/lib/node_modules/agent-browser\n"
        self.assertEqual(tool_packages.parse_listing("npm", text), ["@openai/codex", "agent-browser"])

    def test_bun_pm_ls_rows(self) -> None:
        text = "/home/u/.bun/install/global node_modules (2)\n├── cf@1.2.3\n└── @scope/tool@0.1.0\n"
        self.assertEqual(tool_packages.parse_listing("bun", text), ["cf", "@scope/tool"])

    def test_cargo_install_list_rows(self) -> None:
        text = "wasm-bindgen-cli v0.2.100:\n    wasm-bindgen\n    wasm2es6js\n"
        self.assertEqual(tool_packages.parse_listing("cargo", text), ["wasm-bindgen-cli"])

    def test_cargo_rows_with_a_source_are_off_registry(self) -> None:
        text = "ripgrep v14.1.0:\n    rg\nqa-cli v0.1.0 (https://git.example.com/acme/qa-cli#abc123):\n    qa\nlocal v0.2.0 (/home/u/src/local):\n"
        self.assertEqual(tool_packages.cargo_off_registry(text), {"qa-cli", "local"})
        self.assertEqual(tool_packages.parse_listing("cargo", text), ["ripgrep", "qa-cli", "local"])

    def test_pipx_short_rows_are_normalized(self) -> None:
        self.assertEqual(tool_packages.parse_listing("pipx", "Black 24.1\nhttpie 3.2\n"), ["black", "httpie"])


class CommandTests(unittest.TestCase):
    def test_install_argv_per_manager(self) -> None:
        spec = "x"
        self.assertEqual(tool_packages.install_argv("uv", spec, HOME), ["uv", "tool", "install", "x"])
        self.assertEqual(tool_packages.install_argv("pipx", spec, HOME), ["pipx", "install", "x"])
        self.assertEqual(
            tool_packages.install_argv("npm", spec, HOME), ["npm", "install", "-g", "--prefix", "/home/u/.local", "x"]
        )
        self.assertEqual(tool_packages.install_argv("bun", spec, HOME), ["bun", "add", "-g", "x"])
        self.assertEqual(tool_packages.install_argv("cargo", spec, HOME), ["cargo", "install", "x"])

    def test_a_pep_508_url_spec_is_one_argument(self) -> None:
        spec = "hyper-qa @ git+https://git.example.com/acme/qa-cli.git"
        self.assertEqual(tool_packages.install_argv("uv", spec, HOME)[-1], spec)

    def test_installed_names_ask_the_manager_through_query(self) -> None:
        process = FakeProcess({("uv", "tool", "list"): CommandResult(0, "glances v4 [extras: web]\n- glances\n")})
        self.assertEqual(tool_packages.installed_names(process, "uv", HOME), {"glances"})
        self.assertEqual(process.queries, [("uv", "tool", "list")])

    def test_npm_is_listed_with_the_user_prefix_and_survives_a_nonzero_exit(self) -> None:
        argv = ("npm", "ls", "-g", "--depth=0", "--parseable", "--prefix", "/home/u/.local")
        process = FakeProcess({argv: CommandResult(1, "/home/u/.local/lib/node_modules/opencode-ai\n")})
        self.assertEqual(tool_packages.installed_names(process, "npm", HOME), {"opencode-ai"})

    def test_a_missing_manager_lists_nothing_and_is_reported_missing(self) -> None:
        process = FakeProcess()
        self.assertEqual(tool_packages.installed_names(process, "uv", HOME), set())
        self.assertTrue(tool_packages.is_missing(process, "uv"))
        self.assertEqual(process.queries[-1], ("uv", "--version"))

    def test_a_present_manager_is_not_missing(self) -> None:
        process = FakeProcess({("uv", "--version"): CommandResult(0, "uv 0.9\n")})
        self.assertFalse(tool_packages.is_missing(process, "uv"))

    def test_an_unknown_manager_is_an_error_not_pipx(self) -> None:
        with self.assertRaises(ValueError):
            tool_packages.install_argv("pip", "x", HOME)
        with self.assertRaises(ValueError):
            tool_packages.list_argv("pip", HOME)

    def test_fallbacks_are_uv_to_pipx_and_npm_to_bun(self) -> None:
        self.assertEqual(tool_packages.FALLBACK, {"uv": "pipx", "npm": "bun"})


if __name__ == "__main__":
    unittest.main()

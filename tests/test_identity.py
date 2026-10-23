"""Finding the dotfiles repository through ``gh`` (a fake runner), on a throwaway HOME."""

import json
from pathlib import Path

from tests.util import TempDirCase, write
from ubuntu_tweaks import identity
from ubuntu_tweaks.errors import ConfigError
from ubuntu_tweaks.identity import CACHE_TTL_SECONDS, RepoRef, Source
from ubuntu_tweaks.process import CommandResult, FakeProcess

USER = ("gh", "api", "user", "-q", ".login")
LIST = ("gh", "repo", "list", "alice", "--json", identity.REPO_FIELDS, "--limit", "300")


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def listing(*names: str, private: bool = True, login: str = "alice") -> CommandResult:
    return ok(
        json.dumps(
            [
                {
                    "name": name,
                    "nameWithOwner": f"{login}/{name}",
                    "isPrivate": private,
                    "isArchived": False,
                    "defaultBranchRef": {"name": "main"},
                }
                for name in names
            ]
        )
    )


def gh_account(login: str, *names: str) -> FakeProcess:
    listed = ("gh", "repo", "list", login, "--json", identity.REPO_FIELDS, "--limit", "300")
    return FakeProcess({USER: ok(f"{login}\n"), listed: listing(*names, login=login)})


def gh(*names: str) -> FakeProcess:
    return gh_account("alice", *names)


class DiscoveryTests(TempDirCase):
    def test_the_users_dotfiles_repository_is_found_by_name(self) -> None:
        found = identity.discover_via_gh(gh("notes", ".files", "site"))
        self.assertEqual([c.slug for c in found.candidates], ["alice/.files"])
        self.assertEqual((found.login, found.manual, found.notes), ("alice", None, ()))
        self.assertEqual((found.repo.private, found.repo.default_branch), (True, "main"))

    def test_the_names_are_tried_in_order_and_dot_files_comes_first(self) -> None:
        for names, expected in (
            (("dotfiles",), "alice/dotfiles"),
            ((".dotfiles", "dot-files"), "alice/.dotfiles"),
            (("dot-files",), "alice/dot-files"),
        ):
            with self.subTest(names=names):
                self.assertEqual(identity.discover_via_gh(gh(*names)).repo.slug, expected)

    def test_names_match_case_insensitively(self) -> None:
        self.assertEqual(identity.discover_via_gh(gh("Dotfiles")).repo.slug, "alice/Dotfiles")

    def test_several_matches_pick_the_first_and_say_so(self) -> None:
        found = identity.discover_via_gh(gh("dotfiles", ".files"))
        self.assertEqual([c.slug for c in found.candidates], ["alice/.files", "alice/dotfiles"])
        self.assertEqual(found.repo.slug, "alice/.files")
        (note,) = found.notes
        self.assertIn("alice/.files, alice/dotfiles", note)
        self.assertIn("using alice/.files", note)
        self.assertIn("--dotfiles-repo", note)

    def test_an_archived_repository_does_not_count(self) -> None:
        archived = json.dumps([{"name": ".files", "isArchived": True}, {"name": "dotfiles", "isArchived": False}])
        found = identity.discover_via_gh(FakeProcess({USER: ok("alice"), LIST: ok(archived)}))
        self.assertEqual(found.repo.slug, "alice/dotfiles")

    def test_a_fork_is_not_taken_for_the_users_own_dotfiles(self) -> None:
        listed = json.dumps([{"name": ".files", "isFork": True}, {"name": "dotfiles", "isFork": False}])
        found = identity.discover_via_gh(FakeProcess({USER: ok("alice"), LIST: ok(listed)}))
        self.assertEqual(found.repo.slug, "alice/dotfiles")

    def test_no_matching_repository_is_a_plain_none_not_a_manual_step(self) -> None:
        found = identity.discover_via_gh(gh("notes", "site"))
        self.assertEqual((found.repo, found.manual, found.login), (None, None, "alice"))

    def test_gh_that_is_not_logged_in_is_a_manual_step_with_the_exact_command(self) -> None:
        process = FakeProcess(
            {USER: CommandResult(4, "", "To get started with GitHub CLI, please run:  gh auth login\n")}
        )
        found = identity.discover_via_gh(process)
        self.assertIsNone(found.repo)
        self.assertEqual(found.manual, "not logged in to GitHub: run `gh auth login`")
        self.assertEqual(process.queries, [USER])  # nothing else is asked without a login

    def test_a_missing_gh_is_a_manual_step_naming_the_login_command(self) -> None:
        found = identity.discover_via_gh(FakeProcess())
        self.assertIsNone(found.repo)
        self.assertIn("gh is not installed", found.manual)
        self.assertIn("`gh auth login`", found.manual)

    def test_a_network_failure_is_not_reported_as_a_login_problem(self) -> None:
        process = FakeProcess({USER: CommandResult(1, "", "error connecting to api.github.com\n")})
        found = identity.discover_via_gh(process)
        self.assertIn("error connecting to api.github.com", found.manual)
        self.assertNotIn("gh auth login", found.manual)

    def test_a_failing_or_garbled_listing_is_a_manual_step(self) -> None:
        failing = FakeProcess({USER: ok("alice"), LIST: CommandResult(1, "", "HTTP 502\n")})
        self.assertIn("HTTP 502", identity.discover_via_gh(failing).manual)
        garbled = FakeProcess({USER: ok("alice"), LIST: ok("not json")})
        self.assertIn("not JSON", identity.discover_via_gh(garbled).manual)

    def test_a_full_listing_may_hide_a_match_so_each_missing_name_is_asked_for(self) -> None:
        many = json.loads(listing(*(f"repo{i}" for i in range(identity.LIST_LIMIT - 1))).stdout)
        many.append({"name": "other", "nameWithOwner": "alice/other"})
        view = ("gh", "repo", "view", "alice/dotfiles", "--json", identity.REPO_FIELDS)
        process = FakeProcess(
            {
                USER: ok("alice"),
                LIST: ok(json.dumps(many)),
                view: ok(json.dumps({"name": "dotfiles", "nameWithOwner": "alice/dotfiles", "isPrivate": False})),
            }
        )
        found = identity.discover_via_gh(process)
        self.assertEqual(found.repo.slug, "alice/dotfiles")
        self.assertEqual(found.repo.private, False)

    def test_a_short_listing_is_trusted_and_asks_nothing_more(self) -> None:
        process = gh("notes")
        identity.discover_via_gh(process)
        self.assertEqual(process.queries, [USER, LIST])


class NamesTests(TempDirCase):
    def test_the_clone_target_is_dot_files_for_that_name_else_the_repo_name_in_home(self) -> None:
        home = Path("/home/alice")
        self.assertEqual(identity.default_checkout(RepoRef("alice/.files"), home), home / ".files")
        self.assertEqual(identity.default_checkout(RepoRef("alice/dotfiles"), home), home / "dotfiles")
        self.assertEqual(identity.default_checkout(RepoRef("alice/dot-files"), home), home / "dot-files")
        self.assertEqual(identity.default_checkout(RepoRef("https://example.org/a/.files.git"), home), home / ".files")

    def test_a_configured_clone_target_wins_and_must_be_absolute(self) -> None:
        home = Path("/home/alice")
        self.assertEqual(identity.default_checkout(RepoRef("a/b"), home, "~/src/mine"), home / "src/mine")
        self.assertEqual(identity.default_checkout(RepoRef("a/b"), home, "/srv/mine"), Path("/srv/mine"))
        with self.assertRaisesRegex(ConfigError, "absolute"):
            identity.default_checkout(RepoRef("a/b"), home, "mine")

    def test_a_repo_reference_is_a_slug_a_url_or_a_path_and_never_an_option(self) -> None:
        self.assertTrue(RepoRef("alice/.files").is_slug)
        self.assertFalse(RepoRef("https://example.org/alice/.files").is_slug)
        self.assertFalse(RepoRef("/srv/alice.git").is_slug)
        self.assertEqual(RepoRef("git@example.org:alice/dotfiles.git").name, "dotfiles")
        for bad in ("", "-oProxy=x", "a b/c"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                RepoRef(bad)

    def test_a_repo_reference_must_be_a_slug_a_url_or_an_absolute_path(self) -> None:
        for bad in ("alice/..", "alice/.", "foo", "relative/path/x", "./x"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                RepoRef(bad)
        for good in ("git@example.org:alice/x.git", "ssh://git@example.org/alice/x", "/srv/x.git"):
            self.assertEqual(RepoRef(good).spec, good)

    def test_a_url_or_slug_naming_the_current_or_parent_directory_is_refused(self) -> None:
        for bad in ("https://example.org/alice/..", "ssh://example.org/alice/.", "git@example.org:alice/.."):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                RepoRef(bad)

    def test_two_local_paths_with_the_same_last_names_are_different_repositories(self) -> None:
        self.assertFalse(identity.same_repo("/srv/a/dotfiles.git", "/srv/b/dotfiles.git"))
        self.assertTrue(identity.same_repo("/srv/a/dotfiles.git", "/srv/a/dotfiles"))
        self.assertFalse(identity.same_repo("/srv/a/dotfiles.git", "a/dotfiles"))

    def test_two_named_hosts_must_agree_but_a_slug_fits_any_host(self) -> None:
        self.assertFalse(identity.same_repo("https://github.com/a/b", "https://git.example.org/a/b"))
        self.assertTrue(identity.same_repo("https://git.example.org/a/b", "a/b"))
        self.assertTrue(identity.same_repo("git@git.example.org:a/b.git", "ssh://git.example.org/a/b"))

    def test_url_spellings_of_one_repository_are_the_same_repository(self) -> None:
        for other in (
            "alice/.files",
            "https://github.com/alice/.files.git",
            "git@github.com:Alice/.files.git",
            "https://github.com/alice/.files/",
        ):
            with self.subTest(other=other):
                self.assertTrue(identity.same_repo("alice/.files", other))
        self.assertFalse(identity.same_repo("alice/.files", "https://github.com/bob/.files.git"))
        self.assertFalse(identity.same_repo("alice/.files", "alice/dotfiles"))


class ProfileDirTests(TempDirCase):
    def test_ubuntu_comes_before_ubuntu_tweaks_before_the_repository_root(self) -> None:
        write(self.tmp / "profile.toml")
        write(self.tmp / "ubuntu-tweaks/profile.toml")
        self.assertEqual(identity.find_profile_dir(self.tmp), self.tmp / "ubuntu-tweaks")
        write(self.tmp / "ubuntu/profile.toml")
        self.assertEqual(identity.find_profile_dir(self.tmp), self.tmp / "ubuntu")

    def test_the_repository_root_counts_only_with_a_profile_toml(self) -> None:
        write(self.tmp / "settings/a.ini")  # a mac-era dotfiles layout is not a profile
        self.assertIsNone(identity.find_profile_dir(self.tmp))
        write(self.tmp / "profile.toml")
        self.assertEqual(identity.find_profile_dir(self.tmp), self.tmp)

    def test_a_subdirectory_with_only_settings_is_a_profile(self) -> None:
        write(self.tmp / "ubuntu/settings/power.ini")
        self.assertEqual(identity.find_profile_dir(self.tmp), self.tmp / "ubuntu")

    def test_a_configured_path_replaces_the_search(self) -> None:
        write(self.tmp / "ubuntu/profile.toml")
        write(self.tmp / "linux/box/profile.toml")
        self.assertEqual(identity.find_profile_dir(self.tmp, "linux/box"), self.tmp / "linux/box")
        self.assertIsNone(identity.find_profile_dir(self.tmp, "elsewhere"))
        self.assertEqual(identity.find_profile_dir(self.tmp / "ubuntu", "."), self.tmp / "ubuntu")
        for bad in ("/etc", "../x"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                identity.find_profile_dir(self.tmp, bad)

    def test_an_empty_checkout_has_no_profile(self) -> None:
        (self.tmp / "ubuntu").mkdir()
        self.assertIsNone(identity.find_profile_dir(self.tmp))


class LocateTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {"XDG_STATE_HOME": str(self.tmp / "state")}
        self.cache = self.tmp / "state/ubuntu-tweaks/dotfiles.json"

    def locate(self, process: FakeProcess | None = None, **kwargs: object):
        return identity.locate(home=self.home, env=self.env, process=process or gh(".files"), now=1000.0, **kwargs)

    def test_a_checkout_on_disk_wins_and_gh_is_never_asked(self) -> None:
        write(self.home / "dotfiles/ubuntu/profile.toml")
        process = FakeProcess()
        location = self.locate(process)
        self.assertEqual((location.root, location.source), (self.home / "dotfiles/ubuntu", Source.LOCAL))
        self.assertEqual(process.queries, [])
        self.assertFalse(self.cache.exists())

    def test_local_checkouts_are_tried_in_the_order_of_the_names(self) -> None:
        write(self.home / "dot-files/ubuntu/profile.toml")
        write(self.home / ".dotfiles/ubuntu/profile.toml")
        self.assertEqual(self.locate(FakeProcess()).checkout, self.home / ".dotfiles")
        write(self.home / ".files/ubuntu/profile.toml")
        self.assertEqual(self.locate(FakeProcess()).checkout, self.home / ".files")

    def test_a_checkout_the_module_remembered_beats_the_conventional_ones(self) -> None:
        write(self.home / ".files/ubuntu/profile.toml")
        elsewhere = write(self.tmp / "src/mine/linux/profile.toml").parent
        identity.remember(self.home, self.env, self.tmp / "src/mine", elsewhere)
        self.assertEqual(self.locate(FakeProcess()).root, elsewhere)

    def test_without_a_checkout_the_repository_comes_from_gh_and_is_cached(self) -> None:
        process = gh("dotfiles")
        location = self.locate(process)
        self.assertEqual(location.repo.spec, "alice/dotfiles")
        self.assertEqual((location.checkout, location.root), (self.home / "dotfiles", self.home / "dotfiles/ubuntu"))
        self.assertEqual((location.source, location.login, location.private), (Source.GH, "alice", True))
        self.assertTrue(self.cache.is_file())
        again = self.locate(FakeProcess())  # a second command: no gh at all
        self.assertEqual((again.repo.spec, again.source), ("alice/dotfiles", Source.CACHE))

    def test_the_cache_expires_and_refresh_ignores_it(self) -> None:
        self.locate(gh("dotfiles"))
        process = gh(".files")
        stale = identity.locate(home=self.home, env=self.env, process=process, now=1000.0 + CACHE_TTL_SECONDS + 1)
        self.assertEqual(stale.repo.spec, "alice/.files")
        self.assertTrue(process.queries)
        forced = self.locate(gh("dotfiles"), refresh=True)
        self.assertEqual((forced.repo.spec, forced.source), ("alice/dotfiles", Source.GH))

    def test_the_cache_is_not_reused_after_gh_switched_to_another_account(self) -> None:
        gh_config = self.tmp / "gh-config"
        self.env["GH_CONFIG_DIR"] = str(gh_config)
        write(gh_config / "hosts.yml", "github.com:\n  user: alice\n")
        self.assertEqual(self.locate(gh(".files")).repo.spec, "alice/.files")
        self.assertEqual(self.locate(FakeProcess()).source, Source.CACHE)  # nothing changed: no gh call
        write(gh_config / "hosts.yml", "github.com:\n  user: alice\n  other: carol\n")  # `gh auth switch`
        self.assertEqual(self.locate(gh_account("carol", "dotfiles")).repo.spec, "carol/dotfiles")

    def test_an_unreadable_cache_is_ignored(self) -> None:
        write(self.cache, "{not json")
        self.assertEqual(self.locate(gh(".files")).repo.spec, "alice/.files")

    def test_nothing_found_points_at_the_conventional_place_and_says_why(self) -> None:
        location = self.locate(gh("notes"))
        self.assertIsNone(location.repo)
        self.assertEqual(location.root, self.home / ".files/ubuntu")
        self.assertEqual((location.login, location.manual), ("alice", None))

    def test_a_manual_step_is_not_cached(self) -> None:
        location = self.locate(FakeProcess({USER: CommandResult(4, "", "gh auth login")}))
        self.assertIn("gh auth login", location.manual)
        self.assertFalse(self.cache.exists())

    def test_an_unreachable_github_is_remembered_for_a_minute_only(self) -> None:
        offline = FakeProcess({USER: CommandResult(1, "", "error connecting to api.github.com")})
        first = self.locate(offline)
        self.assertIn("error connecting", first.manual)
        second = self.locate(FakeProcess())  # within the minute: nobody is asked
        self.assertEqual((second.manual, second.source), (first.manual, Source.CACHE))
        later = identity.locate(
            home=self.home, env=self.env, process=gh(".files"), now=1000.0 + identity.FAILURE_TTL_SECONDS + 1
        )
        self.assertEqual((later.manual, later.repo.spec), (None, "alice/.files"))

    def test_an_explicit_repository_is_found_where_the_module_cloned_it(self) -> None:
        elsewhere = write(self.home / "src/setup/linux/profile.toml").parent
        identity.remember(self.home, self.env, self.home / "src/setup", elsewhere, "bob/setup")
        location = self.locate(FakeProcess(), repo_override="bob/setup")
        self.assertEqual((location.root, location.checkout), (elsewhere, self.home / "src/setup"))
        other = self.locate(FakeProcess(), repo_override="carol/setup")  # a different repository: not that checkout
        self.assertEqual(other.checkout, self.home / "setup")

    def test_a_checkout_of_another_repository_is_never_read_as_the_profile(self) -> None:
        write(self.home / ".files/.git/HEAD")
        write(self.home / ".files/ubuntu/profile.toml")  # alice's profile
        origin = ("git", "-C", str(self.home / ".files"), "config", "--get", "remote.origin.url")
        process = FakeProcess({origin: ok("https://github.com/alice/.files.git\n")})
        location = self.locate(process, repo_override="bob/.files")
        self.assertFalse(location.root.exists())
        self.assertIn("is a checkout of https://github.com/alice/.files.git, not of bob/.files", location.manual)
        same = self.locate(process, repo_override="alice/.files")
        self.assertEqual((same.root, same.manual), (self.home / ".files/ubuntu", None))

    def test_an_explicit_repository_skips_the_checkout_scan_and_gh(self) -> None:
        write(self.home / ".files/ubuntu/profile.toml")
        process = FakeProcess()
        location = self.locate(process, repo_override="bob/linux-setup")
        self.assertEqual((location.repo.spec, location.source), ("bob/linux-setup", Source.FLAG))
        self.assertEqual(
            (location.checkout, location.root), (self.home / "linux-setup", self.home / "linux-setup/ubuntu")
        )
        self.assertEqual(process.queries, [])

    def test_the_environment_variable_is_the_same_override(self) -> None:
        self.env[identity.REPO_ENV] = "bob/.files"
        location = self.locate(FakeProcess())
        self.assertEqual(
            (location.repo.spec, location.source, location.checkout), ("bob/.files", Source.ENV, self.home / ".files")
        )

    def test_the_flag_beats_the_environment(self) -> None:
        self.env[identity.REPO_ENV] = "bob/.files"
        self.assertEqual(self.locate(FakeProcess(), repo_override="carol/dotfiles").repo.spec, "carol/dotfiles")

    def test_an_invalid_override_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            self.locate(FakeProcess(), repo_override="--upload-pack=x")

    def test_an_explicit_repository_already_cloned_finds_its_profile(self) -> None:
        write(self.home / "setup/linux/profile.toml")  # not searched: only ubuntu/, ubuntu-tweaks/ and the root are
        write(self.home / "setup/ubuntu-tweaks/profile.toml")
        location = self.locate(FakeProcess(), repo_override="bob/setup")
        self.assertEqual(location.root, self.home / "setup/ubuntu-tweaks")


class MissingLinesTests(TempDirCase):
    def test_a_found_repository_says_init_clones_it(self) -> None:
        location = identity.Location(
            Path("/h/.files/ubuntu"), Path("/h/.files"), RepoRef("alice/.files"), Source.GH, notes=("be careful",)
        )
        text = "\n".join(identity.missing_lines(location))
        self.assertIn("alice/.files", text)
        self.assertIn("ubuntu-tweak init --commit", text)
        self.assertIn("note: be careful", text)

    def test_a_checkout_without_a_profile_is_not_called_uncloned(self) -> None:
        location = identity.Location(self.tmp / "ubuntu", self.tmp, RepoRef("alice/.files"), Source.CACHE, "alice")
        text = "\n".join(identity.missing_lines(location))
        self.assertIn(f"cloned at {self.tmp}, but it holds no ubuntu profile", text)
        self.assertNotIn("not cloned yet", text)
        self.assertIn("(cache, gh login alice)", text)

    def test_nothing_found_names_what_was_looked_for_and_what_to_do(self) -> None:
        text = "\n".join(identity.missing_lines(identity.Location(Path("/h/.files/ubuntu"), login="alice")))
        self.assertIn("no dotfiles repository for alice", text)
        for name in identity.DOTFILES_NAMES:
            self.assertIn(name, text)
        self.assertIn("ubuntu/profile.toml", text)
        self.assertIn("--dotfiles-repo", text)

    def test_a_logged_out_gh_is_a_needs_manual_line(self) -> None:
        location = identity.Location(Path("/h/.files/ubuntu"), manual="not logged in to GitHub: run `gh auth login`")
        self.assertEqual(
            identity.missing_lines(location), ["  needs manual: not logged in to GitHub: run `gh auth login`"]
        )


class ModuleHelperTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {"XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.home)}

    def test_resolve_reports_the_clone_target_of_a_discovered_repository(self) -> None:
        answer = identity.resolve_for_module(self.home, self.env, gh(".files"))
        self.assertEqual(
            (answer["repo"], answer["is_slug"], answer["dir"], answer["profile_dir"]),
            ("alice/.files", True, str(self.home / ".files"), None),
        )

    def test_the_modules_options_override_discovery_and_place_the_checkout(self) -> None:
        self.env[identity.MODULE_CONFIG_ENV] = json.dumps({"repo": "bob/setup", "dir": "~/src/setup", "path": "linux"})
        write(self.home / "src/setup/linux/profile.toml")
        answer = identity.resolve_for_module(self.home, self.env, FakeProcess())
        self.assertEqual(
            (answer["repo"], answer["dir"], answer["profile_dir"]),
            ("bob/setup", str(self.home / "src/setup"), str(self.home / "src/setup/linux")),
        )

    def test_a_configured_dir_is_judged_by_its_own_origin_not_the_occupied_default(self) -> None:
        write(self.home / ".files/.git/HEAD")  # alice's checkout sits on the default path of bob/.files
        self.env[identity.REPO_ENV] = "bob/.files"
        self.env[identity.MODULE_CONFIG_ENV] = json.dumps({"dir": "~/bob-files"})
        answer = identity.resolve_for_module(self.home, self.env, FakeProcess())
        self.assertEqual((answer["dir"], answer["manual"]), (str(self.home / "bob-files"), None))
        origin = ("git", "-C", str(self.home / "bob-files"), "config", "--get", "remote.origin.url")
        write(self.home / "bob-files/.git/HEAD")
        process = FakeProcess({origin: ok("https://github.com/alice/.files.git\n")})
        location = identity.locate(home=self.home, env=self.env, process=process, checkout_dir="~/bob-files")
        self.assertIn("is a checkout of https://github.com/alice/.files.git, not of bob/.files", location.manual)

    def test_the_flag_variable_beats_the_modules_repo_option(self) -> None:
        self.env[identity.MODULE_CONFIG_ENV] = json.dumps({"repo": "bob/setup"})
        self.env[identity.REPO_ENV] = "carol/.files"
        self.assertEqual(identity.resolve_for_module(self.home, self.env, FakeProcess())["repo"], "carol/.files")

    def test_a_local_checkout_has_no_repository_to_compare_with(self) -> None:
        write(self.home / ".files/ubuntu/profile.toml")
        answer = identity.resolve_for_module(self.home, self.env, FakeProcess())
        self.assertEqual((answer["repo"], answer["dir"], answer["source"]), (None, str(self.home / ".files"), "local"))

    def test_a_logged_out_gh_is_passed_on_as_manual(self) -> None:
        answer = identity.resolve_for_module(self.home, self.env, FakeProcess({USER: CommandResult(4, "", "")}))
        self.assertIn("gh auth login", answer["manual"])
        self.assertIsNone(answer["repo"])

    def test_a_configured_checkout_without_a_profile_is_still_the_modules_dir(self) -> None:
        (self.home / "custom-files/.git").mkdir(parents=True)
        self.env[identity.MODULE_CONFIG_ENV] = json.dumps({"dir": "~/custom-files"})
        answer = identity.resolve_for_module(self.home, self.env, gh("notes"))  # gh finds nothing
        self.assertEqual((answer["dir"], answer["profile_dir"]), (str(self.home / "custom-files"), None))

    def test_the_cache_key_changes_with_the_token_of_the_environment(self) -> None:
        plain = identity._gh_account(self.home, self.env)
        self.env["GH_TOKEN"] = "token-a"
        first = identity._gh_account(self.home, self.env)
        self.env["GH_TOKEN"] = "token-b"
        self.assertEqual(len({plain, first, identity._gh_account(self.home, self.env)}), 3)
        self.assertNotIn("token-a", first)  # the token itself is never stored

    def test_the_cli_remembers_a_checkout_and_compares_repositories(self) -> None:
        write(self.tmp / "co/ubuntu/profile.toml")
        self.assertEqual(identity.main(["remember", str(self.tmp / "co")], env=self.env), 0)
        self.assertEqual(
            identity.locate(home=self.home, env=self.env, process=FakeProcess()).root, self.tmp / "co/ubuntu"
        )
        self.assertEqual(identity.main(["same-repo", "a/b", "https://x/A/B.git"], env=self.env), 0)
        self.assertEqual(identity.main(["same-repo", "a/b", "a/c"], env=self.env), 1)
        self.assertEqual(identity.main(["nonsense"], env=self.env), 2)

"""apt-sources against a staged root (APT_SOURCES_ROOT): the real script and the real gpg, a fake curl and apt-get.

Keys are generated here (never a vendor key). curl answers from a map url -> key file / failure, apt-get only logs
its argv, so nothing touches the machine or the network.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SCRIPT = MODULE / "apt-sources"
OFFLINE = "offline"  # the fake curl cannot resolve the host (exit 6)
NOT_FOUND = "404"  # the fake curl gets an HTTP 404 (exit 22)

FAKE_CURL = """\
#!/usr/bin/python3 -B
import json, os, shutil, sys
args = sys.argv[1:]
out = args[args.index("-o") + 1] if "-o" in args else args[args.index("--output") + 1]
url = args[-1]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("curl " + url + "\\n")
answer = json.loads(os.environ["FAKE_CURL_MAP"]).get(url, "404")
if answer == "offline":
    sys.stderr.write("curl: (6) Could not resolve host: example.test\\n")
    sys.exit(6)
if answer == "404":
    sys.stderr.write("curl: (22) The requested URL returned error: 404\\n")
    sys.exit(22)
shutil.copy(answer, out)
"""

FAKE_APT = """\
#!/usr/bin/python3 -B
import os, sys
args = sys.argv[1:]
probe = "indextargets" in args  # the post-condition: does apt accept the configured sources
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(("apt-probe " if probe else "apt-get ") + " ".join(args) + "\\n")
code = int(os.environ.get("FAKE_PROBE_RC" if probe else "FAKE_APT_RC", "0"))
if code:
    sys.stderr.write(os.environ.get("FAKE_PROBE_ERR" if probe else "FAKE_APT_ERR", "E: failed") + "\\n")
sys.exit(code)
"""


def gpg(home, *args):
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--no-tty", *args], check=True, capture_output=True
    ).stdout


def make_key(directory, uid):
    home = Path(directory) / f"gnupg-{uid}"
    home.mkdir(mode=0o700)
    gpg(home, "--passphrase", "", "--quick-generate-key", f"{uid} <{uid}@example.test>", "ed25519", "sign", "never")
    listing = gpg(home, "--with-colons", "--list-keys").decode()
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    binary = Path(directory) / f"{uid}.gpg"
    armored = Path(directory) / f"{uid}.asc"
    binary.write_bytes(gpg(home, "--export"))
    armored.write_bytes(gpg(home, "--armor", "--export"))
    return fingerprint, binary, armored


def source(name="mozilla", **override):
    fields = {
        "name": name,
        "uris": ["https://packages.example.test/apt"],
        "suites": ["stable"],
        "components": ["main"],
        "architectures": ["amd64"],
        "key_url": f"https://keys.example.test/{name}.asc",
    }
    fields.update(override)
    return fields


class Staged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = Path(tempfile.mkdtemp())
        cls.good_fpr, cls.good_bin, cls.good_asc = make_key(cls.keys, "good")
        cls.evil_fpr, cls.evil_bin, cls.evil_asc = make_key(cls.keys, "evil")
        cls.both = cls.keys / "both.asc"
        cls.both.write_bytes(cls.good_asc.read_bytes() + cls.evil_asc.read_bytes())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keys, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("curl", FAKE_CURL), ("apt-get", FAKE_APT)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.log = self.tmp / "log"
        self.log.write_text("")
        self.answers = {}
        self.env_extra = {}
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        (self.root / "etc/apt/preferences.d").mkdir(parents=True)

    def path(self, relative):
        return self.root / relative

    def run_script(self, command, sources, **options):
        config = {"sources": sources, **options}
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "APT_SOURCES_ROOT": str(self.root),
            "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps(config),
            "FAKE_LOG": str(self.log),
            "FAKE_CURL_MAP": json.dumps({url: str(answer) for url, answer in self.answers.items()}),
            "HOME": str(self.tmp / "home"),
            **self.env_extra,
        }
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command], env=env, capture_output=True, text=True, timeout=120
        )

    def calls(self, prefix):
        return [line for line in self.log.read_text().splitlines() if line.startswith(prefix)]

    def good(self, name="mozilla", **override):
        override.setdefault("fingerprint", self.good_fpr)
        item = source(name, **override)
        self.answers.setdefault(item["key_url"], self.good_asc)
        return item

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class Install(Staged):
    def test_check_reports_drift_on_an_empty_machine_and_is_read_only(self):
        before = self.tree()
        result = self.run_script("check", [self.good()])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("mozilla", result.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls("curl"), [])

    def test_install_writes_the_deb822_file_key_and_pin_and_updates_once(self):
        pin = "Package: firefox*\nPin: origin packages.example.test\nPin-Priority: 1000\n"
        result = self.run_script("install", [self.good(pin=pin)])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.path("etc/apt/sources.list.d/mozilla.sources").read_text(),
            "# Managed by ubuntu-tweaks (apt-sources): change [modules.apt-sources] in the profile instead.\n"
            "Types: deb\n"
            "URIs: https://packages.example.test/apt\n"
            "Suites: stable\n"
            "Components: main\n"
            "Architectures: amd64\n"
            "Signed-By: /etc/apt/keyrings/mozilla.asc\n",
        )
        self.assertEqual(self.path("etc/apt/keyrings/mozilla.asc").read_bytes(), self.good_asc.read_bytes())
        self.assertEqual(
            self.path("etc/apt/preferences.d/mozilla").read_text(),
            "# Managed by ubuntu-tweaks (apt-sources): change [modules.apt-sources] in the profile instead.\n" + pin,
        )
        updates = self.calls("apt-get")
        self.assertEqual(len(updates), 1)
        self.assertIn("update", updates[0])
        self.assertIn("Dir::Etc::sourcelist=sources.list.d/mozilla.sources", updates[0])
        self.assertEqual(stat.S_IMODE(self.path("etc/apt/keyrings/mozilla.asc").stat().st_mode), 0o644)
        self.assertEqual(self.run_script("check", [self.good(pin=pin)]).returncode, 0)

    def test_second_run_changes_nothing_and_needs_no_network(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.log.write_text("")
        before = {p: (self.root / p).stat().st_mtime_ns for p in self.tree()}
        again = self.run_script("install", [item])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("nothing to change", again.stdout)
        self.assertEqual(self.calls("curl") + self.calls("apt-get"), [])
        self.assertEqual({p: (self.root / p).stat().st_mtime_ns for p in self.tree()}, before)

    def test_plan_shows_the_exact_diff_and_changes_nothing(self):
        item = self.good()
        plan = self.run_script("plan", [item])
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn("+Signed-By: /etc/apt/keyrings/mozilla.asc", plan.stdout)
        self.assertIn(self.good_fpr, plan.stdout)
        self.assertIn("https://keys.example.test/mozilla.asc", plan.stdout)
        self.assertIn("apt-get update", plan.stdout)
        self.assertEqual(self.tree(), [])
        self.run_script("install", [item])
        self.assertIn("nothing to change", self.run_script("plan", [item]).stdout)

    def test_dry_run_flag_is_the_plan(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--dry-run"],
            env={
                **os.environ,
                "APT_SOURCES_ROOT": str(self.root),
                "UBUNTU_TWEAK_MODULE_CONFIG": json.dumps({"sources": [self.good()]}),
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mozilla.sources", result.stdout)

    def test_key_format_gpg_dearmors_and_verifies_the_final_file(self):
        item = self.good(key_format="gpg")
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        key = self.path("etc/apt/keyrings/mozilla.gpg").read_bytes()
        self.assertNotIn(b"BEGIN PGP", key)
        self.assertEqual(key, self.good_bin.read_bytes())
        self.assertIn(
            "Signed-By: /etc/apt/keyrings/mozilla.gpg", self.path("etc/apt/sources.list.d/mozilla.sources").read_text()
        )
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_table_form_options_equal_the_list_form(self):
        item = self.good()
        name = item.pop("name")
        result = self.run_script("install", {name: item})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.path("etc/apt/sources.list.d/mozilla.sources").is_file())


class KeyVerification(Staged):
    def test_a_key_with_another_fingerprint_is_refused_and_nothing_is_written(self):
        item = source(fingerprint=self.good_fpr)
        self.answers[item["key_url"]] = self.evil_asc
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 1)
        self.assertIn("fingerprint mismatch", result.stdout + result.stderr)
        self.assertIn(self.evil_fpr, result.stdout + result.stderr)
        self.assertEqual(self.tree(), [])
        self.assertEqual(self.calls("apt-get"), [])

    def test_an_extra_primary_key_in_the_file_is_refused(self):
        item = source(fingerprint=self.good_fpr)
        self.answers[item["key_url"]] = self.both
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 1)
        self.assertIn("fingerprint mismatch", result.stdout + result.stderr)
        self.assertEqual(self.tree(), [])

    def test_several_pinned_primaries_accept_exactly_that_set(self):
        item = source(fingerprint=[self.good_fpr, self.evil_fpr])
        self.answers[item["key_url"]] = self.both
        self.assertEqual(self.run_script("install", [item]).returncode, 0)

    def test_a_tampered_installed_key_is_drift_and_is_fetched_again(self):
        item = self.good()
        self.run_script("install", [item])
        self.path("etc/apt/keyrings/mozilla.asc").write_bytes(self.evil_asc.read_bytes())
        checked = self.run_script("check", [item])
        self.assertEqual(checked.returncode, 3)
        self.assertIn("key", checked.stdout)
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.assertEqual(self.path("etc/apt/keyrings/mozilla.asc").read_bytes(), self.good_asc.read_bytes())

    def test_a_cached_key_that_does_not_match_its_own_pin_is_a_permanent_error(self):
        item = source(
            fingerprint=self.good_fpr,
            key_cached=self.evil_asc.read_text(),
        )
        self.answers[item["key_url"]] = OFFLINE
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 1)
        self.assertIn("fingerprint mismatch", result.stdout + result.stderr)

    def test_fingerprint_is_required_and_options_are_validated(self):
        for bad in (
            [source(fingerprint="")],
            [source(fingerprint="1234")],
            [source(key_url="http://insecure.example.test/key")],
            [source(name="Bad Name", fingerprint=self.good_fpr)],
            [source(fingerprint=self.good_fpr, surprise=1)],
            [source(fingerprint=self.good_fpr, key_format="pem")],
            [source(fingerprint=self.good_fpr, suites=[])],
        ):
            result = self.run_script("check", bad)
            self.assertEqual(result.returncode, 1, bad)
            self.assertIn("apt-sources:", result.stderr)

    def test_a_source_without_any_key_location_is_refused(self):
        item = source(fingerprint=self.good_fpr)
        del item["key_url"]
        result = self.run_script("check", [item])
        self.assertEqual(result.returncode, 1)
        self.assertIn("key_url", result.stderr)


class Failover(Staged):
    def test_offline_without_a_cached_key_is_tempfail_and_writes_nothing(self):
        item = self.good()
        self.answers[item["key_url"]] = OFFLINE
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 75, result.stderr + result.stdout)
        self.assertEqual(self.tree(), [])

    def test_offline_falls_back_to_the_cached_key(self):
        item = self.good(key_cached=self.good_asc.read_text())
        self.answers[item["key_url"]] = OFFLINE
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("key_cached", result.stdout)
        self.assertEqual(self.path("etc/apt/keyrings/mozilla.asc").read_bytes(), self.good_asc.read_bytes())

    def test_mirrors_are_tried_in_order_after_key_url(self):
        item = self.good(key_urls=["https://one.example.test/k", "https://two.example.test/k"])
        self.answers[item["key_url"]] = NOT_FOUND
        self.answers["https://one.example.test/k"] = self.evil_asc
        self.answers["https://two.example.test/k"] = self.good_asc
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("key_urls[1]", result.stdout)
        self.assertEqual(
            self.calls("curl"),
            [
                "curl https://keys.example.test/mozilla.asc",
                "curl https://one.example.test/k",
                "curl https://two.example.test/k",
            ],
        )

    def test_one_failing_source_never_blocks_the_others(self):
        broken = source("broken", fingerprint=self.good_fpr)
        self.answers[broken["key_url"]] = self.evil_asc
        fine = self.good("fine")
        result = self.run_script("install", [broken, fine])
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED  broken", result.stdout)
        self.assertIn("ok", next(line for line in result.stdout.splitlines() if "fine" in line))
        self.assertTrue(self.path("etc/apt/sources.list.d/fine.sources").is_file())
        self.assertFalse(self.path("etc/apt/sources.list.d/broken.sources").exists())
        self.assertIn("broken", result.stderr)

    def test_a_failed_index_refresh_is_retried_by_the_next_install(self):
        item = self.good()
        self.env_extra = {
            "FAKE_APT_RC": "100",
            "FAKE_APT_ERR": "W: Temporary failure resolving 'packages.example.test'",
        }
        first = self.run_script("install", [item])
        self.assertEqual(first.returncode, 75, first.stderr + first.stdout)
        self.assertTrue(self.path("etc/apt/sources.list.d/mozilla.sources").is_file())
        pending = self.run_script("check", [item])
        self.assertEqual(pending.returncode, 3)
        self.assertIn("apt index", pending.stdout)
        self.env_extra = {}
        self.log.write_text("")
        second = self.run_script("install", [item])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.calls("apt-get")), 1)
        self.assertEqual(self.calls("curl"), [])
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_a_permanent_update_failure_is_an_error(self):
        self.env_extra = {"FAKE_APT_RC": "100", "FAKE_APT_ERR": "E: The repository does not have a Release file."}
        result = self.run_script("install", [self.good()])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Release file", result.stderr + result.stdout)


class Migration(Staged):
    MOZILLA_LIST = "deb [signed-by=/etc/apt/keyrings/mozilla.asc] https://packages.example.test/apt stable main\n"

    def seed_list(self, name="mozilla.list", text=None):
        path = self.path(f"etc/apt/sources.list.d/{name}")
        path.write_text(text or self.MOZILLA_LIST)
        return path

    def test_a_hand_written_list_of_the_same_repo_is_backed_up_and_replaced(self):
        legacy = self.seed_list()
        item = self.good()
        plan = self.run_script("plan", [item])
        self.assertIn("mozilla.list", plan.stdout)
        self.assertIn("var/backups/ubuntu-tweaks/apt-sources", plan.stdout)
        self.assertTrue(legacy.exists())
        self.assertEqual(self.run_script("check", [item]).returncode, 3)
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(legacy.exists())
        backups = list(self.path("var/backups/ubuntu-tweaks/apt-sources").glob("*/mozilla.list"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.MOZILLA_LIST)
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_a_foreign_deb822_file_of_the_same_repo_is_migrated_too(self):
        foreign = self.path("etc/apt/sources.list.d/vendor.sources")
        foreign.write_text("Types: deb\nURIs: https://packages.example.test/apt/\nSuites: stable\nComponents: main\n")
        self.assertEqual(self.run_script("install", [self.good()]).returncode, 0)
        self.assertFalse(foreign.exists())

    def test_a_foreign_file_with_the_managed_name_is_backed_up_and_replaced(self):
        foreign = self.path("etc/apt/sources.list.d/mozilla.sources")
        foreign.write_text("Types: deb\nURIs: https://packages.example.test/apt\nSuites: stable\nComponents: main\n")
        self.assertEqual(self.run_script("install", [self.good()]).returncode, 0)
        self.assertIn("Managed by ubuntu-tweaks", foreign.read_text())
        self.assertEqual(len(list(self.path("var/backups/ubuntu-tweaks/apt-sources").glob("*/mozilla.sources"))), 1)

    def test_a_hand_written_pin_with_the_managed_name_is_backed_up_and_replaced(self):
        pin = "Package: firefox*\nPin: origin packages.example.test\nPin-Priority: 1000\n"
        old = self.path("etc/apt/preferences.d/mozilla")
        old.write_text("Package: *\nPin: origin packages.example.test\nPin-Priority: 1000\n")
        self.assertEqual(self.run_script("install", [self.good(pin=pin)]).returncode, 0)
        self.assertTrue(old.read_text().endswith(pin))
        self.assertEqual(len(list(self.path("var/backups/ubuntu-tweaks/apt-sources").glob("*/mozilla"))), 1)

    def test_a_file_shared_by_two_sources_stays_until_both_converged(self):
        shared = self.seed_list(
            "vendors.list",
            self.MOZILLA_LIST + "deb https://second.example.test/apt stable main\n",
        )
        first = self.good()
        second = self.good(
            "second",
            uris=["https://second.example.test/apt"],
            key_url="https://keys.example.test/second.asc",
        )
        self.answers[second["key_url"]] = OFFLINE
        failed = self.run_script("install", [first, second])
        self.assertEqual(failed.returncode, 75, failed.stderr + failed.stdout)
        self.assertTrue(shared.exists(), "the only declaration of the repository whose key is missing must stay")
        self.assertTrue(self.path("etc/apt/sources.list.d/mozilla.sources").is_file())
        self.assertIn("vendors.list", failed.stdout)
        self.answers[second["key_url"]] = self.good_asc
        self.assertEqual(self.run_script("check", [first, second]).returncode, 3)
        self.assertEqual(self.run_script("install", [first, second]).returncode, 0)
        self.assertFalse(shared.exists())
        self.assertEqual(self.run_script("check", [first, second]).returncode, 0)

    def test_a_file_that_mixes_other_repos_is_never_edited(self):
        mixed = self.seed_list(
            "mixed.list",
            self.MOZILLA_LIST + "deb https://other.example.test/apt stable main\n",
        )
        item = self.good()
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn("mixed.list", result.stdout)
        self.assertTrue(mixed.exists())
        self.assertTrue(self.path("etc/apt/sources.list.d/mozilla.sources").is_file())
        self.assertIn("mixed.list", self.run_script("check", [item]).stdout)

    def test_unrelated_sources_are_left_alone_and_reported_as_unmanaged(self):
        other = self.path("etc/apt/sources.list.d/ubuntu.sources")
        other.write_text("Types: deb\nURIs: http://archive.example.test/ubuntu\nSuites: resolute\nComponents: main\n")
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.assertTrue(other.exists())
        status = self.run_script("status", [item])
        self.assertEqual(status.returncode, 0)
        self.assertIn("unmanaged", status.stdout)
        self.assertIn("ubuntu.sources", status.stdout)


class Conflict(Staged):
    """Another file declares an enabled source's repository with a different Signed-By: apt rejects every command."""

    FOREIGN_KEY = "/etc/apt/keyrings/packages.mozilla.org.asc"
    LINE = f"deb [signed-by={FOREIGN_KEY}] https://packages.example.test/apt stable main\n"
    OTHER = "deb https://other.example.test/apt stable main\n"
    DIR = "/etc/apt/sources.list.d"

    def seed(self, name="mozilla.list", text=None):
        path = self.path(f"etc/apt/sources.list.d/{name}")
        path.write_text(text or self.LINE)
        return path

    def test_check_names_both_files_and_the_fix_even_when_the_managed_file_is_already_correct(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.seed()
        before = self.tree()
        result = self.run_script("check", [item])
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        for needle in (
            f"{self.DIR}/mozilla.list",
            f"{self.DIR}/mozilla.sources",
            self.FOREIGN_KEY,
            "/etc/apt/keyrings/mozilla.asc",
            f"sudo rm {self.DIR}/mozilla.list",
        ):
            self.assertIn(needle, result.stdout)
        self.assertEqual(self.tree(), before)

    def test_install_removes_the_conflicting_duplicate_before_it_writes_the_managed_file(self):
        legacy = self.seed()
        item = self.good()
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(legacy.exists())
        backups = list(self.path("var/backups/ubuntu-tweaks/apt-sources").glob("*/mozilla.list"))
        self.assertEqual([path.read_text() for path in backups], [self.LINE])
        removal = next(line for line in result.stdout.splitlines() if "removed mozilla.list" in line)
        self.assertIn("mozilla.sources", removal)
        self.assertLess(result.stdout.index("removed mozilla.list"), result.stdout.index("wrote mozilla.sources"))
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_a_conflicting_duplicate_beside_a_correct_managed_file_goes_without_an_index_refresh(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        legacy = self.seed()
        self.log.write_text("")
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.calls("curl") + self.calls("apt-get"), [])
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_a_deb822_file_with_another_signed_by_is_migrated_too(self):
        foreign = self.path("etc/apt/sources.list.d/vendor.sources")
        foreign.write_text(
            "Types: deb\nURIs: https://packages.example.test/apt/\nSuites: stable\nComponents: main\n"
            f"Signed-By: {self.FOREIGN_KEY}\n"
        )
        item = self.good()
        self.assertEqual(self.run_script("check", [item]).returncode, 3)
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.assertFalse(foreign.exists())

    def test_a_shared_conflicting_file_stays_and_no_managed_file_is_written_while_a_co_owner_key_fails(self):
        shared = self.seed("vendors.list", self.LINE + "deb https://second.example.test/apt stable main\n")
        first = self.good()
        second = self.good(
            "second", uris=["https://second.example.test/apt"], key_url="https://keys.example.test/second.asc"
        )
        self.answers[second["key_url"]] = OFFLINE
        failed = self.run_script("install", [first, second])
        self.assertEqual(failed.returncode, 75, failed.stderr + failed.stdout)
        self.assertTrue(shared.exists())
        self.assertFalse(self.path("etc/apt/sources.list.d/mozilla.sources").exists())
        self.assertIn("held", failed.stdout)
        self.assertIn("vendors.list", failed.stdout)
        self.answers[second["key_url"]] = self.good_asc
        self.assertEqual(self.run_script("install", [first, second]).returncode, 0)
        self.assertFalse(shared.exists())
        self.assertTrue(self.path("etc/apt/sources.list.d/mozilla.sources").is_file())
        self.assertEqual(self.run_script("check", [first, second]).returncode, 0)

    def test_a_mixed_file_blocks_the_source_and_names_both_files_and_the_lines_to_delete(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        mixed = self.seed("mixed.list", self.LINE + self.OTHER)
        before = self.tree()
        for command, code in (("check", 3), ("install", 10)):
            result = self.run_script(command, [item])
            self.assertEqual(result.returncode, code, result.stderr + result.stdout)
            for needle in (f"{self.DIR}/mixed.list", f"{self.DIR}/mozilla.sources", self.LINE.strip()):
                self.assertIn(needle, result.stdout)
        self.assertEqual(mixed.read_text(), self.LINE + self.OTHER)
        self.assertEqual(self.tree(), before)

    def test_a_blocked_first_install_writes_nothing_for_that_source(self):
        self.seed("mixed.list", self.LINE + self.OTHER)
        item = self.good()
        before = self.tree()
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn(f"{self.DIR}/mozilla.sources", result.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls("curl") + self.calls("apt-get"), [])

    def test_a_conflicting_line_in_sources_list_blocks_the_source_the_same_way(self):
        main = self.path("etc/apt/sources.list")
        main.write_text(self.LINE)
        item = self.good()
        before = self.tree()
        for command, code in (("check", 3), ("install", 10)):
            result = self.run_script(command, [item])
            self.assertEqual(result.returncode, code, result.stderr + result.stdout)
            for needle in ("/etc/apt/sources.list:", f"{self.DIR}/mozilla.sources", self.LINE.strip()):
                self.assertIn(needle, result.stdout)
        self.assertEqual(main.read_text(), self.LINE)
        self.assertEqual(self.tree(), before)

    def test_a_blocked_source_does_not_stop_the_others(self):
        self.seed("mixed.list", self.LINE + self.OTHER)
        blocked = self.good()
        other = self.good("second", uris=["https://second.example.test/apt"], key_url="https://keys.example.test/s.asc")
        result = self.run_script("install", [blocked, other])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertFalse(self.path("etc/apt/sources.list.d/mozilla.sources").exists())
        self.assertTrue(self.path("etc/apt/sources.list.d/second.sources").is_file())

    def test_a_shared_file_is_kept_while_a_co_owner_is_blocked(self):
        shared = self.seed(
            "vendors.list",
            "deb [signed-by=/etc/apt/keyrings/second.asc] https://second.example.test/apt stable main\n"
            "deb [signed-by=/etc/apt/keyrings/mozilla.asc] https://packages.example.test/apt stable main\n",
        )
        self.seed("mixed.list", self.LINE + self.OTHER)
        first = self.good()
        second = self.good(
            "second", uris=["https://second.example.test/apt"], key_url="https://keys.example.test/second.asc"
        )
        result = self.run_script("install", [first, second])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertTrue(shared.exists(), "the blocked source's only declaration must stay")

    def test_a_blocked_first_owner_does_not_let_a_co_owner_write_next_to_the_shared_clashing_file(self):
        shared = self.seed(
            "vendors.list",
            self.LINE + "deb [signed-by=/etc/apt/keyrings/vendor2.asc] https://second.example.test/apt stable main\n",
        )
        self.seed("mixed.list", self.LINE + self.OTHER)  # blocks the first owner
        first = self.good()
        second = self.good(
            "second", uris=["https://second.example.test/apt"], key_url="https://keys.example.test/second.asc"
        )
        result = self.run_script("install", [first, second])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertTrue(shared.exists())
        self.assertFalse(self.path("etc/apt/sources.list.d/second.sources").exists())

    def test_a_disabled_stanza_of_another_repository_still_makes_the_file_mixed(self):
        mixed = self.seed(
            "vendor.sources",
            "Types: deb\nURIs: https://packages.example.test/apt\nSuites: stable\nComponents: main\n"
            "Signed-By: /etc/apt/keyrings/other.asc\n\n"
            "Types: deb\nURIs: https://disabled.example.test/apt\nSuites: stable\nComponents: main\nEnabled: no\n",
        )
        before = mixed.read_text()
        result = self.run_script("install", [self.good()])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertEqual(mixed.read_text(), before, "a file that declares anything else is never edited")

    def test_a_held_owner_holds_the_other_clashing_file_it_shares_too(self):
        # a's key cannot be fetched: z-ab.list is held, so b has no managed file; a-bc.list (sorted first, so a
        # single pass would remove it) may be b's only declaration of its second repository and must stay as well
        ab = self.seed(
            "z-ab.list",
            self.LINE + "deb [signed-by=/etc/apt/keyrings/vendor.asc] https://b.example.test/apt stable main\n",
        )
        bc = self.seed(
            "a-bc.list",
            "deb [signed-by=/etc/apt/keyrings/vendor.asc] https://b.example.test/apt stable main\n"
            "deb [signed-by=/etc/apt/keyrings/vendor.asc] https://c.example.test/apt stable main\n",
        )
        a = self.good()
        b = self.good("b", uris=["https://b.example.test/apt"], key_url="https://keys.example.test/b.asc")
        c = self.good("c", uris=["https://c.example.test/apt"], key_url="https://keys.example.test/c.asc")
        self.answers[a["key_url"]] = OFFLINE
        result = self.run_script("install", [b, c, a])  # b's files are listed before the one it shares with a
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(ab.exists() and bc.exists())
        self.assertFalse(self.path("etc/apt/sources.list.d/b.sources").exists())

    def test_a_held_source_keeps_its_other_legacy_files(self):
        # vendors.list (shared, clashing) is held because mozilla is blocked; second-b.list is second's only
        # declaration of its other repository and must stay until second.sources is written
        self.seed(
            "vendors.list",
            self.LINE + "deb [signed-by=/etc/apt/keyrings/vendor2.asc] https://second.example.test/apt stable main\n",
        )
        self.seed("mixed.list", self.LINE + self.OTHER)
        legacy = self.seed("second-b.list", "deb https://second-b.example.test/apt stable main\n")
        first = self.good()
        second = self.good(
            "second",
            uris=["https://second.example.test/apt", "https://second-b.example.test/apt"],
            key_url="https://keys.example.test/second.asc",
        )
        result = self.run_script("install", [first, second])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertTrue(legacy.exists(), "no managed file replaces it yet")

    def test_a_disabled_deb822_stanza_is_not_a_declaration(self):
        inactive = self.seed(
            "vendor.sources",
            "Types: deb\nURIs: https://packages.example.test/apt\nSuites: stable\nComponents: main\n"
            "Enabled: no\nSigned-By: /etc/apt/keyrings/other.asc\n",
        )
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.assertTrue(inactive.exists(), "apt ignores it: the module has nothing to migrate")
        self.assertEqual(self.run_script("check", [item]).returncode, 0)


class PostCondition(Staged):
    CAUSE = (
        "E: Conflicting values set for option Signed-By regarding source https://packages.example.test/apt/ "
        "stable: /etc/apt/keyrings/a.asc != /etc/apt/keyrings/b.asc"
    )

    def test_check_asks_apt_and_is_satisfied_when_it_accepts(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        status = self.run_script("status", [item])
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        self.assertIn("apt accepts the configured sources", status.stdout)
        probe = self.calls("apt-probe")[-1]
        self.assertIn(f"Dir::Etc::sourceparts={self.root}/etc/apt/sources.list.d", probe)

    def test_check_is_drift_with_the_cause_when_apt_rejects_the_sources(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.env_extra = {"FAKE_PROBE_RC": "100", "FAKE_PROBE_ERR": self.CAUSE}
        result = self.run_script("check", [item])
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertIn("apt accepts the configured sources", result.stdout)
        self.assertIn(self.CAUSE, result.stdout)
        self.assertIn("/etc/apt/sources.list.d/mozilla.sources", result.stdout)

    def test_install_fails_with_the_cause_and_the_files_when_apt_still_rejects(self):
        self.env_extra = {"FAKE_PROBE_RC": "100", "FAKE_PROBE_ERR": self.CAUSE}
        result = self.run_script("install", [self.good()])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn(self.CAUSE, result.stderr)
        self.assertIn("/etc/apt/sources.list.d/mozilla.sources", result.stderr)

    def test_install_asks_apt_last_and_succeeds_when_it_accepts(self):
        result = self.run_script("install", [self.good()])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        lines = self.log.read_text().splitlines()
        self.assertTrue(lines[-1].startswith("apt-probe"), lines)
        self.assertTrue(any(line.startswith("apt-get update") for line in lines[:-1]))

    def test_a_converged_machine_is_not_reported_as_nothing_to_change_while_apt_rejects_it(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.env_extra = {"FAKE_PROBE_RC": "100", "FAKE_PROBE_ERR": self.CAUSE}
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn(self.CAUSE, result.stderr)

    def test_a_pending_refresh_of_a_blocked_source_is_kept_and_not_run(self):
        mixed = "deb [signed-by=/etc/apt/keyrings/x.asc] https://packages.example.test/apt stable main\n"
        self.path("etc/apt/sources.list.d/mixed.list").write_text(mixed + "deb https://other.example.test/apt s main\n")
        pending = self.path("var/lib/ubuntu-tweaks/apt-sources/pending-update")
        pending.parent.mkdir(parents=True)
        pending.write_text("mozilla\n")
        other = self.good("second", uris=["https://second.example.test/apt"], key_url="https://keys.example.test/s.asc")
        result = self.run_script("install", [self.good(), other])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn("mozilla", pending.read_text())
        updates = [line for line in self.calls("apt-get") if "update" in line]
        self.assertFalse(any("mozilla.sources" in line for line in updates), updates)

    def test_an_unrelated_conflict_does_not_hide_an_error_that_names_no_repository(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        both = (
            "E: Conflicting values set for option Signed-By regarding source https://elsewhere.example.test/apt/ "
            "stable: /a != /b\nE: Malformed entry 1 in list file /etc/apt/sources.list.d/mozilla.sources (Suites)\n"
            "E: The list of sources could not be read."
        )
        self.env_extra = {"FAKE_PROBE_RC": "100", "FAKE_PROBE_ERR": both}
        result = self.run_script("check", [item])
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertIn("Malformed entry 1", result.stdout)
        self.assertNotIn("elsewhere.example.test", result.stdout)

    def test_apt_complaining_only_about_a_repository_the_module_does_not_manage_is_not_this_modules_failure(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        unrelated = (
            "E: Conflicting values set for option Signed-By regarding source https://elsewhere.example.test/apt/ "
            "stable: /a != /b\nE: The list of sources could not be read."
        )
        self.env_extra = {"FAKE_PROBE_RC": "100", "FAKE_PROBE_ERR": unrelated}
        for command in ("check", "install"):
            result = self.run_script(command, [item])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_a_known_manual_conflict_stays_a_manual_step_with_the_cause_appended(self):
        mixed = "deb [signed-by=/etc/apt/keyrings/x.asc] https://packages.example.test/apt stable main\n"
        self.path("etc/apt/sources.list.d/mixed.list").write_text(mixed + "deb https://other.example.test/apt s main\n")
        self.env_extra = {"FAKE_PROBE_RC": "100", "FAKE_PROBE_ERR": self.CAUSE}
        result = self.run_script("install", [self.good()])
        self.assertEqual(result.returncode, 10, result.stderr + result.stdout)
        self.assertIn("mixed.list", result.stdout)
        self.assertIn(self.CAUSE, result.stdout)


@unittest.skipUnless(shutil.which("apt-get"), "apt-get is not installed")
class RealApt(Staged):
    """The real apt-get on the staged tree: the conflict is real, so is its disappearance."""

    def test_real_apt_rejects_the_conflicting_tree_and_accepts_it_after_the_migration(self):
        item = self.good()
        self.assertEqual(self.run_script("install", [item]).returncode, 0)  # with the fake apt-get: no network
        (self.bin / "apt-get").unlink()
        legacy = self.path("etc/apt/sources.list.d/mozilla.list")
        legacy.write_text(Conflict.LINE)
        before = self.run_script("check", [item])
        self.assertEqual(before.returncode, 3, before.stderr + before.stdout)
        self.assertIn("Conflicting values set for option Signed-By", before.stdout)
        after = self.run_script("install", [item])  # nothing is written: no index refresh, no network
        self.assertEqual(after.returncode, 0, after.stderr + after.stdout)
        self.assertFalse(legacy.exists())
        self.assertEqual(self.run_script("check", [item]).returncode, 0)


class Undeclared(Staged):
    def test_a_file_this_module_wrote_for_a_source_that_was_dropped_is_a_manual_step(self):
        item = self.good()
        self.run_script("install", [item])
        written = self.path("etc/apt/sources.list.d/mozilla.sources")
        checked = self.run_script("check", [])
        self.assertEqual(checked.returncode, 3)
        self.assertIn("no longer declared", checked.stdout)
        installed = self.run_script("install", [])
        self.assertEqual(installed.returncode, 10)
        self.assertIn("mozilla.sources", installed.stdout)
        self.assertTrue(written.exists(), "never removed without the owner saying enabled = false")
        self.assertEqual(self.run_script("install", [{**item, "enabled": False}]).returncode, 0)
        self.assertFalse(written.exists())


class Disabled(Staged):
    def test_a_disabled_source_is_absent_and_needs_no_key(self):
        item = source("docker", enabled=False, fingerprint="")
        result = self.run_script("install", [item])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tree(), [])
        self.assertEqual(self.calls("curl"), [])
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_disabling_removes_what_the_module_wrote_and_nothing_else(self):
        item = self.good(pin="Package: x\nPin: origin y\nPin-Priority: 1\n")
        self.run_script("install", [item])
        keep = self.path("etc/apt/keyrings/someone-else.asc")
        keep.write_text("not ours")
        item["enabled"] = False
        self.assertEqual(self.run_script("check", [item]).returncode, 3)
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.assertEqual([p for p in self.tree() if "mozilla" in p and "backups" not in p], [])
        self.assertTrue(keep.exists())
        self.assertEqual(len(list(self.path("var/backups/ubuntu-tweaks/apt-sources").glob("*/mozilla.sources"))), 1)
        self.assertEqual(self.run_script("check", [item]).returncode, 0)

    def test_a_foreign_file_of_a_disabled_source_is_untouched(self):
        foreign = self.path("etc/apt/sources.list.d/docker.sources")
        foreign.write_text("Types: deb\nURIs: https://x.example.test\nSuites: s\nComponents: c\n")
        item = source("docker", enabled=False, fingerprint="")
        self.assertEqual(self.run_script("install", [item]).returncode, 0)
        self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()

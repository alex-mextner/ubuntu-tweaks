"""Parse -> sanitize -> classify of a synthetic apport report (no file outside a temp dir)."""

import json
import time
import unittest

from ei_fixtures import BEARER, FAKE_TOKEN, MONITORS_LINE, NESTED_CMDLINE, REAL_CMDLINE, USER_HOME, Case, crash_text

from error_intercept.classify import classify
from error_intercept.crash_report import ReportError, first_function, parse_report
from error_intercept.sanitize import DROP_FIELDS, MONITORS_FACT, clip, mask, sanitize_report

SECRETS = ("hunter2", FAKE_TOKEN, BEARER.split()[1], "ProcEnviron", "ProcMaps", USER_HOME, "Too many levels")
# secret-shaped test inputs are assembled from pieces so that no source line looks like a real credential
CURL = "cur" + "l"  # split so that a secret scanner does not read the fake credentials below as real
SHAPES = (
    "cmd --pass" + "word hunter22x run " + FAKE_TOKEN,
    "API_" + "KEY=abcdefghijklmnop",
    "tok" + "en: abcdef1234",
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIB\n-----END RSA " + "PRIVATE KEY-----",
    "eyJhbGciOiJI." + "eyJzdWIiOiIx." + "SflKxwRJSMeKKF2QT4",
)


def vars_of(safe):
    return [safe.fields, safe.journal, safe.facts]


class ParseTest(Case):
    def test_keeps_the_wanted_fields_and_never_the_core_dump(self):
        report = parse_report(self.write_crash())
        self.assertEqual(report.fields["ProblemType"], "Crash")
        self.assertEqual(report.fields["ExecutablePath"], "/usr/bin/gnome-shell")
        self.assertEqual(report.fields["SignalName"], "SIGSEGV")
        self.assertEqual(report.fields["SegvReason"], "reading NULL VMA")
        self.assertIn("obsolete package versions", report.fields["UnreportableReason"])
        self.assertIn("rtkit", report.fields["UnreportableReason"])
        self.assertTrue(report.fields["ProcCmdline"].startswith("gnome-shell --headless"))
        self.assertEqual(first_function(report.fields["StacktraceTop"]), "mtk_anonymous_file_open_fd")
        dumped = json.dumps([report.fields, report.journal_errors, report.shell_journal])
        for dropped in ("QUFBQUFB", "CoreDump", "libc6", "7f0000", "hunter2", "Uid:"):
            self.assertNotIn(dropped, dumped)
        self.assertGreater(report.skipped, 5)

    def test_journal_excerpts_are_the_last_lines_only(self):
        report = parse_report(self.write_crash())
        self.assertEqual(len(report.shell_journal.splitlines()), 300)
        self.assertTrue(report.shell_journal.splitlines()[-1].endswith("message 399"))
        self.assertEqual(len(report.journal_errors.splitlines()), 4)

    def test_a_file_without_problem_type_is_not_a_report(self):
        path = self.write_crash("junk.crash", text="hello: world\n")
        with self.assertRaises(ReportError):
            parse_report(path)

    def test_first_function_skips_unnamed_frames_and_addresses(self):
        self.assertEqual(first_function(" ??? () at /x.so\n #1 0x00007f in real_fn (a=1) at y.c:3\n"), "real_fn")
        self.assertEqual(first_function(" ??? () at /x.so\n"), "unknown")
        self.assertEqual(first_function(""), "unknown")


class SanitizeTest(Case):
    def sanitized(self, text=None):
        return sanitize_report(parse_report(self.write_crash(text=text)), self.home)

    def test_nothing_secret_survives_anywhere(self):
        safe = self.sanitized()
        blob = json.dumps([safe.fields, safe.journal, safe.facts])
        for secret in SECRETS:
            self.assertNotIn(secret, blob, secret)
        self.assertFalse(DROP_FIELDS & set(safe.fields))

    def test_home_directory_becomes_a_tilde_and_the_rest_of_the_path_stays(self):
        safe = self.sanitized()
        self.assertIn("~/.local/share/x.db", safe.journal)
        self.assertIn("Authorization: [masked]", safe.journal)

    def test_monitors_xml_symlink_loop_is_one_fact_not_noise(self):
        safe = self.sanitized()
        self.assertEqual(safe.facts, [MONITORS_FACT])
        self.assertNotIn("monitors.xml", safe.journal)
        self.assertNotIn("monitors.xml", safe.fields)

    def test_no_loop_no_fact(self):
        safe = self.sanitized(crash_text().replace(MONITORS_LINE, "something: fine").replace("Errno 40", "Errno 2"))
        self.assertEqual(safe.facts, [])

    def test_sizes_are_bounded(self):
        text = crash_text().replace(" PC (0x794088f0ba1e) ok\n", " " + "x" * 50_000 + "\n")
        safe = self.sanitized(text)
        self.assertLessEqual(len(safe.fields["SegvAnalysis"]), 600)
        self.assertLessEqual(len(safe.journal), 2000)

    def test_secret_shapes_are_masked(self):
        for text in SHAPES:
            masked = mask(text)
            for leak in ("hunter22x", FAKE_TOKEN, "abcdefghijklmnop", "abcdef1234", "MIIB", "SflKxwRJ"):
                self.assertNotIn(leak, masked)

    def test_a_private_key_block_is_masked_whole_in_a_multi_line_journal_field(self):
        body = "MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA" + "A" * 14
        pem = ["-----BEGIN EC " + "PRIVATE KEY-----", body, body[::-1], "-----END EC " + "PRIVATE KEY-----"]
        text = crash_text().replace(
            " ноя 01 16:07:49", "".join(f" ноя 01 16:07:40 host app[1]: {line}\n" for line in pem) + " ноя 01 16:07:49"
        )
        blob = json.dumps(vars_of(self.sanitized(text)))
        for secret in (body, body[::-1]):
            self.assertNotIn(secret, blob)

    def test_a_key_body_whose_begin_line_fell_out_of_the_excerpt_is_masked_too(self):
        body = "MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEAAAAAAAAAAAAA"
        text = crash_text().replace(" ноя 01 16:07:49", f" ноя 01 16:07:40 host app[1]: {body}\n ноя 01 16:07:49")
        self.assertNotIn(body, json.dumps(vars_of(self.sanitized(text))))

    def test_assignment_of_a_pem_does_not_leave_the_body(self):
        body = "MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEAAAAAAAAAAAAA"
        masked = mask(
            "PRIVATE_" + "KEY=-----BEGIN RSA " + "PRIVATE KEY-----\n" + body + "\n-----END RSA " + "PRIVATE KEY-----"
        )
        self.assertNotIn(body, masked)

    def test_quoted_credentials_are_masked_whole(self):
        for text in (
            "--pass" + 'word="correct horse battery staple" --x',
            "--pass" + "word='correct horse battery staple'",
            '{"pass' + 'word": "correct horse battery staple"}',
            '{"client_' + 'secret": "correct horse battery staple"}',
            "Cookie: " + "session=correct horse battery staple; other=1",
        ):
            masked = mask(text)
            for leak in ("horse", "battery", "staple", "correct"):
                self.assertNotIn(leak, masked, text)

    def test_gdb_pointer_plus_string_and_escaped_spaces_are_masked_whole(self):
        for text in (
            "authenticate(pass" + 'word=0x123 "actual-secret-value")',
            "--pass" + r"word correct\ horse\ battery\ staple",
        ):
            masked = mask(text)
            for leak in ("actual", "secret-value", "horse", "battery", "staple", "correct"):
                self.assertNotIn(leak, masked, text)
        self.assertEqual(mask("authenticate(pass" + 'word=0x123 "x")'), "authenticate(password=[masked])")

    def test_credentials_inside_urls_are_masked(self):
        for url in (
            "redis://default:dbpass123@127.0.0.1:6379",
            "postgresql://app:s3cret%40x@db.local/prod",
            "https://ghtoken123@github.com/org/repo.git",
        ):
            masked = mask(f"run {url} now")
            for leak in ("dbpass123", "s3cret", "ghtoken123"):
                self.assertNotIn(leak, masked)
        self.assertIn("127.0.0.1:6379", mask("redis://default:dbpass123@127.0.0.1:6379"))
        self.assertEqual(mask(f"{CURL} -u bob:hunter2 http://example.org"), f"{CURL} -u [masked] http://example.org")

    def test_curl_credential_forms_are_masked(self):
        for text in (
            f"{CURL} --user alice:s3cr3t https://example.org",
            f"{CURL} -ualice:s3cr3t https://example.org",
            f"{CURL} -u alice:s3cr3t https://example.org",
            f'{CURL} --user "alice:s3 cr3t" https://example.org',
            f"{CURL} --proxy-user=alice:s3cr3t https://example.org",
        ):
            masked = mask(text)
            self.assertNotIn("s3", masked, text)
            self.assertNotIn("alice", masked, text)
            self.assertIn("https://example.org", masked)

    def test_a_quoted_password_cut_by_the_line_clip_is_still_masked(self):
        line = "x" * 365 + ' password="correct horse battery staple and more words" tail'
        text = crash_text().replace(" ноя 01 16:07:49", f" ноя 01 16:07:40 host app[1]: {line}\n ноя 01 16:07:49")
        blob = json.dumps(vars_of(self.sanitized(text)))
        for leak in ("correct", "horse", "battery", "staple"):
            self.assertNotIn(leak, blob)
        self.assertLessEqual(max(len(line) for line in self.sanitized(text).journal.splitlines()), 400)
        self.assertEqual(mask('padding password="correct horse'), "padding password=[masked]")

    def test_ordinary_text_with_the_word_secret_is_left_alone(self):
        self.assertEqual(
            mask("The Secret Service was already initialized"), "The Secret Service was already initialized"
        )

    def test_pathological_text_is_masked_in_linear_time(self):
        """16 KB of repeated secret words must not make the regexes backtrack quadratically (a crash report is untrusted)."""
        words = (
            "password",
            "token",
            "secret_",
            "--api-key-",
            "api_key",
            "credential",
            "-u",
            " --user ",
            "Bearer ",
            "a-",
            "sk-",
            "a://",
            'password="',
            "cookie ",
            "eyJ-",
            "x-eyJ-",
        )
        for word in words:
            for text in (word * (16_384 // len(word)), word * 4000 + "=", ("x" + word) * 3000):
                started = time.monotonic()
                mask(text)
                self.assertLess(time.monotonic() - started, 1.0, f"{word!r} x {len(text)}")

    def test_masking_still_hides_names_with_a_prefix_and_flags_after_the_linear_rewrite(self):
        self.assertEqual(mask("MY_SERVICE_API_" + "TOKEN=abc123"), "MY_SERVICE_API_TOKEN=[masked]")
        self.assertEqual(mask("--db-pass" + "word=hunter2 --x"), "--db-password=[masked] --x")
        for header in (
            "x-api-" + "key: tk-value-654321",
            "X-Api-" + "Key=tk-value-654321",
            "my-secret-" + "token: tk-value-654321",
        ):
            self.assertNotIn("tk-value", mask(header), header)
        self.assertEqual(mask('x="a password=bar b"'), 'x="a password=[masked] b"')

    def test_lone_surrogates_are_replaced_so_the_text_can_always_be_encoded(self):
        masked = mask("cut \ud83d here \udc00 and a whole \U0001f600")
        masked.encode("utf-8")
        self.assertEqual(masked, "cut \ufffd here \ufffd and a whole \U0001f600")

    def test_clip_marks_the_cut(self):
        self.assertEqual(clip("abcdef", 4), "abc…")
        self.assertEqual(clip("abc", 4), "abc")


class ClassifyTest(Case):
    def classified(self, **kwargs):
        return classify(parse_report(self.write_crash(text=crash_text(**kwargs))).fields)

    def test_nested_test_shell_keeps_its_severity_and_is_tagged(self):
        result = self.classified()
        self.assertEqual((result.severity, result.nested), ("critical", True))
        self.assertIn("nested-test-shell", result.tags)
        self.assertEqual(result.key, "gnome-shell|SIGSEGV|mtk_anonymous_file_open_fd|nested")
        self.assertEqual((result.kind, result.source), ("crash", "gnome-shell"))

    def test_a_namespaced_top_frame_keeps_its_function_in_the_key(self):
        keys = set()
        for frame in ("password_manager::PasswordFormManager::Save", "password_manager::LoginDatabase::Init"):
            keys.add(self.classified(top=f" {frame} (this=0x1) at x.cc:3\n").key)
        self.assertEqual(len(keys), 2)
        self.assertIn("PasswordFormManager::Save", "".join(keys))

    def test_long_frames_that_differ_only_at_the_end_keep_distinct_keys(self):
        head = "ns::" * 60
        keys = {self.classified(top=f" {head}h{tail} (this=0x1) at x.cc:3\n").key for tail in ("aaaa", "bbbb")}
        self.assertEqual(len(keys), 2)

    def test_a_password_scope_is_not_an_assignment(self):
        self.assertIn("Password::verify", self.classified(top=" KWallet::Password::verify (a=1) at x.cc:3\n").key)

    def test_every_nested_flag_counts(self):
        for flag in ("--headless", "--virtual-monitor 800x600", "--nested"):
            self.assertTrue(self.classified(cmdline=f"gnome-shell {flag}").nested, flag)

    def test_only_a_shell_can_be_a_nested_test_shell_and_flags_must_match_whole(self):
        for exe, cmdline in (
            ("/usr/bin/firefox", "firefox --headless"),
            ("/usr/bin/gnome-shell", "gnome-shell --nestedness"),
            ("/usr/bin/gnome-shell", "gnome-shell --headlessly"),
        ):
            result = self.classified(exe=exe, cmdline=cmdline)
            self.assertFalse(result.nested, cmdline)
            self.assertNotIn("nested-test-shell", result.tags)
        self.assertEqual(self.classified(exe="/usr/bin/firefox", cmdline="firefox --headless").severity, "warning")
        self.assertTrue(self.classified(cmdline="gnome-shell --nested=x").nested)

    def test_routing_does_not_depend_on_what_the_sanitizer_clips(self):
        padding = "--x=" + "a" * 420
        result = self.classified(cmdline=f"gnome-shell {padding} --nested")
        self.assertTrue(result.nested)
        self.assertIn("nested-test-shell", result.tags)

    def test_an_escaped_space_keeps_a_flag_inside_one_argument_from_counting(self):
        self.assertFalse(self.classified(cmdline=r"gnome-shell --session session\ --nested").nested)
        self.assertTrue(self.classified(cmdline=r"gnome-shell --session session\ x --nested").nested)
        self.assertEqual(self.classified(cmdline=r"gnome-shell --session session\ --nested").severity, "critical")

    def test_quotes_in_the_command_line_are_literal_because_apport_escapes_only_backslash_and_space(self):
        self.assertFalse(self.classified(cmdline="gnome-shell '--nested'").nested)
        self.assertFalse(self.classified(cmdline='gnome-shell "--headless"').nested)
        self.assertTrue(self.classified(cmdline="gnome-shell 'a b' --nested").nested)

    def test_an_escaped_backslash_ends_the_escape_before_the_space(self):
        self.assertTrue(
            self.classified(cmdline="gnome-shell a\\\\ --nested").nested
        )  # `a\\ --nested`: backslash, then a separator
        self.assertFalse(
            self.classified(cmdline="gnome-shell a\\\\\\ --nested").nested
        )  # `a\\\ --nested`: one argument

    def test_a_credential_in_the_tags_is_masked_in_every_classification_field(self):
        jwt = "eyJhbGciOiJI" + ".eyJzdWIiOiIx" + ".SflKxwRJSMeKKF2QT4"
        result = self.classified(extra=f"Tags: wayland {jwt} api_" + "key=abcdefghijkl tok" + "en: zzz1234567\n")
        blob = json.dumps([result.tags, result.key, result.source, result.signal, result.function])
        for leak in (jwt, "abcdefghijkl", "zzz1234567"):
            self.assertNotIn(leak, blob)

    def test_a_credential_in_the_executable_or_the_top_frame_is_masked_in_the_key(self):
        top = " handler (password=hunter2hunter2) at x.c:3\n"
        result = self.classified(exe="/usr/bin/app-token=abcdef1234567890", cmdline="app", top=top)  # gitleaks:allow (a fake credential, the test asserts it is masked)
        blob = json.dumps([result.key, result.source, result.function])
        for leak in ("hunter2", "abcdef1234567890"):
            self.assertNotIn(leak, blob)

    def test_an_unbalanced_quote_still_classifies(self):
        self.assertIsInstance(self.classified(cmdline='gnome-shell "unbalanced --nested').nested, bool)

    def test_the_real_session_shell_is_critical_and_its_key_differs_from_the_nested_one(self):
        real = self.classified(cmdline=REAL_CMDLINE)
        self.assertEqual((real.severity, real.nested), ("critical", False))
        self.assertEqual(real.key, "gnome-shell|SIGSEGV|mtk_anonymous_file_open_fd")
        self.assertNotEqual(real.key, self.classified(cmdline=NESTED_CMDLINE).key)

    def test_other_crash_is_a_warning_without_pid_or_address(self):
        top = " ?? ()\n #1 0x00007f12ab in g_object_unref (object=0x55) at ../gobject.c:3\n"
        result = self.classified(exe="/usr/bin/nautilus", cmdline="nautilus", top=top)
        self.assertEqual(result.severity, "warning")
        self.assertEqual(result.key, "nautilus|SIGSEGV|g_object_unref")

    def test_signal_number_is_named_when_apport_gave_no_name(self):
        result = self.classified(exe="/usr/bin/app", cmdline="app", signal="6", signal_name="")
        self.assertEqual(result.signal, "SIGABRT")

    def test_a_report_with_only_an_unreportable_reason_is_a_warning(self):
        fields = {"ProblemType": "Package", "UnreportableReason": "no", "ExecutablePath": "/usr/bin/dpkg"}
        result = classify(fields)
        self.assertEqual((result.severity, result.kind, result.signal), ("warning", "package-problem", "none"))


if __name__ == "__main__":
    unittest.main()

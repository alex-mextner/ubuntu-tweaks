"""How the engine obtains root: every strategy and every failure, on ``FakeProcess`` and a ``Host`` value.

Nothing here runs the real sudo (the machine running the tests may not even be allowed to).
"""

import threading
import time
import unittest

from tests.util import Said, host
from ubuntu_tweaks import process
from ubuntu_tweaks.elevation import (
    EARLIER_STEPS_RAN,
    PKEXEC_CHECK,
    SUDO_ASK,
    SUDO_ASKPASS_ASK,
    SUDO_CHECK,
    SUDO_REFRESH,
    Cause,
    Host,
    RootSession,
    RootUnavailable,
    parse_no_new_privs,
)
from ubuntu_tweaks.errors import ExitCode
from ubuntu_tweaks.process import CommandError, CommandResult, FakeProcess

FIX = "ubuntu-tweak fix"
SKIP = "ubuntu-tweak apply --commit --no-sudo"
PASSWORD_NEEDED = CommandResult(1, "", "sudo: a password is required")


class SessionCase(unittest.TestCase):
    def session(
        self,
        the_host: Host,
        fake: FakeProcess | None = None,
        *,
        refresh_seconds: float = 3600.0,
    ) -> tuple[RootSession, FakeProcess, Said]:
        said = Said()
        fake = fake or FakeProcess()
        root = RootSession(
            process=fake, host=the_host, say=said, refresh_seconds=refresh_seconds, command=FIX, skip=SKIP
        )
        self.addCleanup(root.close)
        return root, fake, said


class AlreadyAllowedTests(SessionCase):
    def test_root_needs_nothing(self) -> None:
        root, fake, said = self.session(host(euid=0, tools=frozenset()))

        root.require(3, "system, system, system")

        self.assertEqual((fake.queries, fake.executed, said.lines), ([], [], []))

    def test_no_root_actions_never_look_at_sudo(self) -> None:
        root, fake, said = self.session(host(no_new_privs=True, tools=frozenset()))

        root.require(0, "")

        self.assertEqual((fake.queries, fake.executed, said.lines), ([], [], []))

    def test_passwordless_sudo_goes_on_without_a_prompt(self) -> None:
        root, fake, said = self.session(host(terminal=True), FakeProcess({SUDO_CHECK: CommandResult(0)}))

        root.require(2, "system, package")

        self.assertEqual((fake.queries, fake.executed, said.lines), ([SUDO_CHECK], [], []))

    def test_the_prefix_follows_the_strategy_and_ends_with_the_session(self) -> None:
        with RootSession(
            process=FakeProcess({SUDO_CHECK: CommandResult(0)}), host=host(), say=Said(), command=FIX
        ) as root:
            root.require(1, "system")
            self.assertEqual(process.elevate(["id"]), ["sudo", "--", "id"])
            self.assertEqual(process.root_prefix(), ("sudo", "--"))
        self.assertIsNone(process.root_prefix())


class KeepAliveTests(SessionCase):
    def test_a_valid_ticket_is_kept_alive_too_so_it_cannot_expire_mid_run(self) -> None:
        root, fake, _ = self.session(
            host(terminal=True),
            FakeProcess({SUDO_CHECK: CommandResult(0), SUDO_REFRESH: CommandResult(0)}),
            refresh_seconds=0.01,
        )

        root.require(1, "system")
        deadline = time.monotonic() + 5
        while fake.queries.count(SUDO_REFRESH) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertGreaterEqual(fake.queries.count(SUDO_REFRESH), 1)
        self.assertEqual(fake.executed, [])  # no prompt: the ticket was already there

    def test_closing_a_session_that_never_got_root_leaves_another_sessions_prefix_alone(self) -> None:
        outer, _, _ = self.session(host(), FakeProcess({SUDO_CHECK: CommandResult(0)}))
        inner, _, _ = self.session(host())
        outer.require(1, "system")

        inner.close()

        self.assertEqual(process.root_prefix(), ("sudo", "--"))
        outer.close()
        self.assertIsNone(process.root_prefix())

    def test_a_hung_refresh_does_not_hold_the_exit(self) -> None:
        release = threading.Event()

        class Hung(FakeProcess):
            def query(self, argv, *, timeout=60.0, cwd=None):  # type: ignore[no-untyped-def]
                if tuple(argv) == SUDO_REFRESH:
                    release.wait(30)
                    return CommandResult(1)
                return super().query(argv, timeout=timeout, cwd=cwd)

        root, _, _ = self.session(host(terminal=True), Hung({SUDO_CHECK: CommandResult(0)}), refresh_seconds=0.01)
        self.addCleanup(release.set)
        root.require(1, "system")
        time.sleep(0.1)  # the thread is now inside the hung `sudo -n -v`

        started = time.monotonic()
        root.close()

        self.assertLess(time.monotonic() - started, 10)


class TerminalTests(SessionCase):
    def test_one_prompt_line_and_one_password_for_many_root_actions(self) -> None:
        root, fake, said = self.session(host(terminal=True), FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}))

        root.require(3, "apt, system")
        root.require(2, "apt")  # a rebuilt plan: already acquired
        root.require(1, "system")

        self.assertEqual(fake.executed, [(SUDO_ASK, None)])
        self.assertEqual(len(said.lines), 1)
        self.assertIn("3 root action(s)", said.lines[0])
        self.assertIn("apt, system", said.lines[0])
        self.assertIn("once", said.lines[0])
        self.assertEqual(fake.queries.count(SUDO_CHECK), 1)

    def test_the_ticket_is_refreshed_until_the_session_ends(self) -> None:
        root, fake, _ = self.session(
            host(terminal=True),
            FakeProcess({SUDO_CHECK: PASSWORD_NEEDED, SUDO_REFRESH: CommandResult(0)}),
            refresh_seconds=0.01,
        )

        root.require(1, "system")
        deadline = time.monotonic() + 5
        while fake.queries.count(SUDO_REFRESH) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(fake.queries.count(SUDO_REFRESH), 2)

        root.close()
        settled = fake.queries.count(SUDO_REFRESH)
        time.sleep(0.05)
        self.assertEqual(fake.queries.count(SUDO_REFRESH), settled)

    def test_a_refresh_that_fails_stops_the_thread_quietly(self) -> None:
        root, fake, said = self.session(
            host(terminal=True), FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}), refresh_seconds=0.01
        )  # SUDO_REFRESH is not answered: 127

        root.require(1, "system")
        time.sleep(0.1)

        self.assertEqual(fake.queries.count(SUDO_REFRESH), 1)
        self.assertEqual(len(said.lines), 1)

    def test_a_wrong_password_is_a_typed_error_without_a_retry(self) -> None:
        def reject(argv: tuple[str, ...], input_text: str | None) -> None:
            raise CommandError("sudo: 3 incorrect password attempts", 1)

        root, fake, _ = self.session(host(terminal=True), FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}, effects=reject))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(1, "system")

        error = caught.exception
        self.assertEqual((error.cause, error.exit_code, int(error.exit_code)), (Cause.REJECTED, ExitCode.NO_ROOT, 6))
        self.assertIn("password", str(error))
        self.assertIn(FIX, str(error))
        self.assertEqual(error.fix, FIX)
        self.assertEqual(len(fake.executed), 1)
        self.assertIsNone(process.root_prefix())


class MessageTests(SessionCase):
    def test_earlier_steps_are_admitted_instead_of_claiming_nothing_changed(self) -> None:
        root, _, _ = self.session(host(no_new_privs=True))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(1, "system", ran_before=True)

        self.assertIn(EARLIER_STEPS_RAN, str(caught.exception))
        self.assertNotIn("Nothing was changed", str(caught.exception))

    def test_the_skip_line_names_the_alternative_and_is_left_out_without_one(self) -> None:
        with_skip, _, _ = self.session(host(no_new_privs=True))
        without = RootSession(command=FIX, host=host(no_new_privs=True), process=FakeProcess())

        with self.assertRaises(RootUnavailable) as first:
            with_skip.require(1, "system")
        with self.assertRaises(RootUnavailable) as second:
            without.require(1, "system")

        self.assertIn(f"Or skip the root actions (they are listed): {SKIP}", str(first.exception))
        self.assertNotIn("skip", str(second.exception))


class UnavailableTests(SessionCase):
    def test_no_new_privs_is_named_with_the_command_and_nothing_is_tried(self) -> None:
        root, fake, _ = self.session(host(terminal=True, askpass=True, no_new_privs=True))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(2, "apt")

        error = caught.exception
        self.assertEqual((error.cause, int(error.exit_code)), (Cause.NO_NEW_PRIVS, 6))
        self.assertIn("NoNewPrivs", str(error))
        self.assertIn(f"Run this in a normal terminal: {FIX}", str(error))
        self.assertIn("--no-sudo", str(error))
        self.assertEqual((fake.queries, fake.executed), ([], []))

    def test_no_terminal_no_askpass_no_display_cannot_ask(self) -> None:
        root, fake, _ = self.session(host(), FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(1, "system")

        self.assertEqual(caught.exception.cause, Cause.NO_PROMPT)
        self.assertIn("terminal", str(caught.exception))
        self.assertIn(FIX, str(caught.exception))
        self.assertEqual(fake.executed, [])

    def test_pkexec_alone_without_a_display_does_not_blame_a_missing_terminal(self) -> None:
        root, fake, _ = self.session(host(terminal=True, tools=frozenset({"pkexec"})))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(1, "system")

        self.assertEqual(caught.exception.cause, Cause.NO_PROMPT)
        self.assertIn("only pkexec is installed", str(caught.exception))
        self.assertIn("apt-get install -y sudo", str(caught.exception))
        self.assertNotIn("no terminal", str(caught.exception))
        self.assertEqual((fake.queries, fake.executed), ([], []))

    def test_neither_sudo_nor_pkexec(self) -> None:
        root, fake, _ = self.session(host(terminal=True, tools=frozenset()))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(1, "system")

        self.assertEqual(caught.exception.cause, Cause.NO_TOOL)
        self.assertIn("apt-get install -y sudo", str(caught.exception))
        self.assertEqual((fake.queries, fake.executed), ([], []))


class GraphicalTests(SessionCase):
    def test_askpass_asks_through_sudo_dash_a_and_keeps_using_it(self) -> None:
        root, fake, said = self.session(host(askpass=True), FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}))

        root.require(1, "system")

        self.assertEqual(fake.executed, [(SUDO_ASKPASS_ASK, None)])
        self.assertEqual(root.prefix, ("sudo", "-A", "--"))
        self.assertEqual(len(said.lines), 1)

    def test_a_terminal_beats_askpass(self) -> None:
        root, fake, _ = self.session(host(terminal=True, askpass=True), FakeProcess({SUDO_CHECK: PASSWORD_NEEDED}))

        root.require(1, "system")

        self.assertEqual(fake.executed, [(SUDO_ASK, None)])

    def test_a_graphical_session_without_sudo_uses_pkexec(self) -> None:
        root, fake, _ = self.session(host(graphical=True, tools=frozenset({"pkexec"})))

        root.require(1, "system")

        self.assertEqual(fake.executed, [(PKEXEC_CHECK, None)])
        self.assertEqual(root.prefix, ("pkexec",))

    def test_pkexec_refused_is_rejected(self) -> None:
        def refuse(argv: tuple[str, ...], input_text: str | None) -> None:
            raise CommandError("pkexec: dismissed", 126)

        root, _, _ = self.session(host(graphical=True, tools=frozenset({"pkexec"})), FakeProcess(effects=refuse))

        with self.assertRaises(RootUnavailable) as caught:
            root.require(1, "system")

        self.assertEqual(caught.exception.cause, Cause.REJECTED)
        self.assertIn("pkexec", str(caught.exception))


class HostTests(unittest.TestCase):
    def test_no_new_privs_is_read_from_the_status_text(self) -> None:
        self.assertTrue(parse_no_new_privs("Name:\tpython3\nNoNewPrivs:\t1\nSeccomp:\t0\n"))
        self.assertFalse(parse_no_new_privs("Name:\tpython3\nNoNewPrivs:\t0\n"))
        self.assertFalse(parse_no_new_privs("Name:\tpython3\n"))

    def test_detect_reads_the_environment(self) -> None:
        detected = Host.detect(environ={"SUDO_ASKPASS": "/usr/bin/ssh-askpass", "WAYLAND_DISPLAY": "wayland-0"})

        self.assertTrue(detected.askpass)
        self.assertTrue(detected.graphical)
        plain = Host.detect(environ={})
        self.assertFalse(plain.askpass or plain.graphical)


if __name__ == "__main__":
    unittest.main()

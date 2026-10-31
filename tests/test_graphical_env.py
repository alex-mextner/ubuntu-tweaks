"""``graphical_env.with_graphical_session``: a shell without a display gets the session's from the user's systemd."""

import unittest

from ubuntu_tweaks.graphical_env import SHOW_ENVIRONMENT, with_graphical_session
from ubuntu_tweaks.process import CommandResult, FakeProcess

MANAGER = (
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
    "DISPLAY=:0\n"
    "WAYLAND_DISPLAY=wayland-0\n"
    "XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.ABC\n"
    "XDG_SESSION_TYPE=wayland\n"
    "XDG_RUNTIME_DIR=/run/user/1000\n"
    "PATH=/usr/bin\n"
)


def manager(stdout: str = MANAGER, returncode: int = 0) -> FakeProcess:
    return FakeProcess({SHOW_ENVIRONMENT: CommandResult(returncode, stdout)})


class GraphicalEnvTests(unittest.TestCase):
    def test_a_shell_without_a_display_gets_the_graphical_variables_and_nothing_else(self) -> None:
        result = with_graphical_session({"PATH": "/bin", "HOME": "/home/me"}, manager())
        self.assertEqual(
            result,
            {
                "PATH": "/bin",
                "HOME": "/home/me",
                "DISPLAY": ":0",
                "WAYLAND_DISPLAY": "wayland-0",
                "XAUTHORITY": "/run/user/1000/.mutter-Xwaylandauth.ABC",
                "XDG_SESSION_TYPE": "wayland",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
        )

    def test_a_shell_that_has_a_display_is_left_alone_and_systemd_is_not_asked(self) -> None:
        for environ in ({"DISPLAY": ":9"}, {"WAYLAND_DISPLAY": "wayland-7"}):
            process = manager()
            self.assertEqual(with_graphical_session(environ, process), environ)
            self.assertEqual(process.queries, [])

    def test_a_variable_the_shell_already_has_is_never_replaced(self) -> None:
        result = with_graphical_session({"XDG_SESSION_TYPE": "x11", "XAUTHORITY": "/mine"}, manager())
        self.assertEqual(result["XDG_SESSION_TYPE"], "x11")
        self.assertEqual(result["XAUTHORITY"], "/mine")
        self.assertEqual(result["WAYLAND_DISPLAY"], "wayland-0")

    def test_an_empty_display_counts_as_none(self) -> None:
        result = with_graphical_session({"DISPLAY": ""}, manager())
        self.assertEqual(result["DISPLAY"], ":0")

    def test_the_input_mapping_is_not_modified(self) -> None:
        environ = {"PATH": "/bin"}
        with_graphical_session(environ, manager())
        self.assertEqual(environ, {"PATH": "/bin"})

    def test_a_failing_or_missing_systemctl_changes_nothing(self) -> None:
        environ = {"PATH": "/bin"}
        self.assertEqual(with_graphical_session(environ, manager(returncode=1)), environ)
        self.assertEqual(with_graphical_session(environ, FakeProcess()), environ)  # 127: not installed

    def test_a_manager_without_a_display_adds_nothing_not_even_the_other_variables(self) -> None:
        environ = {"PATH": "/bin"}
        self.assertEqual(with_graphical_session(environ, manager("XDG_SESSION_TYPE=tty\nPATH=/usr/bin\n")), environ)

    def test_a_value_systemd_quotes_is_decoded_so_a_path_with_a_space_survives(self) -> None:
        quoted = "DISPLAY=:1\nXAUTHORITY=$'/home/me/My Session/Xauthority'\nWAYLAND_DISPLAY=$'wayland-1'\n"
        result = with_graphical_session({}, manager(quoted))
        self.assertEqual(
            result,
            {"DISPLAY": ":1", "XAUTHORITY": "/home/me/My Session/Xauthority", "WAYLAND_DISPLAY": "wayland-1"},
        )

    def test_the_c_escapes_of_a_quoted_value_are_decoded(self) -> None:
        quoted = "DISPLAY=:1\nXAUTHORITY=$'/a\\'b\\\\c\\nd\\x41\\101\\303\\251'\n"
        self.assertEqual(with_graphical_session({}, manager(quoted))["XAUTHORITY"], "/a'b\\c\ndAA\u00e9")

    def test_a_unicode_line_separator_inside_a_value_does_not_split_the_record(self) -> None:
        # U+2028 is a legal path character; systemd writes it as it is
        text = "DISPLAY=:0\nXAUTHORITY=/home/me/a\u2028b/Xauthority\nZ_OTHER=/x\u2028DISPLAY=:999\n"
        result = with_graphical_session({}, manager(text))
        self.assertEqual(result, {"DISPLAY": ":0", "XAUTHORITY": "/home/me/a\u2028b/Xauthority"})

    def test_a_line_without_a_value_is_skipped(self) -> None:
        result = with_graphical_session({}, manager("DISPLAY=:1\nWAYLAND_DISPLAY=\nnoise\n"))
        self.assertEqual(result, {"DISPLAY": ":1"})


if __name__ == "__main__":
    unittest.main()

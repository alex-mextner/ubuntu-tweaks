"""audio-health-check: a plugged-in card with no PipeWire device, or a failed audio service, reaches the user as one
desktop notification. Every command (aplay, wpctl, pactl, systemctl, journalctl, notify-send) is a stand-in on PATH.

    python3 -B -m unittest discover -s modules/audio-health/tests
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
CHECK = MODULE / "audio-health-check"

# `aplay -l` of a desktop on 2026-11-11: the monitor's HDMI audio, a USB headset and the board's analog codec.
APLAY_HEADSET = """**** List of PLAYBACK Hardware Devices ****
card 0: NVidia [HDA NVidia], device 3: HDMI 0 [LS27D80xU]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 0: NVidia [HDA NVidia], device 7: HDMI 1 [HDMI 1]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: MS [Jabra EVOLVE 20 MS], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""
APLAY_WITH_ANALOG = (
    APLAY_HEADSET
    + """card 2: Generic [HD-Audio Generic], device 0: ALC897 Analog [ALC897 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""
)
APLAY_HDMI_ONLY = """**** List of PLAYBACK Hardware Devices ****
card 0: NVidia [HDA NVidia], device 3: HDMI 0 [LS27D80xU]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 0: NVidia [HDA NVidia], device 7: DisplayPort 1 [DP 1]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""
# `wpctl status` with and without the headset's device (the real output when wireplumber is down).
WPCTL_NO_DEVICES = """PipeWire 'pipewire-0' [1.6.2, alice@desktop, cookie:2216664881]
 └─ Clients:
        33. xdg-desktop-portal                  [1.6.2, alice@desktop, pid:24091]

Audio
 ├─ Devices:
 │
 ├─ Sinks:
 │      44. Dummy Output                        [vol: 1.00]
 │
 ├─ Sources:
 │
 └─ Streams:

Video
 ├─ Devices:
 │      60. Integrated Camera                   [v4l2]
 │
 └─ Streams:
"""


def wpctl_status(*devices):
    """`wpctl status` listing these (id, description) audio devices."""
    rows = "".join(f" │      {device_id}. {description:<30} [alsa]\n" for device_id, description in devices)
    return WPCTL_NO_DEVICES.replace(" ├─ Devices:\n │\n ├─ Sinks:", f" ├─ Devices:\n{rows} │\n ├─ Sinks:", 1)


def inspect_of(device_id, index, name):
    """`wpctl inspect ID` of the device PipeWire made for ALSA card `index`: the properties the check reads."""
    return (
        f"id {device_id}, type PipeWire:Interface:Device\n"
        f'    alsa.card = "{index}"\n'
        f'    alsa.card_name = "{name}"\n'
        f'    api.alsa.card = "{index}"\n'
        f'    api.alsa.card.longname = "{name} at somewhere"\n'
        f'    api.alsa.card.name = "{name}"\n'
        f'  * device.name = "alsa_card.{index}"\n'
    )


def pulse_cards_of(*cards):
    """`pactl list cards` of these ALSA (index, name) cards (the pulse names do not carry the index)."""
    return "".join(
        f'Card #{272 + n}\n\tName: alsa_card.{index}\n\tProperties:\n\t\tapi.alsa.card = "{index}"\n'
        f'\t\tapi.alsa.card.name = "{name}"\n\t\tdevice.name = "alsa_card.{index}"\n\n'
        for n, (index, name) in enumerate(cards)
    )


# wireplumber.service as the user manager showed it after the failed state was cleared, and as it is right after a crash.
WP_DEAD = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=78\nNRestarts=0\n"
WP_FAILED = "LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=78\nNRestarts=5\n"
WP_LOOP = "LoadState=loaded\nActiveState=activating\nSubState=auto-restart\nResult=exit-code\nExecMainStatus=78\nNRestarts=3\n"
WP_STARTING = (
    "LoadState=loaded\nActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\nNRestarts=0\n"
)
UNIT_ACTIVE = "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nNRestarts=0\n"
WP_JOURNAL = (
    "2026-11-11T01:36:44+01:00 pc wireplumber[8331]: [0:00:16.296] ERROR DmaBufAllocator dma_buf_allocator.cpp:119 Could not open any dma-buf provider\n"
    "2026-11-11T01:36:44+01:00 pc wireplumber[8331]: failed to load components: failed to load component 'support.xdg-desktop-portal "
    "[module: libxdg-desktop-portal]' (required by policy.xdg-desktop-portal [virtual]): Failed to locate module libxdg-desktop-portal\n"
    "2026-11-11T01:36:44+01:00 pc systemd[2103]: wireplumber.service: Main process exited, code=exited, status=78/CONFIG\n"
    "2026-11-11T01:36:49+01:00 pc systemd[2103]: wireplumber.service: Start request repeated too quickly.\n"
    "2026-11-11T01:36:49+01:00 pc systemd[2103]: Failed to start wireplumber.service - Multimedia Service Session Manager.\n"
)

# Every stand-in uses only shell builtins: PATH holds nothing but this directory, so no real tool is ever reached.
FAKES = {
    "aplay": '[ "$LC_ALL" = C ] || { echo "Karte 1: kaputt" ; exit 0; }\n[ -e "$STUB/aplay" ] && printf \'%s\\n\' "$(<"$STUB/aplay")"\n',
    "wpctl": (
        '[ -e "$STUB/wpctl-down" ] && { echo "Cannot connect to PipeWire" >&2; exit 1; }\n'
        'if [ "$1" = inspect ]; then [ -e "$STUB/wpctl-inspect-$2" ] && printf \'%s\\n\' "$(<"$STUB/wpctl-inspect-$2")"; exit 0; fi\n'
        '[ -e "$STUB/wpctl" ] && printf \'%s\\n\' "$(<"$STUB/wpctl")"\n'
    ),
    "systemctl": (
        "unit=${!#}\n"
        'if [ -e "$STUB/unit-$unit" ]; then printf \'%s\\n\' "$(<"$STUB/unit-$unit")"\n'
        "else printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\nResult=success\\nExecMainStatus=0\\nNRestarts=0\\n'; fi\n"
    ),
    "journalctl": (
        'case " $* " in *" -b "*) ;; *) echo "2026-01-01T00:00:00+01:00 pc wireplumber[1]: failed: OLD BOOT"; exit 0;; esac\n'
        'while [ "$#" -gt 0 ]; do [ "$1" = -u ] && unit=$2; shift; done\n'
        '[ -e "$STUB/journal-$unit" ] && printf \'%s\\n\' "$(<"$STUB/journal-$unit")"\n'
        "exit 0\n"
    ),
    "notify-send": (
        '[ "$LC_ALL" = C ] && case "$*" in *[!\\ -~]*) echo "Invalid byte sequence" >&2; exit 1;; esac\n'
        '[ -e "$STUB/notify-down" ] && { echo "Could not connect: No such file or directory" >&2; exit 1; }\n'
        'args=("$@"); n=${#args[@]}\n'
        'printf \'%s | %s\\n\' "${args[n-2]}" "${args[n-1]}" >> "$STUB/notifications"\n'
    ),
}
PACTL = '[ -e "$STUB/pactl" ] && printf \'%s\\n\' "$(<"$STUB/pactl")"\n'


class CheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stub = self.root / "stub"
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        for directory in (self.stub, self.bin):
            directory.mkdir()
        for name, body in FAKES.items():
            self.fake(name, body)
        self.env = {
            "PATH": str(self.bin),
            "STUB": str(self.stub),
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.state),
        }
        # the incident: the headset is there for ALSA, PipeWire serves nothing, wireplumber is gone
        self.say("aplay", APLAY_HEADSET)
        self.say("wpctl", WPCTL_NO_DEVICES)
        self.say("unit-wireplumber.service", WP_DEAD)
        self.say("journal-wireplumber.service", WP_JOURNAL)

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def say(self, name, text):
        (self.stub / name).write_text(text)

    def serve(self, *cards):
        """PipeWire serves these ALSA cards, (index, name) as `aplay -l` shows them: one wpctl device each."""
        devices = [(60 + index, name) for index, name in cards]
        self.say("wpctl", wpctl_status(*devices))
        for (device_id, _), (index, name) in zip(devices, cards):
            self.say(f"wpctl-inspect-{device_id}", inspect_of(device_id, index, name))

    def serve_headset(self):
        self.serve((0, "HDA NVidia"), (1, "Jabra EVOLVE 20 MS"))

    def run_check(self, **extra):
        return subprocess.run(
            [sys.executable, "-B", str(CHECK)], capture_output=True, text=True, env={**self.env, **extra}
        )

    def run_confirmed(self, **extra):
        """Two runs: a card missing with both services running is announced by the run that sees it again."""
        self.run_check(**extra)
        return self.run_check(**extra)

    def notifications(self):
        path = self.stub / "notifications"
        return path.read_text().splitlines() if path.exists() else []

    def test_no_card_for_a_plugged_in_headset_notifies_the_unit_and_the_log_line(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("wireplumber.service", notes[0])
        self.assertIn("Jabra EVOLVE 20 MS", notes[0])
        self.assertIn(
            "Failed to locate module libxdg-desktop-portal", notes[0]
        )  # the program's own error, not systemd's bookkeeping
        self.assertNotIn("Start request repeated too quickly", notes[0])
        self.assertIn("exit status 78", notes[0])
        self.assertNotIn("HDA NVidia", notes[0])  # HDMI audio is not the card that went missing

    def test_a_working_setup_is_silent(self):
        self.serve_headset()
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.notifications(), [])
        self.assertIn("ok", result.stdout)

    def test_a_machine_with_only_hdmi_audio_and_no_device_is_not_a_problem(self):
        self.say("aplay", APLAY_HDMI_ONLY)
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.run_check()
        self.assertEqual(self.notifications(), [])

    def test_a_failed_wireplumber_is_reported_even_when_alsa_lists_no_card(self):
        self.say("aplay", APLAY_HDMI_ONLY)
        self.say("unit-wireplumber.service", WP_FAILED)
        self.run_check()
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("wireplumber.service is failed", notes[0])
        self.assertIn("Failed to locate module libxdg-desktop-portal", notes[0])

    def test_a_restart_loop_and_a_failed_pipewire_are_reported(self):
        self.say("unit-wireplumber.service", WP_LOOP)
        self.run_check()
        self.assertIn("restart loop", self.notifications()[0])
        shutil.rmtree(self.state, ignore_errors=True)
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve_headset()
        self.say("unit-pipewire.service", WP_FAILED)
        self.run_check()
        self.assertIn("pipewire.service is failed", self.notifications()[-1])

    def test_a_unit_that_is_still_starting_is_given_time(self):
        self.say("unit-wireplumber.service", WP_STARTING)
        self.run_check()
        self.assertEqual(self.notifications(), [])

    def test_wireplumber_stopped_while_pipewire_is_down_too_is_not_a_failure_of_its_own(self):
        # nothing has asked for audio yet: both are socket-activated and stopped, and ALSA lists no card to miss
        self.say("aplay", APLAY_HDMI_ONLY)
        self.say(
            "unit-pipewire.service",
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nNRestarts=0\n",
        )
        self.run_check()
        self.assertEqual(self.notifications(), [])

    def test_pulse_cards_count_as_served_when_wpctl_shows_none(self):
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.fake("pactl", PACTL)
        self.say("pactl", pulse_cards_of((0, "HDA NVidia"), (1, "Jabra EVOLVE 20 MS")))
        self.run_check()
        self.assertEqual(self.notifications(), [])
        self.say("pactl", "")  # pactl answers and lists nothing: still no card
        self.run_confirmed()
        self.assertEqual(len(self.notifications()), 1)

    def test_the_headset_missing_while_hdmi_is_served_notifies_and_names_it(self):
        # the case a bare "PipeWire serves some card" cannot see: HDMI is served, the USB headset is not
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve((0, "HDA NVidia"))
        result = self.run_confirmed()
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("Jabra EVOLVE 20 MS", notes[0])
        self.assertNotIn("HDA NVidia", notes[0])  # HDMI audio is served and is not what went missing
        self.assertIn("Jabra EVOLVE 20 MS", result.stdout)

    def test_the_headset_missing_while_pulse_serves_hdmi_notifies(self):
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.fake("pactl", PACTL)
        self.say("pactl", pulse_cards_of((0, "HDA NVidia")))
        self.run_confirmed()
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("Jabra EVOLVE 20 MS", notes[0])

    def test_every_non_hdmi_card_served_is_silent(self):
        self.say("aplay", APLAY_WITH_ANALOG)
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve((0, "HDA NVidia"), (1, "Jabra EVOLVE 20 MS"), (2, "HD-Audio Generic"))
        result = self.run_check()
        self.assertEqual(self.notifications(), [])
        self.assertIn("ok", result.stdout)

    def test_served_cards_are_found_through_wpctl_and_pactl_together(self):
        self.say("aplay", APLAY_WITH_ANALOG)
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve((1, "Jabra EVOLVE 20 MS"))
        self.fake("pactl", PACTL)
        self.say("pactl", pulse_cards_of((2, "HD-Audio Generic")))
        self.run_check()
        self.assertEqual(self.notifications(), [])

    def test_a_missing_onboard_card_is_named_while_the_headset_is_served(self):
        self.say("aplay", APLAY_WITH_ANALOG)
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve((0, "HDA NVidia"), (1, "Jabra EVOLVE 20 MS"))
        self.run_confirmed()
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("HD-Audio Generic", notes[0])
        self.assertNotIn("Jabra", notes[0])

    def test_a_pipewire_card_of_another_index_or_name_is_not_the_card(self):
        # card 1 is the headset for ALSA; a device made for a card 1 with another name (stale) or for another index is not it
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve((0, "HDA NVidia"), (1, "Something Else"))
        self.run_confirmed()
        self.assertEqual(len(self.notifications()), 1)
        shutil.rmtree(self.state, ignore_errors=True)
        self.serve((0, "HDA NVidia"), (3, "Jabra EVOLVE 20 MS"))
        self.run_confirmed()
        self.assertEqual(len(self.notifications()), 2)

    def test_a_device_that_is_not_an_alsa_card_does_not_serve_one(self):
        # a Bluetooth headset is a PipeWire device without ALSA card properties
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.say("wpctl", wpctl_status((70, "WH-1000XM4")))
        self.say("wpctl-inspect-70", 'id 70, type PipeWire:Interface:Device\n  * device.name = "bluez_card.AA_BB"\n')
        self.run_confirmed()
        self.assertEqual(len(self.notifications()), 1)

    def test_zero_pipewire_devices_with_healthy_units_still_notifies(self):
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.run_confirmed()
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("Jabra EVOLVE 20 MS", notes[0])

    def test_the_missing_headset_is_announced_once_a_day_and_again_after_it_was_served(self):
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.serve((0, "HDA NVidia"))
        self.run_check()
        self.run_check()
        self.assertEqual(len(self.notifications()), 1)
        self.serve_headset()  # recovered: the stamps are cleared
        self.run_check()
        self.serve((0, "HDA NVidia"))  # gone again
        self.run_confirmed()
        self.assertEqual(len(self.notifications()), 2)

    def test_an_unreachable_pipewire_is_no_card(self):
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        (self.stub / "wpctl-down").write_text("")
        self.run_confirmed()
        self.assertEqual(len(self.notifications()), 1)

    def test_a_machine_with_neither_wpctl_nor_pactl_cannot_be_judged_and_is_not_nagged(self):
        # a PulseAudio-only machine: nothing to ask about the cards, so "no card" is not a verdict (the units still are)
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        (self.bin / "wpctl").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.notifications(), [])

    def test_one_notification_per_problem_per_day(self):
        self.run_check()
        self.run_check()
        self.assertEqual(len(self.notifications()), 1)
        self.run_check(AUDIO_HEALTH_RENOTIFY_MIN="0")
        self.assertEqual(len(self.notifications()), 2)

    def test_a_different_problem_is_a_new_notification(self):
        self.run_check()
        self.say("unit-wireplumber.service", WP_FAILED)
        self.run_check()
        self.assertEqual(len(self.notifications()), 2)

    def test_a_recovery_re_arms_the_notification(self):
        self.run_check()
        self.serve_headset()
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.run_check()
        self.say("wpctl", WPCTL_NO_DEVICES)
        self.say("unit-wireplumber.service", WP_DEAD)
        self.run_check()
        self.assertEqual(len(self.notifications()), 2)

    def test_a_notification_that_could_not_be_shown_is_retried_by_the_next_run(self):
        (self.stub / "notify-down").write_text("")  # no notification daemon yet (early in the login)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not delivered", result.stdout)
        self.assertEqual(self.notifications(), [])
        (self.stub / "notify-down").unlink()
        self.run_check()
        self.assertEqual(len(self.notifications()), 1)

    def test_control_characters_and_long_lines_of_the_log_do_not_reach_the_notification(self):
        self.say(
            "journal-wireplumber.service",
            "2026-11-11T01:36:44+01:00 pc wireplumber[1]: failed " + "x" * 600 + "\x1b[31mred\x07\n",
        )
        self.run_check()
        note = self.notifications()[0]
        self.assertNotIn("\x1b", note)
        self.assertNotIn("\x07", note)
        self.assertLess(len(note), 700)

    def test_a_tab_in_the_log_line_becomes_a_space_and_does_not_glue_words(self):
        self.say("journal-wireplumber.service", "2026-11-11T01:36:44+01:00 pc wireplumber[1]: failed\tto\tload\n")
        self.run_check()
        self.assertIn("failed to load", self.notifications()[0])

    def test_a_machine_without_wireplumber_is_left_alone(self):
        self.say(
            "unit-wireplumber.service",
            "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nNRestarts=0\n",
        )
        self.serve_headset()
        self.run_check()
        self.assertEqual(self.notifications(), [])

    def test_a_translating_locale_does_not_switch_the_card_check_off(self):
        result = self.run_check(LANG="de_DE.UTF-8", LC_ALL="de_DE.UTF-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Jabra EVOLVE 20 MS", "".join(self.notifications()))

    def test_a_notification_with_non_ascii_text_is_shown(self):
        self.say("aplay", APLAY_HEADSET.replace("Jabra EVOLVE 20 MS", "Jabra Évolve™ 20"))
        self.run_check()
        self.run_check()
        notes = self.notifications()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("Évolve™", notes[0])

    def test_a_card_missing_on_one_run_only_is_judged_at_the_next_run(self):
        self.say(
            "unit-wireplumber.service", UNIT_ACTIVE
        )  # no unit problem: PipeWire may just not have published it yet
        self.run_check()
        self.assertEqual(self.notifications(), [])
        self.run_check()
        self.assertEqual(len(self.notifications()), 1)
        self.serve_headset()
        self.run_check()
        self.say("wpctl", WPCTL_NO_DEVICES)
        self.run_check()
        self.assertEqual(len(self.notifications()), 1, "a healthy run in between starts the count again")

    def test_a_missing_card_with_healthy_units_does_not_blame_wireplumber_or_quote_its_log(self):
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.run_check()
        self.run_check()
        note = self.notifications()[0]
        self.assertIn("Jabra EVOLVE 20 MS", note)
        self.assertNotIn("Log:", note)
        self.assertNotIn("wireplumber.service", note)

    def test_the_log_line_is_from_this_boot(self):
        self.run_check()
        self.assertNotIn("OLD BOOT", "".join(self.notifications()))

    def test_a_pulseaudio_card_with_the_plain_alsa_properties_is_served(self):
        self.say("wpctl-down", "")
        self.say("unit-wireplumber.service", UNIT_ACTIVE)
        self.fake("pactl", PACTL)
        self.say(
            "pactl",
            pulse_cards_of((1, "Jabra EVOLVE 20 MS"))
            .replace("api.alsa.", "alsa.")
            .replace("alsa.card.name", "alsa.card_name"),
        )
        self.run_check()
        self.assertEqual(self.notifications(), [])

    def test_a_bad_renotify_value_or_unwritable_state_still_exits_zero(self):
        self.assertEqual(self.run_check(AUDIO_HEALTH_RENOTIFY_MIN="1d").returncode, 0)
        blocker = self.root / "blocker"
        blocker.write_text("x")
        self.assertEqual(self.run_check(XDG_STATE_HOME=str(blocker)).returncode, 0)


if __name__ == "__main__":
    os.environ.pop("PYTHONPATH", None)
    unittest.main()

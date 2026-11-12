### Audio health (`audio-health`)

PipeWire without a card is silent: no dialog, only a *Dummy Output* in the sound settings. On 2026-11-11 a Jabra EVOLVE 20 MS headset was
enumerated by the kernel (ALSA card 1) while PipeWire served nothing, because `wireplumber.service` exited with status 78 on every start (a
hand-installed portal configuration named a module the distribution's wireplumber does not search), and nothing told the owner (ticket #236).
A user-level `system` module (`root = false`, order 88, **off by default**: the owner decided it is opt-in, ticket #244; packages `alsa-utils`, `wireplumber`, `libnotify-bin`) installs
`~/.local/bin/audio-health-check`, `~/.config/systemd/user/audio-health.service` (oneshot) and `audio-health.timer`
(`OnActiveSec=1min`: the timer starts with the user manager, so this is the login check; then `OnUnitActiveSec=5min`) and enables the timer.
`check` compares the files byte for byte (script mode 755) and that the timer is enabled and running (enabled alone means from the next login on); the install changes only what differs, runs
`daemon-reload` when a unit changed, `enable --now` when the timer is off or stopped and `restart` when a unit changed under a running timer, so a second
`apply` reports `0 changed`.

**Off by default, on per machine.** `module.toml` says `enabled = false`; a machine that wants the check sets
`[modules.audio-health] enabled = true` in `profile.toml` (every machine) or in `machines/<host>/machine.toml` (one machine), then runs
`ubuntu-tweak apply --commit --only audio-health`. The engine only skips a disabled module, so the module declares a **retirement**
(`retire`, `retire_check`, `retire_preview` in `[source]`; spec *Source kinds*, `system`): while the module is disabled the planner emits
`retire audio-health`, whose end state is *the timer neither enabled nor running, `~/.config/systemd/user/audio-health.{service,timer}` and
`~/.local/bin/audio-health-check` removed*. `retire` runs `systemctl --user disable --now audio-health.timer` first (while the unit file still exists; a timer that runs on after its unit
file is gone is only `stop`ped, `disable` needs the file), removes what is there (the three files and the enable link
`timers.target.wants/audio-health.timer`, a dangling symlink too) and runs `daemon-reload` only when a unit file or the link went; `retire-check`
(exit 0 / 3) lists what is left, so a second `apply` reports nothing to do and an enabled timer, or a file that came back, is drift. Older systemd words a missing unit on stderr: that is an answer, not silence. A user manager that does not answer at all (no session bus: `ssh` without a login session, `su`, cron) is "state unknown":
`retire-check` reports drift (exit 3, never a crash, which would abort the plan of every other module) while anything is left on disk, `retire` stops with a manual step (exit 10: run it in the desktop user's session) and removes nothing under a timer that may still run, and with nothing left on disk the retirement counts as converged (a timer that runs on after its files were deleted by hand, without a `daemon-reload`, is then only found by the next run inside the session). Only systemd's own wording of a missing unit file (`Failed to get unit file state ...`) counts as an answer; the no-bus error also ends in "No such file or directory" and does not. A machine on which none of these files exists changes nothing. (Whatever is at those paths is removed, whatever its content; narrowing that to files the module itself wrote is ticket #246.) The check's
stamps under `$XDG_STATE_HOME/audio-health` and `$XDG_RUNTIME_DIR/audio-health` are not touched (they only record what was announced).

The check script (Python standard library only, exit status 0 whatever the verdict) reads four facts: the non-HDMI cards of `aplay -l` (a card
with at least one device that is not HDMI or DisplayPort audio), the ALSA cards behind the audio devices of `wpctl status` (each device read with `wpctl inspect ID`;
when `pactl` is installed, also the cards of `pactl list cards`), and the state of `wireplumber.service` and `pipewire.service` in the user manager.
A card is served when PipeWire has a device whose `api.alsa.card` (the kernel's card index) and `api.alsa.card.name` (the card name) equal the
`card N: ID [NAME]` (a plain PulseAudio spells them `alsa.card` / `alsa.card_name`, read too; the tools whose output is read run with `LC_ALL=C`, because `aplay` and `pactl` translate it; `notify-send` keeps the user's locale, it rejects non-ASCII text under C) that `aplay -l` prints. This one rule covers USB headsets and onboard PCI codecs alike (the pulse card name only carries the USB
vendor/product/serial or the PCI address, never the index), and the name rejects a stale device left for an index the kernel gave to another card.
A device without ALSA properties (Bluetooth) serves no ALSA card. A problem is

- **no card served**: a non-HDMI ALSA card has no matching PipeWire card, whether PipeWire serves nothing (`wpctl` lists no audio device, `pactl` lists
  no card or is absent, or PipeWire cannot be asked) or only other cards such as HDMI audio or the onboard codec while a USB headset is missing;
- **a unit down**: `failed`, `activating/auto-restart` (a restart loop), `inactive` with `Result` other than `success`, or `wireplumber.service`
  `inactive` while `pipewire.service` is active (the state a failed wireplumber is left in after something cleared its failed state).

A wireplumber that is `activating` (starting) is judged at the next run. A card that is missing while both services run (nothing to blame: PipeWire may not have published it yet right after a start or a plug-in) is announced by the second run in a row that sees it, and then the message names the cards only, no unit and no log line. The previous run is remembered by a `pending-*` stamp in `$XDG_RUNTIME_DIR/audio-health` (the state directory when there is none or it cannot be used; a reboot empties the runtime directory, so with it a run of the last boot is never the "previous run"; the state directory survives a reboot, where the 15 minutes below are the only limit). A healthy run removes the stamp, and every sighting refreshes it. The stamp only counts while it is less than 15 minutes old (two timer periods when a run in between judged nothing, such as one that found wireplumber still starting, plus one period of slack for the timer's accuracy and the run time) and not dated in the future (the clock was set back); a stamp left hours ago, with no healthy run in between to clear it, confirms nothing. A run that reports a failed unit, or sees another set of missing cards, drops the stamp: the sightings must be of the same problem, in a row. A machine with HDMI audio only, or without wireplumber, is not a problem; neither
is one with neither `wpctl` nor `pactl` installed (nothing can be asked about cards, so "no card" is no verdict there; the unit states still count).
A problem sends one `notify-send` message (urgency critical): the title says which, the body names the ALSA cards that have no PipeWire card, the unit with its state and exit
status, and the log line that explains it (the last error-looking line of this boot's `journalctl --user -b -u UNIT` written by the program itself, not by
`systemd[...]`; control characters dropped, 300 characters at most). It is idempotent: the same problem (unit, kind, missing cards) is announced once per
`AUDIO_HEALTH_RENOTIFY_MIN` (default 1440) minutes, tracked by a stamp file in `$XDG_STATE_HOME/audio-health`; a notification the daemon could not show
(none runs early in the login) leaves no stamp, so the next run tries again; a healthy run removes the stamps, so a recurrence is announced at once.
Tests: `modules/audio-health/tests/` (the script against stand-in `aplay`, `wpctl`, `pactl`, `systemctl`, `journalctl`, `notify-send` on a `PATH` that holds
nothing else, and the module against a scratch home).


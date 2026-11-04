1. Read the event: `source`, `key` (`<executable>|<signal>|<top frame>`), severity, the excerpt in the text.
   The sanitized report is in the event data (`agent-sub list --topic errors --json` shows it); the full original
   report (with the core dump, keep it out of chat) is the gzip named in `archive` (`error-intercept archive list`;
   `zcat <file> | grep -a -A40 '^StacktraceTop:'`). `error-intercept list` shows what was handled and when.
2. Use the `systematic-debugging` skill. Find the root cause: package version (`apt policy <package>`), the
   top frame, whether it reproduces. A nested test shell (`nested-test-shell` tag, normal severity) is a crash of a
   shell somebody started to test an extension: decide real bug vs test artefact and say which.
3. Fix it in the project that owns the code (the ubuntu-tweaks repository's modules and extensions, a config), through a
   worktree and a ticket; or open a ticket upstream/in the owning repository with the evidence. Never restart or reload the
   live gnome-shell, never log out, never touch the live dconf, autostart or user units.
4. Run any nested shell as `prlimit --core=0 -- ...` with a temporary HOME and XDG directories, `WAYLAND_DISPLAY`,
   `DISPLAY` and `XAUTHORITY` unset, `GIO_USE_VFS=local GVFS_DISABLE_FUSE=1`, and its own D-Bus session whose service
   directory holds no gvfs/tracker/avahi/wsdd service files (gvfs would start `wsdd`, which talks to the real LAN); check afterwards that no `gnome-shell --headless`,
   `dbus-run-session`, `dbus-daemon`, `wsdd`, `gvfsd*` or `nautilus` process of yours is left (kill by exact PID, only the ones whose
   `/proc/<pid>/environ` carries your nested bus address). When the hyperdock repository's
   `tools/nested-shell-env.sh` exists, use it (it does all of this and puts a fake Nautilus on the nested bus) rather than
   starting `gnome-shell --headless` by hand.
5. Ask the user with `tg` before anything destructive or that needs `sudo`.
6. Finish every event with `agent-sub ack <id> --note '<finding, action, ticket>'`.

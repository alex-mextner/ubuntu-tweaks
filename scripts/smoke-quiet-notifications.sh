#!/usr/bin/env bash
# Smoke run of the quiet-notifications extension inside a headless GNOME Shell of its own: a
# private session bus, runtime dir, data dir, config dir and HOME, so the live shell is never
# touched. Three runs:
#   1. stock Shell members  -> the extension enables and patches the message tray, no error;
#   2. _notificationQueue removed while it is off, then it is enabled again (what a Shell
#      rename looks like) -> it logs exactly one error naming that member and the tray
#      stays unpatched;
#   3. an app source without `notifications` appears after it enabled -> one error, and it
#      switches itself off, restoring the stock methods.
# A throwaway probe extension does the removing and reports what it sees.
# Needs gnome-shell and dbus-run-session; not part of scripts/ci.sh (CI has no Shell).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
uuid=quiet-notifications@ubuntu-tweaks
timeout_s=${SMOKE_TIMEOUT:-60}

for tool in gnome-shell dbus-run-session; do
  command -v "$tool" >/dev/null || { echo "smoke: $tool is not installed" >&2; exit 2; }
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# One headless shell: $1 = "break" to rename the member first. Leaves its log in $work/$1.log.
run_shell() {
  local mode=$1 root=$work/$1
  mkdir -p "$root"/{run,data/gnome-shell/extensions/probe@smoke,config/glib-2.0/settings,cache,home}
  chmod 700 "$root/run"
  ln -s "$repo/gnome-extensions/$uuid" "$root/data/gnome-shell/extensions/$uuid"
  cat >"$root/data/gnome-shell/extensions/probe@smoke/metadata.json" <<'JSON'
{"uuid": "probe@smoke", "name": "Smoke probe", "description": "test only", "shell-version": ["50"]}
JSON
  cat >"$root/data/gnome-shell/extensions/probe@smoke/extension.js" <<'JS'
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import {FdoNotificationDaemonSource} from 'resource:///org/gnome/shell/ui/notificationDaemon.js';

export default class Probe extends Extension {
    enable() {
        const proto = MessageTray.MessageTray.prototype;
        // The extension's replacement closes over its own state; the stock method does not.
        const patched = () => proto._hideNotification.toString().includes('_closedByLock');
        console.log(`SMOKE probe enabled first=${!patched()}`);
        const tray = Main.messageTray;
        const queue = tray._notificationQueue;
        const mode = GLib.getenv('QN_SMOKE_MODE');
        if (mode === 'late') {
            // An app source whose `notifications` field is gone, born after enable().
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => {
                Object.defineProperty(FdoNotificationDaemonSource.prototype, 'notifications',
                    {configurable: true, get: () => undefined, set: () => {}});
                return GLib.SOURCE_REMOVE;
            });
        }
        if (mode !== 'break')
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
                GLib.spawn_command_line_async('notify-send smoke-test body');
                return GLib.SOURCE_REMOVE;
            });
        if (mode === 'break') {
            // Shell's enabling order is not ours to rely on: take the extension off, rename
            // the member, put it back on.
            const uuid = 'quiet-notifications@ubuntu-tweaks';
            GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                Main.extensionManager.disableExtension(uuid);
                delete tray._notificationQueue;
                Main.extensionManager.enableExtension(uuid);
                return GLib.SOURCE_REMOVE;
            });
        }
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 6000, () => {
            tray._notificationQueue = queue;
            const state = Main.extensionManager.lookup('quiet-notifications@ubuntu-tweaks')?.state;
            console.log(`SMOKE patched=${patched()} state=${state}`);
            return GLib.SOURCE_REMOVE;
        });
    }

    disable() {}
}
JS
  cat >"$root/config/glib-2.0/settings/keyfile" <<EOF
[org/gnome/shell]
enabled-extensions=['probe@smoke', '$uuid']
disable-user-extensions=false
EOF
  # shellcheck disable=SC2016  # expanded by the inner shell
  QN_SMOKE_MODE=$mode HOME=$root/home XDG_RUNTIME_DIR=$root/run XDG_DATA_HOME=$root/data \
    XDG_CONFIG_HOME=$root/config XDG_CACHE_HOME=$root/cache GSETTINGS_BACKEND=keyfile \
    XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=wayland \
    timeout "$timeout_s" dbus-run-session -- bash -c '
      gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x720 >"$0" 2>&1 &
      shell=$!
      for _ in $(seq 1 "$1"); do
        grep -q "SMOKE patched=" "$0" 2>/dev/null && break
        kill -0 "$shell" 2>/dev/null || break
        sleep 1
      done
      kill "$shell" 2>/dev/null || true
      wait "$shell" 2>/dev/null || true
    ' "$root.log" "$timeout_s" || true
}

fail() { echo "smoke: FAIL: $*" >&2; exit 1; }
errors() { grep -c "$uuid: not enabled" "$work/$1.log" || true; }

run_shell normal
grep -q 'SMOKE patched=true state=1' "$work/normal.log" ||
  { cat "$work/normal.log" >&2; fail "stock run: extension did not enable and patch the tray"; }
[ "$(errors normal)" = 0 ] || fail "stock run: unexpected not-enabled error"
echo "smoke: stock Shell: enabled and patched, no error"

run_shell break
grep -q 'SMOKE patched=false' "$work/break.log" ||
  { cat "$work/break.log" >&2; fail "renamed run: the tray was patched or the probe never reported"; }
[ "$(errors break)" = 1 ] || { cat "$work/break.log" >&2; fail "renamed run: expected exactly one error"; }
grep "$uuid: not enabled" "$work/break.log" | grep -q 'MessageTray._notificationQueue' ||
  fail "renamed run: the error does not name MessageTray._notificationQueue"
echo "smoke: renamed _notificationQueue: one error naming it, tray left untouched"
grep "$uuid: not enabled" "$work/break.log"

run_shell late
grep -q 'SMOKE patched=false' "$work/late.log" ||
  { cat "$work/late.log" >&2; fail "late run: the tray stayed patched after a source without notifications appeared"; }
[ "$(errors late)" = 1 ] || { cat "$work/late.log" >&2; fail "late run: expected exactly one error"; }
echo "smoke: app source without notifications after enable: one error, stock behaviour restored"

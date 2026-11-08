#!/usr/bin/env bash
# Stub-based unit tests for move-to-backups.sh: sources the helpers only (main is guarded) and never moves,
# mounts, stops or signals anything — users_of/docker/systemctl/rsync/as_owner are replaced by functions.
# Run: scripts/backups-disk/test-move-to-backups.sh   (no root, no sudo)
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT=${MOVE_SCRIPT:-$HERE/move-to-backups.sh}
# shellcheck source=/dev/null
source "$SCRIPT"
set +e   # tests inspect return codes themselves
FAILS=0
check() { # desc expected actual
  if [[ $2 == "$3" ]]; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s (expected %q got %q)\n' "$1" "$2" "$3"; FAILS=$((FAILS + 1)); fi
}
log() { :; }; warn() { :; }

# ---- (a) .cache: GUI fd/mmap holders are noise, a GUI cwd is not
git_partners() { :; }
docker_users_of() { :; }
ROWS=""
users_of() { printf '%s' "$ROWS"; }
CACHE=$HOME_DIR/.cache
row() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5"; }
t_busy() { INFO_SEEN=(); busy "$1" quiet; echo $?; }   # 0 = busy, 1 = free

ROWS=$(row 126060 gnome-shell fd "$CACHE" "$CACHE/event-sound-cache.tdb.f55495e8cc28411988443a13435fc685.x86_64-pc-linux-gnu")
check "(a) gnome-shell fd on event-sound-cache.tdb.* is ignored" 1 "$(t_busy "$CACHE")"
ROWS=$(row 126060 gnome-shell fd,mmap "$CACHE" "$CACHE/nvidia/GLCache/ab/cd/entry")
check "(a) gnome-shell fd,mmap on nvidia/GLCache is ignored" 1 "$(t_busy "$CACHE")"
ROWS=$(row 1 Xwayland mmap "$CACHE" "$CACHE/whatever.bin")
check "(a) Xwayland mmap-only never blocks" 1 "$(t_busy "$CACHE")"
ROWS=$(row 700 ghostty cwd,fd "$CACHE" "$CACHE/somewhere")
check "(a) ghostty with a cwd in .cache still blocks" 0 "$(t_busy "$CACHE")"
ROWS=$(row 126060 gnome-shell fd "$CACHE" "$CACHE/event-sound-cache.tdb.x"; row 700 ghostty cwd "$CACHE" "$CACHE/proj")
check "(a) gnome-shell noise + ghostty cwd together: blocked" 0 "$(t_busy "$CACHE")"
INFO_SEEN=(); busy "$CACHE" quiet >/dev/null
check "(a)   blocking rows keep only the ghostty cwd" "700" "$(cut -f1 <<<"$BUSY_OUT")"
ROWS=$(row 4242 python3 fd "$CACHE" "$CACHE/event-sound-cache.tdb.x")
check "(a) non-GUI process (python3) with an fd still blocks" 0 "$(t_busy "$CACHE")"
ROWS=$(row 126060 gnome-shell fd "$HOME_DIR/work" "$HOME_DIR/work/x")
check "(a) the relaxed rule is .cache only: gnome-shell fd in work blocks" 0 "$(t_busy "$HOME_DIR/work")"
never_kill_pid 126060 gnome-shell; check "(a) gnome-shell stays on the never-signal list" 0 $?
never_kill_pid 700 ghostty;        check "(a) ghostty stays on the never-signal list" 0 $?

# ---- (b) follow-up rsync passes
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
CALLS=0; LATE_PASSES=1; RSYNC_RC=0
# Emulates `rsync -a --remove-source-files src/ dst/`: moves the files; the first LATE_PASSES calls drop a new file
# into src after the copy (a file written while a long copy runs).
rsync() {
  local src=${*: -2:1} dst=${*: -1} f
  CALLS=$((CALLS + 1))
  while IFS= read -r f; do mkdir -p "$dst/$(dirname "${f#"$src"}")"; mv "$f" "$dst/${f#"$src"}"; done < <(find "$src" -type f)
  if (( CALLS <= LATE_PASSES )); then echo late >"${src}late-$CALLS.txt"; fi
  return "$RSYNC_RC"
}
mk() { rm -rf "$T/s" "$T/d"; mkdir -p "$T/s/a/b" "$T/d"; echo x >"$T/s/a/b/f1"; echo y >"$T/s/f2"; CALLS=0; }
mk; LATE_PASSES=1; move_tree "$T/s" "$T/d" t; rc=$?
check "(b) file appearing during pass 1: move_tree ok" 0 "$rc"
check "(b)   exactly 2 rsync passes" 2 "$CALLS"
check "(b)   source holds no files" 0 "$(find "$T/s" ! -type d | wc -l)"
check "(b)   late file reached dst" "yes" "$([[ -f $T/d/late-1.txt ]] && echo yes)"
mk; LATE_PASSES=0; move_tree "$T/s" "$T/d" t; rc=$?
check "(b) nothing new: single pass" "0 1" "$rc $CALLS"
mk; LATE_PASSES=3; move_tree "$T/s" "$T/d" t; rc=$?
check "(b) files created in the first 3 passes: 4th pass cleans, ok" "0 4" "$rc $CALLS"
mk; LATE_PASSES=99; move_tree "$T/s" "$T/d" t; rc=$?
check "(b) source keeps growing: fails after 1+3 passes, count reported" "3 4 1" "$rc $CALLS $LEFT_COUNT"
mk; LATE_PASSES=0; RSYNC_RC=23; move_tree "$T/s" "$T/d" t; rc=$?; RSYNC_RC=0
check "(b) rsync exit 23: fails at once, no follow-up" "1 1" "$rc $CALLS"
mk; LATE_PASSES=0; RSYNC_RC=24; move_tree "$T/s" "$T/d" t; rc=$?; RSYNC_RC=0
check "(b) rsync exit 24 (vanished files) is fine" "0" "$rc"

# ---- (c) watchdog guard of aisis-* units/containers
SYSTEMCTL=(); ACTIVE=1
as_owner() { SYSTEMCTL+=("$*"); if [[ $* == *is-active* ]]; then return $((1 - ACTIVE)); fi; return 0; }
declare -A G_KIND=() G_UNIT=()
G_KIND[unit:aisis-yandex-embed.service]=unit; G_UNIT[unit:aisis-yandex-embed.service]=aisis-yandex-embed.service
G_KIND[docker:aisis-quasar-prod-blue]=docker
G_KIND[unit:localsearch-3.service]=unit;      G_UNIT[unit:localsearch-3.service]=localsearch-3.service
STOPPED_GUARDS=(); SYSTEMCTL=()
stop_watchdogs unit:localsearch-3.service
check "(c) unrelated unit: failover guard untouched" "0" "${#STOPPED_GUARDS[@]}"
stop_watchdogs unit:localsearch-3.service unit:aisis-yandex-embed.service docker:aisis-quasar-prod-blue
check "(c) aisis-yandex-embed + container in plan: aisis-failover.service stopped once" "aisis-failover.service|1" "${STOPPED_GUARDS[0]}|${#STOPPED_GUARDS[@]}"
n=$(printf '%s\n' "${SYSTEMCTL[@]}" | grep -c -- '--user stop aisis-failover.service'); check "(c)   stop issued exactly once" 1 "$n"
stop_watchdogs unit:aisis-yandex-embed.service   # retry pass
n=$(printf '%s\n' "${SYSTEMCTL[@]}" | grep -c -- '--user stop aisis-failover.service'); check "(c)   retry pass does not stop it again" 1 "$n"
ORDER=()
start_unit() { ORDER+=("unit:$1"); }; start_container() { ORDER+=("container:$1"); }
STOPPED_UNITS=(aisis-yandex-embed.service); STOPPED_CONTAINERS=(aisis-quasar-prod-blue)
restart_stopped_units
check "(c) restart order: units, containers, guard last" "unit:aisis-yandex-embed.service container:aisis-quasar-prod-blue unit:aisis-failover.service" "${ORDER[*]}"
check "(c)   guard list emptied (idempotent for traps)" 0 "${#STOPPED_GUARDS[@]}"
ACTIVE=0; STOPPED_GUARDS=(); SYSTEMCTL=()
stop_watchdogs unit:aisis-yandex-embed.service
check "(c) guard not running: nothing stopped/recorded" "0" "${#STOPPED_GUARDS[@]}"

ACTIVE=1; STOPPED_GUARDS=(); SYSTEMCTL=()
G_KIND[unit:aisis-failover.service]=unit; G_UNIT[unit:aisis-failover.service]=aisis-failover.service
stop_watchdogs unit:aisis-failover.service
check "(c) the guard as a plain stop group is not recorded as its own guard (no early restart)" "0" "${#STOPPED_GUARDS[@]}"

echo; if (( FAILS == 0 )); then echo "ALL OK"; else echo "$FAILS FAILED"; exit 1; fi

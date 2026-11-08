#!/usr/bin/env bash
# Stub-based unit tests for move-to-backups.sh: sources the helpers only (main is guarded) and never moves,
# mounts, stops or signals anything — users_of/docker/systemctl/rsync/as_owner are replaced by functions.
# Run: scripts/backups-disk/test-move-to-backups.sh   (no root, no sudo)
# The tests replace helpers with stub functions and set the script's globals, which shellcheck cannot see being used.
# shellcheck disable=SC2034,SC2329,SC2016
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT=${MOVE_SCRIPT:-$HERE/move-to-backups.sh}
# shellcheck source=/dev/null
source "$SCRIPT"
set +e   # tests inspect return codes themselves
# Originals of helpers that the sections below replace with stubs; later sections restore them where the real code is under test.
eval "$(declare -f users_of | sed '1s/^users_of/real_users_of/')"
eval "$(declare -f docker_users_of | sed '1s/^docker_users_of/real_docker_users_of/')"
FAILS=0
check() { # desc expected actual
  if [[ $2 == "$3" ]]; then printf 'ok   %s\n' "$1"; else printf 'FAIL %s (expected %q got %q)\n' "$1" "$2" "$3"; FAILS=$((FAILS + 1)); fi
}
# Re-sources the script: puts back every real function and default that a section stubbed or changed.
reload_script() {
  # shellcheck source=/dev/null
  source "$SCRIPT"; set +e
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


# ---- (d) log file: private dir, O_EXCL name, no symlink following (#231 finding: predictable log path)
warn() { WARNED+=("$*"); }; WARNED=()
LOG_DIR=$T/logs; LOG=""
init_log; rc=$?
first=$LOG
check "(d) init_log creates the dir and a file in it" "0 yes" "$rc $([[ -f $first && $first == "$LOG_DIR"/move-to-backups-* ]] && echo yes)"
check "(d)   log file is private (0600)" "600" "$(stat -c %a "$first")"
init_log; check "(d)   a second run gets another file (no reuse of a name)" "differs" "$([[ $LOG != "$first" ]] && echo differs)"
# attacker (the owner) pre-plants a link at the OLD predictable path inside the home and in the log dir
echo precious >"$T/victim"; ln -s "$T/victim" "$T/old-style.log"; ln -s "$T/victim" "$LOG_DIR/move-to-backups-$(date +%Y%m%d-%H%M%S).log"
init_log; echo more >>"$LOG"
check "(d)   planted symlinks are never written through" "precious" "$(cat "$T/victim")"
ln -s "$T/logs" "$T/logs-link"; LOG_DIR=$T/logs-link; LOG=""
init_log; rc=$?
check "(d) LOG_DIR that is a symlink: refused, no file created" "1 " "$rc $LOG"
mkdir -m 0777 "$T/open"; chmod 0777 "$T/open"; LOG_DIR=$T/open; LOG=""
init_log; rc=$?
check "(d) LOG_DIR writable by others: refused" "1 0" "$rc $(find "$T/open" -type f | wc -l)"
# the log dir itself is root-owned and tight, but an ANCESTOR the attacker can write lets him swap the whole dir
mkdir -p "$T/anc/sub"; chmod 0777 "$T/anc"; LOG_DIR=$T/anc/sub/logs; LOG=""
init_log; rc=$?
check "(d) LOG_DIR below a group/other-writable ancestor: refused" "1 0" "$rc $(find "$T/anc" -type f | wc -l)"
mkdir -p "$T/own2/sub"
stat() { if [[ $* == *%u* && $* == *"$T/own2" ]]; then echo 12345; else command stat "$@"; fi; }
LOG_DIR=$T/own2/sub/logs; LOG=""
init_log; rc=$?
unset -f stat
check "(d) LOG_DIR below an ancestor owned by another user: refused" "1 0" "$rc $(find "$T/own2" -type f | wc -l)"
# a relative path cannot be validated
LOG_DIR=relative/logs; LOG=""; init_log; check "(d) a relative LOG_DIR is refused" "1" "$?"
# unprivileged default (`cleanup` is run without sudo): a private dir under the owner's state dir
HOME_DIR=$T/fakehome; mkdir -p "$HOME_DIR"; LOG_DIR=""; LOG=""
init_log; rc=$?
check "(d) unprivileged default dir: created 0700 under ~/.local/state, file made" "0 700 yes" "$rc $(stat -c %a "$HOME_DIR/.local/state/move-to-backups") $([[ -f $LOG && $LOG == "$HOME_DIR"/.local/state/move-to-backups/* ]] && echo yes)"
# main <phase> tees its output into that file
out=$(main cleanup 2>&1)
check "(d) main cleanup writes the output into a log file in that dir too" "1" "$(grep -l 'regenerable caches' "$HOME_DIR"/.local/state/move-to-backups/*.log | wc -l)"
HOME_DIR=/home/ultra; LOG_DIR=$T/logs

# ---- (e) destination symlinks are refused BEFORE rsync/mkdir/chown (#231 finding: root's rsync through a planted link)
# move_item against a scratch HOME/backup tree; mount-table, busy check, rsync, mount, chown are stubs.
HOME_DIR=$T/h; MNT=$T/m; DEST=$MNT/home-ultra; FSTAB=$T/fstab; : >"$FSTAB"
is_bind_mounted() { return 1; }; nested_mounts() { :; }; busy() { return 1; }
mount() { :; }; umount() { :; }; chown() { :; }; systemctl() { :; }
mk_e() { rm -rf "$HOME_DIR" "$MNT" "$T/elsewhere"; mkdir -p "$HOME_DIR/proj" "$DEST" "$T/elsewhere"; echo x >"$HOME_DIR/proj/f"; CALLS=0; }
mk_e; ln -s "$T/elsewhere" "$DEST/proj"
move_item proj; rc=$?
check "(e) destination itself is a symlink: refused, rsync never started" "1 0" "$rc $CALLS"
check "(e)   the link target was not written to" "0" "$(find "$T/elsewhere" -mindepth 1 | wc -l)"
check "(e)   the source is untouched" "yes" "$([[ -f $HOME_DIR/proj/f ]] && echo yes)"
mk_e; rm -rf "$MNT/home-ultra"; ln -s "$T/elsewhere" "$MNT/home-ultra"
move_item proj; rc=$?
check "(e) a PARENT of the destination is a symlink: refused, rsync never started" "1 0" "$rc $CALLS"
check "(e)   nothing created through the link" "0" "$(find "$T/elsewhere" -mindepth 1 | wc -l)"
mk_e
move_item proj >/dev/null; check "(e) control: a plain destination reaches rsync" "yes" "$([[ $CALLS -ge 1 ]] && echo yes)"
# back to the real functions and defaults (a later section stubs only what it needs)
# shellcheck source=/dev/null
reload_script
log() { :; }; warn() { WARNED+=("$*"); }
# ---- (f) fstab append is checked, atomic and duplicate-free (#231 finding: unchecked append, DONE after a failed write)
FSTAB=$T/fstab2; printf 'UUID=abc / ext4 defaults 0 1\n# note' >"$FSTAB"   # no final newline
fstab_append_line "UUID=zzz /mnt/x ext4 defaults 0 2"; rc=$?
check "(f) append to a file without a final newline: ok" "0" "$rc"
check "(f)   old last line is not glued to the new one" $'UUID=abc / ext4 defaults 0 1\n# note\nUUID=zzz /mnt/x ext4 defaults 0 2' "$(cat "$FSTAB")"
fstab_append_line "UUID=zzz /mnt/x ext4 defaults 0 2"
check "(f)   the same line twice is not duplicated" "1" "$(grep -c '^UUID=zzz' "$FSTAB")"
before=$(cat "$FSTAB")
mv() { return 1; }
fstab_append_line "UUID=new /mnt/n ext4 defaults 0 2"; rc=$?
unset -f mv
check "(f) failing replace: append reports failure" "1" "$rc"
check "(f)   fstab unchanged and no temp file left" "$before 0" "$(cat "$FSTAB") $(find "$T" -maxdepth 1 -name 'fstab2.*' | wc -l)"
fstab_append_line $'a\nb'; check "(f) a multi-line entry is refused" "1" "$?"
FSTAB_BACKED_UP=1
fstab_add_bind "$T/d d" "$T/p"; check "(f) bind line with whitespace in a path is refused" "1" "$?"
fstab_add_bind "$T/dst" "$T/mp"; rc=$?
check "(f) fstab_add_bind writes one bind line" "0 1" "$rc $(awk -v p="$T/mp" '$2 == p && $4 ~ /bind/' "$FSTAB" | wc -l)"
fstab_add_bind "$T/dst" "$T/mp"; check "(f)   a second add keeps one line" "1" "$(awk -v p="$T/mp" '$2 == p' "$FSTAB" | wc -l)"
# move_item: a failed fstab write must not end in DONE; the bind is taken down again and the item reports failure
HOME_DIR=$T/h; MNT=$T/m; DEST=$MNT/home-ultra; FSTAB=$T/fstab3; : >"$FSTAB"
is_bind_mounted() { return 1; }; nested_mounts() { :; }; busy() { return 1; }
mount() { cp -a "$2/." "$3/"; }; umount() { UMOUNTED+=("$1"); }; chown() { :; }; systemctl() { :; }
eval "$(declare -f fstab_add_bind | sed '1s/^fstab_add_bind/real_fstab_add_bind/')"
fstab_add_bind() { return 1; }
LOGS=(); log() { LOGS+=("$*"); }; UMOUNTED=()
mk_e; CALLS=0; move_item proj; rc=$?
check "(f) fstab write fails: move_item fails" "1" "$rc"
check "(f)   the fresh bind is unmounted again" "$HOME_DIR/proj" "${UMOUNTED[*]}"
check "(f)   no DONE line was logged" "0" "$(printf '%s\n' "${LOGS[@]}" | grep -c '^DONE')"
fstab_add_bind() { real_fstab_add_bind "$@"; }
FSTAB=$T/fstab3; : >"$FSTAB"; FSTAB_BACKED_UP=1
mk_e; LOGS=(); log() { LOGS+=("$*"); }; move_item proj; rc=$?
check "(f) control: fstab writable: DONE and one bind line" "0 1 1" "$rc $(printf '%s\n' "${LOGS[@]}" | grep -c '^DONE') $(grep -c "^$DEST/proj $HOME_DIR/proj " "$FSTAB")"
unset -f is_bind_mounted nested_mounts busy mount umount chown systemctl
reload_script
log() { :; }; warn() { WARNED+=("$*"); }
# ---- (g) a container mounting an ANCESTOR of the tree is a blocker (#231 finding)
DOCKER_INSPECT=""
docker() { case $1 in ps) echo abc123 ;; inspect) printf '%s\n' "$DOCKER_INSPECT" ;; esac; }
TREE=$T/g/work
d_rows() { DOCKER_INSPECT=$1; real_docker_users_of "$TREE"; }
check "(g) mount source = the tree: reported" "docker:app" "$(d_rows "/app $TREE " | cut -f1)"
check "(g) mount source below the tree: reported" "docker:app" "$(d_rows "/app $TREE/sub " | cut -f1)"
check "(g) mount source = the parent dir: reported" "docker:app" "$(d_rows "/app $T/g " | cut -f1)"
check "(g) mount source = a distant ancestor: reported" "docker:app" "$(d_rows "/app $T " | cut -f1)"
check "(g) mount source = / : reported" "docker:app" "$(d_rows "/app / " | cut -f1)"
check "(g) ancestor row carries the real mount source" "$T/g" "$(d_rows "/app $T/g " | cut -f5)"
check "(g) sibling with a common name prefix is not an ancestor" "" "$(d_rows "/app $TREE-other ")"
check "(g) unrelated source: not reported" "" "$(d_rows "/app /var/lib/x ")"
check "(g) only the matching one of several mounts: reported once" "1" "$(d_rows "/app /var/lib/x $T/g " | wc -l)"
users_of() { :; }; git_partners() { :; }; docker_users_of() { real_docker_users_of "$@"; }
INFO_SEEN=(); DOCKER_INSPECT="/app $T/g "; busy "$TREE" quiet; check "(g) busy(): container mounting the parent blocks the item" "0" "$?"
INFO_SEEN=(); DOCKER_INSPECT="/app /var/lib/x "; busy "$TREE" quiet; check "(g)   unrelated container does not" "1" "$?"
unset -f docker

# ---- (h) writable shared mmap with no fd is a holder (#231 finding); a read-only mapping stays harmless
H2=$T/h2; mkdir -p "$H2"; echo data >"$H2/rw.bin"; echo data >"$H2/ro.bin"
holder() { # file mode -> starts a process that maps the file and closes the fd; pid in HPID
  # libc mmap + close(fd): Python's mmap module keeps a dup of the fd, which would make this an fd holder. The path
  # comes through the environment so the command line does not mention it either.
  HOLD_FILE=$1 HOLD_MODE=$2 python3 -c 'import ctypes,os,time
libc=ctypes.CDLL(None,use_errno=True); libc.mmap.restype=ctypes.c_void_p
libc.mmap.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_long]
w=os.environ["HOLD_MODE"]=="w"
fd=os.open(os.environ["HOLD_FILE"],os.O_RDWR if w else os.O_RDONLY)
addr=libc.mmap(None,4096,3 if w else 1,1,fd,0)  # PROT_READ|PROT_WRITE, MAP_SHARED
assert addr not in (None,ctypes.c_void_p(-1).value)
os.close(fd)
print("ready",flush=True); time.sleep(60)' >"$T/ready.$2" &
  HPID=$!
  for _ in $(seq 100); do [[ -s $T/ready.$2 ]] && break; sleep 0.1; done
}
users_of() { real_users_of "$@"; }; docker_users_of() { :; }; git_partners() { :; }
holder "$H2/rw.bin" w; W=$HPID; holder "$H2/ro.bin" r; R=$HPID
rows=$(real_users_of "$H2")
check "(h) writable shared mmap-only process is listed with kind mmapw" "mmap,mmapw" "$(awk -F'\t' -v p="$W" '$1 == p {print $3}' <<<"$rows")"
check "(h)   read-only mmap-only process is listed as plain mmap" "mmap" "$(awk -F'\t' -v p="$R" '$1 == p {print $3}' <<<"$rows")"
INFO_SEEN=(); busy "$H2" quiet; rc=$?
check "(h) busy(): the writable mapper blocks" "0 $W" "$rc $(cut -f1 <<<"$BUSY_OUT")"
kill "$W"; wait "$W" 2>/dev/null
INFO_SEEN=(); busy "$H2" quiet; rc=$?
check "(h)   with only the read-only mapper left the path is free" "1" "$rc"
kill "$R"; wait "$R" 2>/dev/null
cache_holder_ignorable "$HOME_DIR/.cache" ghostty fd,mmap,mmapw; check "(h) .cache rule: a GUI process's writable cache mmap stays ignorable" "0" "$?"
cache_holder_ignorable "$HOME_DIR/.cache" python3 mmap,mmapw; check "(h)   but not for a non-GUI process" "1" "$?"
reload_script
log() { :; }; warn() { WARNED+=("$*"); }
# ---- (i) secrets never reach the log (#231 finding): redaction of command lines
SEC='SUPERSECRET-9f3a'
has_secret() { grep -q -- "$SEC" <<<"$1" && echo leaked || echo clean; }
for s in "app --token=$SEC --port 80" "app --api-key $SEC -p 3" "DB_PASSWORD=$SEC cmd" "x --passwd $SEC --v" \
          "curl -H Authorization:\\ Bearer\\ $SEC http://u:$SEC@h/x" "run --client-secret=$SEC" "env AWS_SECRET_ACCESS_KEY=$SEC a"; do
  check "(i) redact_secrets hides the secret in: ${s%%"$SEC"*}…" "clean" "$(has_secret "$(redact_secrets "$s")")"
done
check "(i)   harmless words are kept" "sleep 5 --port 80 plain.txt" "$(redact_secrets 'sleep 5 --port 80 plain.txt')"
check "(i)   an option after a redacted one is kept" "app --token <redacted> --verbose" "$(redact_secrets "app --token $SEC --verbose")"
# a real process with secrets on its command line: what is recorded for display/log vs what is needed to restart it
python3 -c 'import time; time.sleep(30)' "--token=$SEC" --api-key "$SEC" --name ok & SPID=$!
for _ in $(seq 100); do grep -q -- "$SEC" "/proc/$SPID/cmdline" 2>/dev/null && break; sleep 0.05; done   # wait for the exec
TERMINATED=(); TERM_DESC=(); TERM_INNER=()
record_terminated "$SPID"
check "(i) record_terminated: TERMINATED text (logged) has no secret" "clean" "$(has_secret "${TERMINATED[*]}")"
check "(i)   TERM_DESC (prompt) has no secret" "clean" "$(has_secret "${TERM_DESC[*]}")"
check "(i)   the restart line still holds the real value (the process must start the same)" "leaked" "$(has_secret "${TERM_INNER[*]}")"
check "(i)   plain_start_cmd (displayed in prompt and report) has no secret" "clean" "$(has_secret "$(plain_start_cmd "${TERM_INNER[0]}")")"
as_owner() { return 1; }
check "(i)   describe_pid (plan/explanations) has no secret" "clean" "$(has_secret "$(describe_pid "$SPID" fd "$T/x")")"
KEEP_INNER=("${TERM_INNER[@]}")
unset -f as_owner; reload_script; log() { :; }; warn() { WARNED+=("$*"); }
# the whole restart step: everything it prints/logs, and what it hands to runuser
TERM_INNER=("${KEEP_INNER[@]}"); TERMINATED=("pid $SPID sleep (cwd $T)"); TERM_DESC=("pid $SPID sleep (cwd $T)")
runuser() { printf '%s\n' "$@" >"$T/runuser.args"; }   # file, not a variable: restart_phase runs in a $(...) subshell below
ASSUME_YES=1; log() { echo "$*"; }; warn() { echo "WARN $*"; }
out=$(restart_phase 2>&1)
unset ASSUME_YES
log() { :; }; warn() { WARNED+=("$*"); }
check "(i)   restart_phase output (what tee writes into the log) has no secret" "clean" "$(has_secret "$out")"
kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null

# ---- (j) restart commands: quoted for bash, run by bash (#231 finding: %q quoting run through sh -c)
check "(j) restart_phase starts the process with setsid -f bash -c, not sh -c" "setsid -f bash -c" "$(sed -n '/^setsid$/,/^-c$/p' "$T/runuser.args" | paste -sd' ')"
NASTY=($'a\nb' "x'y" '$(touch '"$T"'/PWN1)' ';touch '"$T"'/PWN2' '`touch '"$T"'/PWN3`' $'tab\there' '--flag=*' '')
inner=$(restart_inner "$T" bash -c 'printf "%s|" "$@" >"$0"' "$T/args.out" "${NASTY[@]}")
bash -c "$inner"
expected=$(printf '%s|' "${NASTY[@]}")
check "(j) every argument arrives unchanged (newline, quotes, \$(), ;, backticks, tab, glob, empty)" "$expected" "$(cat "$T/args.out")"
check "(j)   no argument was executed as code" "0" "$(find "$T" -maxdepth 1 -name 'PWN*' | wc -l)"
inner=$(restart_inner "$T/dir with space" echo hi)
check "(j)   a cwd with spaces stays one word" "cd $(printf '%q' "$T/dir with space") && exec echo hi" "$inner"
unset -f runuser

# ---- (k) exit status of `move`: non-zero when an item failed (#231 finding)
need_root() { :; }; require_mnt() { :; }; enabled_items() { printf 'core a 1\ncore b 1\n'; }; upfront_stop() { return 0; }
restart_phase() { :; }; restart_report() { :; }
FAILURES=0
run_item() { ITEMS_FAILED=$((ITEMS_FAILED + FAILURES)); }
k_rc() { ( ITEMS_FAILED=0 ITEMS_MOVED=0; phase_move >/dev/null 2>&1 ); echo $?; }
FAILURES=1; check "(k) an item failed: phase_move exits non-zero" "1" "$(k_rc)"
FAILURES=0; check "(k)   every item moved or skipped: exits 0" "0" "$(k_rc)"
unset -f need_root require_mnt enabled_items upfront_stop restart_phase restart_report run_item
reload_script; log() { :; }; warn() { WARNED+=("$*"); }



echo; if (( FAILS == 0 )); then echo "ALL OK"; else echo "$FAILS FAILED"; exit 1; fi

#!/usr/bin/env bash
# move-to-backups.sh — move cold bulk data from /home/ultra to the USB disk "backups"
# and keep every ORIGINAL path working through a bind mount (never a symlink).
#
# Disk: 1 TB ext4, UUID b93572e7-27ab-4390-be44-bb1089d013d7, label "backups" (/dev/sda1),
# mounted by an /etc/fstab line at /run/media/ultra/backups (the path Ubuntu/GNOME uses).
# Data lands in /run/media/ultra/backups/home-ultra/<relative path>.
#
# Commands (run as root):
#   sudo ./move-to-backups.sh prep      verify UUID/ext4, add the fstab mount line (backup first, only if no
#                                       active line already mounts that UUID or target), re-mount the disk
#                                       through fstab if GNOME/udisks had mounted it, chown ultra:ultra on the
#                                       mount point and home-ultra/, check free space against the enabled items
#   sudo ./move-to-backups.sh move      per enabled item: busy check -> (unmount nested binds) -> rsync move -> bind
#                                       mount -> fstab line (-> drop the nested bind lines it replaces)
#   sudo ./move-to-backups.sh status    df of / and the backup disk; every bind line in fstab with its
#                                       mounted/unmounted state and entry count
#   ./move-to-backups.sh cleanup        only PRINTS cache/docker/snap/journal deletion suggestions
#
# Optional switches (use `sudo -E` so they survive sudo, or `sudo VAR=1 ./move-to-backups.sh move`):
#   WITH_OPT=1   also include tier "opt"  (modified in the last ~2 days or referenced by units/symlinks)
#   WITH_STOP=1  also include tier "stop" (needs a service stopped first, see below)
#   ONLY=regex   restrict to items whose relative path matches the regex (e.g. ONLY='^projects/')
#   ASSUME_YES=1 answer "yes" to the up-front question ("stop everything listed, move, start it back?") and to every
#                per-item restart question. Without it the questions are asked on /dev/tty (works even when
#                stdin/stdout are redirected), else on stdin; with no way to ask, nothing is stopped and nothing is
#                moved when blockers exist. Flow of `move`: ONE combined scan of every enabled item -> a plan (what is
#                stopped, what is left alone) -> ONE question -> everything is stopped (systemd --user units, docker
#                containers, a `cd ~` for idle tmux shells, SIGTERM then SIGKILL after 10 s for other processes) ->
#                the move -> a retry pass for items that were busy -> restart step: a SEPARATE question for each
#                unit, each container (default yes) and each plain process (default no; the recorded cmdline/cwd is
#                shown). "n" leaves the item stopped; the final report lists its manual start command. Units and
#                containers are also started again, without asking, on Ctrl-C/TERM. NEVER signalled (kept under «НЕ
#                трогаю», their item is skipped): terminal emulators (ghostty, gnome-terminal, kitty, alacritty,
#                konsole, tmux server), this script and every ancestor of it (PPid chain of $$ and SUDO_PID),
#                gnome-shell, Xorg/Xwayland, systemd, session units. Holders that never block: a process that only
#                mmaps (keeps the old inode, the bind path serves identical data), and — for item .cache only — a
#                GUI/session process (gnome-shell, Xwayland, ghostty, chrome, firefox...) whose only holds are fds/
#                mmaps (any file under ~/.cache: event-sound-cache.tdb.*, nvidia/GLCache...; losing a cache write is
#                harmless). A GUI process with a cwd there still blocks and is never signalled. A browser with a real
#                fd on other state is offered as «ЗАКРОЕТ ОКНА БРАУЗЕРА» in the plan. Watchdogs that would revive what
#                was stopped (WATCHDOGS: aisis-failover.service restarts aisis-* units/containers within seconds) are
#                stopped first and started last.
#   e.g.  sudo -E WITH_OPT=1 ONLY='^(work|xp)$' ./move-to-backups.sh move
#
# Tiers:
#   core  cold, safe once no process is inside. Enabled by default.
#   opt   cold-ish (touched yesterday) — big wins, enable deliberately.
#   stop  skipped by default, requires stopping a process first:
#           wyoming-env, wyoming-data
#             -> systemctl --user stop wyoming-whisper.service      (python3 -m wyoming_faster_whisper uses both)
#             run `move` with WITH_STOP=1, then: systemctl --user start wyoming-whisper.service
#
# WHOLE-TREE items: `xp`, `work`, `.codex`, `.cache`, `Downloads`, `Pictures` and `.rustup` are moved as ONE tree each; `work` and `xp` with every worktree inside
# (work/<repo>/.worktrees, work/*-worktrees, work/wt-*, xp/*/.worktrees/*, ...). The tree's own path
# becomes the bind mount, so a directory or git worktree created later anywhere below it is written straight to the
# backup disk. Consequences: (a) such an item moves only while NO process has its cwd, an open fd, a memory mapping
# or a cmdline anywhere inside the tree — a busy tree is skipped as a whole and retried on the next run; (b) ITEMS
# must stay non-nested, so nothing below `work` or `xp` is listed separately; (c) bind mounts that
# earlier runs made below the tree (work/<x> ...) are merged into it, see step 3.
# .codex, .cache, Downloads, Pictures and .rustup are whole-tree items as well. Earlier sub-binds below them
# (.codex/sessions/2026/NN, .cache/*) are merged back into the whole tree by the nested-bind logic (nested_mounts,
# umount, rsync merge, removal of their fstab lines). .bun/install/cache and .npm are bind mounts too; uv/bun then copy
# instead of hardlink.
#
# How `move` treats one item (path = /home/ultra/<rel>, dst = <backups>/home-ultra/<rel>):
#   1. skip when it is not a directory, or is already a bind mount, or when a mount that is NOT one of this script's
#      bind mounts (a bind of <backups>/home-ultra/<same relative path>) sits anywhere below it
#   2. busy check (as root, /proc scan + docker): a process with cwd, exe, an open fd or a memory mapping inside the
#      path or any descendant (nested bind mounts included), or a running docker container with a bind mount source
#      inside it (or equal to it), blocks the item. A process that only MENTIONS the path in its cmdline holds nothing
#      and is ignored (logged as info). If the item is a git repo or a git worktree, every other worktree (and the
#      main dir) of that repo is scanned as well, and the item is skipped when any of them is busy (a worktree's
#      metadata lives in the main repo, so the set is moved together or not at all). A busy item is explained one line
#      per group (unit / tmux terminal / docker container / process, see print_groups), optionally unblocked
#      (resolve_blockers), else SKIPPED, the run continues with the next item.
#   3. nested bind mounts of earlier runs strictly below the item (findmnt) are unmounted, deepest first, with a
#      plain umount (never lazy, never forced). Their data already lives at dst/<same sub path>. A umount error, or
#      a mountpoint directory that is not empty underneath, aborts the item with FAIL and every nested bind that was
#      taken down is mounted again (mount --bind), as is also done on any later failure of the item or on Ctrl-C/TERM.
#   4. the busy check is repeated immediately before rsync starts
#   5. rsync -aHAX --numeric-ids -x --remove-source-files --partial "$src/" "$dst/"
#      The transfer MERGES into what is already in dst (nothing in dst is ever deleted); the former nested
#      mountpoints are now empty directories in the source and only refresh the directory attributes of their
#      twins in dst. Then empty source directories are deleted bottom-up (the item directory itself stays). A source
#      file is removed only after rsync transferred it successfully.
#   6. files still left in the source after rsync (created meanwhile) -> up to 3 short follow-up rsync passes (same
#      flags, move_tree); FAIL (no bind) only when non-directory entries remain after the last one
#   7. `mount --bind dst src`, entry counts of src and dst must match and every former nested mountpoint must still
#      be a directory, then the bind line is appended to fstab (fstab is backed up once per run), the fstab bind
#      lines of the nested paths are removed (exact match on the mount target field, bind lines only, nothing else
#      touched) and `systemctl daemon-reload` runs
#
# WINDOW: between the moment rsync starts removing files and the bind mount, the original path is INCOMPLETE
# (it holds only what has not been moved yet). That is why busy items are refused: a process inside would see
# its files vanish. Keep programs away from the enabled paths while `move` runs.
#
# Re-runnable: if rsync fails or the run is interrupted the item stays partly moved (rest still in place,
# nothing is lost); running `move` again continues from where it stopped, and finished items are skipped.
# Per-item failures never abort the other items.
#
# SINGLE-COPY RISK: this is a MOVE, not a copy. After an item is moved the only copy of its data is on the
# USB disk. If the disk dies or is unplugged the data is gone/unavailable (bind mounts use nofail, so the
# machine still boots, with empty directories at the original paths). Keep your own backup of anything
# irreplaceable before running `move`.
#
# After `move`, reboot-test:  sudo systemctl daemon-reload && mount -a   and after a reboot check that
# /run/media/ultra/backups is mounted by fstab and the bind paths show their data.
#
# Items (tier|relative path under /home/ultra|approx MB). Keep entries non-nested.
set -euo pipefail

UUID="b93572e7-27ab-4390-be44-bb1089d013d7"
MNT="/run/media/ultra/backups"
DEST="$MNT/home-ultra"
HOME_DIR="/home/ultra"
OWNER="ultra:ultra"
FSTAB="/etc/fstab"
MOUNT_LINE="UUID=$UUID $MNT ext4 defaults,nofail,x-mount.mkdir,x-systemd.device-timeout=10s 0 2"
BIND_OPTS="none bind,nofail,x-systemd.requires-mounts-for=$MNT 0 0"
LOG="$HOME_DIR/move-to-backups-$(date +%Y%m%d-%H%M%S).log"

# shellcheck disable=SC2034  # table is consumed by enabled_items below
ITEMS=(
  # --- big repos / worktree sets, idle for days
  # --- whole trees (every repo and worktree below them goes along; see WHOLE-TREE items above). Already-moved parts
  # --- are counted in the size too, so prep over-reserves. xp replaces the separate xp/* entries (no nesting).
  "core|xp|43000"
  "core|work|88000"
  # --- package caches: hot, but bind mounts keep every path working (no symlinks); uv/bun fall back to copying
  # --- instead of hardlinking from a cache on another filesystem. Busy ones are skipped until processes stop.
  "core|.bun/install/cache|15000"
  "core|.npm|1000"
  # --- agent state: finished runs only (live task-cli/review-cli/.omp/.claude stay on the SSD)
  "core|.local/state/rig-cccc-20261026|5605"
  "core|.local/state/hsw-worktree-consolidation-20261022|1724"
  "core|.local/state/rig-cccc-resume-20261027|635"
  "core|.local/share/tasks-app-migration|662"
  # --- whole trees, see WHOLE-TREE items above
  "core|.codex|7300"
  "core|.cache|48400"
  "core|Downloads|2000"
  "core|Pictures|450"
  "core|.rustup|1700"
  # --- apps
  "core|Applications/blender-4.5.3-linux-x64|1131"
  # --- opt: touched in the last ~2 days or referenced by units/symlinks
  "opt|.local/opt/blender-5.2.3|1213"
  "opt|.local/state/rig-cccc-finish-20261029|675"
  "opt|.local/state/rig-cccc-finish-20261028-resume|507"
  # (opt .cache/* items are gone: .cache is a whole-tree core item)
  # --- stop: needs `systemctl --user stop wyoming-whisper.service` first
  "stop|wyoming-env|2607"
  "stop|wyoming-data|1547"
)

# ---------------------------------------------------------------- logging / helpers
log()  { printf '%s %s\n' "$(date +%T)" "$*"; }
warn() { printf '%s WARN %s\n' "$(date +%T)" "$*"; }
die()  { printf '%s ERROR %s\n' "$(date +%T)" "$*"; exit 1; }

need_root() { [[ $EUID -eq 0 ]] || die "run as: sudo $0 <phase>"; }

# Prints "tier rel mb" for every enabled item.
enabled_items() {
  local entry tier rel mb
  for entry in "${ITEMS[@]}"; do
    IFS='|' read -r tier rel mb <<<"$entry"
    case "$tier" in
      opt)  [[ ${WITH_OPT:-0}  == 1 ]] || continue ;;
      stop) [[ ${WITH_STOP:-0} == 1 ]] || continue ;;
    esac
    if [[ -n ${ONLY:-} ]] && ! [[ $rel =~ $ONLY ]]; then continue; fi
    printf '%s %s %s\n' "$tier" "$rel" "$mb"
  done
}

is_bind_mounted() { findmnt -rn -M "$1" >/dev/null 2>&1; }

# Prints "mountpoint<TAB>root<TAB>maj:min" for every mount of the kernel table (or of $MOUNTINFO, a file in
# mountinfo format, used by self-tests), findmnt's \xNN escapes decoded.
mount_table() {
  local -a o=(-rn -o "TARGET,FSROOT,MAJ:MIN")
  local t r d
  if [[ -n ${MOUNTINFO:-} ]]; then o+=(-k -F "$MOUNTINFO"); fi
  findmnt "${o[@]}" | while read -r t r d; do
    printf '%b\t%b\t%s\n' "$t" "$r" "$d"
  done
}

# Mounts strictly below $1, deepest first, one per line: "bind<TAB>path" for a bind of the backup disk's
# <home-ultra>/<same relative path> (made by this script), "foreign<TAB>path" for anything else.
nested_mounts() {
  local base=${1%/} droot=${DEST#"$MNT"} mdev t r d rel table
  table=$(mount_table) || return 1
  mdev=$(awk -F'\t' -v m="$MNT" '$1 == m {print $3; exit}' <<<"$table")
  LC_ALL=C sort -r <<<"$table" | while IFS=$'\t' read -r t r d; do
    [[ $t == "$base"/* ]] || continue
    rel=${t#"$HOME_DIR"/}
    if [[ -n $mdev && $d == "$mdev" && $r == "$droot/$rel" ]]; then
      printf 'bind\t%s\n' "$t"
    else
      printf 'foreign\t%s\n' "$t"
    fi
  done
}

# Nested bind mounts taken down by the current item (umount order, deepest first).
NESTED_DOWN=()

# Creates a missing directory (and missing parents) with the owner/mode of its twin on the backup disk.
mkdir_like() {
  local t=$1 ref=$2
  [[ -d $t ]] && return 0
  mkdir_like "$(dirname "$t")" "$(dirname "$ref")" || return 1
  mkdir "$t" && chown --reference="$ref" "$t" && chmod --reference="$ref" "$t"
}

# Plain umount of the given mountpoints in order (never lazy/forced), recorded in NESTED_DOWN.
# Stops with 1 at the first error; the caller then runs nested_remount.
nested_umount() {
  local t
  for t in "$@"; do
    umount "$t" || { warn "umount $t failed (busy?)"; return 1; }
    NESTED_DOWN+=("$t")
  done
}

# Mounts every nested bind in NESTED_DOWN again, shallowest first, and forgets them. Idempotent; the EXIT/INT/TERM
# traps of `move` call it too. The fstab lines of these mounts are still in place (they are removed only after the
# whole-tree bind succeeded), so `mount <path>` is the manual fix when this fails.
nested_remount() {
  local i t d rc=0
  for (( i = ${#NESTED_DOWN[@]} - 1; i >= 0; i-- )); do
    t=${NESTED_DOWN[i]}
    d="$DEST/${t#"$HOME_DIR"/}"
    if mkdir_like "$t" "$d" && mount --bind "$d" "$t"; then
      log "re-mounted $t"
    else
      warn "COULD NOT re-mount $t — fix with: sudo mount $t   (fstab line is still there)"
      rc=1
    fi
  done
  NESTED_DOWN=()
  return $rc
}

# Lists live processes using any of the given paths (or anything below them): cwd, exe, open fds,
# memory mappings, or the path in the cmdline. One tab-separated line "pid comm kinds root path" per hit
# and scan root; path is the real matched path (cwd/exe/fd/maps, in that preference) or, for a cmdline-only
# match, the root itself; empty output = free.
users_of() {
  python3 - "$@" <<'PY'
import os, re, sys
targets = [t.rstrip('/') for t in sys.argv[1:]]
pats = [re.compile(re.escape(t) + r'(/|\s|$)') for t in targets]
me = {os.getpid(), os.getppid()}
PREF = ('cwd', 'exe', 'fd', 'mmap', 'cmdline')

def match(p):
    return [i for i, t in enumerate(targets) if p == t or p.startswith(t + '/')]

for pid in filter(str.isdigit, os.listdir('/proc')):
    if int(pid) in me:
        continue
    base = '/proc/' + pid
    hits = {}
    fds = {}
    def add(i, kind, path):
        hits.setdefault(i, {}).setdefault(kind, path)
    for name in ('cwd', 'exe'):
        try:
            p = os.readlink(f'{base}/{name}')
            for i in match(p):
                add(i, name, p)
        except OSError:
            pass
    try:
        for fd in os.listdir(f'{base}/fd'):
            try:
                p = os.readlink(f'{base}/fd/{fd}')
                for i in match(p):
                    add(i, 'fd', p)
                    fds.setdefault(i, set()).add(p)
            except OSError:
                pass
    except OSError:
        pass
    try:
        with open(f'{base}/maps', 'r', errors='replace') as f:
            for line in f:
                parts = line.split(None, 5)
                if len(parts) == 6:
                    p = parts[5].rstrip('\n').removesuffix(' (deleted)')
                    for i in match(p):
                        add(i, 'mmap', p)
    except OSError:
        pass
    try:
        with open(f'{base}/cmdline', 'rb') as f:
            cmd = f.read().replace(b'\0', b' ').decode('utf-8', 'replace')
        for i, pat in enumerate(pats):
            if pat.search(cmd):
                add(i, 'cmdline', targets[i])
    except OSError:
        pass
    if hits:
        try:
            with open(f'{base}/comm') as f:
                comm = f.read().strip()
        except OSError:
            comm = '?'
        for i, kp in sorted(hits.items()):
            real = next(kp[k] for k in PREF if k in kp)
            print(f'{pid}\t{comm}\t{",".join(sorted(kp))}\t{targets[i]}\t{real}')
            # every other distinct fd path as its own row, so a cache fd cannot hide a state fd
            for p in sorted(fds.get(i, ())):
                if p != real:
                    print(f'{pid}\t{comm}\tfd\t{targets[i]}\t{p}')
PY
}

# Running docker containers that bind-mount a path inside one of the given trees (or exactly the tree): invisible to
# the /proc scan, because the container's processes hold the files through their own mount namespace. Rows in the
# users_of format with the pseudo pid "docker:NAME", comm docker, kind mount. A source that is a PARENT of the tree
# (e.g. all of $HOME) is not reported: it would flag every container that merely sees the tree and no mount source
# below the tree is actually in the way. Needs root or docker group; a docker that cannot be queried (daemon down)
# means no running containers, so it is skipped. Mount sources with spaces are not supported
# (inspect output is space separated).
docker_users_of() {
  local line name srcs s t
  local -a ids=() sources=()
  command -v docker >/dev/null 2>&1 || return 0
  mapfile -t ids < <(docker ps -q 2>/dev/null || true)
  (( ${#ids[@]} )) || return 0
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    read -r name srcs <<<"$line"
    name=${name#/}
    read -ra sources <<<"$srcs"
    for s in "${sources[@]}"; do
      for t in "$@"; do
        t=${t%/}
        if [[ $s == "$t" || $s == "$t"/* ]]; then printf 'docker:%s\tdocker\tmount\t%s\t%s\n' "$name" "$t" "$s"; fi
      done
    done
  done < <(docker inspect -f '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' "${ids[@]}" 2>/dev/null || true)
}

# Prints the other worktree/main directories of the git repo that $1 belongs to (nothing when $1 is no repo,
# or when the directory is inside $1 and therefore scanned anyway).
git_partners() {
  local p=$1 q
  [[ -e $p/.git ]] || return 0
  while IFS= read -r q; do
    [[ -n $q && $q != "$p" && -d $q ]] || continue
    [[ $q/ == "$p"/* ]] && continue
    printf '%s\n' "$q"
  done < <(git -c safe.directory='*' -C "$p" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')
}

# ---- explaining and clearing blockers ------------------------------------------------------------------------
# The script runs as root via sudo, but the processes that block a move belong to the owner's systemd --user
# manager and tmux server; both are reachable only as that user, so every such call goes through as_owner
# (it also works unprivileged, which is how describe_pid is tested).
OWNER_USER=${OWNER%%:*}
as_owner() {
  local rt
  rt="/run/user/$(id -u "$OWNER_USER")"
  if (( EUID == 0 )); then
    runuser -u "$OWNER_USER" -- env XDG_RUNTIME_DIR="$rt" "$@"
  else
    XDG_RUNTIME_DIR="$rt" "$@"
  fi
}

# Units that must never be stopped from here: stopping them takes the whole session (tmux server = every terminal).
UNIT_PROTECT='^(tmux.*|user@.*|user-runtime-dir@.*|dbus.*|systemd-.*|org\.gnome\..*)\.service$'
STOPPED_UNITS=()
STOPPED_CONTAINERS=()
# Watchdog units that put a stopped unit/container straight back (`regex of stopped names` -> guard unit). Found by
# reading the journal of the 2026-11-08 run: aisis-failover.service (Restart=always, probes every few seconds)
# probes aisis-yandex-embed.service and the aisis-quasar-* containers and, seeing them inactive, runs
# `systemctl --user restart` / `docker restart` within ~5 s — hence the embed unit "came back with a new pid"
# after the stop and blocked .cache again. The unit itself is Restart=on-failure, so systemd alone never did it.
# The guard is stopped BEFORE the units/containers it watches and started AFTER them (nothing to "repair" then).
WATCHDOGS=('^aisis-|aisis-failover.service')
STOPPED_GUARDS=()
RESTART_OK=() RESTART_BAD=() TERMINATED=()
# Plain processes that were SIGTERMed: the `cd CWD && exec CMD` line that starts each one again.
TERM_INNER=()
# Same order as TERM_INNER: the TERMINATED text of that process (per-item question, removal from TERMINATED).
TERM_DESC=()
# Scan result of upfront_stop/stop_again.
ALL_ROWS=""
STOP_APPROVED=0
RETRY_ITEMS=()
declare -A INFO_SEEN=()

# Holders that are NEVER signalled (comm is the 15-char kernel name, hence the prefixes).
NEVER_KILL_COMM='^(ghostty|gnome-terminal.*|kitty|alacritty|konsole|tmux.*|gnome-shell|Xorg|Xwayland|systemd.*)$'
# GUI/browser/terminal processes whose cache-file fds/mmaps are ignored (losing a cache write is harmless).
GUI_COMM='^(chrome|chromium|firefox.*|ghostty|gnome-terminal.*|kitty|alacritty|gnome-shell|Xwayland)$'
BROWSER_COMM='^(chrome|chromium|firefox.*)$'
# (For item .cache ANY fd/mmap held by a GUI process counts as cache noise, see cache_holder_ignorable: the shell's
# event-sound-cache.tdb.* and nvidia/GLCache files are not matched by any name pattern worth maintaining.)
# This script, its parents and the sudo caller's chain: never signalled.
declare -A SELF_PIDS=()
load_self_pids() {
  local p depth
  for p in $$ "${SUDO_PID:-}" "$PPID"; do
    depth=0
    while [[ $p =~ ^[0-9]+$ ]] && (( p > 1 && depth < 64 )); do
      SELF_PIDS[$p]=1
      p=$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null || true)
      depth=$((depth + 1))
    done
  done
}
# Is pid $1 (kernel comm $2) one that must never get a signal?
never_kill_pid() {
  (( ${#SELF_PIDS[@]} )) || load_self_pids
  [[ -n ${SELF_PIDS[$1]:-} ]] || [[ $2 =~ $NEVER_KILL_COMM ]]
}
# Blocking row that is only a GUI/session process holding fd/mmap files inside ~/.cache: ignorable. Everything under
# ~/.cache is regenerable by definition of the dir, and losing a cache write is harmless (gnome-shell holds
# event-sound-cache.tdb.*, nvidia/GLCache/*; browsers hold their own caches). A cwd or exe kind never matches (the
# kinds regex) so a GUI process with its cwd in the tree still blocks, and is never signalled (NEVER_KILL_COMM).
# Args: item path, comm, kinds, real path.
cache_holder_ignorable() {
  local path=$1 comm=$2 kinds=$3
  [[ $path == "$HOME_DIR/.cache" ]] || return 1
  [[ $kinds =~ ^(fd|mmap)(,(fd|mmap))*$ ]] || return 1
  [[ $comm =~ $GUI_COMM ]]
}
# Blocker groups (see group_blockers): key -> kind/unit/pane/name/pids/kinds/first real path/distinct path count.
declare -a G_KEYS=()
declare -A G_KIND=() G_UNIT=() G_PANE=() G_NAME=() G_PIDS=() G_KINDS=() G_PATH=() G_NPATH=() G_SEEN=()
declare -A PANE_BY_PID=() UNIT_DESC=() BL_COMM=() BL_KINDS=() BL_TARGET=()
TMUX_LOADED=0
CTX_KIND=proc CTX_UNIT="" CTX_PANE="" CTX_ID=""

# pane_pid -> "pane_id|session:win.pane|command|path|window name" (window name last: it may contain anything).
load_tmux_panes() {
  local pp id label cmd path win
  PANE_BY_PID=(); TMUX_LOADED=1
  while IFS='|' read -r pp id label cmd path win; do
    [[ -n $pp ]] && PANE_BY_PID[$pp]="$id|$label|$cmd|$path|$win"
  done < <(as_owner tmux list-panes -a -F '#{pane_pid}|#{pane_id}|#{session_name}:#{window_index}.#{pane_index}|#{pane_current_command}|#{pane_current_path}|#{window_name}' 2>/dev/null || true)
}

# Classifies a pid into CTX_KIND = pane (it or an ancestor is a tmux pane's process) | unit (systemd --user
# service) | sysunit (system service) | proc (anything else). The pane test goes first: a pane's shell can sit
# in a service cgroup (tmux-boot.service) that must not be mistaken for "the service to stop".
pid_context() {
  local pid=$1 p=$1 depth=0 c line seen_user=0
  local -a comps=()
  (( TMUX_LOADED )) || load_tmux_panes
  CTX_KIND=proc CTX_UNIT="" CTX_PANE="" CTX_ID="pid:$pid"
  while [[ -n $p ]] && (( p > 1 && depth < 64 )); do
    if [[ -n ${PANE_BY_PID[$p]:-} ]]; then
      CTX_KIND=pane CTX_PANE=${PANE_BY_PID[$p]} CTX_ID="pane:${PANE_BY_PID[$p]%%|*}"
      return 0
    fi
    p=$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null || true)
    depth=$((depth + 1))
  done
  line=$(cat "/proc/$pid/cgroup" 2>/dev/null || true)
  IFS=/ read -ra comps <<<"${line#0::}"
  for c in "${comps[@]}"; do
    if [[ $c == user@*.service ]]; then seen_user=1; continue; fi
    [[ $c == *.service ]] || continue
    if (( seen_user )); then CTX_KIND=unit; CTX_ID="unit:$c"; else CTX_KIND=sysunit; CTX_ID="sys:$c"; fi
    CTX_UNIT=$c
    return 0
  done
  return 0
}

unit_description() {
  local u=$1
  if [[ -z ${UNIT_DESC[$u]+x} ]]; then
    UNIT_DESC[$u]=$(as_owner systemctl --user show -p Description --value "$u" 2>/dev/null || true)
  fi
  printf '%s' "${UNIT_DESC[$u]}"
}

# ~-shortened path for messages.
short_path() { if [[ $1 == "$HOME_DIR" || $1 == "$HOME_DIR"/* ]]; then printf '~%s' "${1#"$HOME_DIR"}"; else printf '%s' "$1"; fi; }

# Prints one human-readable line (Russian, the owner's language) about a pid: what it is (service / terminal /
# plain process) and how it uses the path. KINDS/TARGET (the real path, 5th column of users_of) come from users_of;
# without them the cwd is shown. A "docker:NAME" pseudo pid describes a container. Read-only, works unprivileged.
describe_pid() {
  local pid=$1 kinds=${2:-} target=${3:-} comm args who="" use="" k t desc
  local id label cmd path win
  if [[ $pid == docker:* ]]; then
    printf 'docker контейнер %s: монтирует %s\n' "${pid#docker:}" "$(short_path "$target")"
    return 0
  fi
  if [[ ! -d /proc/$pid ]]; then printf 'pid %s: процесс уже завершился\n' "$pid"; return 0; fi
  comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo '?')
  args=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  args=$(tr -s ' ' <<<"$args"); args=${args% }; [[ -n $args ]] || args="[$comm]"
  (( ${#args} <= 110 )) || args="${args:0:107}..."
  pid_context "$pid"
  if [[ -z $kinds ]]; then
    kinds=cwd; target=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    # cwd of other users' processes needs root; a tmux pane still knows its own directory.
    if [[ -z $target && $CTX_KIND == pane ]]; then IFS='|' read -r id label cmd target win <<<"$CTX_PANE"; fi
    [[ -n $target ]] || target="(путь не прочитать без root)"
  fi
  t=$(short_path "$target")
  local -a ks=()
  IFS=, read -ra ks <<<"$kinds"
  for k in "${ks[@]}"; do
    case $k in
      cwd)     use+="${use:+; }cwd (рабочая папка) в $t" ;;
      exe)     use+="${use:+; }запущен из исполняемого файла в $t" ;;
      fd)      use+="${use:+; }держит открытый файл в $t" ;;
      mmap)    use+="${use:+; }отображает файл из $t в памяти" ;;
      cmdline) use+="${use:+; }путь $t указан в командной строке" ;;
      *)       use+="${use:+; }использует $t ($k)" ;;
    esac
  done
  case $CTX_KIND in
    unit)
      desc=$(unit_description "$CTX_UNIT")
      who="служба $CTX_UNIT (systemd --user${desc:+, «$desc»})" ;;
    sysunit)
      who="системная служба $CTX_UNIT" ;;
    pane)
      IFS='|' read -r id label cmd path win <<<"$CTX_PANE"
      if [[ ${PANE_BY_PID[$pid]:-} == "$CTX_PANE" ]]; then
        who="терминал tmux $label '$win' (сейчас в нём: $cmd)"
      else
        who="программа в терминале tmux $label '$win' (сейчас в нём: $cmd): $args"
      fi ;;
    *)
      who="процесс без службы и терминала: $args" ;;
  esac
  printf 'pid %s %s: %s, %s\n' "$pid" "$comm" "$who" "$use"
}

pid_alive() {
  local s
  s=$(awk '/^State:/{print $2}' "/proc/$1/status" 2>/dev/null || true)
  [[ -n $s && $s != Z ]]
}

# Can a question be asked on the controlling terminal? -r alone is not enough: without a controlling tty the open fails.
tty_usable() { [[ -r /dev/tty && -w /dev/tty ]] && { : </dev/tty; } 2>/dev/null; }

# y/Y/д/Д answers yes. $2 is the default for an empty or missing answer (n unless given). ASSUME_YES=1 answers yes
# without asking. The question goes to /dev/tty when it is usable (stdin/stdout may be redirected, e.g. through
# `exec > >(tee)`), else to stdin; when nothing can be read the default is taken.
ask_yes() {
  local prompt=$1 def=${2:-n} a="" got=1
  if [[ ${ASSUME_YES:-0} == 1 ]]; then printf '%s y (ASSUME_YES=1)\n' "$prompt"; return 0; fi
  if tty_usable; then
    printf '%s ' "$prompt" >/dev/tty
    if read -r a </dev/tty; then got=0; fi
  else
    printf '%s ' "$prompt"
    if read -r a; then got=0; fi
  fi
  if (( got )); then printf '(нет ответа, по умолчанию %s)\n' "$def"; a=$def; fi
  if [[ -z $a ]]; then a=$def; fi
  log "ответ на «$prompt»: $a"
  [[ $a == [yYдД]* ]]
}

# SIGTERM, wait up to 10 s, then SIGKILL (the owner confirmed the whole plan up front).
# The pids come from the /proc scan of users_of, never from an empty or computed expression.
terminate_pids() {
  local p i c
  local -a alive=() targets=()
  # last line of defence: terminals, this script's ancestry, shell/Xorg/systemd never get a signal
  for p in "$@"; do
    c=$(cat "/proc/$p/comm" 2>/dev/null || echo '?')
    if never_kill_pid "$p" "$c"; then warn "pid $p ($c) защищён, сигнал не посылаю"; else targets+=("$p"); fi
  done
  set -- "${targets[@]}"
  (( $# )) || return 0
  for p in "$@"; do [[ $p =~ ^[0-9]+$ ]] && kill -TERM "$p" 2>/dev/null; done || true
  for ((i = 0; i < 20; i++)); do
    alive=()
    for p in "$@"; do if pid_alive "$p"; then alive+=("$p"); fi; done
    if (( ${#alive[@]} == 0 )); then return 0; fi
    sleep 0.5
  done
  warn "не завершились за 10 с после SIGTERM: ${alive[*]} — SIGKILL"
  for p in "${alive[@]}"; do kill -KILL "$p" 2>/dev/null || true; done
  sleep 1
}

# Stop handlers: no questions (the plan was confirmed once). Each returns 0 when it acted, 1 on failure.
stop_unit() {
  local unit=$1 u
  if as_owner systemctl --user stop "$unit"; then
    for u in "${STOPPED_UNITS[@]}"; do [[ $u == "$unit" ]] && return 0; done
    STOPPED_UNITS+=("$unit")
    log "остановлена $unit; будет запущена снова в конце"
    return 0
  fi
  warn "не удалось остановить $unit"
  return 1
}

# A shell that only has the tree as cwd is not killed — the owner types there. It gets `cd ~` instead.
stop_pane() {
  local pane=$1; shift
  local id label cmd path win p
  local -a shells=() others=()
  IFS='|' read -r id label cmd path win <<<"$pane"
  for p in "$@"; do
    if [[ ${BL_COMM[$p]} =~ ^(zsh|bash|sh|fish|dash)$ ]]; then shells+=("$p"); else others+=("$p"); fi
  done
  if (( ${#others[@]} )); then
    log "в терминале tmux $label '$win' сейчас выполняется «$cmd»; останавливаю pid ${others[*]}"
    record_terminated "${others[@]}"
    terminate_pids "${others[@]}"
    sleep 1
    cmd=$(as_owner tmux display-message -p -t "$id" '#{pane_current_command}' 2>/dev/null || true)
  fi
  if (( ${#shells[@]} )); then
    if [[ $cmd =~ ^(zsh|bash|sh|fish|dash)$ ]]; then
      if as_owner tmux send-keys -t "$id" 'cd ~' Enter; then log "в терминал $label отправлено 'cd ~'"; fi
      sleep 1
    else
      warn "терминал $label занят («$cmd»), 'cd ~' не отправляю"
    fi
  fi
  return 0
}

stop_procs() {
  record_terminated "$@"
  terminate_pids "$@"
}

# Remembers cmdline and cwd BEFORE the signal: after SIGTERM /proc is gone, and a plain process has no unit, so this
# is the only way to start it again (offered by restart_phase, listed by restart_report). TERM_INNER gets the
# `cd CWD && exec CMD` line (arguments shell-quoted) for every process with a real cmdline.
record_terminated() {
  local p cmd cwd cmdq
  local -a argv=()
  for p in "$@"; do
    [[ $p =~ ^[0-9]+$ && -d /proc/$p ]] || continue
    argv=()
    mapfile -d '' -t argv <"/proc/$p/cmdline" 2>/dev/null || true
    cwd=$(readlink "/proc/$p/cwd" 2>/dev/null || true)
    if (( ${#argv[@]} )); then
      cmd=$(printf '%s ' "${argv[@]}"); cmd=${cmd% }
      printf -v cmdq '%q ' "${argv[@]}"; cmdq=${cmdq% }
      TERM_INNER+=("cd $(printf '%q' "${cwd:-$HOME_DIR}") && exec $cmdq")
    else
      cmd="[$(cat "/proc/$p/comm" 2>/dev/null || echo '?')]"
    fi
    TERMINATED+=("pid $p $cmd (cwd ${cwd:-?})")
    if (( ${#argv[@]} )); then TERM_DESC+=("pid $p $cmd (cwd ${cwd:-?})"); fi
  done
}

# docker stop, remembered in STOPPED_CONTAINERS (recorded BEFORE the stop so an interrupt in between still restarts
# it; `docker start` of a running container is a no-op). `docker stop` is a manual stop: per the docker docs the
# restart policies always and unless-stopped do NOT bring such a container back until the daemon restarts (always)
# or the container is started by hand — so it stays down while rsync runs. It is started again after the bind mount
# exists, so the container's mount source resolves to the new tree.
stop_container() {
  local name=$1 c known=0 policy
  policy=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$name" 2>/dev/null || true)
  for c in "${STOPPED_CONTAINERS[@]}"; do if [[ $c == "$name" ]]; then known=1; fi; done
  if (( ! known )); then STOPPED_CONTAINERS+=("$name"); fi
  if docker stop "$name" >/dev/null; then
    log "остановлен контейнер $name; будет запущен снова в конце"
    if [[ -n $policy && $policy != no ]]; then
      warn "у контейнера $name политика перезапуска '$policy': он должен оставаться остановленным на всё время переноса (после docker stop docker сам его не поднимет, но перезапуск демона docker поднимет 'always')"
    fi
    return 0
  fi
  warn "не удалось остановить контейнер $name"
  return 1
}

# Stops every watchdog (WATCHDOGS) that would revive one of the stop-planned groups given as keys. Call right after
# group_blockers and BEFORE stop_group. Safe to repeat: a guard already recorded is not touched again.
stop_watchdogs() {
  local key name rule re guard g known
  for rule in "${WATCHDOGS[@]}"; do
    re=${rule%%|*} guard=${rule#*|}
    for key in "$@"; do
      if [[ ${G_KIND[$key]} == docker ]]; then name=${key#docker:}; else name=${G_UNIT[$key]}; fi
      # The guard itself (name matches ^aisis-) is a normal stop group when it holds a path: stop_unit handles it.
      [[ -n $name && $name != "$guard" && $name =~ $re ]] || continue
      known=0
      for g in "${STOPPED_GUARDS[@]}"; do [[ $g == "$guard" ]] && known=1; done
      (( known )) && break
      as_owner systemctl --user is-active --quiet "$guard" || break
      STOPPED_GUARDS+=("$guard")   # recorded BEFORE the stop so an interrupt in between still starts it again
      if as_owner systemctl --user stop "$guard"; then
        log "остановлен сторож $guard (иначе он поднимет $name обратно через пару секунд); будет запущен после остальных"
      else
        warn "не удалось остановить сторож $guard — $name может подняться сам"
      fi
      break
    done
  done
}

# Stops the G_* group $1 (call right after group_blockers, before anything else rebuilds G_*).
stop_group() {
  local key=$1
  local -a members=()
  read -ra members <<<"${G_PIDS[$key]}"
  case ${G_KIND[$key]} in
    unit)   stop_unit "${G_UNIT[$key]}" ;;
    pane)   stop_pane "${G_PANE[$key]}" "${members[@]}" ;;
    docker) stop_container "${key#docker:}" ;;
    *)      stop_procs "${members[@]}" ;;
  esac
}

# Groups the users_of rows in $1 (plus docker pseudo-pid rows) into the G_* arrays: one group per tmux pane,
# systemd --user/system unit, docker container, or (plain) process. BL_* keep the per-pid data handle_pane needs.
group_blockers() {
  local line pid comm kinds target rpath key name desc label cmd path win id
  G_KEYS=(); G_KIND=(); G_UNIT=(); G_PANE=(); G_NAME=(); G_PIDS=(); G_KINDS=(); G_PATH=(); G_NPATH=(); G_SEEN=()
  BL_COMM=(); BL_KINDS=(); BL_TARGET=()
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    IFS=$'\t' read -r pid comm kinds target rpath <<<"$line"
    if [[ $pid == docker:* ]]; then
      key=$pid
      if [[ -z ${G_KIND[$key]:-} ]]; then
        G_KEYS+=("$key"); G_KIND[$key]=docker; G_UNIT[$key]=""; G_PANE[$key]=""; G_PIDS[$key]=""
        G_NAME[$key]="docker контейнер ${pid#docker:}"
      fi
    else
      pid_context "$pid"
      key=$CTX_ID
      if [[ -z ${G_KIND[$key]:-} ]]; then
        G_KEYS+=("$key"); G_KIND[$key]=$CTX_KIND; G_UNIT[$key]=$CTX_UNIT; G_PANE[$key]=$CTX_PANE; G_PIDS[$key]=""
        case $CTX_KIND in
          unit)
            desc=$(unit_description "$CTX_UNIT")
            name="$CTX_UNIT${desc:+ «$desc»}" ;;
          sysunit) name="системная служба $CTX_UNIT" ;;
          pane)
            IFS='|' read -r id label cmd path win <<<"$CTX_PANE"
            name="терминал tmux $label '$win' (сейчас: $cmd)" ;;
          *) name="процесс $comm" ;;
        esac
        G_NAME[$key]=$name
      fi
      if [[ -z ${BL_COMM[$pid]:-} ]]; then
        BL_COMM[$pid]=$comm; BL_KINDS[$pid]=$kinds; BL_TARGET[$pid]=$rpath
        G_PIDS[$key]+=" $pid"
      else
        BL_KINDS[$pid]+=",$kinds"
      fi
    fi
    G_KINDS[$key]+=",$kinds"
    if [[ -z ${G_SEEN["$key|$rpath"]:-} ]]; then
      G_SEEN["$key|$rpath"]=1
      G_NPATH[$key]=$(( ${G_NPATH[$key]:-0} + 1 ))
      if [[ -z ${G_PATH[$key]:-} ]]; then G_PATH[$key]=$rpath; fi
    fi
  done <<<"$1"
}

# Distinct kinds of group $1, comma separated.
group_kinds() { tr ',' '\n' <<<"${G_KINDS[$1]#,}" | sort -u | paste -sd,; }

# One line for group $1, e.g. "3d-printer-watch@k1se.service «описание»: pid 8247,10979 (cwd,mmap) в xp/3d-cli";
# the path is shown relative to $HOME (the repo and the subpath that is really held).
print_group() {
  local key=$1 pids kinds rel np extra
  pids=${G_PIDS[$key]# }
  if [[ -n $pids ]]; then pids="pid ${pids// /,} "; else pids="bind mount "; fi
  kinds=$(group_kinds "$key")
  rel=${G_PATH[$key]}
  if [[ $rel == "$HOME_DIR"/* ]]; then rel=${rel#"$HOME_DIR"/}; fi
  np=${G_NPATH[$key]}; extra=""
  if (( np > 1 )); then extra=" (+ ещё $((np - 1)) путей)"; fi
  printf '  %s: %s(%s) в %s%s\n' "${G_NAME[$key]}" "$pids" "$kinds" "$rel" "$extra"
}

# All groups, at most 25.
print_groups() {
  local key n=0 total=${#G_KEYS[@]}
  for key in "${G_KEYS[@]}"; do
    if (( n >= 25 )); then printf '  … и ещё %d\n' $(( total - n )); break; fi
    n=$((n + 1))
    print_group "$key"
  done
}

# Prints the reason and returns 0 when group $1 must be left alone: protected units (session services, GNOME
# Shell, see UNIT_PROTECT), system services, and any group holding a never-signalled process (terminal emulator,
# this script or its ancestor, gnome-shell, Xorg/Xwayland, systemd).
group_keep_reason() {
  local key=$1 p
  case ${G_KIND[$key]} in
    unit)
      if [[ ${G_UNIT[$key]} =~ $UNIT_PROTECT ]]; then
        printf 'защищённая служба (сессия), не останавливаю; закройте то, что внутри, вручную — этот пункт будет пропущен'
        return 0
      fi ;;
    sysunit) printf 'системная служба, не останавливаю — этот пункт будет пропущен'; return 0 ;;
  esac
  for p in ${G_PIDS[$key]}; do
    if never_kill_pid "$p" "${BL_COMM[$p]}"; then
      printf 'pid %s (%s): терминал / оболочка этого скрипта или её предок / GNOME Shell / Xorg / systemd — SIGTERM не посылаю; закройте вручную или перенесите позже из другого терминала; этот пункт будет пропущен' "$p" "${BL_COMM[$p]}"
      return 0
    fi
  done
  return 1
}

# What the plan does with group $1.
group_action() {
  local key=$1 p n_other=0
  case ${G_KIND[$key]} in
    unit)   printf 'systemctl --user stop, в конце start' ;;
    docker) printf 'docker stop, в конце docker start' ;;
    pane)
      for p in ${G_PIDS[$key]}; do
        if [[ ! ${BL_COMM[$p]} =~ ^(zsh|bash|sh|fish|dash)$ ]]; then n_other=$((n_other + 1)); fi
      done
      if (( n_other )); then printf 'SIGTERM (через 10 с SIGKILL) для процессов терминала; шеллу — «cd ~»; процессы потом предложу запустить'
      else printf 'шеллу в терминале отправлю «cd ~» (сам шелл не убиваю)'; fi ;;
    *)
      for p in ${G_PIDS[$key]}; do
        if [[ ${BL_COMM[$p]} =~ $BROWSER_COMM ]]; then
          printf 'ЗАКРОЕТ ОКНА БРАУЗЕРА: SIGTERM (через 10 с SIGKILL) — сохраните вкладки/работу заранее; потом предложу запустить'; return 0
        fi
      done
      printf 'SIGTERM (через 10 с SIGKILL); потом предложу запустить командой с тем же cwd' ;;
  esac
}

# Scans every given item (relative paths) with the quiet busy check and collects the blocking rows into ALL_ROWS
# (deduplicated, in scan order). Items move_item would skip anyway are not scanned.
collect_rows() {
  local rel src
  ALL_ROWS=""
  for rel in "$@"; do
    src="$HOME_DIR/$rel"
    [[ -d $src && ! -L $src ]] || continue
    if is_bind_mounted "$src"; then continue; fi
    if busy "$src" quiet; then ALL_ROWS+="${ALL_ROWS:+$'\n'}$BUSY_OUT"; fi
  done
  if [[ -n $ALL_ROWS ]]; then ALL_ROWS=$(awk '!s[$0]++' <<<"$ALL_ROWS"); fi
}

# Up-front phase of `move`: ONE scan of all items, ONE plan, ONE question, then everything stoppable is stopped.
# Returns 0 to go on with the move, 1 when the owner declined (nothing was changed).
upfront_stop() {
  local key reason
  local -a stop_keys=() keep_keys=()
  load_self_pids
  log "Сканирую все выбранные пути: кто их держит…"
  collect_rows "$@"
  if [[ -z $ALL_ROWS ]]; then log "Ничто не держит выбранные пути: останавливать нечего."; return 0; fi
  TMUX_LOADED=0
  group_blockers "$ALL_ROWS"
  for key in "${G_KEYS[@]}"; do
    if reason=$(group_keep_reason "$key"); then keep_keys+=("$key"); else stop_keys+=("$key"); fi
  done
  log "== ПЛАН. Переносу мешают (пока процесс держит путь, он после монтирования работал бы со старой опустевшей папкой):"
  if (( ${#stop_keys[@]} )); then
    log "Будет ОСТАНОВЛЕНО (после переноса верну обратно):"
    for key in "${stop_keys[@]}"; do print_group "$key"; printf '      -> %s\n' "$(group_action "$key")"; done
  fi
  if (( ${#keep_keys[@]} )); then
    log "Останется как есть (НЕ трогаю):"
    for key in "${keep_keys[@]}"; do print_group "$key"; printf '      -> %s\n' "$(group_keep_reason "$key")"; done
  fi
  if (( ${#stop_keys[@]} == 0 )); then log "Останавливать нечего."; return 0; fi
  log "Затем: перенос на диск, bind mount на тех же путях (без симлинков), в конце спрошу про запуск обратно."
  local g
  for key in "${stop_keys[@]}"; do
    if [[ ${G_KIND[$key]} == docker ]]; then reason=${key#docker:}; else reason=${G_UNIT[$key]}; fi
    for g in "${WATCHDOGS[@]}"; do
      if [[ -n $reason && $reason =~ ${g%%|*} ]]; then log "Заодно будет остановлен сторож ${g#*|} (поднимает остановленное обратно), потом запущен."; break 2; fi
    done
  done
  if ! ask_yes "Остановить всё перечисленное, перенести и после запустить обратно? [y/N]"; then return 1; fi
  STOP_APPROVED=1
  stop_watchdogs "${stop_keys[@]}"
  for key in "${stop_keys[@]}"; do stop_group "$key" || true; done
  sleep 1
  return 0
}

# Retry pass: processes may have come back (or something new started) while the items were moving. Everything
# stoppable that holds the given items is stopped again, without a question (the plan was confirmed). A unit with
# Restart=always stays down after `systemctl --user stop` (a manual stop is honored by systemd).
stop_again() {
  local key
  collect_rows "$@"
  [[ -n $ALL_ROWS ]] || return 0
  TMUX_LOADED=0
  group_blockers "$ALL_ROWS"
  local -a again=()
  for key in "${G_KEYS[@]}"; do group_keep_reason "$key" >/dev/null || again+=("$key"); done
  if (( ${#again[@]} )); then stop_watchdogs "${again[@]}"; fi
  for key in "${G_KEYS[@]}"; do
    if group_keep_reason "$key" >/dev/null; then continue; fi
    stop_group "$key" || true
  done
  sleep 1
}

# Start one item again; each outcome goes to RESTART_OK / RESTART_BAD for restart_report.
start_unit() {
  if as_owner systemctl --user start "$1"; then log "запущена снова $1"; RESTART_OK+=("$1")
  else warn "НЕ запустилась $1"; RESTART_BAD+=("systemctl --user start $1"); fi
}
start_container() {
  if docker start "$1" >/dev/null; then log "запущен снова контейнер $1"; RESTART_OK+=("docker контейнер $1")
  else warn "НЕ запустился контейнер $1"; RESTART_BAD+=("docker start $1"); fi
}

# Starts again every unit and container still in the lists, WITHOUT any question (the traps use it; restart_phase
# removes each item from its list before handling it, so nothing is asked twice or started twice). Idempotent: the
# lists are emptied first, so the normal end of `move` and the EXIT/INT/TERM traps can all call it.
restart_stopped_units() {
  local u c g
  local -a todo=() ctodo=() gtodo=()
  (( ${#STOPPED_UNITS[@]} + ${#STOPPED_CONTAINERS[@]} + ${#STOPPED_GUARDS[@]} )) || return 0
  todo=("${STOPPED_UNITS[@]}"); STOPPED_UNITS=()
  ctodo=("${STOPPED_CONTAINERS[@]}"); STOPPED_CONTAINERS=()
  gtodo=("${STOPPED_GUARDS[@]}"); STOPPED_GUARDS=()
  for u in "${todo[@]}"; do start_unit "$u"; done
  for c in "${ctodo[@]}"; do start_container "$c"; done
  for g in "${gtodo[@]}"; do start_unit "$g"; done
}

# Command line that starts a recorded plain process again as the owner, detached from this script.
plain_start_cmd() {
  printf 'runuser -u %q -- env XDG_RUNTIME_DIR=%q setsid -f sh -c %q' \
    "$OWNER_USER" "/run/user/$(id -u "$OWNER_USER")" "$1"
}

# End of `move`: asks separately for every unit, container and plain process ("start it back?"; units and
# containers default yes, plain processes default no; ASSUME_YES=1 yes to all). Declined units/containers go to
# RESTART_BAD, declined plain processes stay in TERM_INNER/TERMINATED: restart_report lists their manual commands.
# An item is taken off its list BEFORE the question, so an interrupt during it only restarts the items not yet handled
# (restart_stopped_units in the trap) and never one twice.
restart_phase() {
  local u c i inner desc k
  local -a keep_inner=() keep_desc=() cur_inner=() cur_desc=()
  if (( ${#STOPPED_UNITS[@]} + ${#STOPPED_CONTAINERS[@]} + ${#STOPPED_GUARDS[@]} + ${#TERM_INNER[@]} == 0 )); then return 0; fi
  log "== Запуск обратно: по каждому пункту отдельный вопрос"
  while (( ${#STOPPED_UNITS[@]} )); do
    u=${STOPPED_UNITS[0]}; STOPPED_UNITS=("${STOPPED_UNITS[@]:1}")
    if ask_yes "Запустить службу $u (systemctl --user start $u)? [Y/n]" y; then start_unit "$u"
    else RESTART_BAD+=("systemctl --user start $u (отказ)"); fi
  done
  while (( ${#STOPPED_CONTAINERS[@]} )); do
    c=${STOPPED_CONTAINERS[0]}; STOPPED_CONTAINERS=("${STOPPED_CONTAINERS[@]:1}")
    if ask_yes "Запустить контейнер $c (docker start $c)? [Y/n]" y; then start_container "$c"
    else RESTART_BAD+=("docker start $c (отказ)"); fi
  done
  # Watchdogs go last and without a question: they only supervise what was just started above.
  while (( ${#STOPPED_GUARDS[@]} )); do
    u=${STOPPED_GUARDS[0]}; STOPPED_GUARDS=("${STOPPED_GUARDS[@]:1}")
    start_unit "$u"
  done
  cur_inner=("${TERM_INNER[@]}"); cur_desc=("${TERM_DESC[@]}")
  TERM_INNER=(); TERM_DESC=()
  for i in "${!cur_inner[@]}"; do
    inner=${cur_inner[$i]} desc=${cur_desc[$i]}
    if ask_yes "Запустить процесс $desc ($(plain_start_cmd "$inner"))? [y/N]"; then
      if runuser -u "$OWNER_USER" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$OWNER_USER")" setsid -f sh -c "$inner"; then
        log "запущен: $inner"; RESTART_OK+=("процесс: $inner")
        for k in "${!TERMINATED[@]}"; do
          if [[ ${TERMINATED[$k]} == "$desc" ]]; then unset 'TERMINATED[k]'; break; fi
        done
        TERMINATED=("${TERMINATED[@]}")
        continue
      fi
      warn "НЕ запустился: $inner"
    fi
    keep_inner+=("$inner"); keep_desc+=("$desc")
  done
  TERM_INNER=("${keep_inner[@]}"); TERM_DESC=("${keep_desc[@]}")
}

# Final summary for the owner; clears what it printed, so the traps can call it again harmlessly.
restart_report() {
  local s line inner
  (( ${#RESTART_OK[@]} + ${#RESTART_BAD[@]} + ${#TERMINATED[@]} )) || return 0
  log "== Что было остановлено скриптом"
  if (( ${#RESTART_OK[@]} )); then
    printf -v s '%s, ' "${RESTART_OK[@]}"
    log "Запущены обратно: ${s%, }"
  fi
  for line in "${RESTART_BAD[@]}"; do log "Не запущены (нужно вручную): $line"; done
  for line in "${TERMINATED[@]}"; do log "Завершены без автозапуска: $line"; done
  for inner in "${TERM_INNER[@]}"; do log "  запустить вручную: $(plain_start_cmd "$inner")"; done
  RESTART_OK=(); RESTART_BAD=(); TERMINATED=(); TERM_INNER=(); TERM_DESC=()
}

# Returns 0 when a live process or docker container uses the path or a git partner of it; sets BUSY_WHY to a short
# reason and BUSY_OUT to the blocking users_of rows, and (unless $2 is "quiet") prints them grouped. Returns 1 when
# free. Processes that only mention the path in their cmdline hold nothing (cwd/fd/mmap are the real hazards): they
# are logged once per run and ignored. A failed scan counts as busy (never assume free).
BUSY_WHY="" BUSY_OUT=""
busy() {
  local path=$1 quiet=${2:-} out dout line pid comm kinds target rpath first=1 blocking="" key reason
  local -a targets=("$path") partners=()
  TMUX_LOADED=0; BUSY_OUT=""
  mapfile -t partners < <(git_partners "$path")
  targets+=("${partners[@]}")
  out=$(users_of "${targets[@]}") || { BUSY_WHY="process scan failed"; warn "process scan failed for $path"; return 0; }
  dout=$(docker_users_of "${targets[@]}")
  if [[ -n $dout ]]; then out+="${out:+$'\n'}$dout"; fi
  [[ -n $out ]] || return 1
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    IFS=$'\t' read -r pid comm kinds target rpath <<<"$line"
    if [[ $kinds == cmdline ]]; then
      if [[ -z ${INFO_SEEN[$pid]:-} ]]; then
        INFO_SEEN[$pid]=1
        log "Инфо: pid $pid ($comm) упоминает путь в командной строке, не блокирует"
      fi
      continue
    fi
    # A holder with ONLY mmaps never blocks: rsync --remove-source-files unlinks the file, the process keeps
    # the old inode and the new bind path serves identical data.
    if [[ $kinds == mmap ]]; then
      if [[ -z ${INFO_SEEN["mmap:$pid"]:-} ]]; then
        INFO_SEEN["mmap:$pid"]=1
        warn "Инфо: pid $pid ($comm) держит только mmap $rpath — не блокирует: будет держать старую копию до перезапуска (место на SSD освободится после)"
      fi
      continue
    fi
    # GUI/browser/terminal with only fd/mmap on regenerable ~/.cache files: losing a cache write is harmless.
    if cache_holder_ignorable "$path" "$comm" "$kinds" "$rpath"; then
      if [[ -z ${INFO_SEEN["cache:$pid"]:-} ]]; then
        INFO_SEEN["cache:$pid"]=1
        log "Инфо: pid $pid ($comm) держит кэш ($rpath) — игнорирую, потеря записи в кэш безвредна"
      fi
      continue
    fi
    blocking+="${blocking:+$'\n'}$line"
    if (( first )); then
      first=0
      if [[ $target == "$path" ]]; then
        BUSY_WHY=$(describe_pid "$pid" "$kinds" "$rpath")
      else
        BUSY_WHY="git partner $target is busy: $(describe_pid "$pid" "$kinds" "$rpath")"
      fi
    fi
  done <<<"$out"
  [[ -n $blocking ]] || return 1
  BUSY_OUT=$blocking
  if [[ $quiet != quiet ]]; then
    group_blockers "$BUSY_OUT"; print_groups
    for key in "${G_KEYS[@]}"; do
      if reason=$(group_keep_reason "$key"); then printf '  НЕ ТРОГАЮ %s: %s\n' "${G_NAME[$key]}" "$reason"; fi
    done
  fi
  return 0
}

top_count() { find "$1" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l; }

FSTAB_BACKED_UP=0
fstab_backup() {
  local ts
  (( FSTAB_BACKED_UP )) && return 0
  ts=$(date +%Y%m%d-%H%M%S)
  cp -a "$FSTAB" "$FSTAB.bak-$ts"
  FSTAB_BACKED_UP=1
  log "fstab backup: $FSTAB.bak-$ts"
}

fstab_has_bind() { awk -v p="$1" '$1 !~ /^#/ && $2 == p && $4 ~ /bind/ {f=1} END{exit !f}' "$FSTAB"; }

fstab_add_bind() {
  local src=$1 path=$2
  fstab_has_bind "$path" && return 0
  fstab_backup
  printf '%s %s %s\n' "$src" "$path" "$BIND_OPTS" >>"$FSTAB"
}

# Removes every active bind line whose mount-target field is exactly $1 (nothing else is touched; the file is
# replaced atomically, keeping owner and mode).
fstab_remove_bind() {
  local path=$1 tmp
  fstab_has_bind "$path" || return 0
  fstab_backup
  tmp=$(mktemp "$FSTAB.XXXXXX") || return 1
  if chmod --reference="$FSTAB" "$tmp" && chown --reference="$FSTAB" "$tmp" \
     && awk -v p="$path" '!($1 !~ /^#/ && NF >= 4 && $2 == p && $4 ~ /bind/)' "$FSTAB" >"$tmp" \
     && mv -f "$tmp" "$FSTAB"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# Active fstab line that already mounts this UUID or this target?
fstab_has_mount() {
  awk -v u="$UUID" -v m="$MNT" \
    '$1 !~ /^#/ && NF >= 2 && ($1 == "UUID=" u || $1 == "/dev/disk/by-uuid/" u || $2 == m) {f=1} END{exit !f}' "$FSTAB"
}

require_mnt() {
  mountpoint -q "$MNT" || die "$MNT is not mounted — run: sudo $0 prep"
  [[ "$(findmnt -rn -o SOURCE -M "$MNT")" == "$(readlink -f "/dev/disk/by-uuid/$UUID")" ]] \
    || die "$MNT is mounted from a different device"
}

# ---------------------------------------------------------------- phases
phase_prep() {
  need_root
  local dev total_mb=0 tier rel mb avail_mb t fstab_new=0 src
  dev=$(readlink -f "/dev/disk/by-uuid/$UUID" 2>/dev/null) || true
  [[ -n $dev && -b $dev ]] || die "no block device with UUID $UUID — is the USB disk plugged in?"
  [[ "$(blkid -s TYPE -o value "$dev")" == ext4 ]] || die "$dev is not ext4"
  log "disk: $dev (UUID $UUID, label $(blkid -s LABEL -o value "$dev"))"

  if fstab_has_mount; then
    log "fstab already has an active line for this UUID or $MNT — leaving it alone"
  else
    fstab_backup
    printf '%s\n' "$MOUNT_LINE" >>"$FSTAB"
    systemctl daemon-reload
    fstab_new=1
    log "fstab line added: $MOUNT_LINE"
  fi

  # Mounted elsewhere, or mounted at $MNT by udisks while the fstab line is new: unmount, remount via fstab.
  while read -r t; do
    [[ -n $t ]] || continue
    if [[ $t == "$MNT" && $fstab_new -eq 0 ]]; then continue; fi
    log "umount $t"
    umount "$t" || die "cannot umount $t (something is using it)"
  done < <(findmnt -rn -o TARGET -S "$dev" || true)

  mkdir -p "$MNT"
  if ! mountpoint -q "$MNT"; then
    log "mount $MNT (via fstab)"
    mount "$MNT" || die "mount $MNT failed (check the fstab line for $MNT)"
  fi
  require_mnt
  mkdir -p "$DEST"
  chown "$OWNER" "$MNT" "$DEST"
  log "chown $OWNER $MNT $DEST done"

  while read -r tier rel mb; do
    src="$HOME_DIR/$rel"
    if [[ -d $src ]] && ! is_bind_mounted "$src"; then total_mb=$((total_mb + mb)); fi
  done < <(enabled_items)
  avail_mb=$(df -m --output=avail "$MNT" | tail -n 1 | tr -d ' ')
  log "enabled items still to move need ~${total_mb} MB, disk has ${avail_mb} MB free"
  (( total_mb < avail_mb )) || die "not enough free space for the enabled items"
  log "prep OK"
}

SKIPPED=()
MOVED_BYTES=0
skip() { log "SKIP $1 — $2"; SKIPPED+=("$1: $2"); }

# Number of short rsync passes that may follow the first (long) one. A multi-hour copy of a live tree (work: 1 file
# written during 107 minutes) always leaves a few files that appeared meanwhile; each follow-up pass is seconds long.
FOLLOWUP_PASSES=3
LEFT_COUNT=0

# Moves the tree $1 into $2 (rsync, same flags every pass), deletes the emptied source directories and, while
# non-directory entries remain in $1, runs up to FOLLOWUP_PASSES more passes. Returns 0 when the source holds only
# (empty) directories, 1 when an rsync failed (anything but 0/24), 3 when files are still left after the last pass
# (count in LEFT_COUNT). Idempotent: every pass merges into $2 and removes only what it transferred.
move_tree() {
  local src=$1 dst=$2 rel=$3 pass=0 rc
  while :; do
    rc=0
    rsync -aHAX --numeric-ids -x --remove-source-files --partial "$src/" "$dst/" || rc=$?
    if [[ $rc -ne 0 && $rc -ne 24 ]]; then
      warn "$rel: rsync exit $rc (pass $((pass + 1)))"
      return 1
    fi
    [[ $rc -eq 24 ]] && warn "$rel: some source files vanished during the move (ok)"
    find "$src" -mindepth 1 -depth -type d -empty -delete || true
    LEFT_COUNT=$(find "$src" -mindepth 1 ! -type d | wc -l)
    (( LEFT_COUNT == 0 )) && return 0
    (( pass >= FOLLOWUP_PASSES )) && return 3
    pass=$((pass + 1))
    log "$rel: $LEFT_COUNT file(s) appeared during the copy — follow-up rsync pass $pass/$FOLLOWUP_PASSES"
  done
}

# Returns 0 moved, 1 failed, 2 skipped.
move_item() {
  local rel=$1 src="$HOME_DIR/$1" dst="$DEST/$1" rc=0 bytes n_src n_dst p kind t rows
  local -a nested=()
  [[ -d $src && ! -L $src ]] || { skip "$rel" "not a directory"; return 2; }
  if is_bind_mounted "$src"; then skip "$rel" "already a bind mount"; return 2; fi
  rows=$(nested_mounts "$src") || { warn "FAIL $rel: cannot read the mount table"; return 1; }
  while IFS=$'\t' read -r kind t; do
    [[ -n $kind ]] || continue
    if [[ $kind == foreign ]]; then
      skip "$rel" "mount below it that is not a bind of $DEST: $t — unmount it or take it out first"
      return 2
    fi
    nested+=("$t")
  done <<<"$rows"
  if busy "$src"; then
    # The up-front stop (upfront_stop) already cleared what it could; whatever still holds the path is explained
    # per group and the item is skipped (retried once at the end of the run).
    RETRY_ITEMS+=("$rel")
    skip "$rel" "busy: $BUSY_WHY"
    return 2
  fi

  if (( ${#nested[@]} )); then
    log "$rel: unmounting ${#nested[@]} nested bind mount(s), their data is already in $dst:"
    printf '    %s\n' "${nested[@]}"
    if ! nested_umount "${nested[@]}"; then
      warn "FAIL $rel: could not unmount all nested binds — re-mounting them, nothing was moved"
      nested_remount || true
      return 1
    fi
    for t in "${nested[@]}"; do
      if [[ ! -d $t || -n $(find "$t" -mindepth 1 -print -quit) ]]; then
        warn "FAIL $rel: $t is not an empty directory underneath its bind mount — re-mounting, nothing was moved"
        nested_remount || true
        return 1
      fi
    done
  fi

  bytes=$(du -sxb "$src" 2>/dev/null | cut -f1) || bytes=0
  mkdir -p "$dst"
  p=$(dirname "$dst")
  while [[ $p != "$MNT" && $p != / ]]; do chown "$OWNER" "$p"; p=$(dirname "$p"); done

  # Last chance: nothing may have started inside since the first check. The path is incomplete from here on.
  if busy "$src"; then
    nested_remount || true
    RETRY_ITEMS+=("$rel")
    skip "$rel" "busy (re-check): $BUSY_WHY"
    return 2
  fi
  log "MOVE $rel (~$((bytes / 1048576)) MB)"
  rc=0
  move_tree "$src" "$dst" "$rel" || rc=$?
  if (( rc == 1 )); then
    warn "FAIL $rel: rsync failed — not binding; the rest stays in $src, re-run 'move' to continue"
    nested_remount || true
    return 1
  elif (( rc != 0 )); then
    warn "FAIL $rel: $LEFT_COUNT non-directory entr(ies) still left in $src after $FOLLOWUP_PASSES follow-up rsync pass(es) — not binding; re-run 'move'"
    nested_remount || true
    return 1
  fi

  if ! mount --bind "$dst" "$src"; then
    warn "FAIL $rel: bind mount failed — data is complete in $dst, $src is empty; re-run 'move' to retry the bind"
    nested_remount || true
    return 1
  fi
  n_src=$(top_count "$src"); n_dst=$(top_count "$dst")
  if [[ $n_src -ne $n_dst ]]; then
    warn "FAIL $rel: entry count mismatch (src=$n_src dst=$n_dst) — unmounting; re-run 'move'"
    umount "$src" || warn "umount $src failed"
    nested_remount || true
    return 1
  fi
  for t in "${nested[@]}"; do
    if [[ ! -d $t ]]; then
      warn "FAIL $rel: former nested mountpoint $t is missing in the merged tree — unmounting; re-run 'move'"
      umount "$src" || warn "umount $src failed"
      nested_remount || true
      return 1
    fi
  done
  fstab_add_bind "$dst" "$src"
  # The whole tree is bound now; the nested binds are gone for good, so their fstab lines are redundant.
  for t in "${nested[@]}"; do
    if fstab_remove_bind "$t"; then
      log "fstab: removed the nested bind line of $t"
    else
      warn "could not remove the fstab bind line of $t (harmless, redundant) — remove it by hand"
    fi
  done
  NESTED_DOWN=()
  systemctl daemon-reload
  MOVED_BYTES=$((MOVED_BYTES + bytes))
  log "DONE $rel -> bind mount of $dst ($n_src top-level entries)"
}

# Runs one item and updates the counters.
ITEMS_MOVED=0 ITEMS_FAILED=0
run_item() {
  local rc=0
  move_item "$1" || rc=$?
  case $rc in 0) ITEMS_MOVED=$((ITEMS_MOVED + 1)) ;; 2) ;; *) ITEMS_FAILED=$((ITEMS_FAILED + 1)) ;; esac
}

phase_move() {
  need_root; require_mnt
  command -v rsync >/dev/null || die "rsync not found"
  command -v python3 >/dev/null || die "python3 not found (needed for the busy check)"
  trap 'nested_remount || true; restart_stopped_units || true; restart_report || true' EXIT
  trap 'nested_remount || true; restart_stopped_units || true; restart_report || true; exit 130' INT TERM
  local tier rel mb s failed
  local -a rels=() retry=() keep=()
  while read -r tier rel mb; do rels+=("$rel"); done < <(enabled_items)
  if ! upfront_stop "${rels[@]}"; then
    log "Отказ (или нет возможности спросить: запустите из терминала или с ASSUME_YES=1). Ничего не остановлено, ничего не перенесено."
    return 0
  fi
  for rel in "${rels[@]}"; do run_item "$rel"; done
  if (( ${#RETRY_ITEMS[@]} )); then
    retry=("${RETRY_ITEMS[@]}"); RETRY_ITEMS=()
    log "== Повторный проход для занятых: ${retry[*]}"
    if (( STOP_APPROVED )); then stop_again "${retry[@]}"; fi
    for rel in "${retry[@]}"; do
      keep=()
      for s in "${SKIPPED[@]}"; do [[ $s == "$rel: busy"* ]] || keep+=("$s"); done
      SKIPPED=("${keep[@]}")
      run_item "$rel"
    done
  fi
  failed=$ITEMS_FAILED
  log "move done: moved=$ITEMS_MOVED skipped=${#SKIPPED[@]} failed=$failed, $(awk -v b="$MOVED_BYTES" 'BEGIN{printf "%.1f", b/1073741824}') GB moved"
  for s in "${SKIPPED[@]}"; do log "  skipped: $s"; done
  restart_phase
  restart_report
  (( failed == 0 )) || warn "failed items stay partly moved; fix the cause and re-run 'move' (it continues where it stopped)"
  cat <<EOF

REBOOT TEST (do this now):
  sudo systemctl daemon-reload && mount -a
  after the next reboot check that $MNT is mounted by fstab (findmnt $MNT) and that the moved paths show their data
  (sudo $0 status).
EOF
}

phase_status() {
  log "== df"; df -h / "$MNT" 2>&1 || true
  log "== fstab bind lines (target source mounted entries)"
  local dst path rest st n
  while read -r dst path rest; do
    if is_bind_mounted "$path"; then st="mounted"; else st="NOT mounted"; fi
    n=$(top_count "$path")
    printf '%-62s <- %-62s %-11s %s entries\n' "$path" "$dst" "$st" "$n"
  done < <(awk '$1 !~ /^#/ && NF >= 4 && $4 ~ /bind/ {print $1, $2}' "$FSTAB")
}

phase_cleanup() {
  log "Commands below are only PRINTED. Review, then run them yourself."
  cat <<'EOF'
# --- regenerable caches (run as ultra, after you checked nothing is mid-install):
uv cache clean                       # ~10.6 GB  (~/.cache/uv)
bun pm cache rm                      # ~12 GB    (~/.bun/install/cache)
npm cache clean --force              # ~1.5 GB   (~/.npm)
rm -rf ~/.cache/trivy ~/.cache/ms-playwright ~/.cache/puppeteer ~/.cache/Homebrew   # ~3.7 GB, re-downloaded on demand
# --- root-owned (the 23 GB df/du gap lives here):
docker system df                     # images 22 GB (6.8 GB reclaimable), containers, volumes, build cache
sudo docker image prune -a           # only unused images; review `docker images` first
sudo docker builder prune
snap list --all | awk '/disabled/{print "sudo snap remove " $1 " --revision=" $3}'   # old snap revisions
sudo journalctl --vacuum-size=500M
EOF
}

main() {
  if [[ ${1:-} != "" ]]; then
    : >>"$LOG" 2>/dev/null || true
    chown "$OWNER" "$LOG" 2>/dev/null || true
    exec > >(tee -a "$LOG") 2>&1
  fi
  case "${1:-}" in
    prep)    phase_prep ;;
    move)    phase_move ;;
    status)  phase_status ;;
    cleanup) phase_cleanup ;;
    *) cat <<EOF; exit 2 ;;
usage: sudo $0 {prep|move|status|cleanup}
  prep     fstab mount line for the backup disk, mount it, check free space
  move     per enabled item: busy check -> rsync to the disk -> bind mount at the original path -> fstab line.
           Whole-tree items (work, xp) take every worktree below them along and the path itself
           becomes the bind mount, so new dirs/worktrees created later land on the disk. A whole tree is skipped
           while ANY process has a cwd, open fd, mapping or cmdline anywhere inside it. Bind mounts made earlier
           below it are unmounted (plain umount), merged and their fstab lines removed; re-mounted on failure.
  status   df, and every fstab bind line with mounted state and entry count
  cleanup  only prints deletion suggestions
           All blockers of all enabled items are scanned ONCE and shown as a plan (stopped / left alone); one
           question (ASSUME_YES=1 answers it) stops them all: systemd --user units and docker containers
           (started again at the end), a shell that only sits in the tree gets 'cd ~', other processes get SIGTERM
           (SIGKILL after 10 s). After the move the restart step asks SEPARATELY for each stopped unit, container
           and plain process: "Запустить <what> (<exact command>)? [Y/n]" (plain processes: [y/N], with the recorded
           cmdline + cwd). n leaves the item stopped and lists its manual command in the final report.
env: WITH_OPT=1 WITH_STOP=1 ONLY=regex ASSUME_YES=1 (use sudo -E)
EOF
  esac
}

# Not run when sourced (self-tests source the helpers).
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi

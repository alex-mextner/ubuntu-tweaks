#!/usr/bin/env bash
# memguard module installer: swap-drain (gated, abortable swap-to-RAM drain) with its 10-min
# swap-drain.timer, the memguard CLI, /etc/memguard/memguard.toml and the polkit rule for
# `memguard swap-drain`. Replaces the hand-installed swap-drain and its hourly timer from
# root-fixes/mem-guard.sh (same unit names, so they are overwritten in place).
#
#   sudo modules/memguard/install.sh        show the diff of every change, then apply it
#   modules/memguard/install.sh --dry-run   only show it (no root needed); exit 3 = changes pending
#   modules/memguard/install.sh --check     same comparison for ubuntu-tweak (quiet footer):
#                                           exit 0 = installed files match the repo, 3 = drift
#
# MEMGUARD_ROOT=DIR prefixes every destination (tests; skips the systemd timer probe and the
# user lookup). With it the install is *staged*: it needs no root, takes the user from
# $MEMGUARD_USER (default: the current user), writes the files and the stamp
# under DIR and never touches systemd or runuser. Idempotent: a second run prints
# "nothing to change". Spec: docs/specs/memguard.md.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dry=0
check=0
root=${MEMGUARD_ROOT:-}
case "${1:-}" in
  --dry-run) dry=1 ;;
  --check) dry=1; check=1 ;;
  "") ;;
  -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--dry-run|--check]" >&2; exit 2 ;;
esac
if [ "$dry" = 0 ] && [ -z "$root" ] && [ "$(id -u)" != 0 ]; then
  echo "run with sudo (or --dry-run to only see the changes)" >&2
  exit 1
fi

# Syntax-check the python and the config before touching anything (no __pycache__ in the repo).
python3 -B - "$here" <<'EOF'
import ast, sys
here = sys.argv[1]
for name in ("memguard_swap.py", "swap-drain", "memguard"):
    with open(f"{here}/{name}") as f:
        ast.parse(f.read(), name)
sys.path.insert(0, here)
import memguard_swap
memguard_swap.load_config(f"{here}/memguard.toml")
EOF
cfg_user=$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import memguard_swap as m; print(m.load_config(sys.argv[1] + "/memguard.toml")["user"])' "$here")

# The desktop user: staged mode takes $MEMGUARD_USER or the current user; a real install takes
# $SUDO_USER, else the $PKEXEC_UID account, else `user` of memguard.toml; a non-root dry run or
# --check (run by the desktop user itself) falls back to the current user.
user=
if [ -n "$root" ]; then
  user=${MEMGUARD_USER:-$(id -un)}
else
  [ "${SUDO_USER:-}" != root ] && user=${SUDO_USER:-}
  if [ -z "$user" ] && [ -n "${PKEXEC_UID:-}" ]; then
    user=$(getent passwd "$PKEXEC_UID" | cut -d: -f1 || true)
  fi
  [ -n "$user" ] || user=$cfg_user
  if [ -z "$user" ] && [ "$(id -u)" != 0 ]; then user=$(id -un); fi
fi
if [ -z "$user" ]; then
  echo "memguard.toml: user is not set and no SUDO_USER/PKEXEC_UID" >&2
  exit 1
fi
if ! [[ $user =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]]; then
  echo "memguard: refusing user name '$user'" >&2
  exit 1
fi
[ -n "$root" ] || getent passwd "$user" >/dev/null || { echo "memguard: user '$user' does not exist" >&2; exit 1; }
[ "$check" = 1 ] || echo "memguard: desktop user: $user"

# memguard.toml with `user = "<resolved user>"` (the repo copy leaves it empty = detect).
render_toml() {
  if grep -q '^user = ' "$here/memguard.toml"; then
    sed "s|^user = \"[^\"]*\"|user = \"$user\"|" "$here/memguard.toml"
  else
    sed "s|^\[swap\]\$|&\nuser = \"$user\"|" "$here/memguard.toml"
  fi
}

# source : target : mode
files=(
  "memguard_swap.py:/usr/local/lib/memguard/memguard_swap.py:644"
  "swap-drain:/usr/local/sbin/swap-drain:755"
  "memguard:/usr/local/bin/memguard:755"
  "memguard.toml:/etc/memguard/memguard.toml:644"
  "swap-drain.service:/etc/systemd/system/swap-drain.service:644"
  "swap-drain.timer:/etc/systemd/system/swap-drain.timer:644"
  "50-memguard-swap-drain.rules:/etc/polkit-1/rules.d/50-memguard-swap-drain.rules:644"
)

render=$(mktemp -d)
trap 'rm -rf "$render"' EXIT
changed=()
pending=0
stamp_lines=()
stamp="$root/etc/memguard/installed.sha256"
for entry in "${files[@]}"; do
  IFS=: read -r src dst mode <<<"$entry"
  dst="$root$dst"
  out="$render/$src"
  case "$src" in
    *.rules) sed "s/@USER@/$user/g" "$here/$src" >"$out" ;;
    # the installed copy carries the resolved user: swap-drain reads it at run time
    memguard.toml) render_toml >"$out" ;;
    *) cp "$here/$src" "$out" ;;
  esac
  stamp_lines+=("$mode $(sha256sum <"$out" | cut -d' ' -f1) $dst")
  parent=$(dirname "$dst")
  # Only the polkit rules directory is root-only by design; a stamp vouches for files inside it
  # and nothing else, so any other unreadable file is reported as drift.
  if [ -d "$parent" ] && [ ! -x "$parent" ] && [ "$parent" = "$root/etc/polkit-1/rules.d" ] &&
    grep -qxF -- "${stamp_lines[-1]}" "$stamp" 2>/dev/null; then
    continue
  fi
  if { [ -d "$parent" ] && [ ! -x "$parent" ]; } || { [ -e "$dst" ] && [ ! -r "$dst" ]; }; then
    echo "? $dst: not readable without sudo and the install stamp does not vouch for it"
    pending=1
    continue
  fi
  if [ -e "$dst" ] && cmp -s "$dst" "$out" && [ "$(stat -c %a "$dst")" = "$mode" ]; then
    continue
  fi
  if [ -e "$dst" ]; then
    echo "~ $dst"
    diff -u --label "$dst (installed)" --label "$dst (new)" "$dst" "$out" || true
    [ "$(stat -c %a "$dst")" = "$mode" ] || echo "  mode $(stat -c %a "$dst") -> $mode"
  else
    # a new file's diff is the whole file: name it, the content is $here/$src
    echo "+ $dst (new, mode $mode, $(wc -l <"$out") lines from modules/memguard/$src)"
  fi
  changed+=("$src:$dst:$mode")
done
timer_on=enabled
[ -n "$root" ] || timer_on=$(systemctl is-enabled swap-drain.timer 2>/dev/null || true)
enable_timer=0
if [ "$timer_on" != enabled ]; then
  enable_timer=1
  echo "* swap-drain.timer (${timer_on:-not-found}): systemctl enable --now"
fi

if [ "${#changed[@]}" = 0 ] && [ "$enable_timer" = 0 ] && [ "$pending" = 0 ]; then
  echo "memguard: nothing to change"
  [ "$dry" = 1 ] && exit 0
elif [ "$dry" = 1 ]; then
  [ "$check" = 1 ] || echo "memguard: dry run, nothing changed; apply with: sudo $here/install.sh"
  exit 3
fi

reload=0
restart_timer=0
for entry in "${changed[@]}"; do
  IFS=: read -r src dst mode <<<"$entry"
  install -D -m "$mode" "$render/$src" "$dst"
  echo "installed $dst"
  case "$dst" in *.service|*.timer) reload=1 ;; esac
  case "$dst" in *.timer) restart_timer=1 ;; esac
done
# The polkit rules directory is unreadable for the desktop user, so `--check` cannot compare that
# file; it trusts this stamp (mode, sha256, path per file) written by the last root install.
printf '%s\n' "${stamp_lines[@]}" >"$render/stamp"
if ! cmp -s "$render/stamp" "$stamp" 2>/dev/null; then
  install -D -m 644 "$render/stamp" "$stamp"
  echo "installed $stamp"
fi
if [ -n "$root" ]; then
  echo "memguard: staged install into $root; systemd and the user lookup are real-root only"
  exit 0
fi
[ "$reload" = 1 ] && systemctl daemon-reload
systemctl enable --now swap-drain.timer >/dev/null 2>&1
# a rewritten timer (e.g. the old hourly one) keeps its old schedule until restarted
[ "$restart_timer" = 1 ] && systemctl restart swap-drain.timer

echo "=== memguard status ==="
echo "swap-drain.timer: $(systemctl is-enabled swap-drain.timer) $(systemctl is-active swap-drain.timer)," \
     "next $(systemctl show -p NextElapseUSecRealtime --value swap-drain.timer)"
echo "decision now (as $user, executes nothing):"
runuser -u "$user" -- /usr/local/bin/memguard swap-drain --dry-run || true
echo "verify: journalctl -t swap-drain --since -20min"

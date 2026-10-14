#!/usr/bin/env bash
# memguard module installer: swap-drain (gated, abortable, event-triggered swap-to-RAM drain),
# mem-sentinel (with drain awareness, the drain trigger and a systemd watchdog), the memguard
# CLI, /etc/memguard/memguard.toml and the polkit rule for `memguard swap-drain`.
# Replaces the hand-installed swap-drain from root-fixes/mem-guard.sh: the hourly
# swap-drain.timer is disabled and removed (the sentinel triggers drains now).
#
#   sudo modules/memguard/install.sh        show the diff of every change, then apply it
#   modules/memguard/install.sh --dry-run   only show it (no root needed); exit 3 = changes pending
#
# Idempotent: a second run prints "nothing to change". Spec: docs/specs/memguard.md.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dry=0
case "${1:-}" in
  --dry-run) dry=1 ;;
  "") ;;
  -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
if [ "$dry" = 0 ] && [ "$(id -u)" != 0 ]; then
  echo "run with sudo (or --dry-run to only see the changes)" >&2
  exit 1
fi

# Syntax-check the python and the config before touching anything (no __pycache__ in the repo).
python3 -B - "$here" <<'EOF'
import ast, sys
here = sys.argv[1]
for name in ("memguard_swap.py", "swap-drain", "memguard", "mem-sentinel"):
    with open(f"{here}/{name}") as f:
        ast.parse(f.read(), name)
sys.path.insert(0, here)
import memguard_swap
memguard_swap.load_config(f"{here}/memguard.toml")
EOF
user=$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import memguard_swap as m; print(m.load_config(sys.argv[1] + "/memguard.toml")["user"])' "$here")
getent passwd "$user" >/dev/null || { echo "memguard.toml: user '$user' does not exist" >&2; exit 1; }

# source : target : mode
files=(
  "memguard_swap.py:/usr/local/lib/memguard/memguard_swap.py:644"
  "swap-drain:/usr/local/sbin/swap-drain:755"
  "memguard:/usr/local/bin/memguard:755"
  "mem-sentinel:/usr/local/sbin/mem-sentinel:755"
  "memguard.toml:/etc/memguard/memguard.toml:644"
  "swap-drain.service:/etc/systemd/system/swap-drain.service:644"
  "mem-sentinel.service:/etc/systemd/system/mem-sentinel.service:644"
  "50-memguard-swap-drain.rules:/etc/polkit-1/rules.d/50-memguard-swap-drain.rules:644"
)
old_timer=/etc/systemd/system/swap-drain.timer

render=$(mktemp -d)
trap 'rm -rf "$render"' EXIT
changed=()
pending=0
for entry in "${files[@]}"; do
  IFS=: read -r src dst mode <<<"$entry"
  out="$render/$src"
  case "$src" in
    *.rules) sed "s/@USER@/$user/g" "$here/$src" >"$out" ;;
    *) cp "$here/$src" "$out" ;;
  esac
  if [ -e "$dst" ] && [ ! -r "$dst" ]; then
    echo "? $dst: not readable without sudo (compared on the real run)"
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
timer_state=$(systemctl is-enabled swap-drain.timer 2>/dev/null || true)
remove_timer=0
if [ -e "$old_timer" ] || { [ -n "$timer_state" ] && [ "$timer_state" != "not-found" ]; }; then
  remove_timer=1
  echo "- $old_timer (hourly drain, $timer_state): systemctl disable --now, then removed"
fi

if [ "${#changed[@]}" = 0 ] && [ "$remove_timer" = 0 ] && [ "$pending" = 0 ]; then
  echo "memguard: nothing to change"
  [ "$dry" = 1 ] && exit 0
elif [ "$dry" = 1 ]; then
  echo "memguard: dry run, nothing changed; apply with: sudo $here/install.sh"
  exit 3
fi

reload=0
restart_sentinel=0
for entry in "${changed[@]}"; do
  IFS=: read -r src dst mode <<<"$entry"
  install -D -m "$mode" "$render/$src" "$dst"
  echo "installed $dst"
  case "$dst" in *.service) reload=1 ;; esac
  case "$src" in mem-sentinel*|memguard_swap.py|memguard.toml) restart_sentinel=1 ;; esac
done
if [ "$remove_timer" = 1 ]; then
  systemctl disable --now swap-drain.timer 2>/dev/null || true
  rm -f "$old_timer"
  reload=1
  echo "removed $old_timer"
fi
[ "$reload" = 1 ] && systemctl daemon-reload
mkdir -p /var/log/hang-probe
systemctl enable mem-sentinel.service >/dev/null 2>&1
if [ "$restart_sentinel" = 1 ] || ! systemctl is-active -q mem-sentinel.service; then
  systemctl restart mem-sentinel.service
  echo "restarted mem-sentinel.service"
fi

echo "=== memguard status ==="
echo "mem-sentinel: $(systemctl is-active mem-sentinel) $(systemctl is-enabled mem-sentinel)," \
     "Type=$(systemctl show -p Type --value mem-sentinel) watchdog=$(systemctl show -p WatchdogUSec --value mem-sentinel)"
echo "swap-drain.timer: $(systemctl is-enabled swap-drain.timer 2>/dev/null || echo gone)"
echo "decision now (as $user, executes nothing):"
runuser -u "$user" -- /usr/local/bin/memguard swap-drain --dry-run || true
echo "verify: journalctl -t mem-sentinel -t swap-drain --since -10min | grep -E 'start uid|swap-drain'"

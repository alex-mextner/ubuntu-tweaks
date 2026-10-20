#!/usr/bin/env bash
# apple-hid-battery installer: builds the livepatch module for every installed kernel that has
# headers and no module yet, installs it under /lib/modules/<k>/updates/, makes it load at
# boot (/etc/modules-load.d) and loads it into the running kernel.
#
#   sudo modules/apple-hid-battery/install.sh   build (when missing) + install + load
#   modules/apple-hid-battery/install.sh --check
#       read-only, no root: exit 0 = the running kernel has the module installed, set to load
#       at boot and enabled (or needs none: upstream fix present), 3 = drift, reasons on stdout
#
# Kernels >= 7.3 carry the upstream fix (d07644524b65): there the installer removes the module
# and the modules-load entry instead. Unsigned module: needs Secure Boot disabled.
# Rollback: echo 0 > /sys/kernel/livepatch/livepatch_hid_battery_offset/enabled; rmmod it.
# Test hooks: APPLE_HID_ROOT=DIR prefixes /lib/modules, /etc and /sys; APPLE_HID_KVER=VERSION
# replaces `uname -r`. Spec: docs/specs/memguard.md (apple-hid-battery).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=${APPLE_HID_ROOT:-}
kver=${APPLE_HID_KVER:-$(uname -r)}
name=livepatch_hid_battery_offset
conf="$root/etc/modules-load.d/livepatch-hid-battery-offset.conf"
enabled_file="$root/sys/kernel/livepatch/$name/enabled"
fixed_in=7.3

case "${1:-}" in
  --check) check=1 ;;
  "") check=0 ;;
  -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

ko_of() { echo "$root/lib/modules/$1/updates/$name.ko"; }

# True when kernel $1 (e.g. 7.0.0-38-generic) already has the upstream fix.
has_upstream_fix() {
  local base=${1%%-*}
  [ "$(printf '%s\n%s\n' "$fixed_in" "$base" | sort -V | sed -n 1p)" = "$fixed_in" ]
}

if [ "$check" = 1 ]; then
  problems=()
  if has_upstream_fix "$kver"; then
    [ ! -e "$(ko_of "$kver")" ] || problems+=("$(ko_of "$kver") should be removed: kernel $kver has the upstream fix")
  else
    [ -e "$(ko_of "$kver")" ] || problems+=("$(ko_of "$kver") is missing")
    [ "$(cat "$conf" 2>/dev/null || true)" = "$name" ] || problems+=("$conf is missing or does not name $name")
    [ "$(cat "$enabled_file" 2>/dev/null || true)" = 1 ] || problems+=("livepatch $name is not loaded and enabled")
  fi
  if [ "${#problems[@]}" = 0 ]; then
    echo "apple-hid-battery: ok for $kver"
    exit 0
  fi
  printf 'apple-hid-battery: %s\n' "${problems[@]}"
  exit 3
fi

if [ -z "$root" ] && [ "$(id -u)" != 0 ]; then
  echo "run with sudo (or --check to only see the state)" >&2
  exit 1
fi

changed=0
running_without_headers=0
work=""
trap '[ -z "$work" ] || rm -rf "$work"' EXIT
for dir in "$root"/lib/modules/*/; do
  [ -d "$dir" ] || continue
  k=$(basename "$dir")
  ko=$(ko_of "$k")
  if has_upstream_fix "$k"; then
    if [ -e "$ko" ]; then
      rm -f "$ko"
      echo "removed $ko (upstream fix present in $k)"
      depmod -a "$k"
      changed=1
    fi
    continue
  fi
  [ ! -e "$ko" ] || continue
  if [ ! -d "$dir/build" ]; then
    echo "skipped $k: no kernel headers (apt install linux-headers-$k, then rerun)"
    [ "$k" != "$kver" ] || running_without_headers=1
    continue
  fi
  work=$(mktemp -d)
  cp "$here/livepatch_hid_battery_offset.c" "$here/Makefile" "$work/"
  make -C "$work" KVER="$k" KDIR="$dir/build" all >&2
  strip --strip-debug "$work/$name.ko"
  install -D -m 644 "$work/$name.ko" "$ko"
  rm -rf "$work"
  work=""
  depmod -a "$k"
  echo "installed $ko"
  changed=1
done

# The modules-load entry is global: keep it while any kernel still carries the module.
any_ko=0
for ko in "$root"/lib/modules/*/updates/"$name.ko"; do
  [ ! -e "$ko" ] || any_ko=1
done
if [ "$any_ko" = 0 ]; then
  if [ -e "$conf" ]; then
    rm -f "$conf"
    echo "removed $conf"
    changed=1
  fi
elif [ "$(cat "$conf" 2>/dev/null || true)" != "$name" ]; then
  # a module loaded by hand or dropped in by a pod may be missing from the running kernel's
  # index; index first, so a failed depmod leaves the conf unwritten and the next run retries
  has_upstream_fix "$kver" || depmod -a "$kver"
  mkdir -p "$(dirname "$conf")"
  printf '%s\n' "$name" >"$conf"
  echo "installed $conf"
  changed=1
fi

if [ "$running_without_headers" = 1 ]; then
  echo "the running kernel $kver has no headers: no module was built for it" >&2
  exit 1
fi
if ! has_upstream_fix "$kver" && [ "$(cat "$enabled_file" 2>/dev/null || true)" != 1 ]; then
  # a loaded but disabled livepatch cannot be re-enabled by modprobe (its sysfs directory is
  # already gone, but /sys/module/<name> stays): unload it first
  if [ -d "$root/sys/module/$name" ]; then
    rmmod "$name"
  fi
  depmod -a "$kver"
  modprobe "$name"
  if [ "$(cat "$enabled_file" 2>/dev/null || true)" != 1 ]; then
    echo "$name did not come up enabled in $kver" >&2
    exit 1
  fi
  echo "loaded $name into $kver"
  changed=1
fi
[ "$changed" = 1 ] || echo "apple-hid-battery: nothing to change"

#!/usr/bin/env bash
# Usage: sudo /home/ultra/relocate-chihuahua.sh
# Moves chihuahua-hunyuan and chihuahua-realistic-v2 from the backups disk's home-ultra/projects/ to
# home-ultra/xp/garage-band/projects/, drops their /home/ultra/projects/<name> bind mounts + fstab lines and
# removes the then-empty projects dirs. Idempotent. Creates NO bind mount at xp/garage-band/projects:
# `move-to-backups.sh move` binds the whole xp tree and merges that path from the backups disk.
set -euo pipefail

# shellcheck disable=SC1091  # the deployed copy of move-to-backups.sh (README: "Where the live copies are"); CI has no such file
source /home/ultra/move-to-backups.sh   # helpers only (busy, users_of, print_groups); main is guarded; die/log come from it

M=$MNT
FSTAB=/etc/fstab
P=/home/ultra/projects
SRC=$M/home-ultra/projects
DSTROOT=$M/home-ultra/xp/garage-band/projects
NAMES=(chihuahua-hunyuan chihuahua-realistic-v2)
CHECK_ONLY=0
[[ ${1:-} == --check-only ]] && CHECK_ONLY=1

[[ $CHECK_ONLY == 1 || $EUID -eq 0 ]] || die "must run as root"
mountpoint -q "$M" || die "$M is not mounted"

# Real per-path check (cwd/exe/fd/mmap inside the path only), not a whole-filesystem mount scan.
# Runs before any umount/mv/fstab change, so a refusal changes nothing.
check_free() {
  local n d blocked=0
  command -v python3 >/dev/null || die "python3 not found (needed for the busy check)"
  for n in "${NAMES[@]}"; do
    for d in "$P/$n" "$SRC/$n"; do
      [[ -d $d ]] || continue
      if busy "$d"; then blocked=1; fi
    done
  done
  if (( blocked )); then
    echo "ОШИБКА: путь занят процессами (список выше). Остановите их или выйдите (cd) из этих каталогов и запустите снова; ничего не изменено." >&2
    return 1
  fi
}

check_free || exit 1
[[ $CHECK_ONLY == 1 ]] && { echo "OK: проверка пройдена, пути свободны"; exit 0; }

for n in "${NAMES[@]}"; do
  if mountpoint -q "$P/$n"; then
    log "umount $P/$n"
    umount "$P/$n"
  fi
done

mkdir -p "$DSTROOT"
for d in "$M/home-ultra/xp" "$M/home-ultra/xp/garage-band" "$DSTROOT"; do
  chown ultra:ultra "$d"
done

for n in "${NAMES[@]}"; do
  if [[ -e $SRC/$n && ! -e $DSTROOT/$n ]]; then
    log "mv $SRC/$n -> $DSTROOT/$n"
    mv "$SRC/$n" "$DSTROOT/$n"
  elif [[ -e $SRC/$n ]]; then
    die "both $SRC/$n and $DSTROOT/$n exist; resolve by hand"
  else
    log "$SRC/$n already gone"
  fi
done

ts=$(date +%Y%m%d-%H%M%S)
backed=0
for n in "${NAMES[@]}"; do
  t=$P/$n
  if awk -v p="$t" '$1 !~ /^#/ && NF >= 4 && $2 == p && $4 ~ /bind/ {f=1} END{exit !f}' "$FSTAB"; then
    if (( ! backed )); then
      cp -a "$FSTAB" "$FSTAB.bak-$ts"; backed=1
      log "fstab backup: $FSTAB.bak-$ts"
    fi
    tmp=$(mktemp "$FSTAB.XXXXXX")
    if chmod --reference="$FSTAB" "$tmp" && chown --reference="$FSTAB" "$tmp" \
       && awk -v p="$t" '!($1 !~ /^#/ && NF >= 4 && $2 == p && $4 ~ /bind/)' "$FSTAB" >"$tmp" \
       && mv -f "$tmp" "$FSTAB"; then
      log "fstab: removed bind line of $t"
    else
      rm -f "$tmp"; die "could not edit $FSTAB"
    fi
  else
    log "fstab: no bind line for $t"
  fi
done

for d in "$P/${NAMES[0]}" "$P/${NAMES[1]}" "$P" "$SRC"; do
  if [[ -d $d ]]; then
    rmdir "$d" && log "rmdir $d" || echo "WARN: $d not removed (not empty?)" >&2
  fi
done

systemctl daemon-reload

echo "--- summary"
for n in "${NAMES[@]}"; do
  [[ -d $DSTROOT/$n ]] && echo "OK  $DSTROOT/$n" || echo "MISSING $DSTROOT/$n"
done
echo "Next: run move-to-backups.sh move (binds whole xp and merges garage-band/projects)."

#!/usr/bin/env bash
# End-to-end check of the CLI against a scratch dconf: `apply --commit` on an empty profile,
# then `status` exits 0, then a second `apply --commit` reports `0 changed`.
#
# The scratch environment (temporary HOME, XDG_* incl. XDG_RUNTIME_DIR, DCONF_PROFILE) is exported
# BEFORE `dbus-run-session` starts: the dconf service is activated by the bus daemon and inherits
# the daemon's environment, so setting these only inside the session would let it write the
# live user's dconf database (safety invariant 6). The inner run refuses to continue unless it is
# inside that scratch environment. Limited to the english-in-overview module: the others need
# network (hyperdock submodule) or root (memguard). Skips (exit 0, loudly) when the
# org.gnome.shell schema is not installed.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${UBUNTU_TWEAKS_SCRATCH:-}" != "1" ]]; then
    schemas="$(/usr/bin/gsettings list-schemas 2>/dev/null || true)"
    if ! grep -qx org.gnome.shell <<<"$schemas"; then
        echo "integration: SKIPPED, schema org.gnome.shell is not installed (package gnome-shell-common)"
        exit 0
    fi
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/home" "$tmp/profile" "$tmp/runtime"
    chmod 700 "$tmp/runtime"
    printf 'user-db:user\n' >"$tmp/dconf-profile"
    env -u UBUNTU_TWEAK_PROFILE -u DBUS_SESSION_BUS_ADDRESS \
        UBUNTU_TWEAKS_SCRATCH=1 \
        UBUNTU_TWEAKS_SCRATCH_ROOT="$tmp" \
        HOME="$tmp/home" \
        XDG_CONFIG_HOME="$tmp/home/.config" \
        XDG_DATA_HOME="$tmp/home/.local/share" \
        XDG_STATE_HOME="$tmp/home/.local/state" \
        XDG_CACHE_HOME="$tmp/home/.cache" \
        XDG_RUNTIME_DIR="$tmp/runtime" \
        DCONF_PROFILE="$tmp/dconf-profile" \
        GSETTINGS_BACKEND=dconf \
        dbus-run-session -- "${BASH_SOURCE[0]}" "$@"
    exit $?
fi

# Inside the scratch session. Refuse to touch anything that is not under the scratch root.
root="${UBUNTU_TWEAKS_SCRATCH_ROOT:?}"
[[ "$HOME" == "$root/home" && "$DCONF_PROFILE" == "$root/dconf-profile" && "$XDG_RUNTIME_DIR" == "$root/runtime" ]] ||
    { echo "integration: refusing to run outside the scratch environment" >&2; exit 2; }

python="${PYTHON:-/usr/bin/python3}"
flags=(--profile "$root/profile" --machine integration --only english-in-overview)
tweak() { "$python" -B "$repo/bin/ubuntu-tweak" "$@"; }

echo "== apply --commit (empty profile)"
first="$(tweak apply --commit "${flags[@]}")"
echo "$first"
grep -Eq '^[1-9][0-9]* changed$' <<<"$first" || { echo "integration: first apply changed nothing" >&2; exit 1; }

echo "== status"
tweak status "${flags[@]}"

echo "== apply --commit again"
second="$(tweak apply --commit "${flags[@]}")"
echo "$second"
grep -qx '0 changed' <<<"$second" || { echo "integration: second apply was not a no-op" >&2; exit 1; }

link="$XDG_DATA_HOME/gnome-shell/extensions/english-in-overview@ultra"
[[ -L "$link" ]] || { echo "integration: extension link missing: $link" >&2; exit 1; }
enabled="$(/usr/bin/gsettings get org.gnome.shell enabled-extensions)"
[[ "$enabled" == *"english-in-overview@ultra"* ]] || { echo "integration: not enabled in scratch dconf: $enabled" >&2; exit 1; }
echo "integration: OK"

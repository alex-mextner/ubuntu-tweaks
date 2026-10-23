#!/usr/bin/env bash
# End-to-end check of the CLI against a scratch dconf and a scratch HOME: a module settings preset,
# a profile panel file, a machine overlay, two linked files and an extension module are applied
# with `apply --commit`; then `status` exits 0, a second `apply --commit` reports `0 changed`,
# and a `capture` preview reads the result back without writing.
#
# The scratch environment (temporary HOME, XDG_* incl. XDG_RUNTIME_DIR, DCONF_PROFILE) is exported
# BEFORE `dbus-run-session` starts: the dconf service is activated by the bus daemon and inherits
# the daemon's environment, so setting these only inside the session would let it write the
# live user's dconf database (safety invariant 6). The inner run refuses to continue unless it is
# inside that scratch environment and on a bus other than the parent's. It runs a scratch copy of
# the engine with an extra fixture module, so it needs neither network (hyperdock submodule) nor
# root (memguard). Skips (exit 0, loudly) when the org.gnome.shell or org.gnome.desktop.interface schema is not installed.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${UBUNTU_TWEAKS_SCRATCH:-}" != "1" ]]; then
    schemas="$(/usr/bin/gsettings list-schemas 2>/dev/null || true)"
    for schema_package in org.gnome.shell:gnome-shell-common org.gnome.desktop.interface:gsettings-desktop-schemas; do
        if ! grep -qx "${schema_package%%:*}" <<<"$schemas"; then
            echo "integration: SKIPPED, schema ${schema_package%%:*} is not installed (package ${schema_package##*:})"
            exit 0
        fi
    done
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/home" "$tmp/profile" "$tmp/runtime"
    chmod 700 "$tmp/runtime"
    printf 'user-db:user\n' >"$tmp/dconf-profile"
    env -u UBUNTU_TWEAK_PROFILE -u DBUS_SESSION_BUS_ADDRESS \
        UBUNTU_TWEAKS_SCRATCH=1 \
        UBUNTU_TWEAKS_PARENT_BUS="${DBUS_SESSION_BUS_ADDRESS:-}" \
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

# Inside the scratch session. Refuse to touch anything that is not under the scratch root, and
# refuse when the session bus is the parent's (a bus-activated dconf would then be the live one).
root="${UBUNTU_TWEAKS_SCRATCH_ROOT:?}"
[[ "$HOME" == "$root/home" && "$DCONF_PROFILE" == "$root/dconf-profile" && "$XDG_RUNTIME_DIR" == "$root/runtime" ]] ||
    { echo "integration: refusing to run outside the scratch environment" >&2; exit 2; }
[[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" && "$DBUS_SESSION_BUS_ADDRESS" != "${UBUNTU_TWEAKS_PARENT_BUS:-}" ]] ||
    { echo "integration: refusing to run on the parent's session bus" >&2; exit 2; }

python="${PYTHON:-/usr/bin/python3}"

# A scratch copy of the engine with one extra fixture module, so the run needs neither network
# (hyperdock submodule) nor root (memguard): english-in-overview (extension link + enable) plus
# scratch-demo (module settings preset).
scratch_repo="$root/repo"
mkdir -p "$scratch_repo/modules" "$scratch_repo/gnome-extensions"
cp -a "$repo/bin" "$repo/ubuntu_tweaks" "$scratch_repo/"
cp -a "$repo/modules/english-in-overview" "$scratch_repo/modules/"
cp -a "$repo/gnome-extensions/english-in-overview@ultra" "$scratch_repo/gnome-extensions/"
mkdir -p "$scratch_repo/modules/scratch-demo"
cat >"$scratch_repo/modules/scratch-demo/module.toml" <<'EOF'
description = "integration fixture: a module settings preset"

[source]
kind = "builtin"

[[settings]]
file = "preset.ini"
schema = "org.gnome.desktop.interface"
EOF
printf "[/]\nclock-show-seconds=true\n" >"$scratch_repo/modules/scratch-demo/preset.ini"

# The profile: a shared panel file, a machine overlay (wins over shared, adds a key of its own),
# a shared file and a machine file to link.
profile="$root/profile"
mkdir -p "$profile/settings" "$profile/files/.config" \
    "$profile/machines/integration/settings" "$profile/machines/integration/files/.config"
printf "[org/gnome/desktop/interface]\ncolor-scheme='prefer-dark'\nclock-show-date=false\n" >"$profile/settings/appearance.ini"
printf "[org/gnome/desktop/interface]\ncolor-scheme='prefer-light'\ntext-scaling-factor=1.25\n" \
    >"$profile/machines/integration/settings/appearance.ini"
printf "shared = 1\n" >"$profile/files/.config/ubuntu-tweak-integration.conf"
printf "machine = 1\n" >"$profile/machines/integration/files/.config/ubuntu-tweak-integration-machine.conf"
# Two more bundles: a shared one in a subdirectory (header, reasons, keys of two schemas) and a machine overlay
# bundle of the same name that wins for one key.
mkdir -p "$profile/settings/dev" "$profile/machines/integration/settings/dev"
cat >"$profile/settings/dev/look.ini" <<'EOF'
[ubuntu-tweak]
description = "integration bundle"

[org/gnome/desktop/interface]
# I want the weekday
clock-show-weekday=true

[org/gnome/desktop/wm/preferences]
# focus follows the mouse
focus-mode='sloppy'
EOF
cat >"$profile/machines/integration/settings/dev/look.ini" <<'EOF'
[org/gnome/desktop/wm/preferences]
# pointer focus only on this machine
focus-mode='mouse'
EOF

flags=(--profile "$profile" --machine integration)
tweak() { "$python" -B "$scratch_repo/bin/ubuntu-tweak" "$@"; }
fail() { echo "integration: $*" >&2; exit 1; }

echo "== machine"
tweak machine "${flags[@]}"

echo "== apply (preview changes nothing)"
preview="$(tweak apply "${flags[@]}")"
echo "$preview"
[[ ! -e "$HOME/.config/ubuntu-tweak-integration.conf" ]] || fail "the preview linked a file"
grep -q "# pointer focus only on this machine" <<<"$preview" || fail "the apply preview does not show the reason of a key"
grep -q "# I want the weekday" <<<"$preview" || fail "the apply preview does not show the reason of a shared bundle key"

echo "== apply --commit (empty system)"
first="$(tweak apply --commit "${flags[@]}")"
echo "$first"
grep -Eq '^[1-9][0-9]* changed$' <<<"$first" || fail "first apply changed nothing"

echo "== status"
tweak status "${flags[@]}"

echo "== apply --commit again"
second="$(tweak apply --commit "${flags[@]}")"
echo "$second"
grep -qx '0 changed' <<<"$second" || fail "second apply was not a no-op"

link="$XDG_DATA_HOME/gnome-shell/extensions/english-in-overview@ultra"
[[ -L "$link" ]] || fail "extension link missing: $link"
enabled="$(/usr/bin/gsettings get org.gnome.shell enabled-extensions)"
[[ "$enabled" == *"english-in-overview@ultra"* ]] || fail "not enabled in scratch dconf: $enabled"

interface=org.gnome.desktop.interface
[[ "$(/usr/bin/gsettings get $interface clock-show-seconds)" == true ]] || fail "module preset key not applied"
[[ "$(/usr/bin/gsettings get $interface clock-show-date)" == false ]] || fail "shared panel key not applied"
[[ "$(/usr/bin/gsettings get $interface color-scheme)" == "'prefer-light'" ]] || fail "machine overlay did not win"
[[ "$(/usr/bin/gsettings get $interface text-scaling-factor)" == 1.25 ]] || fail "machine-only key not applied"
for name in ubuntu-tweak-integration ubuntu-tweak-integration-machine; do
    [[ -L "$HOME/.config/$name.conf" ]] || fail "$name.conf is not a symlink"
    [[ "$(readlink "$HOME/.config/$name.conf")" == "$profile"/* ]] || fail "$name.conf does not point into the profile"
done

echo "== bundles: merge, shadows, reasons"
listing="$(tweak bundles "${flags[@]}")"
echo "$listing"
grep -q "dev/look" <<<"$listing" || fail "bundles does not list the nested bundle"
grep -q "shadowed" <<<"$listing" || fail "bundles does not report the overlay shadowing the shared key"
merged="$(tweak bundles --show dev/look "${flags[@]}")"
echo "$merged"
grep -q "focus-mode=" <<<"$merged" || fail "bundles --show lost a key"
grep -q "# I want the weekday" <<<"$merged" || fail "bundles --show lost a reason"
wm=org.gnome.desktop.wm.preferences
[[ "$(/usr/bin/gsettings get $wm focus-mode)" == "'mouse'" ]] || fail "overlay bundle key did not win over the shared bundle key"
[[ "$(/usr/bin/gsettings get $interface clock-show-weekday)" == true ]] || fail "shared bundle key (other schema) not applied"

echo "== bundle-move and note (regroup without changing what applies)"
look_before="$(cat "$profile/settings/dev/look.ini")"
moves="$(tweak bundle-move regrouped 'org/gnome/desktop/wm/preferences/**' org/gnome/desktop/interface/clock-show-weekday "${flags[@]}")"
echo "$moves"
grep -q "preview" <<<"$moves" || fail "bundle-move did not say it was a preview"
[[ "$(cat "$profile/settings/dev/look.ini")" == "$look_before" ]] || fail "the bundle-move preview wrote a file"
[[ ! -e "$profile/settings/regrouped.ini" ]] || fail "the bundle-move preview created a bundle"
tweak bundle-move regrouped 'org/gnome/desktop/wm/preferences/**' org/gnome/desktop/interface/clock-show-weekday "${flags[@]}" --commit
grep -q "# I want the weekday" "$profile/settings/regrouped.ini" || fail "the moved key lost its reason"
grep -q "# pointer focus only on this machine" "$profile/machines/integration/settings/regrouped.ini" || fail "the overlay key lost its reason"
tweak status "${flags[@]}" || fail "regrouping changed what applies"
again="$(tweak apply --commit "${flags[@]}")"
grep -qx '0 changed' <<<"$again" || fail "apply after bundle-move was not a no-op"
tweak note regrouped:org/gnome/desktop/interface/clock-show-weekday "the weekday matters on this desk" "${flags[@]}" --commit
grep -q "# the weekday matters on this desk" "$profile/settings/regrouped.ini" || fail "note did not write the reason"
tweak status "${flags[@]}" || fail "a note changed what applies"

echo "== capture (preview; the scratch dconf and HOME are all it can see)"
# Every file and symlink of the profile and of HOME before and after, but for dconf's own database,
# flatpak's user installation (`flatpak list`, which the packages section runs, creates it) and the
# session log directory (every command, a preview included, writes its own log: spec, *Session log*).
snapshot() {
    find "$profile" "$HOME" \( -path "$HOME/.config/dconf" -o -path "$HOME/.local/share/flatpak" \
        -o -path "$HOME/.local/state/ubuntu-tweaks/logs" \) -prune -o \
        \( -type f -o -type l \) -printf '%p %y %s %T@ %l\n' | sort
}
before="$(snapshot)"
capture="$(tweak capture "${flags[@]}" --report "$root/capture.md" -v)"
echo "$capture"
[[ -s "$root/capture.md" ]] || fail "capture wrote no report"
grep -q "color-scheme" <<<"$capture" || fail "capture does not list the applied keys"
after="$(snapshot)"
[[ "$after" == "$before" ]] || { diff <(echo "$before") <(echo "$after") >&2 || true; fail "the capture preview changed the profile or HOME"; }
echo "integration: OK"

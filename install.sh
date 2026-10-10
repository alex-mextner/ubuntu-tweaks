#!/usr/bin/env bash
# Installs every tweak from this repo. Idempotent: safe to re-run.
# GNOME extensions are symlinked, so edits in the repo apply after re-login.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ext_dir="$HOME/.local/share/gnome-shell/extensions"
mkdir -p "$ext_dir"

shopt -s nullglob
for src in "$repo"/gnome-extensions/*/; do
    [[ -f "$src/metadata.json" ]] || continue
    uuid="$(basename "$src")"
    # -T: fail loudly instead of nesting if a real directory is already there.
    ln -sfnT "${src%/}" "$ext_dir/$uuid"
    # gnome-extensions can't see new extensions until re-login on Wayland,
    # so add the uuid to the enabled list directly. dconf, not gsettings:
    # on Ubuntu gsettings shows a different view and writing it back would
    # drop ding/ubuntu-dock/tiling-assistant.
    key=/org/gnome/shell/enabled-extensions
    enabled="$(dconf read "$key")"
    if [[ "$enabled" != *"'$uuid'"* ]]; then
        if [[ -z "$enabled" || "$enabled" == "@as []" || "$enabled" == "[]" ]]; then
            enabled="['$uuid']"
        else
            enabled="${enabled%]}, '$uuid']"
        fi
        dconf write "$key" "$enabled"
    fi
    echo "installed: $uuid"
done

echo "Log out and back in to load new GNOME extensions."

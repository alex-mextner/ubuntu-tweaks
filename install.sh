#!/usr/bin/env bash
# Installs every tweak from this repo. Idempotent: safe to re-run.
# Repo extensions (incl. the dock submodule) are symlinked, so edits in the
# repo apply after re-login.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ext_dir="$HOME/.local/share/gnome-shell/extensions"
mkdir -p "$ext_dir"

# Lists are edited with dconf, not gsettings: on Ubuntu gsettings shows a
# different view and writing it back would drop ding/tiling-assistant.
# usage: dconf_list_add <key> <item> | dconf_list_remove <key> <item>
dconf_list_add() {
    local list
    list="$(dconf read "$1")"
    [[ "$list" == *"'$2'"* ]] && return
    if [[ -z "$list" || "$list" == "@as []" || "$list" == "[]" ]]; then
        list="['$2']"
    else
        list="${list%]}, '$2']"
    fi
    dconf write "$1" "$list"
}
dconf_list_remove() {
    local list
    list="$(dconf read "$1")"
    [[ "$list" == *"'$2'"* ]] || return 0
    list="${list//"'$2', "/}"
    list="${list//", '$2'"/}"
    list="${list//"'$2'"/}"
    # an untyped [] is not a valid GVariant
    [[ "$list" == "[]" ]] && list="@as []"
    dconf write "$1" "$list"
}

# gnome-extensions can't see new extensions until re-login on Wayland,
# so enable by uuid directly.
enable_extension() {
    dconf_list_add /org/gnome/shell/enabled-extensions "$1"
    dconf_list_remove /org/gnome/shell/disabled-extensions "$1"
}

# Extensions with settings need compiled schemas next to them.
compile_schemas() {
    local dir=$1/schemas
    compgen -G "$dir/*.gschema.xml" >/dev/null || return 0
    if [[ ! -f "$dir/gschemas.compiled" ]] || [[ -n "$(find "$dir" -name '*.gschema.xml' -newer "$dir/gschemas.compiled")" ]]; then
        glib-compile-schemas "$dir"
    fi
}

if [[ -f "$repo/.gitmodules" ]]; then
    git -C "$repo" submodule update --init --recursive
fi

shopt -s nullglob
for src in "$repo"/gnome-extensions/*/; do
    [[ -f "$src/metadata.json" ]] || continue
    uuid="$(basename "$src")"
    compile_schemas "${src%/}"
    # -T: fail loudly instead of nesting if a real directory is already there.
    ln -sfnT "${src%/}" "$ext_dir/$uuid"
    enable_extension "$uuid"
    echo "installed: $uuid"
done

# Magic Lamp (earlier versions installed it) is replaced by the dock's own GPU
# genie: Clutter.DeformEffect paints nothing on Mutter 50. Disable it only;
# its files are left alone in case the user installed it themselves.
dconf_list_remove /org/gnome/shell/enabled-extensions compiz-alike-magic-lamp-effect@hermes83.github.com

# Dock: Dash2Dock replaces Ubuntu Dock; two docks would overlap.
dconf_list_add /org/gnome/shell/disabled-extensions ubuntu-dock@ubuntu.com
dconf_list_remove /org/gnome/shell/enabled-extensions ubuntu-dock@ubuntu.com
dconf load /org/gnome/shell/extensions/dash2dock-lite/ < "$repo/dconf/dock.ini"
# Screenshots stack, unless folder stacks were already chosen (kept on re-run).
# GNOME saves screenshots to <Pictures>/<localized "Screenshots">.
stacks_key=/org/gnome/shell/extensions/dash2dock-lite/folder-stacks
if [[ -z "$(dconf read "$stacks_key")" ]]; then
    pictures="$(xdg-user-dir PICTURES 2>/dev/null || echo "$HOME/Pictures")"
    screenshots="$pictures/$(gettext -d gnome-shell Screenshots 2>/dev/null || echo Screenshots)"
    mkdir -p "$screenshots"
    # JSON inside a GVariant string: escape for both layers, the path may
    # contain quotes or backslashes
    path="${screenshots//\\/\\\\}"
    path="${path//\"/\\\"}"
    path="${path//$'\t'/\\t}"
    path="${path//$'\n'/\\n}"
    path="${path//$'\r'/\\r}"
    stacks="[{\"path\": \"$path\", \"icon\": \"folder-pictures\", \"view\": \"automatic\", \"sort\": \"date-added\", \"display\": \"stack\"}]"
    stacks="${stacks//\\/\\\\}"
    stacks="${stacks//\'/\\\'}"
    dconf write "$stacks_key" "'$stacks'"
fi
echo "configured: macOS-style dock (dconf/dock.ini)"

echo "Log out and back in to load new GNOME extensions."

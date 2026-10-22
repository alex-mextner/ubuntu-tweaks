#!/usr/bin/env bash
# dotfiles module installer (user level, never root): puts a checkout of alex-mextner/.files at
# ~/.files so the default profile location (~/.files/ubuntu) exists. It clones when the directory is
# absent and fast-forwards a clean checkout; it NEVER writes a file of the repository, links nothing
# into $HOME and never touches the mac-era dotfiles (zsh, zplug, brew) in it.
#
#   modules/dotfiles/install.sh           clone ~/.files, or fast-forward it when it is clean
#   modules/dotfiles/install.sh --plan    read-only: what it would do
#   modules/dotfiles/install.sh --check   read-only: exit 0 = checkout present, origin is the
#                                         configured repo (when one is set), not behind, not diverged (last-fetched state); 3 = drift
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual
# step (the output says which), anything else is a failure. Options come from `[modules.dotfiles]` of
# profile.toml / machine.toml as JSON in UBUNTU_TWEAK_MODULE_CONFIG (never a secret):
#
#   repo   git URL or path to clone   (default: `gh repo clone alex-mextner/.files` when gh is logged in,
#                                      else https://github.com/alex-mextner/.files)
#   dir    checkout directory         (default ~/.files)
#
# Spec: docs/specs/ubuntu-tweak-cli.md, "Optional modules: rig, dotfiles".
set -euo pipefail

default_slug=alex-mextner/.files

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

command -v python3 >/dev/null || { echo "needs manual: python3 is not installed" >&2; exit 10; }

opt() { # opt KEY DEFAULT: one option of UBUNTU_TWEAK_MODULE_CONFIG
  python3 -B -c 'import json, os, sys
print(json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], sys.argv[2]))' "$1" "$2"
}

repo=$(opt repo "")
dir=$(opt dir "$HOME/.files")
case "$dir" in \~/*) dir="$HOME/${dir#\~/}" ;; esac
case "$dir" in /*) ;; *) echo "[modules.dotfiles] dir must be absolute or start with ~/: $dir" >&2; exit 2 ;; esac
case "$repo" in -*) echo "[modules.dotfiles] repo must not start with '-': $repo" >&2; exit 2 ;; esac
manual() { echo "needs manual: $1" >&2; exit 10; }

# the checkout must be rooted AT $dir: a plain directory inside another work tree (a git-managed $HOME)
# is not one, and fetching or merging there would act on the enclosing repository
is_repo() {
  local top here
  top=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || return 1
  here=$(cd "$dir" 2>/dev/null && pwd -P) || return 1
  [ -n "$top" ] && [ "$top" = "$here" ]
}
# the stored remote.origin.url (get-url would expand url.<base>.insteadOf rewrites)
origin_url() { git -C "$dir" config --get remote.origin.url 2>/dev/null || echo none; }
origin_ok() { [ -z "$repo" ] || [ "$(origin_url)" = "$repo" ]; }
clean() { [ -z "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; }
# "ahead behind" against the last-fetched upstream; empty when there is none
counts() { git -C "$dir" rev-list --left-right --count 'HEAD...@{u}' 2>/dev/null || true; }

if [ "$mode" = check ]; then
  [ -e "$dir" ] || { echo "drift: $dir is missing"; exit 3; }
  is_repo || { echo "drift: $dir is not a git checkout"; exit 3; }
  if ! origin_ok; then
    echo "drift: $dir has origin $(origin_url), not $repo (git -C $dir remote set-url origin $repo, or fix the repo option)"
    exit 3
  fi
  read -r ahead behind <<<"$(counts)"
  if [ -n "${behind:-}" ] && [ "$behind" -gt 0 ]; then
    if [ "${ahead:-0}" -gt 0 ]; then echo "drift: $dir has diverged from its upstream"; else echo "drift: $dir is $behind commit(s) behind its upstream"; fi
    exit 3
  fi
  echo "$dir is a checkout that is not behind its upstream"
  exit 0
fi

if [ "$mode" = plan ]; then
  if [ ! -e "$dir" ]; then
    if [ -n "$repo" ]; then echo "git clone $repo $dir"
    elif command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then echo "gh repo clone $default_slug $dir"
    else echo "git clone https://github.com/$default_slug $dir"; fi
  elif is_repo; then
    echo "git -C $dir fetch, then fast-forward when the working tree is clean (no file of the repository is written otherwise)"
  else
    echo "$dir exists and is not a git checkout: needs manual"
  fi
  exit 0
fi

command -v git >/dev/null || manual "git is not installed (module base-tools installs it)"
if [ ! -e "$dir" ]; then
  mkdir -p "$(dirname "$dir")"
  if [ -n "$repo" ]; then
    git clone -- "$repo" "$dir" || manual "cannot clone $repo; check the URL, credentials and network, then re-run"
  elif command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh repo clone "$default_slug" "$dir" || manual "gh could not clone $default_slug; check gh auth status, then re-run"
  else
    git clone -- "https://github.com/$default_slug" "$dir" \
      || manual "cannot clone https://github.com/$default_slug (private repository: gh auth login, or module git-gh with a keychain token); then re-run"
  fi
  exit 0
fi

is_repo || manual "$dir exists and is not a git checkout; move it away or set [modules.dotfiles] dir"
origin_ok || manual "$dir has origin $(origin_url), not $repo; run git -C $dir remote set-url origin $repo, or fix the repo option"
git -C "$dir" fetch --quiet origin 2>/dev/null || { echo "$dir: could not fetch (offline?), left as it is"; exit 0; }
read -r ahead behind <<<"$(counts)"
if [ -z "${behind:-}" ] || [ "$behind" = 0 ]; then
  echo "$dir is up to date"
  exit 0
fi
[ "${ahead:-0}" = 0 ] || manual "$dir has diverged from its upstream; rebase or merge it yourself"
clean || manual "$dir is $behind commit(s) behind but has uncommitted changes; commit or stash them, then re-run"
git -C "$dir" merge --ff-only --quiet '@{u}' || manual "$dir could not be fast-forwarded (an untracked file is in the way?); fix it, then re-run"
echo "$dir fast-forwarded by $behind commit(s)"

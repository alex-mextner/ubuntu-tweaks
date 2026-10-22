#!/usr/bin/env bash
# shellcheck disable=SC2154  # eval of the resolver's answer defines repo, is_slug, dir, source, login, blocked, ...
# dotfiles module installer (user level, never root): puts a checkout of the user's dotfiles repository on
# disk so the ubuntu profile inside it exists. Which repository is not written down anywhere: it is found
# through gh (ubuntu_tweaks/identity.py: the user's own `.files`, `dotfiles`, `.dotfiles` or `dot-files`).
# It clones when the directory is absent and fast-forwards a clean checkout; it NEVER writes a file of
# the repository, links nothing into $HOME and never touches anything else the repository holds (zsh, brew...).
#
#   modules/dotfiles/install.sh           clone the checkout, or fast-forward it when it is clean
#   modules/dotfiles/install.sh --plan    read-only: what it would do
#   modules/dotfiles/install.sh --check   read-only: exit 0 = nothing to do or a checkout is present, its origin
#                                         is the repository asked for, not behind, not diverged (last-fetched
#                                         state); 3 = drift
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual
# step (the output says which: `gh auth login`, a checkout that is not the repository), anything else is a
# failure. Options come from `[modules.dotfiles]` of profile.toml / machine.toml as JSON in
# UBUNTU_TWEAK_MODULE_CONFIG (never a secret); `--dotfiles-repo` reaches it as UBUNTU_TWEAK_DOTFILES_REPO:
#
#   repo   OWNER/NAME, git URL or path   (default: found through gh)
#   dir    checkout directory            (default ~/.files for a repository named .files, else ~/<name>)
#   path   the profile inside it         (default: ubuntu/, then ubuntu-tweaks/, then the root)
#
# Spec: docs/specs/ubuntu-tweak-cli.md, "Profile discovery" and "The dotfiles module".
set -euo pipefail

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

command -v python3 >/dev/null || { echo "needs manual: python3 is not installed" >&2; exit 10; }

engine=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ident() { PYTHONPATH="$engine" python3 -B -m ubuntu_tweaks.identity "$@"; }
manual() { echo "needs manual: $1" >&2; exit 10; }

# one interpreter start for every fact: shell assignments repo, is_slug, dir, source, login, blocked, profile_path, notes
resolved=$(ident resolve --shell) || exit $?
# shellcheck disable=SC2154  # eval defines repo, is_slug, dir, source, login, blocked, profile_path and notes
eval "$resolved"
[ -z "$notes" ] || printf '%s\n' "$notes"

if [ -n "$blocked" ] && [ -z "$dir" ]; then # gh is missing or not logged in, and no checkout is on disk
  case "$mode" in
    check) echo "drift: $blocked"; exit 3 ;;
    plan) echo "needs manual: $blocked"; exit 0 ;;
    *) manual "$blocked" ;;
  esac
fi
if [ -z "$dir" ]; then # logged in, and no repository of that name: nothing to install, which is fine
  who=${login:+ for $login}
  echo "no dotfiles repository found$who (looked for .files, dotfiles, .dotfiles, dot-files): nothing to install;" \
    "create one with ubuntu/profile.toml in it, or set [modules.dotfiles] repo, or pass --dotfiles-repo OWNER/NAME"
  exit 0
fi
case "$repo" in -*) echo "[modules.dotfiles] repo must not start with '-': $repo" >&2; exit 2 ;; esac

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
origin_ok() { [ -z "$repo" ] || ident same-repo "$(origin_url)" "$repo"; }
clean() { [ -z "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; }
# "ahead behind" against the last-fetched upstream; empty when there is none
counts() { git -C "$dir" rev-list --left-right --count 'HEAD...@{u}' 2>/dev/null || true; }
gh_ready() { command -v gh >/dev/null && gh auth status >/dev/null 2>&1; }
clone_command() {
  if [ "$is_slug" = True ] && gh_ready; then echo "gh repo clone $repo $dir"
  elif [ "$is_slug" = True ]; then echo "git clone https://github.com/$repo $dir"
  else echo "git clone $repo $dir"; fi
}
remember() { # record where the checkout is, so the next command finds it (a custom dir, a custom path)
  local args=("$dir")
  [ -z "$profile_path" ] || args+=(--path "$profile_path")
  [ -z "$repo" ] || args+=(--repo "$repo")
  ident remember "${args[@]}"
}
report_profile() {
  local found
  if found=$(ident profile "$dir" ${profile_path:+"$profile_path"}); then echo "profile: $found"
  else echo "note: $dir holds no ubuntu profile yet (looked for ubuntu/profile.toml, ubuntu-tweaks/, profile.toml at the root)"; fi
}

if [ "$mode" = check ]; then
  [ -z "$blocked" ] || { echo "drift: $blocked"; exit 3; }
  [ -e "$dir" ] || { echo "drift: $dir is missing (the checkout of $repo)"; exit 3; }
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
  [ -z "$blocked" ] || { echo "needs manual: $blocked"; exit 0; }
  [ -z "$repo" ] || echo "dotfiles repository: $repo (${source:-given}${login:+, gh login $login})"
  if [ ! -e "$dir" ]; then
    clone_command
  elif is_repo; then
    echo "git -C $dir fetch, then fast-forward when the working tree is clean (no file of the repository is written otherwise)"
  else
    echo "$dir exists and is not a git checkout: needs manual"
  fi
  exit 0
fi

[ -z "$blocked" ] || manual "$blocked"
command -v git >/dev/null || manual "git is not installed (module base-tools installs it)"
if [ ! -e "$dir" ]; then
  [ -n "$repo" ] || manual "$dir does not exist and no repository is known to clone it from"
  mkdir -p "$(dirname "$dir")"
  if [ "$is_slug" = True ] && gh_ready; then
    gh repo clone "$repo" "$dir" || manual "gh could not clone $repo; check gh auth status, then re-run"
  elif [ "$is_slug" = True ]; then
    git clone -- "https://github.com/$repo" "$dir" \
      || manual "cannot clone https://github.com/$repo (a private repository needs gh auth login); then re-run"
  else
    git clone -- "$repo" "$dir" || manual "cannot clone $repo; check the URL, credentials and network, then re-run"
  fi
  echo "cloned $repo to $dir"
  remember
  report_profile
  exit 0
fi

is_repo || manual "$dir exists and is not a git checkout; move it away or set [modules.dotfiles] dir"
origin_ok || manual "$dir has origin $(origin_url), not $repo; run git -C $dir remote set-url origin $repo, or fix the repo option"
git -C "$dir" fetch --quiet origin 2>/dev/null || { echo "$dir: could not fetch (offline?), left as it is"; remember; exit 0; }
read -r ahead behind <<<"$(counts)"
if [ -z "${behind:-}" ] || [ "$behind" = 0 ]; then
  echo "$dir is up to date"
  remember
  report_profile
  exit 0
fi
[ "${ahead:-0}" = 0 ] || manual "$dir has diverged from its upstream; rebase or merge it yourself"
clean || manual "$dir is $behind commit(s) behind but has uncommitted changes; commit or stash them, then re-run"
git -C "$dir" merge --ff-only --quiet '@{u}' || manual "$dir could not be fast-forwarded (an untracked file is in the way?); fix it, then re-run"
echo "$dir fast-forwarded by $behind commit(s)"
remember
report_profile

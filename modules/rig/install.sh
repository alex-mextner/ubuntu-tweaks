#!/usr/bin/env bash
# rig module installer (user level, never root): installs rig, brings in the shared rig profile and
# lets `rig activate` switch on everything this machine can run (skills, agent hooks incl. omp, git
# hooks, tmux, omp config, harness wrappers, sysmon, the ctmux tool).
#
#   modules/rig/install.sh           clone rig-cli, link ~/.local/bin/rig, `rig profile sync`, `rig activate commit`
#   modules/rig/install.sh --plan    read-only: the exact steps, and the `rig activate` preview once rig exists
#   modules/rig/install.sh --check   read-only: exit 0 = installed and nothing pending, 3 = drift
#
# Exit codes (the ubuntu-tweak system-module protocol): 0 ok, 3 drift (--check), 10 needs a manual step
# (the output says which), anything else is a failure. Options come from `[modules.rig]` of
# profile.toml / machine.toml as JSON in UBUNTU_TWEAK_MODULE_CONFIG (never a secret):
#
#   repo          git URL of rig-cli                  (default https://git.hyperide.ai/ultrabricks/rig-cli)
#   dir           where the checkout lives            (default ~/dev/rig-cli)
#   profile_url   private rig profile repo to `rig profile init` when none is set up yet (default none)
#   profile_sync  false = never touch the rig profile (default true)
#   activate      false = install rig only, no `rig activate commit` (default true)
#
# Spec: docs/specs/ubuntu-tweak-cli.md, "Optional modules: rig, dotfiles".
set -euo pipefail

default_repo=https://git.hyperide.ai/ultrabricks/rig-cli

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

mode=install
case "${1:-}" in
  --check) mode=check ;;
  --plan) mode=plan ;;
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) echo "usage: $0 [--plan|--check]" >&2; exit 2 ;;
esac

opt() { # opt KEY DEFAULT: one option of UBUNTU_TWEAK_MODULE_CONFIG (booleans as true/false)
  python3 -B -c 'import json, os, sys
value = json.loads(os.environ.get("UBUNTU_TWEAK_MODULE_CONFIG") or "{}").get(sys.argv[1], sys.argv[2])
print(str(value).lower() if isinstance(value, bool) else value)' "$1" "$2"
}

repo_url=$(opt repo "$default_repo")
dir=$(opt dir "$HOME/dev/rig-cli")
profile_url=$(opt profile_url "")
profile_sync=$(opt profile_sync true)
activate=$(opt activate true)
case "$dir" in \~/*) dir="$HOME/${dir#\~/}" ;; esac
case "$repo_url" in -*) echo "[modules.rig] repo must not start with '-': $repo_url" >&2; exit 2 ;; esac
case "$profile_url" in -*) echo "[modules.rig] profile_url must not start with '-': $profile_url" >&2; exit 2 ;; esac

link="$HOME/.local/bin/rig"
rig="$dir/bin/rig"
profile_checkout="${XDG_CONFIG_HOME:-$HOME/.config}/rig/profile/repo"

has_checkout() { [ -f "$dir/riglib/cli.py" ] && [ -x "$rig" ]; }
link_ok() { [ -L "$link" ] && [ "$(readlink -f "$link")" = "$(readlink -f "$rig")" ]; }
profile_configured() { [ -e "$profile_checkout/.git" ]; }
manual() { echo "needs manual: $1" >&2; exit 10; }

# -- --check: read-only ------------------------------------------------------------------------
if [ "$mode" = check ]; then
  problems=()
  has_checkout || problems+=("rig checkout missing at $dir")
  link_ok || problems+=("$link is missing or points elsewhere")
  if [ ${#problems[@]} -eq 0 ]; then
    if [ "$profile_sync" != false ]; then
      if profile_configured; then
        "$rig" profile status >/dev/null 2>&1 || problems+=("the rig profile is not in sync (rig profile sync)")
      elif [ -n "$profile_url" ]; then
        problems+=("the rig profile is not initialised (rig profile init $profile_url)")
      fi
    fi
    if [ "$activate" != false ]; then
      preview=$("$rig" activate --json 2>/dev/null) || { echo "rig activate --json failed: the check cannot decide" >&2; exit 1; }
      pending=$(printf '%s' "$preview" | python3 -B -c 'import json, sys; print(len(json.load(sys.stdin).get("config_changes", [])))') || exit 1
      [ "$pending" = 0 ] || problems+=("rig activate would write $pending config switch(es)")
      (cd "$HOME" && "$rig" status >/dev/null 2>&1) || problems+=("rig status reports drift (rig activate commit)")
    fi
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'drift: %s\n' "${problems[@]}"
    exit 3
  fi
  echo "rig is installed and nothing is pending"
  exit 0
fi

# -- --plan: read-only -------------------------------------------------------------------------
if [ "$mode" = plan ]; then
  if has_checkout; then echo "rig-cli: $dir present (fast-forward when clean)"; else echo "git clone $repo_url $dir"; fi
  if link_ok; then echo "link: $link already points at $rig"; else echo "ln -s $rig $link"; fi
  if [ "$profile_sync" = false ]; then
    echo "rig profile: not touched (profile_sync = false)"
  elif profile_configured; then
    echo "rig profile sync"
  elif [ -n "$profile_url" ]; then
    echo "rig profile init $profile_url"
  else
    echo "rig profile: none set up (set [modules.rig] profile_url, or run rig profile init <url>)"
  fi
  if [ "$activate" = false ]; then
    echo "rig activate: skipped (activate = false)"
  elif has_checkout; then
    echo "rig activate commit; its preview now:"
    "$rig" activate 2>&1 | sed 's/^/  | /' || echo "  (rig activate preview unavailable)"
  else
    echo "rig activate commit (its preview is shown once rig-cli is cloned)"
  fi
  exit 0
fi

# -- install -----------------------------------------------------------------------------------
command -v git >/dev/null || manual "git is not installed (module base-tools installs it)"
command -v python3 >/dev/null || manual "python3 is not installed"

if [ ! -e "$dir" ]; then
  mkdir -p "$(dirname "$dir")"
  echo "cloning $repo_url -> $dir"
  git clone -- "$repo_url" "$dir" || manual "cannot clone $repo_url (git credentials for that forge, e.g. fj auth login, or the network); then re-run"
elif has_checkout; then
  if git -C "$dir" fetch --quiet origin 2>/dev/null; then
    if [ -z "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
      git -C "$dir" merge --ff-only --quiet '@{u}' 2>/dev/null || echo "rig-cli: not fast-forwarded (diverged or no upstream), left as it is"
    else
      echo "rig-cli: working tree has changes, not updated"
    fi
  else
    echo "rig-cli: could not fetch (offline?), left as it is"
  fi
else
  manual "$dir exists and is not a rig-cli checkout; move it away or set [modules.rig] dir"
fi
has_checkout || manual "$dir does not look like rig-cli (no riglib/cli.py and bin/rig)"

mkdir -p "$(dirname "$link")"
if [ -e "$link" ] && [ ! -L "$link" ]; then
  manual "$link is a regular file; move it away so it can become a symlink to $rig"
fi
link_ok || ln -sfn "$rig" "$link"

if [ "$profile_sync" != false ]; then
  if profile_configured; then
    rc=0; "$rig" profile sync || rc=$?
  elif [ -n "$profile_url" ]; then
    rc=0; "$rig" profile init "$profile_url" || rc=$?
  else
    echo "rig profile: none set up (set [modules.rig] profile_url, or run rig profile init <url>)"
    rc=0
  fi
  case "$rc" in
    0) ;;
    1|3) manual "rig profile did not sync (exit $rc): authenticate to the forge (fj auth login) or resolve the conflict with rig profile sync --take local|remote; then re-run" ;;
    *) exit "$rc" ;;
  esac
fi

if [ "$activate" != false ]; then
  "$rig" activate commit
fi

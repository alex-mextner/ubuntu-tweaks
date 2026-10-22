#!/bin/sh
# One command on a stock Ubuntu (no curl, no git): the four prerequisites, the checkout, `ubuntu-tweak init`.
#   sh bootstrap.sh          preview only (init without --commit)
#   sh bootstrap.sh --yes    init --commit --yes: install and apply without asking
#   sh bootstrap.sh ARGS     anything else goes to `ubuntu-tweak init` as it is (--commit, --no-sudo)
# Spec: docs/specs/ubuntu-tweak-cli.md, "One command on a fresh machine". Prerequisites are installed
# even for the preview: without git and python3-gi there is nothing to preview with.
set -eu

REPO=${UBUNTU_TWEAKS_REPO:-https://github.com/alex-mextner/ubuntu-tweaks}
DIR=${UBUNTU_TWEAKS_DIR:-$HOME/xp/ubuntu-tweaks}

if [ "$(id -u)" -eq 0 ]; then SUDO=; else SUDO=sudo; fi

missing=
for tool in git curl; do command -v "$tool" >/dev/null 2>&1 || missing=1; done
[ -f /etc/ssl/certs/ca-certificates.crt ] || missing=1
python3 -c 'import gi' >/dev/null 2>&1 || missing=1
if [ -n "$missing" ]; then
    $SUDO apt-get update
    $SUDO apt-get install -y git curl ca-certificates python3-gi
fi

if [ -e "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only
    git -C "$DIR" submodule update --init --recursive
else
    mkdir -p "$(dirname "$DIR")"
    git clone --recurse-submodules "$REPO" "$DIR"
fi

if [ "${1:-}" = "--yes" ]; then
    shift
    set -- --commit --yes "$@"
fi
exec "$DIR/bin/ubuntu-tweak" init "$@"

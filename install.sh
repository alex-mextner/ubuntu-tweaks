#!/usr/bin/env bash
# Old entry point, kept so existing instructions work: installs everything via the CLI.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bin/ubuntu-tweak" init --commit "$@"

#!/usr/bin/env bash
# Run a module root installer in a disposable docker root; see scripts/root_container.py.
exec python3 -B "$(dirname "${BASH_SOURCE[0]}")/root_container.py" "$@"

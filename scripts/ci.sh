#!/usr/bin/env bash
# The repo's checks, run by .github/workflows/ci.yml and runnable locally as-is: ShellCheck
# on shell scripts, ruff + a compile pass on Python (including extensionless scripts with a
# python shebang), every modules/*/tests unittest suite, a syntax check of the GNOME
# extensions' JavaScript (ES modules, as GNOME Shell loads them) and JSON validation.
set -euo pipefail
cd "$(dirname "$0")/.."

mapfile -t shell_files < <(git ls-files '*.sh')
mapfile -t py_files < <(
  git ls-files | while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
      *.py) echo "$f" ;;
      *.*) ;;
      *) head -n1 "$f" | grep -q '^#!.*python' && echo "$f" ;;
    esac
  done
)

status=0
if [ ${#shell_files[@]} -gt 0 ]; then
  echo "== shellcheck (${#shell_files[@]} files)"
  shellcheck "${shell_files[@]}" || status=1
fi
if [ ${#py_files[@]} -gt 0 ]; then
  echo "== ruff (${#py_files[@]} files)"
  ruff check "${py_files[@]}" || status=1
  echo "== compile"
  for f in "${py_files[@]}"; do
    python3 -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$f" || status=1
  done
fi
mapfile -t js_files < <(git ls-files '*.js')
if [ ${#js_files[@]} -gt 0 ]; then
  echo "== node --check (${#js_files[@]} files)"
  tmp=$(mktemp -d)
  for f in "${js_files[@]}"; do
    cp "$f" "$tmp/check.mjs"          # .mjs: parse as an ES module (import/export)
    node --check "$tmp/check.mjs" 2>&1 | sed "s|$tmp/check.mjs|$f|" || status=1
  done
  rm -rf "$tmp"
fi
mapfile -t json_files < <(git ls-files '*.json')
if [ ${#json_files[@]} -gt 0 ]; then
  echo "== json (${#json_files[@]} files)"
  for f in "${json_files[@]}"; do
    python3 -m json.tool "$f" >/dev/null || status=1
  done
fi
for tests in modules/*/tests; do
  [ -d "$tests" ] || continue
  echo "== unittest $tests"
  (cd "$(dirname "$tests")" && python3 -B -m unittest discover -s tests) || status=1
done
# The ubuntu-tweak CLI: formatted code, and its unittest suite under an interpreter that has
# PyGObject (a distro package, absent from a setup-python interpreter); tests/test_settings.py
# skips itself when `gi` is missing.
echo "== ruff format (ubuntu_tweaks, tests, bin)"
ruff format --check ubuntu_tweaks tests bin/ubuntu-tweak || status=1
tweak_python=python3
if /usr/bin/python3 -c 'import gi, sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
  tweak_python=/usr/bin/python3
fi
echo "== unittest tests ($tweak_python)"
"$tweak_python" -B -m unittest discover -s tests -t . || status=1
exit $status

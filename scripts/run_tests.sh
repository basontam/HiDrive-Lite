#!/usr/bin/env bash
# HiDrive-Lite test command: syntax check + unit / API-contract / page-smoke tests.
#
#   scripts/run_tests.sh [extra pytest arguments]
#
# Creates .venv from requirements-dev.txt on first use.  Reports are written to
# build/test-artifacts/ (syntax.txt, junit.xml, pytest-output.txt). Nothing here
# reads production paths or contacts HDHive/115/OpenList.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  UV="$(command -v uv || true)"
  if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
  if [ -n "$UV" ]; then
    "$UV" venv .venv --python 3.11 --quiet
    "$UV" pip install --python "$PY" --quiet -r requirements-dev.txt
  else
    python3 -m venv .venv
    "$PY" -m pip install --quiet -r requirements-dev.txt
  fi
fi

# Keep the venv in sync with requirements-dev.txt on every run (no-op when satisfied).
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -n "$UV" ]; then
  "$UV" pip install --python "$PY" --quiet -r requirements-dev.txt
else
  "$PY" -m pip install --quiet -r requirements-dev.txt
fi

# This container's Chromium (used by tests/test_ui_screenshots.py) only
# launches with this shared-library dir on LD_LIBRARY_PATH -- see
# ~/.local/bin/with-chromium-env. ui_screenshots.py's _browser_env() applies
# the same default on its own, but exporting it here too means a plain
# `scripts/run_tests.sh` (no wrapper) still works.
CHROMIUM_LIB_DIR="${HIDRIVE_CHROMIUM_LIB_DIR:-$HOME/.local/chromium-deps/root/usr/lib/x86_64-linux-gnu}"
if [ -d "$CHROMIUM_LIB_DIR" ]; then
  export LD_LIBRARY_PATH="$CHROMIUM_LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

ART="$ROOT/build/test-artifacts"
rm -rf "$ART"
mkdir -p "$ART"

echo "== syntax check =="
"$PY" - app.py sync_openlist_115.py scripts/*.py <<'PYCHECK' | tee "$ART/syntax.txt"
import ast, sys
for name in sys.argv[1:]:
    with open(name, encoding="utf-8") as handle:
        ast.parse(handle.read(), name)
    print(f"{name}: OK")
print("syntax check OK")
PYCHECK

echo "== pytest =="
PYTEST_ARGS=("$@")
if [ "${HIDRIVE_RUN_SLOW:-0}" != "1" ]; then
  PYTEST_ARGS+=( -m "not slow" )
fi
"$PY" -m pytest --junitxml="$ART/junit.xml" "${PYTEST_ARGS[@]}" 2>&1 | tee "$ART/pytest-output.txt"

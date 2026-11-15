#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# sudo validation may have left root-owned caches beside the source files.
# Keep both compileall and packaging imports in a build-owned cache directory.
if [ -z "${PYTHONPYCACHEPREFIX:-}" ]; then
  build_cache=$(mktemp -d)
  trap 'rm -rf "$build_cache"' EXIT HUP INT TERM
  export PYTHONPYCACHEPREFIX="$build_cache"
fi
python -m compileall -q src
python -m pip wheel . --no-deps --no-build-isolation -w dist

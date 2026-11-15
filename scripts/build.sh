#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python -m compileall -q src
python -m pip wheel . --no-deps --no-build-isolation -w dist

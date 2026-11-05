#!/bin/sh
# Firmware host coverage gate (`AGENTS.md` Test Strategy): 80% line over the
# host-testable modules. Run from `firmware/test_host/`.
set -e
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../components" && pwd)
BUILD=$(dirname -- "$0")/build
PY=${PY:-$(dirname -- "$0")/../../.venv/bin/python}

"$PY" -m gcovr -r "$ROOT" --filter '\.\./components/' --filter 'components/' \
         --fail-under-line 80 "$BUILD"

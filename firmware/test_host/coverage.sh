#!/bin/sh
# Firmware host coverage gate (`AGENTS.md` Test Strategy): 98% line over the
# host-testable modules. Run from `firmware/test_host/` after a `-DCOVERAGE=ON`
# build.
#
# `--filter` keeps the report to `components/`: the ESP-IDF build tree and the
# unity sources are not production code and would drown the number.
#
# The build directory is named, not inferred: `gcovr` walks a directory tree, and
# a stale `build/` from another target leaves objects gcov cannot read, which
# aborts the whole report with "not a gcov notes file". `BUILD` is overridable so
# a worktree with a differently-named build directory can still use this gate.
set -e
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../components" && pwd)
BUILD=${BUILD:-$(dirname -- "$0")/build}
PY=${PY:-$(dirname -- "$0")/../../.venv/bin/python}

"$PY" -m gcovr -r "$ROOT" --filter '\.\./components/' --filter 'components/' \
         --gcov-ignore-errors=no_working_dir_found \
         --fail-under-line 98 "$BUILD"

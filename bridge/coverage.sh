#!/bin/sh
# Bridge coverage gate (`AGENTS.md` Test Strategy): 98% line and branch, per scope.
#
# One pytest run, then one check per scope, because a single global number would let
# a well-covered package hide a thin one. Run from the repo root.
#
# The gates are checked from `coverage.json` rather than `coverage report
# --fail-under`: that flag compares a single combined figure, so a scope at 100% line
# and 94% branch would pass a `98` branch gate. Line and branch are separate numbers
# in the table and are checked separately here.
set -e
PY=${PY:-.venv/bin/python}

"$PY" -m pytest bridge/tests --ignore=bridge/tests/e2e -p no:cacheprovider \
      --cov=bridge --cov-branch --cov-report=json --cov-fail-under=0

"$PY" - <<'PYCHECK'
import json
import sys

# `scope:line_gate:branch_gate`; a branch gate of `None` means line only.
SPECS = [
    ("bridge/protocol/", 100, 100),
    ("bridge/link/", 98, 98),
    ("bridge/client/", 98, 98),
    ("bridge/commands.py", 98, 98),
    ("bridge/ui/", 98, 98),
    ("bridge/tools/", 98, None),
]

with open("coverage.json", encoding="utf-8") as fh:
    data = json.load(fh)

failed = []
print(f"{'scope':22} {'line':>6} {'branch':>7}  gate")
for scope, line_gate, branch_gate in SPECS:
    files = {name: info for name, info in data["files"].items()
             if name == scope or name.startswith(scope)}
    if not files:
        failed.append(f"{scope}: no measured file (the scope is not imported by any test)")
        print(f"{scope:22} {'-':>6} {'-':>7}  {line_gate}%")
        continue
    total = sum(i["summary"]["num_statements"] for i in files.values())
    covered = sum(i["summary"]["covered_lines"] for i in files.values())
    branches = sum(i["summary"]["num_branches"] for i in files.values())
    taken = sum(i["summary"]["covered_branches"] for i in files.values())
    line = 100.0 * covered / total if total else 100.0
    branch = 100.0 * taken / branches if branches else 100.0
    ok = line >= line_gate and (branch_gate is None or branch >= branch_gate)
    gate = f"{line_gate}%" + ("" if branch_gate is None else f"/{branch_gate}% branch")
    print(f"{scope:22} {line:5.1f}% {branch:6.1f}%  {gate} {'ok' if ok else 'FAIL'}")
    if not ok:
        failed.append(
            f"{scope}: line {line:.1f}% (gate {line_gate}), "
            f"branch {branch:.1f}% (gate {branch_gate})")

if failed:
    print("\n" + "\n".join(failed), file=sys.stderr)
    sys.exit(1)
PYCHECK

"""`08-code-architecture.md` §1.1, asserted instead of remembered.

The bridge half of the arrangement laws already ships in `test_docs.py`: §6's two
import rules over `bridge/`. The firmware half did not exist, and §1.1 has called it
"checkable by grep in CI" since the component tree appeared. This is that check.

The scan reads `#include` lines rather than compiled symbol graphs, which is the same
granularity the law is written at: a module header is the module's public surface, so
including one is the dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# --- `08` §1.1: strictly lower layer, or an enumerated same-layer edge ---

LAYER = {
    "cfg": 0, "store": 0, "net": 0, "clock": 0, "sys": 0,
    "llm": 1, "skills": 1, "mcp": 1, "mqtt": 1, "geo": 1,
    "link": 2, "sched": 2, "tools": 2,
    "agent": 3,
}

# A component directory whose name is not the module's, because ESP-IDF already owns
# that name: `mqtt` is the `mqtt` module, living in `components/broker/` so its
# directory does not shadow IDF's own `mqtt` component (`02-firmware.md` §mqtt).
DIRECTORY_MODULE = {"broker": "mqtt"}

# Named, not inferred: five below L2 and the two L2 edges. A new one is a doc edit.
PERMITTED_SAME_LAYER = frozenset(
    {
        ("cfg", "store"),
        ("net", "cfg"),
        ("clock", "net"),
        ("clock", "cfg"),
        ("clock", "store"),
        ("sched", "link"),
        ("tools", "sched"),
    }
)

_INCLUDE = re.compile(r'^\s*#include\s+"([a-z_0-9]+)\.h"', re.MULTILINE)


def _firmware_edges() -> list[tuple[str, str, str]]:
    """(caller, callee, file) for every component-header include."""
    components = REPO / "firmware" / "components"
    if not components.is_dir():
        pytest.skip("firmware/components/ is not present")
    edges: list[tuple[str, str, str]] = []
    for module in sorted(components.iterdir()):
        if not module.is_dir():
            continue
        name = DIRECTORY_MODULE.get(module.name, module.name)
        if name not in LAYER:
            continue
        for path in sorted(list(module.rglob("*.c")) + list(module.rglob("*.h"))):
            for callee in _INCLUDE.findall(path.read_text(encoding="utf-8")):
                if callee in LAYER and callee != name:
                    edges.append((name, callee, str(path.relative_to(REPO))))
    return edges


def test_a_firmware_module_calls_only_lower_layers_or_a_named_edge() -> None:
    """The law is directional, so a cycle is an offence too: a higher layer reaching
    down is fine, the reverse is not, and same-layer needs its edge in the table."""
    offenders = []
    for caller, callee, where in _firmware_edges():
        above = LAYER[callee] > LAYER[caller]
        same_unlisted = (
            LAYER[callee] == LAYER[caller]
            and (caller, callee) not in PERMITTED_SAME_LAYER
        )
        if above or same_unlisted:
            offenders.append(f"{where}: {caller}(L{LAYER[caller]}) -> {callee}(L{LAYER[callee]})")
    assert offenders == []

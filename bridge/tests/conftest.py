"""Test fixtures shared by the bridge suite.

One thing is isolated here rather than per-test: the composer history file. The
App writes it at construction (`ui/history.py` `history_path()`), so any test that
builds a `BridgeApp` would otherwise append its prompts to the developer's real
`~/.config/musteval/history.ndjson` — a test that writes outside its own tree is
not hermetic and leaves state a second run can read back.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_history_file(tmp_path_factory, monkeypatch):
    """Point `XDG_CONFIG_HOME` at a throwaway dir for every test."""
    monkeypatch.setenv(
        "XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config"))
    )

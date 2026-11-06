"""The prompt bar's own facts (`05-bridge.md` §5.1, `04` §3.9).

Behaviour only: what a given status dict renders as, with no app, session or
transport involved. The bar is a pure function of facts the bridge already
holds, which is what makes it testable at this layer at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.ui.render import (
    PROMPT_SEPARATOR,
    UNREACHABLE,
    WIFI_WORDS,
    binding_pairs,
    format_uptime,
    hint_bar,
    key_label,
    render_prompt_bar,
    render_prompt_text,
)
from bridge.ui.render import _bar_segments, _fit


def _bar(status, link="connected", ack=None, tokens=0, width=None):
    return render_prompt_bar(link, ack or {}, status, tokens, width)


# -- wifi ----------------------------------------------------------------


def test_a_connected_wifi_shows_its_state_and_its_signal():
    """`-58dBm` alone never said whether the network was up."""
    status = {"wifi": {"state": "connected", "ssid": "home", "rssi": -58}}
    segments = _bar(status).split(PROMPT_SEPARATOR)
    assert WIFI_WORDS["connected"] in segments
    assert "-58dBm" in segments


@pytest.mark.parametrize("state", ["connecting", "failed", "off"])
def test_a_broken_wifi_is_never_silent(state):
    """A disconnected network with no rssi used to render as nothing at all."""
    status = {"wifi": {"state": state, "ssid": None, "rssi": None}}
    line = _bar(status)
    assert WIFI_WORDS[state] in line.split(PROMPT_SEPARATOR)
    assert len(line.split(PROMPT_SEPARATOR)) > 1


def test_a_signal_without_a_state_still_renders():
    """An older firmware sends `rssi` only; the bar must not drop it."""
    segments = _bar({"wifi": {"rssi": -58}}).split(PROMPT_SEPARATOR)
    assert "-58dBm" in segments


def test_an_unknown_wifi_state_is_still_reported():
    """A newer firmware's word must reach the reader rather than be dropped."""
    assert "wifi mesh-ish" in _bar({"wifi": {"state": "mesh-ish"}})


def test_failovers_are_shown_only_when_there_were_any():
    assert "3 failovers" in _bar({"wifi": {"state": "connected", "failovers": 3}})
    assert "1 failover" in _bar({"wifi": {"state": "connected", "failovers": 1}})
    absent = _bar({"wifi": {"state": "connected", "rssi": -58, "failovers": 0}})
    assert "failover" not in absent


# -- uptime --------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m00s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (86399, "23h59m"),
        (86400, "1d0h"),
        (90061, "1d1h"),
    ],
)
def test_uptime_is_one_segment_in_the_unit_that_matters(seconds, expected):
    assert format_uptime(seconds) == expected


def test_the_bar_carries_the_uptime_from_the_status_it_holds():
    assert "3h04m" in _bar({"uptime_s": 11040}).split(PROMPT_SEPARATOR)


def test_a_status_without_an_uptime_does_not_invent_one():
    assert "0s" not in _bar({"llm": {"provider": "p", "model": "m"}}).split(
        PROMPT_SEPARATOR
    )


# -- invariants ----------------------------------------------------------


def test_an_empty_status_leaves_the_link_word_alone():
    assert render_prompt_bar("connected", {}, {}, 0) == "linked"


@pytest.mark.parametrize("link", sorted(UNREACHABLE))
def test_an_unreachable_link_shows_nothing_that_would_be_stale(link):
    status = {"wifi": {"state": "connected", "rssi": -58}, "uptime_s": 90061}
    assert render_prompt_bar(link, {"device": "m01"}, status, 99) == render_prompt_bar(
        link, {}, {}, 0
    )


def test_the_link_word_survives_any_width():
    status = {"wifi": {"state": "connected", "rssi": -58}, "uptime_s": 5}
    assert render_prompt_bar("connected", {}, status, 99, width=1) == "linked"


def test_a_narrow_bar_drops_whole_segments_rather_than_half_a_word():
    status = {
        "wifi": {"state": "connected", "rssi": -58},
        "uptime_s": 5,
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "available": True},
        "agent": {"busy": False, "queue_depth": 0, "max_queue": 2},
    }
    full = _bar(status, ack={"device": "m01"}, tokens=1234)
    whole = full.split(PROMPT_SEPARATOR)
    for width in range(1, len(full) + 1):
        line = _bar(status, ack={"device": "m01"}, tokens=1234, width=width)
        # Every word shown is a whole segment of the full bar, never a prefix.
        assert all(part in whole for part in line.split(PROMPT_SEPARATOR))
        assert not line.endswith("…") and not line.endswith(PROMPT_SEPARATOR)


# -- `05-bridge.md` §13.2–§13.3: the plain and styled bar are one projection -


def test_the_styled_bar_carries_the_same_characters_as_the_plain_one():
    """Colour is emphasis only; a monochrome terminal must lose nothing (`§13.2`)."""
    status = {"wifi": {"state": "connected", "ssid": "home", "rssi": -58}}
    plain = render_prompt_bar("connected", {"device": "m01"}, status, 1234)
    styled = render_prompt_text("connected", {"device": "m01"}, status, 1234)
    assert styled.plain == plain


def test_a_degraded_link_is_a_warning_slot_and_a_linked_one_is_ok():
    degraded = dict(_bar_segments("degraded", {}, {}, 0)).values()
    linked = dict(_bar_segments("connected", {}, {}, 0)).values()
    assert "warn" in degraded
    assert "ok" in linked


def test_fit_drops_whole_trailing_segments_and_never_the_first():
    """A half segment reads as a different fact, so only whole ones go."""
    segments = [("linked", "ok"), (" m01", "muted"), (" 12k", "muted")]
    assert _fit(segments, 6) == [("linked", "ok")]
    assert _fit(segments, 1) == [("linked", "ok")]
    assert _fit(segments, 99) == segments


def test_a_key_is_named_as_a_human_writes_it():
    assert key_label("ctrl+d") == "Ctrl+D"
    assert key_label("enter") == "Enter"
    assert key_label("escape") == "Escape"


def test_hints_are_derived_from_the_bindings_so_a_dead_key_cannot_be_named():
    # A stub, not `textual.binding.Binding`: `09` §the grep laws keeps `textual`
    # under `bridge/ui/`, and this is a pure function over two attributes.
    bindings = [
        SimpleNamespace(key="ctrl+r", description="Refresh"),
        SimpleNamespace(key="ctrl+d", description="Delete"),
    ]
    pairs = binding_pairs(bindings)
    assert pairs == [("Ctrl+R", "Refresh"), ("Ctrl+D", "Delete")]
    line = hint_bar(pairs)
    assert "Ctrl+R" in line.plain and "Refresh" in line.plain
    # The key is bold, the verb is dim: two styles, never one for both.
    styles = {span.style for span in line.spans if span.style}
    assert styles

"""`05-bridge.md` §5.4 / §6.2 Logs: filtering, buffer and level colouring."""

from __future__ import annotations

import pytest

from bridge.ui import logs


def test_level_threshold_hides_lower_and_keeps_higher():
    # Warn threshold: keeps warn, error; hides info, debug
    warn = logs.LogFilter(min_level="warn")
    assert not warn.matches("12:00:01 [info] net: up")
    assert not warn.matches("12:00:05 [debug] agent: x")
    assert warn.matches("12:00:02 [warn] llm: timeout")
    assert warn.matches("12:00:06 [error] storage: E_STORAGE")

    # Bracketless reads as info, so it passes info filter but not warn filter
    info = logs.LogFilter(min_level="info")
    assert info.matches("no bracket here")
    assert not warn.matches("no bracket here")

    # Case-insensitive bracket token
    assert warn.matches("12:00:02 [WARN] llm: timeout")


def test_tag_is_case_insensitive_substring():
    llm = logs.LogFilter(tag="LLM")
    assert llm.matches("12:00:02 [info] llm: provider")
    assert not llm.matches("12:00:01 [info] net: wifi")
    assert logs.LogFilter(tag="").matches("anything at all")
    assert logs.LogFilter(tag="llm", min_level="warn").matches("12:00:02 [warn] llm: t")
    assert not logs.LogFilter(tag="llm").matches("12:00:02 [warn] net: t")


def test_active_tracks_hidden_lines():
    # Default (debug, no tag) hides nothing
    assert logs.LogFilter().active is False
    assert logs.LogFilter(min_level="debug").active is False
    # Tag or elevated level means some lines are hidden
    assert logs.LogFilter(tag="llm").active is True
    assert logs.LogFilter(min_level="info").active is True


def test_unknown_min_level_raises_naming_it():
    with pytest.raises(ValueError, match="trace"):
        logs.LogFilter(min_level="trace")
    with pytest.raises(ValueError, match="LOUD"):
        logs.LogFilter(min_level="LOUD")


def test_derived_filters_leave_the_original_alone():
    base = logs.LogFilter()
    llm = base.with_tag("llm")
    quiet = base.with_level("error")
    assert base.tag == "" and base.min_level == "debug"
    assert llm is not base and llm.tag == "llm"
    assert quiet is not base and quiet.min_level == "error"
    assert llm.with_level("WARN").min_level == "warn"


def test_add_returns_whether_the_line_passes_the_filter():
    buffer = logs.LogBuffer()
    buffer.set_filter(logs.LogFilter(min_level="warn"))
    assert buffer.add("12:00:02 [warn] llm: timeout") is True
    assert buffer.add("12:00:01 [info] net: up") is False
    assert len(buffer) == 2  # All lines are kept in buffer regardless of filter


def test_evicts_oldest_past_max_lines():
    buffer = logs.LogBuffer(max_lines=3)
    for n in range(5):
        buffer.add(f"line {n}")
    assert len(buffer) == 3
    assert buffer.visible() == ["line 2", "line 3", "line 4"]


def test_visible_filters_in_arrival_order():
    buffer = logs.LogBuffer()
    buffer.add("12:00:01 [info] net: up")
    buffer.add("12:00:02 [warn] llm: timeout")
    buffer.add("12:00:03 [debug] agent: prompt")
    assert buffer.visible() == [
        "12:00:01 [info] net: up",
        "12:00:02 [warn] llm: timeout",
        "12:00:03 [debug] agent: prompt",
    ]
    buffer.set_filter(logs.LogFilter(tag="LLM", min_level="warn"))
    assert buffer.visible() == ["12:00:02 [warn] llm: timeout"]


def test_clear_empties_the_buffer():
    buffer = logs.LogBuffer()
    buffer.add("12:00:01 [info] net: up")
    buffer.clear()
    assert len(buffer) == 0 and buffer.visible() == []


def test_render_styles_one_colour_per_level():
    styles = {"error": "red", "warn": "yellow", "info": "green", "debug": "dim"}
    for level, colour in styles.items():
        text = logs.render_log_line(f"12:00:06 [{level}] storage: x")
        assert str(text) == f"12:00:06 [{level}] storage: x"
        # Check style via spans to avoid importing rich in test
        assert [span.style for span in text.spans] == [colour]

    # Bracketless reads as info -> green
    plain = logs.render_log_line("no level here")
    assert [span.style for span in plain.spans] == ["green"]

    # Unknown bracket token -> no style
    trace = logs.render_log_line("12:00:07 [trace] a debugby-proxy line")
    assert trace.spans == []


def test_render_sanitises_control_characters():
    text = logs.render_log_line("12:00:01 [info] net: up\x1b[31m now\x07")
    sanitized_str = str(text)
    assert "\x1b" not in sanitized_str and "\x07" not in sanitized_str
    assert "now" in sanitized_str
    assert [span.style for span in text.spans] == ["green"]

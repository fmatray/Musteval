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
    context_blocks,
    context_summary,
    format_uptime,
    hint_bar,
    key_label,
    render_prompt_bar,
    reboot_line,
    reload_lines,
    render_prompt_text,
    todo_lines,
    tool_table,
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


def test_the_bar_names_the_thinking_level_beside_the_model_it_belongs_to():
    """`04` §3.9 and `05` §5.1: the level is a property of the model, so it rides in
    the model's segment. Both arms are one test because they are one rule: a level is
    named when the device reports one, and `null` reads as absent rather than as
    `p/m/None`, which would name a level no setting produced."""
    with_level = _bar(
        {"llm": {"provider": "ollama", "model": "gemma4:31b", "thinking": "max"}}
    ).split(PROMPT_SEPARATOR)
    assert "ollama/gemma4:31b/max" in with_level

    without = _bar(
        {"llm": {"provider": "p", "model": "m", "thinking": None}}
    ).split(PROMPT_SEPARATOR)
    assert "p/m" in without
    assert not [part for part in without if "None" in part]


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


# -- `17` §2–§3: the plain and styled bar are one projection ----


def test_the_styled_bar_carries_the_same_characters_as_the_plain_one():
    """Colour is emphasis only; a monochrome terminal must lose nothing (`17` §2)."""
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


from bridge.ui.render import geo_line, wifi_scan_lines, wifi_state_line

def test_wifi_scan_renders_networks():
    result = {
        "networks": [
            {"ssid": "HomeLab", "rssi": -47, "auth": "wpa2"},
            {"ssid": "Cafe-FREE", "rssi": -71, "auth": "open"},
            {"ssid": "OldRouter", "rssi": -83, "auth": "wep"},
        ],
        "truncated": False,
    }
    lines = wifi_scan_lines(result)
    assert len(lines) == 3
    for line in lines:
        assert "dBm" in line
    assert "HomeLab" in lines[0] and "wpa2" in lines[0]
    assert "OldRouter" in lines[2] and "wep" in lines[2]

def test_wifi_scan_renders_truncated():
    result = {"networks": [{"ssid": "A", "rssi": -50, "auth": "open"}], "truncated": True}
    lines = wifi_scan_lines(result)
    assert "truncated" in lines[-1]

def test_wifi_scan_renders_empty():
    assert wifi_scan_lines({"networks": []}) == ["no networks heard"]

def test_a_connected_wifi_state_line_names_the_network():
    line = wifi_state_line(
        {"state": "connected", "ssid": "home-2g", "rssi": -58, "ip": "192.168.1.42"}
    )
    assert "connected" in line and "home-2g" in line
    assert "-58" in line and "192.168.1.42" in line


@pytest.mark.parametrize("state", ["off", "connecting", "failed"])
def test_a_station_with_no_identity_reports_only_its_state(state):
    """A name slot holding `None` reads as a device fault instead of a link that is
    simply not up."""
    assert wifi_state_line({"state": state, "ssid": None, "rssi": None, "ip": None}) == (
        f"wifi {state}"
    )


def test_wifi_state_line_sanitises_ssid():
    line = wifi_state_line({"state": "connected", "ssid": "\x1b[31mHostile\x1b[0m"})
    assert "\x1b" not in line and "Hostile" in line


def test_wifi_scan_sanitises_ssid():
    result = {"networks": [{"ssid": "\x1b[31mHostile\x1b[0m", "rssi": -50, "auth": "open"}]}
    lines = wifi_scan_lines(result)
    assert "\x1b" not in lines[0]
    assert "Hostile" in lines[0]

    assert key_label("ctrl+d") == "Ctrl+D"
    assert key_label("enter") == "Enter"
    assert key_label("escape") == "Escape"


def test_todo_lines_distinguish_the_five_states():
    """`05` §7: one line per task, every state named. Two tasks in one phase must
    not collapse into one line: `done` on a task is the whole point of the command."""
    result = {
        "phases": [
            {
                "name": "auth",
                "tasks": [
                    {"content": "wire jwt", "status": "completed"},
                    {"content": "rotate key", "status": "in_progress"},
                    {"content": "audit", "status": "pending"},
                    {"content": "rewrite", "status": "abandoned"},
                ],
            }
        ]
    }
    lines = todo_lines(result)
    assert lines[0] == "auth"
    # The status is the last field; the mark is the first. A padded content column
    # must not swallow either.
    statuses = [line.rsplit("  ", 1)[1].strip() for line in lines[1:]]
    assert statuses == ["completed", "in_progress", "pending", "abandoned"]
    # The mark is a fixed three-character field; `[ ]` contains a space, so splitting
    # on whitespace would read it as `[` and quietly pass for the wrong reason.
    marks = [line.lstrip()[:3] for line in lines[1:]]
    assert marks == ["[x]", "[>]", "[ ]", "[-]"]


def test_todo_lines_show_a_blocker_and_sanitise_every_payload():
    result = {
        "phases": [
            {
                "name": "\x1b[31mship\x1b[0m",
                "tasks": [
                    {
                        "content": "\x1b[31mtag\x1b[0m",
                        "status": "blocked",
                        "blocker": "\x1b[31mci\x1b[0m",
                    }
                ],
            }
        ]
    }
    lines = todo_lines(result)
    assert not any("\x1b" in line for line in lines)
    assert lines[1].startswith("  [!] [31mtag")
    assert "([31mci[0m)" in lines[1]


def test_todo_lines_report_an_empty_list_rather_than_nothing():
    """A fresh boot has no list (`tools/todo.md` §State). Silence would read as a
    render fault, so `/todo` says so in one line."""
    assert todo_lines({"phases": []}) == ["no todos"]
    assert todo_lines({}) == ["no todos"]


def test_todo_lines_show_a_phase_that_holds_no_tasks():
    """`tools/todo.md` §Operations `init`: a phase may be empty, which is how the
    editor adds a phase before it has anything in it. Printing nothing for it would
    make a plan the reader just created look like a list that failed to load."""
    assert todo_lines({"phases": [{"name": "Plan", "tasks": []}]}) == ["Plan"]


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


def test_a_geo_line_names_the_fix_provider_and_age():
    """`04` §4.13: all four members are what the reader needs to judge a fix."""
    line = geo_line({"lat": 52.3676, "lon": 4.9041, "accuracy_m": 65,
                     "provider": "home_wifi", "age_s": 42})
    assert "52.3676, 4.9041" in line and "+/-65m" in line
    assert "home_wifi" in line and "age 42s" in line


def test_a_geo_line_omits_a_provider_and_age_it_was_not_given():
    """`04` §4.13 makes both optional on a fresh fix; printing `None` where a name
    belongs reads as a device fault."""
    line = geo_line({"lat": 1.5, "lon": -2.5, "accuracy_m": 10})
    assert "None" not in line and "via" not in line and "age" not in line


def test_geo_line_sanitises_the_provider_id():
    """The id is a config-file string, so it is as untrusted as an ssid."""
    line = geo_line({"lat": 1.0, "lon": 2.0, "accuracy_m": 5,
                     "provider": "\x1b[31mHostile\x1b[0m"})
    assert "\x1b" not in line and "Hostile" in line


def test_a_geo_line_names_the_place_and_locale_the_provider_returned():
    """`04` §4.13: the place and locale members are the provider's own words, and
    they answer the question a fix alone leaves open: where this device is."""
    line = geo_line({
        "lat": 48.8534, "lon": 2.3488, "accuracy_m": 5000,
        "provider": "ip_fallback", "age_s": 0,
        "city": "Paris", "region": "Ile-de-France", "region_code": "IDF",
        "country": "France", "country_code": "FR", "postal": "75001",
        "country_calling_code": "+33",
        "timezone": "Europe/Paris", "utc_offset": "+01:00",
        "currency": "EUR", "currency_name": "Euro",
        "languages": "fr-FR,frp,br",
    })
    assert "Paris" in line and "Ile-de-France (IDF)" in line
    assert "France [FR]" in line and "75001" in line
    assert "+33" in line
    assert "Europe/Paris +01:00" in line
    assert "EUR Euro" in line and "fr-FR,frp,br" in line
    # One result is one chat line: the caller writes the return as a single row.
    assert "\n" not in line


def test_a_geo_line_omits_the_locale_the_live_provider_does_not_send():
    """The fallback provider answers no `currency`, `currency_name` or `languages`,
    so that shape is normal, not a fault: no `None`, no empty bracket, no separator
    left with nothing after it."""
    line = geo_line({
        "lat": 48.8534, "lon": 2.3488, "accuracy_m": 5000,
        "provider": "ip_fallback", "age_s": 0,
        "city": "Paris", "region": "Ile-de-France", "region_code": "IDF",
        "country": "France", "country_code": "FR", "country_code_iso3": "FRA",
        "country_capital": "Paris", "country_tld": ".fr", "continent_code": "EU",
        "in_eu": True, "postal": "75001", "country_calling_code": "+33",
        "timezone": "Europe/Paris", "utc_offset": "+01:00",
    })
    assert "Paris, Ile-de-France (IDF), France [FR]" in line
    assert "Europe/Paris +01:00" in line
    assert "None" not in line and "()" not in line and "[]" not in line
    assert all(part.strip() for part in line.split(" · "))
    assert not line.endswith(("·", ",", " "))


def test_geo_line_sanitises_every_place_string_the_provider_supplies():
    """`06` §6.4: a city and a timezone are third-party strings, so an escape
    sequence in either cannot reach this terminal. The words around it survive."""
    line = geo_line({
        "lat": 1.0, "lon": 2.0, "accuracy_m": 5,
        "city": "\x1b[31mParis\x1b[0m",
        "region": "\x1b[2JIle-de-France",
        "country": "\x1b[1mFrance",
        "timezone": "\x1b[31mEurope/Paris\x1b[0m",
        "currency_name": "\x1b[5mEuro",
        "languages": "\x1b[0mfr-FR",
    })
    assert "\x1b" not in line
    assert "Paris" in line and "Ile-de-France" in line and "France" in line
    assert "Europe/Paris" in line and "Euro" in line and "fr-FR" in line


def test_a_geo_line_marks_eu_only_when_the_provider_sent_a_bool():
    """`in_eu` is a boolean: `bool` subclasses `int`, so an `isinstance(…, int)`
    check would print a marker for a value the provider never claimed."""
    base = {"lat": 1.0, "lon": 2.0, "accuracy_m": 5, "city": "Paris"}
    assert "EU" in geo_line({**base, "in_eu": True})
    assert "EU" not in geo_line(base)
    assert "EU" not in geo_line({**base, "in_eu": 1})


# -- `/tools`, `/sys reload`, `/sys reboot` ---------------------------------


def test_tool_table_is_one_row_per_tool_in_name_order_with_three_columns():
    """`04` §4.29: the description is what makes this frame worth a round trip, and a
    disabled row is the reason it lists names the offer set omits — so the state sits
    between the name and the prose, where it survives a narrow terminal."""

    lines = tool_table(
        {
            "tools": [
                {"name": "time.now", "description": "Return the current time.",
                 "offered": True},
                {"name": "geo.locate", "description": "Return the position.",
                 "offered": False},
            ]
        }
    )
    assert lines == [
        "  name        state     description",
        "  geo.locate  disabled  Return the position.",
        "  time.now    enabled   Return the current time.",
    ]


def test_tool_table_sanitises_a_server_written_description():
    """An MCP server writes its own description, so the table carries device text."""
    header, line = tool_table(
        {"tools": [{"name": "mcp.x", "description": "a\x1bb", "offered": True}]}
    )
    assert "\x1b" not in line and "\x1b" not in header


def test_tool_table_headers_its_columns():
    """`05` §7.4: three unlabelled columns read as prose. The labels are also the
    width floor, so a catalogue of short names still lines up under them."""

    lines = tool_table({"tools": [{"name": "a", "description": "d", "offered": True}]})
    assert lines == ["  name  state    description", "  a     enabled  d"]


def test_tool_table_says_so_when_the_catalogue_is_empty():
    assert tool_table({"tools": []}) == ["no tools"]


def test_reload_lines_give_each_warning_its_own_line():
    """`04` §4.28: the warnings are the only part a reader acts on."""
    assert reload_lines(
        {"reloaded": ["cfg", "tools"],
         "warnings": [{"scope": "mqtt", "message": "broker unreachable"}]}
    ) == ["reloaded: cfg, tools", "  warning (mqtt): broker unreachable"]


def test_reload_lines_say_when_there_was_nothing_to_report():
    """A clean reload and a reply that carried no `warnings` field read differently to
    a reader deciding whether to fix something, so the quiet case says so."""
    assert reload_lines({"reloaded": [], "warnings": []}) == [
        "reloaded: nothing",
        "  no warnings",
    ]


def test_reboot_line_names_the_grace_period():
    assert reboot_line({"grace_ms": 250}) == "rebooting in 250 ms"
    assert reboot_line({}) == "rebooting"


def test_context_blocks_name_every_block_in_the_documents_order() -> None:
    """`04` §4.30's eight blocks, all eight offered as rows.

    A missing block reads as one the device cannot report, and a reader checking
    "where did my budget go" cannot tell a zero from an omission, so `thinking` must
    appear with its measured 0 rather than being filtered out of the list.
    """
    counts = {
        "skills": 3,
        "tools": 18,
        "user": 2,
        "assistant": 1,
        "thinking": 0,
        "tool_calls": 4,
        "tool_output": 4,
    }
    # A device reply carries all eight: a measured zero and an absent block are
    # different answers, and only the second is an em dash.
    blocks = {
        "system_prompt": 4096, "skills": 0, "tools": 1638, "user": 40,
        "assistant": 0, "thinking": 0, "tool_calls": 0, "tool_output": 0,
    }
    rows = context_blocks({"budget": 16384, "counts": counts, "blocks": blocks})
    assert [key for key, *_ in rows] == [
        "system_prompt", "skills", "tools", "user", "assistant", "thinking",
        "tool_calls", "tool_output",
    ]
    assert [label for _, label, *_ in rows] == [
        "system prompt", "skills", "tools", "user messages", "assistant messages",
        "assistant thinking", "tool calls", "tool outputs",
    ]
    # The counter is the item count, and `thinking`'s zero is a measurement rather
    # than an omission: the trace never enters the history.
    assert [counter for *_, counter in rows] == [
        "—", "3", "18", "2", "1", "0", "4", "4"
    ]
    # The share is the block's byte share of the budget, so "what can I drop" and
    # "what is in there" are answered from the same row.
    assert [share for _, _, _, share, _ in rows] == [
        "25%", "0%", "9%", "0%", "0%", "0%", "0%", "0%"
    ]
    # The size column is the block's measured bytes, so the four columns of one row
    # are that block's answer and a reader never has to line up two lists.
    assert [size for _, _, size, _, _ in rows] == [
        "4096", "0", "1638", "40", "0", "0", "0", "0"
    ]


def test_context_blocks_show_an_em_dash_for_a_block_with_no_measurement() -> None:
    """`system_prompt` is one document, so the device sends no counter for it, and a
    reply with no `budget` makes no share measurable.

    A zero would be an answer and would tell a reader there is no prompt, or that a
    block takes none of the budget.
    """
    rows = context_blocks({"counts": {"user": 1}, "blocks": {"user": 10}})
    prompt = next(row for row in rows if row[0] == "system_prompt")
    assert prompt[4] == "—"
    # No budget in the reply: no share is measurable, which is not "0%".
    assert prompt[3] == "—"
    user = next(row for row in rows if row[0] == "user")
    assert (user[2], user[4]) == ("10", "1")
    assert user[3] == "—"


def test_context_blocks_survive_a_reply_that_omits_the_counts() -> None:
    """A malformed or older reply lists the blocks with no numbers, not nothing."""
    rows = context_blocks({})
    assert len(rows) == 8
    assert {counter for *_, counter in rows} == {"—"}
    assert {share for _, _, _, share, _ in rows} == {"—"}
    assert {size for _, _, size, _, _ in rows} == {"—"}


def test_context_blocks_treat_a_non_numeric_measurement_as_unmeasured() -> None:
    """`17` §4: a value the device chose is data. A string in a numeric field is the
    device's reply, not the bridge's to crash on — the compositor is behind this
    function, so a `TypeError` here blanks every screen the modal is opened over.
    """
    rows = context_blocks(
        {"budget": "many", "blocks": {"skills": "big", "tools": None},
         "counts": {"skills": "lots", "tools": 18}}
    )
    by_key = {key: (size, share, counter) for key, _, size, share, counter in rows}
    # An unmeasurable size has no share, and an unmeasurable count is not a zero.
    assert by_key["skills"] == ("—", "—", "—")
    # No usable budget means no share, even for a block whose size is known: a share of
    # zero is a claim about a budget the reply did not give. A `null` size is unmeasured,
    # which is not the same as a measured zero.
    assert by_key["tools"] == ("—", "—", "18")
    assert by_key["system_prompt"] == ("—", "—", "—")


def test_context_summary_reads_the_frames_own_totals() -> None:
    """`used` and `budget` are the frame's numbers, not a sum of the blocks.

    A reader comparing them is asking how close the next prompt is to being refused,
    which is a different question from which block is largest.
    """
    assert context_summary(
        {"budget": 16384, "used": 5000, "free": 11384, "messages": 3}
    ) == "used 5000 of 16384 B, 11384 B free · 3 messages in the history"


def test_context_summary_treats_a_non_numeric_total_as_zero() -> None:
    """Same trust boundary as the blocks: `"many"` is a reply this function must
    render, not one it must raise on."""
    assert context_summary({"budget": "many", "used": 5000, "free": 11384}) == (
        "used 0 of 0 B, 0 B free · 0 messages in the history"
    )


def test_context_summary_calls_one_message_singular() -> None:
    assert context_summary(
        {"budget": 100, "used": 1, "free": 99, "messages": 1}
    ).endswith("1 message in the history")


def test_context_summary_survives_a_reply_that_omits_the_totals() -> None:
    assert context_summary({}) == "used 0 of 0 B, 0 B free · 0 messages in the history"

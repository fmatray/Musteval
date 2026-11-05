"""L3 ui: the splash art and the `/onboard` planner.

`05-bridge.md` §5.9 and §7. Both halves have a rule that drifts silently: a tip
that names a command the catalogue dropped, and a `config.set` that names an NVS
key written after it. Each gets a test.
"""

from __future__ import annotations

import random

import pytest

from bridge.commands import LOCAL_COMMANDS
from bridge.ui.help import _NAMES
from bridge.ui.onboard import Answers, narration, provider_id, writes
from bridge.ui.splash import (
    MASCOT,
    SPLASH_SECONDS,
    TIPS,
    WORDMARK,
    pick_tip,
    render_splash,
)


# -- the art ------------------------------------------------------------


def test_neither_block_drifts_as_the_panel_draws():
    """`05` §5.9: a row of another width shifts everything under it.

    Asserted on the rendered panel, not on the literals: the literals are stored
    unpadded, so their widths say nothing about what reaches the screen.
    """
    lines = render_splash(TIPS[0]).plain.splitlines()
    for block in (WORDMARK, MASCOT):
        start = next(i for i, line in enumerate(lines) if line.startswith(block[0]))
        widths = {len(line) for line in lines[start : start + len(block)]}
        assert widths == {max(len(row) for row in block)}


def test_the_art_survives_rendering_character_for_character():
    """`from_markup` silently drops or eats glyphs; the panel is built per char."""
    body = render_splash(TIPS[0]).plain.splitlines()
    start = next(i for i, line in enumerate(body) if line.startswith(MASCOT[0]))
    for index, row in enumerate(MASCOT):
        assert body[start + index].startswith(row)


def test_splash_shows_the_mascot_and_the_tip():
    """Identity is the wordmark plus the art; a panel missing either is a bug."""
    tip = TIPS[0]
    body = render_splash(tip).plain
    for row in WORDMARK:
        assert row.rstrip() in body
    for row in MASCOT:
        assert row.rstrip() in body
    assert tip.split()[0] in body





def test_every_tip_names_a_command_that_exists():
    """Drift is a test, not a habit: a tip naming a dropped command lies."""
    for tip in TIPS:
        for word in tip.split():
            if word.startswith("/"):
                head = word[1:].split()[0]
                assert head in _NAMES, f"{tip!r} names /{head}"


def test_pick_tip_is_reproducible_from_a_seed():
    assert pick_tip(random.Random(7)) == pick_tip(random.Random(7))


def test_pick_tip_returns_a_listed_tip():
    assert pick_tip(random.Random(1)) in TIPS


def test_splash_timer_is_long_enough_to_read_a_tip():
    assert SPLASH_SECONDS >= 5


def test_no_tip_makes_the_panel_wider_than_the_terminal():
    """A line over the budget soft-wraps and drops to column 0, breaking the art."""
    for tip in TIPS:
        for line in render_splash(tip).plain.splitlines():
            assert len(line) <= 78, line


# -- the planner --------------------------------------------------------


FULL = Answers(
    ssid="home",
    password="hunter2",
    base_url="https://api.openai.com/v1",
    api_key="sk-test",
    model="gpt-4o-mini",
)


def test_every_secret_is_written_before_the_config_that_names_it():
    """`03` rule 9 rejects an `nvs:` reference to a key that does not exist."""
    plan = writes(FULL)
    secrets = {body["key"] for kind, body in plan if kind == "secret"}
    written: set[str] = set()
    for kind, body in plan:
        if kind == "secret":
            written.add(body["key"])
            continue
        for value in _strings(body):
            if value.startswith("nvs:"):
                assert value[4:] in written, f"{body} names a key not yet stored"
    assert secrets == {"wifi_psk", "llm_api_key"}


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_the_arrays_are_written_whole_so_a_missing_index_cannot_fail():
    """`04` §4.9 does not promise that `networks.0.ssid` creates the array."""
    paths = [next(iter(body)) for kind, body in writes(FULL) if kind == "config"]
    assert "wifi.networks" in paths
    assert "llm.providers" in paths
    assert not any(path.endswith((".ssid", ".psk_ref", ".id")) for path in paths)


def test_an_empty_answer_writes_nothing_for_that_step():
    plan = writes(Answers(ssid="", password="", base_url="", api_key="", model=""))
    assert plan == []


def test_skipping_the_password_drops_the_network_write():
    """`password_ref` is required, so a network without a key is not writable."""
    answers = Answers(ssid="home", password="", base_url="", api_key="", model="")
    assert writes(answers) == []


def test_skipping_the_api_key_drops_the_provider_write():
    """`api_key_ref` is required, so a provider without a key is not writable."""
    answers = Answers(ssid="", password="", base_url="https://api.openai.com/v1",
                      api_key="", model="gpt")
    assert writes(answers) == []


def test_provider_id_comes_from_the_host():
    assert provider_id("https://api.openai.com/v1") == "openai"
    assert provider_id("http://10.0.0.5:8080/v1") == "custom"
    assert provider_id("not a url") == "custom"


def test_provider_id_fits_the_config_schema():
    """`03` §Validation: `[a-z0-9_-]{1,16}`."""
    import re

    for url in ("https://api.openai.com/v1", "https://generativelanguage.googleapis.com"):
        assert re.fullmatch(r"[a-z0-9_-]{1,16}", provider_id(url))


def test_narration_names_what_it_replaces():
    lines = narration(FULL, {"wifi.networks": [1, 2, 3]})
    assert "replaces wifi.networks (3 entries → 1)" in lines
    assert "creates llm.providers (1 entry)" in lines


def test_narration_says_creates_when_there_is_no_array_yet():
    lines = narration(FULL, {})
    assert "creates wifi.networks (1 entry)" in lines


def test_onboard_is_a_local_command_so_it_sends_no_frame():
    assert "onboard" in LOCAL_COMMANDS

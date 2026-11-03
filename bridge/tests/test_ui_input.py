"""L3 ui: composer history, Tab acceptance and the `/settings` screen.

`05-bridge.md` §5.1 and §5.7. These assert what a user sees, driven through the
pilot: a recalled prompt lands in the field, Tab fills the completion, and a
setting edit travels as a coerced `config.set`.
"""

from __future__ import annotations

import json

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode
from bridge.ui import BridgeApp
from bridge.ui.app import BOUNDS_CLIP, ChatScreen
from bridge.ui.history import MAX_ENTRIES, PromptHistory
from bridge.ui.modals import ValuePrompt
from bridge.ui.settings import ROWS, coerce, flatten, lookup, render_value

from .test_ui import _reply

CONFIG = {
    "device": {"name": "musteval", "log_level": "info"},
    "wifi": {"connect_timeout_ms": 15000, "retry_backoff_ms": [1000, 2000]},
    "tools": {"http_allow_hosts": []},
}


def _reply_with_config(line: bytes) -> list[bytes]:
    frame = json.loads(line)
    if frame["type"] == "command" and frame["name"] == "config.get":
        return [
            encode(
                Frame(
                    type="command.result",
                    id="d-cfg",
                    ts=0,
                    body={"name": "config.get", "ok": True,
                          "result": {"config": CONFIG}, "duration_ms": 1},
                    reply_to=frame["id"],
                )
            )
        ]
    if frame["type"] == "command" and frame["name"] == "config.set":
        return [
            encode(
                Frame(
                    type="command.result",
                    id="d-set",
                    ts=0,
                    body={"name": "config.set", "ok": True,
                          "result": {"path": frame["args"]["path"], "applied": True,
                                     "requires_reboot": False},
                          "duration_ms": 1},
                    reply_to=frame["id"],
                )
            )
        ]
    return _reply(line)


async def _connected(pilot) -> None:
    for _ in range(60):
        if pilot.app.session.state.value == "connected":
            break
        await pilot.pause(0.02)
    await pilot.pause(0.15)


def _log(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)


# -- history: the model --------------------------------------------------


def test_history_walks_back_then_returns_the_empty_draft():
    history = PromptHistory()
    history.add("one")
    history.add("two")
    assert [history.previous(""), history.previous("")] == ["two", "one"]
    assert [history.next(""), history.next("")] == ["two", ""]


def test_history_keeps_a_typed_draft_across_a_walk():
    history = PromptHistory()
    history.add("old")
    assert history.previous("half typed") == "old"
    assert history.next("old") == "half typed"


def test_history_is_bounded_and_ignores_blank_lines():
    history = PromptHistory(max_entries=3)
    for text in ("a", "b", "c", "d"):
        history.add(text)
    history.add("   ")
    assert len(history) == 3
    assert history.previous("") == "d"


def test_history_collapses_only_adjacent_duplicates():
    history = PromptHistory()
    history.add("a")
    history.add("a")
    history.add("b")
    history.add("a")
    assert len(history) == 3
    assert MAX_ENTRIES == 100


# -- history and Tab: the composer --------------------------------------


async def test_up_recalls_the_last_prompt_and_down_returns_to_empty():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        for text in ("first prompt", "second prompt"):
            await pilot.press(*text)
            await pilot.press("enter")
            await pilot.pause(0.15)
        assert field.value == ""
        await pilot.press("up")
        await pilot.pause(0.1)
        assert field.value == "second prompt"
        await pilot.press("up")
        await pilot.pause(0.1)
        assert field.value == "first prompt"
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause(0.1)
        assert field.value == ""
        await session.close()


async def test_tab_accepts_the_completion_instead_of_moving_focus():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        await pilot.press("/", "s", "t", "a")
        await pilot.pause(0.2)
        assert field._suggestion == "/status"
        await pilot.press("tab")
        await pilot.pause(0.15)
        assert field.value == "/status"
        assert app.focused is field
        await session.close()


def test_tab_is_bound_on_the_chat_screen_not_the_app():
    """Tab must stay scoped to the composer, or every screen loses focus traversal."""
    assert "tab" in {binding.key for binding in ChatScreen.BINDINGS}
    assert "tab" not in {binding.key for binding in BridgeApp.BINDINGS}


# -- settings: model -----------------------------------------------------


def test_every_row_round_trips_its_default():
    for row in ROWS:
        assert coerce(row, render_value(row, row.default)) == row.default, row.path


def test_coerce_rejects_what_the_bounds_forbid():
    row = next(candidate for candidate in ROWS if candidate.path == "wifi.connect_timeout_ms")
    with pytest.raises(ValueError, match="wifi.connect_timeout_ms"):
        coerce(row, "999")
    with pytest.raises(ValueError):
        coerce(row, "abc")
    enum_row = next(candidate for candidate in ROWS if candidate.path == "device.log_level")
    with pytest.raises(ValueError):
        coerce(enum_row, "verbose")


def test_flatten_resolves_a_dotted_path_and_an_array_key():
    flat = flatten(CONFIG)
    assert lookup(flat, "device.log_level") == "info"
    assert lookup(flat, "tools.http_allow_hosts[]") == []
    assert lookup(flat, "wifi.retry_backoff_ms") == [1000, 2000]


async def test_the_settings_hint_and_bounds_survive_a_real_render():
    """Regression: `[Enter]` was eaten as markup and a 132-char bound blew the table."""
    transport = FakeTransport(on_write=_reply_with_config)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(130, 44)) as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").focus()
        await pilot.press(*"/settings")
        await pilot.press("enter")
        for _ in range(80):
            if app.query_one("#settings-table").row_count:
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        hint = app.query_one("#settings-hint").render()
        assert "Enter" in str(hint)
        table = app.query_one("#settings-table")
        widest = max(len(row.bound) for row in ROWS)
        assert widest > BOUNDS_CLIP, "the width test is pointless if no bound is long"
        bounds_column = table.columns[list(table.columns)[-1]]
        assert bounds_column.width == BOUNDS_CLIP
        await session.close()


# -- settings: the screen ------------------------------------------------


async def test_settings_screen_renders_every_row_from_the_device_config():
    transport = FakeTransport(on_write=_reply_with_config)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").focus()
        await pilot.press(*"/settings")
        await pilot.press("enter")
        for _ in range(80):
            if app.query_one("#settings-table").row_count:
                break
            await pilot.pause(0.02)
        table = app.query_one("#settings-table")
        assert table.row_count == len(ROWS)
        assert any(b'"name":"config.get"' in line for line in transport.written)
        await session.close()


async def test_editing_a_row_sends_a_coerced_config_set():
    transport = FakeTransport(on_write=_reply_with_config)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").focus()
        await pilot.press(*"/settings")
        await pilot.press("enter")
        for _ in range(80):
            if app.query_one("#settings-table").row_count:
                break
            await pilot.pause(0.02)
        table = app.query_one("#settings-table")
        table.focus()
        table.move_cursor(row=next(i for i, row in enumerate(ROWS) if row.path == "device.log_level"))
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(app.screen, ValuePrompt):
                break
            await pilot.pause(0.02)
        assert app.screen.query_one("#value-input").value == "info"
        app.screen.query_one("#value-input").value = "debug"
        await pilot.press("enter")
        for _ in range(80):
            if any(b'"name":"config.set"' in line for line in transport.written):
                break
            await pilot.pause(0.02)
        sent = [
            json.loads(line)["args"]
            for line in transport.written
            if b'"name":"config.set"' in line
        ]
        assert sent == [{"path": "device.log_level", "value": "debug"}]
        await session.close()


async def test_an_out_of_bounds_value_never_reaches_the_wire():
    transport = FakeTransport(on_write=_reply_with_config)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").focus()
        await pilot.press(*"/settings")
        await pilot.press("enter")
        for _ in range(80):
            if app.query_one("#settings-table").row_count:
                break
            await pilot.pause(0.02)
        table = app.query_one("#settings-table")
        table.focus()
        table.move_cursor(row=next(i for i, row in enumerate(ROWS) if row.path == "wifi.connect_timeout_ms"))
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(app.screen, ValuePrompt):
                break
            await pilot.pause(0.02)
        app.screen.query_one("#value-input").value = "999"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert not any(b'"name":"config.set"' in line for line in transport.written)
        assert "wifi.connect_timeout_ms" in _log(app)
        await session.close()


async def test_settings_escape_cancels_the_value_prompt():
    transport = FakeTransport(on_write=_reply_with_config)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.edit_setting("device.name"))
        for _ in range(80):
            if isinstance(app.screen, ValuePrompt):
                break
            await pilot.pause(0.02)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not any(b'"name":"config.set"' in line for line in transport.written)
        await session.close()

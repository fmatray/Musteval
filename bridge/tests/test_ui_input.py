"""L3 ui: composer history, Tab acceptance and the `/settings` screen.

`05-bridge.md` §5.1 and §5.7. These assert what a user sees, driven through the
pilot: a recalled prompt lands in the field, Tab fills the completion, and a
setting edit travels as a coerced `config.set`.
"""

from __future__ import annotations

import json

import pytest

from bridge.client import LinkState, Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode
from bridge.ui import BridgeApp
from bridge.ui.modals import HelpModal
from bridge.ui.app import BOUNDS_CLIP, COMPLETION_ROWS, COMPOSER_MAX_ROWS, ChatScreen
from bridge.ui.render import LINK_WORDS, clip
from bridge.ui.history import MAX_ENTRIES, PromptHistory, history_path
from bridge.ui.testing import chat_lines
from bridge.ui.modals import ConfirmPrompt, FilterPrompt, ValuePrompt
from bridge.ui.task_prompt import TaskPrompt
from bridge.ui.settings import (
    ROWS,
    coerce,
    flatten,
    lookup,
    render_value,
    rows_for,
    sections,
)

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


def test_history_persistence_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    PromptHistory()
    assert not (tmp_path / "musteval").exists()


def test_history_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "h.ndjson")
    h1 = PromptHistory(path=path)
    h1.add("first")
    h1.add("second")
    h2 = PromptHistory(path=path)
    assert h2.previous("") == "second"


def test_history_persistence_missing_file_starts_empty(tmp_path):
    path = str(tmp_path / "missing.ndjson")
    h = PromptHistory(path=path)
    assert len(h) == 0
    assert not (tmp_path / "missing.ndjson").exists()


def test_history_persistence_creates_nested_parent_directory(tmp_path):
    path = str(tmp_path / "a" / "b" / "h.ndjson")
    h = PromptHistory(path=path)
    h.add("test")
    assert (tmp_path / "a" / "b" / "h.ndjson").exists()


def test_history_persistence_load_caps_to_newest_entries(tmp_path):
    path = tmp_path / "h.ndjson"
    path.write_text("\n".join(json.dumps(f"e{i}") for i in range(5)))
    h = PromptHistory(max_entries=2, path=str(path))
    assert len(h) == 2
    assert h.previous("") == "e4"


def test_history_persistence_skips_corrupt_lines(tmp_path):
    path = tmp_path / "h.ndjson"
    path.write_text(f'{json.dumps("good1")}\nNOT JSON\n{json.dumps("good2")}')
    h = PromptHistory(path=str(path))
    assert len(h) == 2
    assert h.previous("") == "good2"


def test_history_persistence_skips_non_string_values(tmp_path):
    path = tmp_path / "h.ndjson"
    path.write_text(f'{json.dumps("good")}\n{json.dumps(123)}\n{json.dumps(None)}')
    h = PromptHistory(path=str(path))
    assert len(h) == 1
    assert h.previous("") == "good"


def test_history_persistence_skips_blank_lines(tmp_path):
    path = tmp_path / "h.ndjson"
    path.write_text(f'{json.dumps("good")}\n\n  \n{json.dumps("next")}')
    h = PromptHistory(path=str(path))
    assert len(h) == 2
    assert h.previous("") == "next"


def test_history_persistence_round_trips_leading_slash(tmp_path):
    path = str(tmp_path / "h.ndjson")
    h = PromptHistory(path=path)
    h.add("/status")
    h2 = PromptHistory(path=path)
    assert h2.previous("") == "/status"


def test_history_persistence_swallows_unwritable_path(tmp_path):
    # Path parent is a file
    file_path = tmp_path / "h.ndjson"
    file_path.write_text("content")
    path = str(file_path / "x")
    h = PromptHistory(path=path)
    h.add("test")  # should not raise


def test_history_path_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert history_path() == str(tmp_path / "musteval" / "history.ndjson")


def test_a_history_file_that_is_not_utf8_does_not_stop_the_bridge(tmp_path):
    path = tmp_path / "h.ndjson"
    path.write_bytes(b'"kept"\n\xff\xfe not utf8\n')
    history = PromptHistory(path=str(path))
    assert history.previous("") == "kept"


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


async def test_tab_leaves_the_composer_where_it_is():
    """Regression: Tab bounced focus to the log, where typing is silently lost."""
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(120, 40)) as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        for _ in range(4):
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert app.focused is field
        # A prompt typed after Tab still reaches the composer.
        await pilot.press("h", "i")
        await pilot.pause(0.1)
        assert field.value == "hi"
        await session.close()


async def test_a_screen_takes_focus_where_its_keys_are_needed():
    """Regression: `/settings` appeared with focus on the chat log and ignored Enter."""
    session = Session(FakeTransport(on_write=_reply_with_config), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(120, 40)) as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").focus()
        await pilot.press(*"/settings")
        await pilot.press("enter")
        for _ in range(80):
            if app.query_one("#settings-table").row_count:
                break
            await pilot.pause(0.02)
        table = app.query_one("#settings-table")
        assert app.focused is table
        await pilot.press("down")
        await pilot.pause(0.1)
        assert table.cursor_row == 1
        await pilot.press("enter")
        for _ in range(80):
            if isinstance(app.screen, ValuePrompt):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, ValuePrompt)
        await pilot.press("escape")
        await pilot.pause(0.2)
        # Back on Chat, focus returns to the composer, not the log.
        app.action_show("chat")
        await pilot.pause(0.2)
        assert app.focused is app.query_one("#chat-input")
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


async def test_an_empty_config_reply_is_fetched_once_not_in_a_loop():
    """Regression: `action_show("settings")` refetched whenever `_config` was empty,
    so a device that reports no config spun forever."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "command" and frame["name"] == "config.get":
            return [
                encode(
                    Frame(
                        type="command.result",
                        id="d-empty",
                        ts=0,
                        body={"name": "config.get", "ok": True,
                              "result": {"echo": {}}, "duration_ms": 0},
                        reply_to=frame["id"],
                    )
                )
            ]
        return _reply(line)

    transport = FakeTransport(on_write=reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("settings")
        await pilot.pause(0.6)
        fetches = sum(1 for line in transport.written if b'"name":"config.get"' in line)
        assert fetches == 1, f"{fetches} config.get frames: the screen loops"
        assert app.query_one("#settings-table").row_count == len(ROWS)
        # `R` is the documented way to ask again.
        app.query_one("#settings").action_refetch()
        for _ in range(80):
            if sum(1 for line in transport.written
                   if b'"name":"config.get"' in line) == 2:
                break
            await pilot.pause(0.02)
        assert sum(1 for line in transport.written if b'"name":"config.get"' in line) == 2
        await session.close()


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
        assert widest > BOUNDS_CLIP, "the clip test is pointless if no bound is long"
        # Assert the rendered cells, not the constant: a column `width` clips the
        # display but leaves the cell text at 132 characters.
        cells = [str(table.get_row_at(index)[3]) for index in range(table.row_count)]
        assert max(len(cell) for cell in cells) <= BOUNDS_CLIP
        for row in ROWS:
            assert clip(row.bound, BOUNDS_CLIP) in cells
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


# -- log lines: the shape the filter reads ------------------------------


async def test_a_log_frame_reaches_the_buffer_with_its_level_visible_to_the_filter():
    """`05` §5.4: level drives colour and filtering, so it must survive to the screen."""
    from bridge.ui.logs import LogFilter

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app._on_log(
            Frame(type="log", id="d-1", ts=0,
                  body={"level": "warn", "tag": "llm", "msg": "slow"})
        )
        buffered = app.query_one("#logs").buffer.visible()
        assert len(buffered) == 1
        assert LogFilter("llm", "warn").matches(buffered[0])
        assert not LogFilter("net", "warn").matches(buffered[0])
        await session.close()


async def test_ctrl_l_clears_the_log_buffer():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("logs")
        await pilot.pause(0.1)
        for level in ("warn", "info"):
            app._on_log(Frame(type="log", id="d-1", ts=0,
                              body={"level": level, "tag": "net", "msg": "x"}))
        await pilot.press("ctrl+l")
        await pilot.pause(0.1)
        assert len(app.query_one("#logs").buffer) == 0
        await session.close()


async def test_ctrl_y_toggles_follow_and_the_header_says_so():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("logs")
        await pilot.pause(0.1)
        await pilot.press("ctrl+y")
        await pilot.pause(0.1)
        assert "OFF" in str(app.query_one("#log-header").content)
        await session.close()


# -- Status and MQTT refresh -------------------------------------------


async def test_ctrl_r_on_status_reissues_the_status_request():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("status")
        await pilot.pause(0.1)
        before = sum(b'"name":"status"' in line for line in transport.written)
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        after = sum(b'"name":"status"' in line for line in transport.written)
        assert after == before + 1
        await session.close()


async def test_mqtt_arrows_move_the_broker_cursor():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("mqtt")
        await pilot.pause(0.1)
        mqtt = app.query_one("#mqtt")
        mqtt.render_brokers([{"id": "home", "connected": True, "subscriptions": []},
                             {"id": "lab", "connected": False, "subscriptions": []}])
        await pilot.pause(0.1)
        await pilot.press("down")
        await pilot.pause(0.1)
        body = str(app.query_one("#mqtt-brokers").content).splitlines()
        assert body[1].startswith("> lab"), body
        await pilot.press("up")
        await pilot.pause(0.1)
        body = str(app.query_one("#mqtt-brokers").content).splitlines()
        assert body[0].startswith("> home"), body
        await session.close()


# -- Tasks screen: the §5.3 keys ---------------------------------------

TASKS = [
    {"id": "morning", "name": "Morning brief", "enabled": True, "trigger": {"type": "cron"}},
    {"id": "lab", "name": "Lab", "enabled": False, "trigger": {"type": "cron"}},
]


def _reply_with_tasks(line: bytes) -> list[bytes]:
    frame = json.loads(line)
    if frame["type"] == "command" and frame["name"].startswith("tasks."):
        result = {"tasks": TASKS} if frame["name"] == "tasks.list" else {"id": "morning"}
        return [
            encode(
                Frame(
                    type="command.result",
                    id="d-t",
                    ts=0,
                    body={"name": frame["name"], "ok": True, "result": result,
                          "duration_ms": 1},
                    reply_to=frame["id"],
                )
            )
        ]
    return _reply(line)


def _sent(transport, name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in transport.written
        if b'"type":"command"' in line and json.loads(line)["name"] == name
    ]


async def _tasks_screen(app, pilot):
    app.action_show("tasks")
    await pilot.pause(0.1)
    app._tasks = TASKS
    app._render_tasks()
    await pilot.pause(0.1)
    return app.query_one("#task-table")


async def test_enter_on_a_task_runs_it():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert _sent(transport, "tasks.run")[0]["args"] == {"id": "morning"}
        await session.close()


async def test_ctrl_y_toggles_the_selected_task_only():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        await pilot.press("ctrl+y")
        await pilot.pause(0.3)
        task = _sent(transport, "tasks.set")[0]["args"]["task"]
        assert task["id"] == "morning"
        assert task["enabled"] is False
        await session.close()


async def test_ctrl_d_asks_before_deleting_and_a_refusal_sends_nothing():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        await pilot.press("ctrl+d")
        for _ in range(60):
            if isinstance(app.screen, ConfirmPrompt):
                break
            await pilot.pause(0.02)
        await pilot.press("n")
        await pilot.pause(0.3)
        assert _sent(transport, "tasks.delete") == []
        await session.close()


async def test_ctrl_d_confirmed_sends_the_delete():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        await pilot.press("ctrl+d")
        for _ in range(60):
            if isinstance(app.screen, ConfirmPrompt):
                break
            await pilot.pause(0.02)
        await pilot.press("y")
        await pilot.pause(0.3)
        assert _sent(transport, "tasks.delete")[0]["args"] == {"id": "morning"}
        await session.close()


async def test_ctrl_e_edits_the_selected_task_through_the_json_modal():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        await pilot.press("ctrl+e")
        for _ in range(60):
            if isinstance(app.screen, TaskPrompt):
                break
            await pilot.pause(0.02)
        field = app.screen.query_one("#task-input")
        assert json.loads(field.value)["id"] == "morning"
        field.value = '{"id": "morning", "name": "renamed"}'
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert _sent(transport, "tasks.set")[0]["args"]["task"]["name"] == "renamed"
        await session.close()


async def test_ctrl_n_starts_from_an_empty_task():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        await pilot.press("ctrl+n")
        for _ in range(60):
            if isinstance(app.screen, TaskPrompt):
                break
            await pilot.pause(0.02)
        assert app.screen.query_one("#task-input").value == "{}"
        await pilot.press("escape")
        await pilot.pause(0.2)
        await session.close()


async def test_follow_off_stops_the_view_chasing_new_lines():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("logs")
        await pilot.pause(0.1)
        await pilot.press("ctrl+y")
        await pilot.pause(0.1)
        body = app.query_one("#log-body")
        assert body.auto_scroll is False
        app._on_log(Frame(type="log", id="d-1", ts=0,
                          body={"level": "info", "tag": "net", "msg": "still buffered"}))
        await pilot.pause(0.1)
        assert body.auto_scroll is False
        assert len(app.query_one("#logs").buffer) == 1
        await session.close()


async def test_the_log_filter_hides_lines_whose_level_is_below_it():
    from bridge.ui.logs import LogFilter

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("logs")
        await pilot.pause(0.1)
        for level, msg in (("info", "quiet"), ("error", "loud"), ("warn", "medium")):
            app._on_log(Frame(type="log", id="d-1", ts=0,
                              body={"level": level, "tag": "llm", "msg": msg}))
        await pilot.pause(0.1)
        app.query_one("#logs").apply_filter("llm", "warn")
        await pilot.pause(0.1)
        shown = app.query_one("#log-body")
        text = "\n".join(str(line.text) for line in shown.lines)
        assert "loud" in text and "medium" in text
        assert "quiet" not in text
        assert LogFilter("llm", "warn").active
        await session.close()


async def test_an_unknown_level_in_the_filter_is_reported_not_sent():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.action_show("logs")
        await pilot.pause(0.1)
        app.run_worker(app.edit_log_filter())
        for _ in range(60):
            if isinstance(app.screen, FilterPrompt):
                break
            await pilot.pause(0.02)
        app.screen.query_one("#filter-input").value = "llm:loud"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "unknown log level" in _log(app)
        assert app.query_one("#logs").buffer.log_filter.min_level == "debug"
        await session.close()


async def test_a_task_mutation_reissues_tasks_list():
    transport = FakeTransport(on_write=_reply_with_tasks)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _tasks_screen(app, pilot)
        before = len(_sent(transport, "tasks.list"))
        await pilot.press("ctrl+y")
        await pilot.pause(0.4)
        assert len(_sent(transport, "tasks.list")) == before + 1
        await session.close()


async def test_every_screen_claims_focus_when_shown():
    """`05` §6: focus follows the screen, so its bindings can fire at all.

    This is the contract the §6.2 keys rely on; without it a screen appears with
    focus left on the hidden composer and every key is swallowed.
    """
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        expected = {
            "chat": "#chat-input",
            "status": "#status",
            "tasks": "#task-table",
            "logs": "#logs",
            "settings": "#settings-table",
            "mqtt": "#mqtt",
        }
        for name, widget in expected.items():
            app.action_show(name)
            # `Show` is posted, and a worker may re-show the screen afterwards, so
            # the focus lands on a message boundary, not at a fixed latency. Poll
            # for the condition rather than guessing a sleep that a loaded suite
            # can outrun.
            for _ in range(50):
                await pilot.pause(0.02)
                if app.focused is not None and f"#{app.focused.id}" == widget:
                    break
            assert (app.focused is not None
                    and f"#{app.focused.id}" == widget), (name, app.focused)
        await session.close()


async def test_slash_logs_opens_the_logs_screen_without_a_frame():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        before = len(transport.written)
        app.submit("/logs")
        await pilot.pause(0.2)
        assert app.query_one("#logs").display is True
        assert app.query_one("#chat").display is False
        assert not [line for line in transport.written[before:] if b'"type":"command"' in line]
        await session.close()


async def test_slash_help_opens_the_help_modal_without_a_frame():
    """`05` §5.6: help is a modal over the current screen, not a screen swap."""
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        before = len(transport.written)
        app.submit("/help")
        await pilot.pause(0.2)
        assert isinstance(app.screen, HelpModal)
        assert app.query_one("#chat").display is True  # the screen is still there
        assert not [line for line in transport.written[before:] if b'"type":"command"' in line]
        await session.close()


async def test_slash_status_opens_the_status_screen_and_still_sends_the_command():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        before = len(_sent(transport, "status"))
        app.submit("/status")
        await pilot.pause(0.2)
        assert app.query_one("#status").display is True
        assert app.query_one("#chat").display is False
        assert len(_sent(transport, "status")) == before + 1
        await session.close()


async def test_a_sub_command_reports_in_chat_without_moving_the_reader():
    """`05` §7: only the bare `/mqtt` opens the screen; publishing stays in chat."""
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.submit("/mqtt")
        await pilot.pause(0.2)
        assert app.query_one("#mqtt").display is True
        app.submit("/mqtt publish lab t p")
        await pilot.pause(0.2)
        assert app.query_one("#chat").display is True
        assert app.query_one("#mqtt").display is False
        assert _sent(transport, "mqtt.publish")
        await session.close()


# -- completion list (`05` §5.1) ----------------------------------------


async def test_typing_a_slash_lists_the_commands_above_the_composer():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = app.query_one("#completion")
        assert panel.display is False
        app.query_one("#chat-input").value = "/"
        await pilot.pause(0.1)
        assert panel.display is True
        listed = str(panel.content).splitlines()
        assert listed[0] == "> /status"
        assert len(listed) == COMPLETION_ROWS + 1  # eight rows, then the overflow
        assert listed[-1].startswith("+") and listed[-1].endswith("more")
        await session.close()


async def test_the_list_filters_to_the_typed_prefix():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").value = "/ta"
        await pilot.pause(0.1)
        assert str(app.query_one("#completion").content).splitlines() == ["> /tasks"]
        app.query_one("#chat-input").value = "/tasks "
        await pilot.pause(0.1)
        assert "+" not in str(app.query_one("#completion").content)
        assert str(app.query_one("#completion").content).splitlines()[0] == "> /tasks add"
        await session.close()


async def test_the_list_disappears_when_the_text_is_not_a_command():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").value = "/"
        await pilot.pause(0.1)
        panel = app.query_one("#completion")
        assert panel.display is True
        app.query_one("#chat-input").value = "hello"
        await pilot.pause(0.1)
        assert panel.display is False
        await session.close()


async def test_submitting_clears_the_list():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").value = "/us"
        await pilot.pause(0.1)
        assert app.query_one("#completion").display is True
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.query_one("#completion").display is False
        await session.close()


async def test_the_rendered_list_is_never_taller_than_its_own_box():
    """`05` §5.1: the `+N more` row must be visible, not clipped by the box."""
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(120, 40)) as pilot:
        await _connected(pilot)
        app.query_one("#chat-input").value = "/"
        await pilot.pause(0.2)
        panel = app.query_one("#completion")
        lines = str(panel.content).splitlines()
        assert lines[-1].endswith("more")
        # A `Static` has no border and no vertical padding, so the box must be
        # exactly as tall as its content; a shorter region clips the last row.
        assert panel.region.height == len(lines), (panel.region, lines)
        await session.close()


# -- OMP navigation semantics (`05` §5.1) --------------------------------


async def _open_list(pilot, app, value="/"):
    app.query_one("#chat-input").value = value
    await pilot.pause(0.1)
    return app.query_one("#completion")


async def test_the_list_opens_with_its_top_row_selected():
    """OMP's SelectList starts on index 0 rather than on nothing."""
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = await _open_list(pilot, app)
        first = str(panel.content).splitlines()[0]
        assert first == "> /status"
        await session.close()


async def test_the_arrows_walk_the_list_and_wrap_at_the_edges():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = await _open_list(pilot, app)

        await pilot.press("down")
        await pilot.pause(0.1)
        assert str(panel.content).splitlines()[1] == "> /reload"

        # Down past the last row wraps to the first.
        for _ in range(COMPLETION_ROWS - 1):
            await pilot.press("down")
        await pilot.pause(0.1)
        assert str(panel.content).splitlines()[0] == "> /status"

        # Up from the top wraps to the last.
        await pilot.press("up")
        await pilot.pause(0.1)
        assert str(panel.content).splitlines()[COMPLETION_ROWS - 1].startswith("> ")
        await session.close()


async def test_an_arrow_key_does_not_recall_history_while_the_list_is_open():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.submit("first prompt")
        await pilot.pause(0.2)
        await _open_list(pilot, app)
        await pilot.press("up")
        await pilot.pause(0.1)
        assert app.query_one("#chat-input").value == "/"
        await session.close()


async def test_tab_accepts_the_highlighted_row_not_the_ghost():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _open_list(pilot, app)
        await pilot.press("down")
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app.query_one("#chat-input").value == "/reload"
        await session.close()


async def test_enter_applies_the_selected_row_and_submits_it():
    """OMP applies the selection, then falls through to submission."""
    sent: list[Frame] = []
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    app.submit = lambda text: sent.append(text)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _open_list(pilot, app)
        await pilot.press("down")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.2)
        # `/tasks a` runs `/tasks add`, not the half-typed fragment.
        assert sent == ["/reload"]
        await session.close()


async def test_enter_on_an_untouched_list_takes_the_top_row():
    """The first row is selected on open, so Enter needs no arrow press first."""
    sent: list[str] = []
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    app.submit = lambda text: sent.append(text)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _open_list(pilot, app)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert sent == ["/status"]
        await session.close()


async def test_escape_closes_the_list_without_leaving_the_screen():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = await _open_list(pilot, app)
        assert panel.display is True
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert panel.display is False
        # Esc closed the list, so it did not also leave the screen.
        assert app.screen is app.query_one("#chat-log").screen
        await session.close()


async def test_home_and_end_jump_to_the_edges():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = await _open_list(pilot, app)
        await pilot.press("end")
        await pilot.pause(0.1)
        assert str(panel.content).splitlines()[COMPLETION_ROWS - 1].startswith("> ")
        await pilot.press("home")
        await pilot.pause(0.1)
        assert str(panel.content).splitlines()[0] == "> /status"
        await session.close()


async def test_right_at_the_end_of_the_line_accepts_the_selected_row():
    """`Input` binds Right itself, so the Chat binding needs priority to win."""
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        await _open_list(pilot, app)
        await pilot.press("down")
        await pilot.pause(0.1)
        await pilot.press("right")
        await pilot.pause(0.2)
        assert app.query_one("#chat-input").value == "/reload"
        await session.close()


async def test_right_before_the_end_of_the_line_still_moves_the_cursor():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.value = "/ta"
        field.cursor_position = 1
        await pilot.pause(0.1)
        await pilot.press("right")
        await pilot.pause(0.1)
        # Mid-line the key keeps its cursor role and the text is untouched.
        assert field.value == "/ta"
        assert field.cursor_position == 2
        await session.close()


async def test_a_new_list_starts_on_the_top_row_again():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = await _open_list(pilot, app)
        await pilot.press("down")
        await pilot.pause(0.1)
        assert str(panel.content).splitlines()[0] != "> /status"
        await pilot.press("backspace")
        await pilot.pause(0.1)
        app.query_one("#chat-input").value = "/"
        await pilot.pause(0.2)
        assert str(panel.content).splitlines()[0] == "> /status"
        await session.close()


async def test_the_selection_never_lands_on_a_row_that_is_not_drawn():
    """The cap and the render must agree, or Tab applies an invisible row."""
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        panel = await _open_list(pilot, app)
        for _ in range(COMPLETION_ROWS + 3):
            await pilot.press("down")
            await pilot.pause(0.02)
            drawn = str(panel.content).splitlines()
            marked = [line[2:] for line in drawn if line.startswith("> ")]
            assert len(marked) == 1, drawn
            # The marked row is a real candidate, never the `+N more` counter.
            assert not marked[0].startswith("+"), marked
            # And it is one the user can see: Tab applies exactly this text.
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert app.query_one("#chat-input").value == marked[0]
            app.query_one("#chat-input").value = "/"
            await pilot.pause(0.1)
        await session.close()


async def test_prompt_with_no_link_is_refused_at_once():
    """A prompt cannot be acknowledged when nothing is linked.

    The ack timeout is `6 x connect_timeout`, so the old path left the reader
    looking at their own line for half a minute; the refusal names the link word
    instead, and it arrives without waiting for a reply.
    """
    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.action_show("chat")
        await pilot.pause(0.1)
        app.submit("hello board")
        await pilot.pause(0.2)
        lines = chat_lines(app)
        # The prefix and the prompt are two renderables laid out as a grid, so
        # the line is not a `str` and `line.text` is its repr, not the text.
        assert any("you: hello board" in line for line in lines), lines
        # The refusal names the bar's word, not the internal state enum: a reader
        # comparing it with the prompt bar must never see two vocabularies.
        #
        # The word itself is whatever the bar said at that moment. Asserting
        # `linking` pinned a transient: `ack_timeout=1` with no hello reply means
        # the ack can lapse to `down` before the assertion runs, which it did
        # under a loaded suite. The rule is that the refusal speaks the bar's
        # vocabulary rather than the enum's, and it holds in either state.
        assert any(
            f"✗ {word}; the prompt was not sent" in line
            for line in lines
            for word in LINK_WORDS.values()
        ), lines
    await session.close()


async def test_prompt_timeout_is_reported_not_swallowed():
    """A linked peer that never answers leaves a line, not silence."""
    # Answers `hello` and `ping`, then goes quiet on the prompt: a live link
    # whose run never reports back.
    def silent_prompt(line: bytes) -> list[bytes]:
        return [] if json.loads(line)["type"] == "prompt" else _reply(line)

    session = Session(
        FakeTransport(on_write=silent_prompt), ack_timeout=0.1, ping_interval=99
    )
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.submit("hello board")
        await pilot.pause(0.6)
        lines = [str(line) for line in app.query_one("#chat-log").lines]
        assert any("did not answer in time" in line for line in lines), lines
        assert any("✗ " in line for line in lines), lines
    await session.close()


def test_every_row_belongs_to_exactly_one_section():
    """`05` §6.6 groups the settings screen by section; a row in two would be edited
    twice, and a section with no rows would open an empty tab."""
    named = sections()
    assert named
    assert len(set(named)) == len(named)
    assert sum(len(rows_for(name)) for name in named) == len(ROWS)
    assert all(rows_for(name) for name in named)


def test_a_bool_row_rejects_a_word_that_is_not_true_or_false():
    bool_row = next(row for row in ROWS if row.kind == "bool")
    with pytest.raises(ValueError, match="expects true or false"):
        coerce(bool_row, "maybe")


def test_an_int_row_naming_a_closed_set_rejects_a_value_outside_it():
    """`03` §Full schema writes some int cells as a set of backticked values rather
    than a range; the screen must enforce the set it displays."""
    listed = next(
        row
        for row in ROWS
        if row.kind == "int" and not _range_of(row) and _tokens(row)
    )
    with pytest.raises(ValueError, match="expects one of"):
        coerce(listed, "7")


def test_an_int_list_rejects_a_word_among_the_numbers():
    list_row = next(row for row in ROWS if row.kind == "int[]")
    with pytest.raises(ValueError, match="comma-separated integers"):
        coerce(list_row, "1,two")


def _range_of(row):
    from bridge.ui.settings import _range

    return _range(row.bound)


def _tokens(row):
    from bridge.ui.settings import _BACKTICKED, _INTEGER

    return [token for token in _BACKTICKED.findall(row.bound) if _INTEGER.fullmatch(token)]


# -- Ctrl+C clear_input tests ---------------------------------------------

async def test_ctrl_c_clears_typed_text():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        await pilot.press(*"hello world")
        assert field.value == "hello world"
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert field.value == ""
        await session.close()


async def test_ctrl_c_closes_the_candidate_panel():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        # Trigger candidate list
        field.value = "/st"
        await pilot.pause(0.1)
        panel = app.query_one("#completion")
        assert panel.display is True
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert panel.display is False
        assert field.value == ""
        await session.close()


async def test_ctrl_c_resets_the_history_walk():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        # Submit two prompts to establish history
        for text in ("first prompt", "second prompt"):
            await pilot.press(*text)
            await pilot.press("enter")
            await pilot.pause(0.15)
        assert field.value == ""
        # Walk back to the first (older) prompt
        await pilot.press("up")
        await pilot.pause(0.1)
        await pilot.press("up")
        await pilot.pause(0.1)
        assert field.value == "first prompt"
        # Clear input (and reset walk)
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert field.value == ""
        # Next 'up' should land on the newest prompt, not the older one
        await pilot.press("up")
        await pilot.pause(0.1)
        assert field.value == "second prompt"
        await session.close()


async def test_ctrl_c_shrinks_the_composer_box():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        # Press Ctrl+J enough times to hit max height (6 rows + 2 borders = 8)
        # Need 6 newlines to reach COMPOSER_MAX_ROWS
        for _ in range(COMPOSER_MAX_ROWS + 1):
            await pilot.press("ctrl+j")
        await pilot.pause(0.1)
        assert field.styles.height.value == COMPOSER_MAX_ROWS + 2
        # Clear
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert field.styles.height.value == 3
        await session.close()


async def test_ctrl_c_does_not_cancel_a_run_or_quit():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        # Submit a prompt to start a run
        await pilot.press(*"hi")
        await pilot.press("enter")
        await pilot.pause(0.1)
        # Clear input
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert app.is_running
        # Check that no cancel frame was sent to the device
        assert not any(b'"type":"cancel"' in line for line in transport.written)
        await session.close()


async def test_ctrl_c_is_scoped_to_chat_and_ignored_when_hidden():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connected(pilot)
        field = app.query_one("#chat-input")
        field.focus()
        await pilot.press(*"keep me")
        assert field.value == "keep me"
        # Switch to logs screen
        app.action_show("logs")
        await pilot.pause(0.1)
        # Press Ctrl+C while Chat is hidden
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        # Chat composer should still hold its text
        assert field.value == "keep me"
        await session.close()

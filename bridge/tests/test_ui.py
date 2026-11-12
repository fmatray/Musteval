"""L3 ui: rendering through Textual's pilot, against a scripted transport.

No pty, no subprocess: the UI is driven by `FakeTransport`, and assertions are on
what a user can see (`05-bridge.md` §5), not on which handler ran.
"""

from __future__ import annotations

import json

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode, reset_ids

from bridge.ui.modals import HelpModal
from bridge.ui.testing import chat_input
from bridge.ui.render import (
    clip,
    render_diag,
    render_prompt_bar,
    render_status,
    render_tool_call,
    render_tool_result,
    sanitize,
)

ACK = {
    "device": "test-agent",
    "fw_version": "test-1.0.0",
    "protocol": 1,
    "boot_id": "b1",
    "time_synced": True,
    "limits": {"rx_max": 4096, "tx_max": 16384},
    "capabilities": {"tools": ["time.now"], "mcp": [], "dangerous_tools": False, "streaming": False},
}

STATUS = {
    "wifi": {"state": "up", "ssid": "home", "rssi": -55, "ip": "10.0.0.9", "failovers": 0},
    "time": {"synced": True, "unix": 1730563600, "tz": "UTC"},
    "llm": {"provider": "openai", "model": "gpt", "available": True, "last_error": None,
            "calls_hour": 1, "calls_day": 2, "limit_hour": 30, "limit_day": 200},
    "mcp": [],
    "agent": {"busy": False, "run_id": None, "queue_depth": 0, "max_queue": 2},
    "mqtt": [],
    "geo": None,
    "storage": {"userfs": {"used_bytes": 10, "total_bytes": 100},
                "wsfs": {"used_bytes": 0, "total_bytes": 50}, "workspace_quota_bytes": 20},
    "mem": {"heap_free": 111, "psram_free": 222},
    "sys": {"safe_mode": False, "boot_failures": 0, "tool_calls": 0, "tool_failures": 0},
    "uptime_s": 5,
    "boot_id": "b1",
}


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _reply(line: bytes) -> list[bytes]:
    frame = json.loads(line)
    kind = frame["type"]
    if kind == "hello":
        return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK, reply_to=frame["id"]))]
    if kind == "ping":
        return [encode(Frame(type="pong", id="d-2", ts=0, body={}, reply_to=frame["id"]))]
    if kind == "prompt":
        return [
            encode(Frame(type="agent.start", id="d-3", ts=0,
                         body={"run_id": "r-1", "trigger": "manual", "source": "prompt",
                               "provider": "openai", "model": "gpt", "input_bytes": 5,
                               "queue_depth": 0}, reply_to=frame["id"])),
            encode(Frame(type="agent.final", id="d-4", ts=0,
                         body={"run_id": "r-1", "text": "echo: hi",
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                         "total_tokens": 2},
                               "rounds": 1, "provider": "openai", "model": "gpt",
                               "duration_ms": 3, "truncated": False, "todo_open": False},
                         reply_to=frame["id"])),
        ]
    if kind == "secret.set":
        return [encode(Frame(type="command.result", id="d-5", ts=0,
                             body={"name": "secret.set", "ok": True,
                                   "result": {"key": frame["key"], "stored": True,
                                              "length": len(frame["value"])},
                                   "duration_ms": 1}, reply_to=frame["id"]))]
    if kind == "command":
        name = frame["name"]
        if name == "tasks.list":
            result = {"tasks": [{"id": "t1", "description": "d", "enabled": True,
                                 "trigger": {"type": "schedule"}, "next_fire_at": 7}]}
        elif name == "mqtt.status":
            result = [{"id": "srv", "connected": True, "subscriptions": ["a"]}]
        else:
            result = STATUS
        return [encode(Frame(type="command.result", id="d-5", ts=0,
                             body={"name": name, "ok": True, "result": result,
                                   "duration_ms": 1}, reply_to=frame["id"]))]
    return []


# -- render helpers -----------------------------------------------------


def test_sanitize_strips_control_characters():
    assert sanitize("a\x1b[31mb") == "a[31mb"
    assert sanitize("ok\ttext") == "ok\ttext"


def test_clip_collapses_newlines_and_truncates():
    assert clip("line1\nline2") == "line1 line2"
    long = "x" * 300
    assert len(clip(long)) == 120
    assert clip(long).endswith("…")


def test_tool_call_renders_arguments():
    assert render_tool_call({"tool": "led.set", "args": {"pin": 21}}) == "→ led.set(pin=21)"


def test_tool_result_marks_success_and_failure():
    assert "✓" in render_tool_result({"tool": "t", "ok": True, "result": 1, "duration_ms": 2})
    failed = render_tool_result({"tool": "t", "ok": False, "duration_ms": 2,
                                 "error": {"code": "E_TOOL", "message": "boom"}})
    assert "✗" in failed and "E_TOOL" in failed


def test_ping_tool_result_reads_as_the_same_sentence_as_the_command():
    """The `net.ping` tool and the `net.ping` command return the same eight members
    (`04-protocol.md` §4.25), so the chat log words them the same way. A tool result
    printed as a dict would make the model's reachability answer unreadable next to
    the reader's own `/ping`.
    """
    result = {
        "target": "ntp",
        "host": "0.europe.pool.ntp.org",
        "sent": 4,
        "received": 3,
        "loss_pct": 25,
        "rtt_min_ms": 30,
        "rtt_avg_ms": 36,
        "rtt_max_ms": 44,
    }
    line = render_tool_result(
        {"tool": "net.ping", "ok": True, "duration_ms": 900, "result": result}
    )
    assert "ping ntp 0.europe.pool.ntp.org: 3/4, 25% loss, rtt 30/36/44 ms" in line

    # 100 % loss drops the rtt fields, because there is no round trip to report.
    dead = dict(result, received=0, loss_pct=100, rtt_min_ms=0, rtt_avg_ms=0,
                rtt_max_ms=0)
    assert render_tool_result(
        {"tool": "net.ping", "ok": True, "duration_ms": 900, "result": dead}
    ).endswith("0/4, 100% loss")


def test_status_grid_names_every_row():
    lines = "\n".join(render_status(STATUS))
    for row in ("WIFI", "TIME", "LLM", "AGENT", "MEM", "DISK", "MQTT", "GEO", "BOOT"):
        assert row in lines


def test_status_reports_no_brokers_and_no_fix():
    lines = "\n".join(render_status(STATUS))
    assert "none" in lines and "no fix" in lines


def test_diag_exposes_the_diagnostic_counters():
    text = "\n".join(render_diag(STATUS))
    assert "heap_free: 111" in text and "llm calls day: 2/200" in text


# -- the app ------------------------------------------------------------


async def test_ui_connects_and_renders_the_greeting():
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        log = app.query_one("#chat-log")
        text = "\n".join(str(line.text) for line in log.lines)
        assert "test-agent" in text
        await session.close()


async def test_a_path_scoped_config_get_prints_the_value_in_chat():
    """`04` §4.8 gives `config.get` two shapes: a whole document under `config`,
    a path read under `path`/`value`. The second was read as the first, flattened
    into the Settings grid, and printed nothing — so `/config get <path>` looked
    like it had done nothing at all.
    """

    def reply_for(sent):
        frame = json.loads(sent)
        if frame.get("name") != "config.get":
            return _reply(sent)
        return [
            encode(
                Frame(
                    type="command.result",
                    id="r-1",
                    ts=0,
                    reply_to=frame["id"],
                    body={
                        "name": "config.get",
                        "ok": True,
                        "duration_ms": 1,
                        "result": {"path": "wifi.networks.0.ssid", "value": "home"},
                    },
                )
            )
        ]

    from bridge.ui import BridgeApp

    session = Session(
        FakeTransport(on_write=reply_for), ack_timeout=1, ping_interval=99
    )
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        log = app.query_one("#chat-log")
        before = len(log.lines)
        field = app.query_one("#chat-input")
        field.focus()
        field.value = "/config get wifi.networks.0.ssid"
        await pilot.pause(0.05)
        await pilot.press("enter")
        for _ in range(100):
            if len(log.lines) > before:
                break
            await pilot.pause(0.02)
        written = " ".join(str(line.text) for line in log.lines[before:])
        assert "home" in written
        await session.close()


async def test_a_typed_ping_answers_in_chat_as_well_as_the_footer():
    """`05-bridge.md` §10 calls `/ping` the latency check.

    The footer alone is not an answer: it is on another screen and empty until the
    status grid has loaded, so a reader who typed `/ping` saw nothing happen. The
    heartbeat must stay silent, or a line every 10 s buries the conversation.
    """
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        log = app.query_one("#chat-log")
        before = len(log.lines)

        field = app.query_one("#chat-input")
        field.focus()
        field.value = "/ping device"
        await pilot.pause(0.05)
        await pilot.press("enter")
        for _ in range(100):
            if len(log.lines) > before:
                break
            await pilot.pause(0.02)

        assert "pong:" in str(log.lines[-1].text)
        await session.close()


@pytest.mark.parametrize(
    ("loss", "marker", "expect_rtt"),
    [(0, "", True), (50, "!", True), (100, "✗", False)],
)
async def test_net_ping_result_is_one_chat_line(loss, marker, expect_rtt):
    """`04-protocol.md` §4.25: the device measured reachability, so the reader gets
    the number it measured — and 100 % loss is a result, coloured as the failure it
    is, not an error frame that would read as a protocol fault.
    """
    from bridge.ui import BridgeApp

    def reply_for(sent):
        frame = json.loads(sent)
        if frame.get("name") != "net.ping":
            return _reply(sent)
        sent_count = 4
        received = sent_count - sent_count * loss // 100
        return [
            encode(
                Frame(
                    type="command.result",
                    id="r-1",
                    ts=0,
                    reply_to=frame["id"],
                    body={
                        "name": "net.ping",
                        "ok": True,
                        "duration_ms": 900,
                        "result": {
                            "target": "router",
                            "host": "192.168.1.1",
                            "sent": sent_count,
                            "received": received,
                            "loss_pct": loss,
                            "rtt_min_ms": 1 if received else 0,
                            "rtt_avg_ms": 2 if received else 0,
                            "rtt_max_ms": 3 if received else 0,
                        },
                    },
                )
            )
        ]

    session = Session(
        FakeTransport(on_write=reply_for), ack_timeout=1, ping_interval=99
    )
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        log = app.query_one("#chat-log")
        before = len(log.lines)

        field = app.query_one("#chat-input")
        field.focus()
        field.value = "/ping router"
        await pilot.pause(0.05)
        await pilot.press("enter")
        for _ in range(100):
            if len(log.lines) > before:
                break
            await pilot.pause(0.02)

        line = log.lines[-1]
        text = str(line.text)
        assert "192.168.1.1" in text
        assert f"{4 - 4 * loss // 100}/4" in text
        assert f"{loss}% loss" in text
        assert ("rtt" in text) is expect_rtt
        # The kind is the marker in the text, not a widget attribute: `ChatLog.write`
        # prefixes `!` for warn and `✗` for error so the fact survives a pipe.
        assert text.startswith(f"{marker} ping " if marker else "ping ")
        await session.close()


async def test_todo_round_trips_the_device_list_into_chat():
    """`05` §7: `/todo` sends `todo.apply` and renders the answer. Before this it
    wrote "no open todos" from a local flag and sent nothing, so a device list the
    model had built was invisible to the reader — the doc promised the render and
    the code did not do it.
    """
    from bridge.ui import BridgeApp

    sent_names: list[str] = []

    def reply_for(sent):
        frame = json.loads(sent)
        if frame.get("name") != "todo.apply":
            return _reply(sent)
        sent_names.append(frame["name"])
        assert frame["args"] == {"op": "view"}
        return [
            encode(
                Frame(
                    type="command.result",
                    id="r-1",
                    ts=0,
                    reply_to=frame["id"],
                    body={
                        "name": "todo.apply",
                        "ok": True,
                        "duration_ms": 3,
                        "result": {
                            "phases": [
                                {
                                    "name": "auth",
                                    "tasks": [
                                        {"content": "wire jwt", "status": "completed"},
                                        {"content": "rotate key", "status": "in_progress"},
                                        {
                                            "content": "tag release",
                                            "status": "blocked",
                                            "blocker": "needs ci",
                                        },
                                    ],
                                }
                            ]
                        },
                    },
                )
            )
        ]

    session = Session(FakeTransport(on_write=reply_for), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        log = app.query_one("#chat-log")

        field = app.query_one("#chat-input")
        field.focus()
        field.value = "/todo"
        await pilot.pause(0.05)
        await pilot.press("enter")
        for _ in range(100):
            if any(str(line.text).startswith("auth") for line in log.lines):
                break
            await pilot.pause(0.02)

        assert sent_names == ["todo.apply"]
        # The connect banner lands asynchronously, so the todo block is selected by
        # its own first line rather than by an index taken before the keystroke.
        start = next(
            i for i, line in enumerate(log.lines) if str(line.text) == "auth"
        )
        texts = [str(line.text) for line in log.lines[start:]]
        assert texts[0] == "auth"
        assert "[x] wire jwt" in texts[1]
        assert "completed" in texts[1]
        assert "[>] rotate key" in texts[2]
        assert "in_progress" in texts[2]
        assert "[!] tag release" in texts[3]
        assert "(needs ci)" in texts[3]
        await session.close()


async def test_status_footer_shows_the_round_trip_a_real_pong_measured():
    """`05-bridge.md` §10: `pong` updates the latency the Status screen shows.

    The heartbeat's pong must reach the app, not only resolve the future inside
    `Session`: before this, `_deliver` returned on resolution, so no pong ever
    reached `_on_pong` and the footer could only be filled by calling it directly.
    The test therefore drives a real heartbeat rather than the screen's setter.
    """
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1,
                      ping_interval=0.05, pong_timeout=0.5)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        screen = app.query_one("#status")
        for _ in range(100):
            if getattr(screen, "_rtt_ms", None) is not None:
                break
            await pilot.pause(0.02)
        assert session.last_rtt_ms is not None
        body = str(screen.query_one("#status-body").render())
        assert "PING" in body and "no pong yet" not in body
        assert "pong" not in app.title
        await session.close()


async def test_ui_shows_a_prompt_reply():
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        await pilot.click("#chat-input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause(0.3)
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "echo: hi" in text
        await session.close()


async def test_ui_routes_a_local_command_without_touching_the_wire():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        before = len(transport.written)
        app.submit("/tools")
        await pilot.pause(0.2)
        assert len(transport.written) == before
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "time.now" in text
        await session.close()


async def test_ui_displays_a_protocol_error_instead_of_sending_it():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        before = len(transport.written)
        app.submit("/nonsense")
        await pilot.pause(0.2)
        assert len(transport.written) == before
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "unknown command" in text
        await session.close()


async def test_ui_status_screen_renders_after_a_status_reply():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        app.action_show("status")
        await pilot.pause(0.1)
        body = str(app.query_one("#status-body").content)  # type: ignore[attr-defined]
        assert "WIFI" in body and "home" in body
        await session.close()


async def test_ui_tasks_screen_lists_tasks():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        app.submit("/tasks")
        await pilot.pause(0.2)
        app.action_show("tasks")
        await pilot.pause(0.1)
        assert app.query_one("#task-table").row_count == 1
        await session.close()


async def test_ui_mqtt_screen_lists_brokers():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        app.submit("/mqtt")
        await pilot.pause(0.2)
        assert "srv" in str(app.query_one("#mqtt-brokers").content)  # type: ignore[attr-defined]
        await session.close()


async def test_ui_secret_reply_never_shows_a_value():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        app._pending_secret = "hunter2"
        app.submit("/secret set wifi")
        for _ in range(50):
            if "stored" in "\n".join(str(l.text) for l in app.query_one("#chat-log").lines):
                break
            await pilot.pause(0.02)
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "hunter2" not in text
        assert "stored" in text
        await session.close()


async def test_ui_log_screen_accepts_device_logs():
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        app._on_frame(Frame(type="log", id="d-9", ts=0,
                            body={"level": "E", "tag": "llm", "msg": "boom"}))
        # A log arriving while the screen is hidden is buffered; opening Logs
        # is what a user does to see it (`05-bridge.md` §5.4).
        app.action_show("logs")
        await pilot.pause(0.1)
        text = "\n".join(str(line.text) for line in app.query_one("#log-body").lines)
        assert "llm" in text and "boom" in text
        await session.close()


async def test_ui_help_opens_as_a_modal_over_chat():
    """`05` §5.6: help is a modal, so the screen under it keeps its state.

    The old expectation was a `#help` screen reached by `action_show`. That is
    the behaviour this replaces, not a regression: a screen swap discarded a
    half-typed prompt, and `F1` is pressed mid-composition.
    """
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        chat_input(app).value = "half typed"
        await pilot.press("f1")
        await pilot.pause(0.1)
        assert isinstance(app.screen, HelpModal)
        # Chat is still mounted underneath and still holds the draft.
        assert app.query_one("#chat").display is True
        assert chat_input(app).value == "half typed"
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert chat_input(app).value == "half typed"
        await session.close()


# -- prompt bar (`05` §5.1) ---------------------------------------------


def test_the_prompt_bar_names_the_link_and_costs_a_live_status():
    line = render_prompt_bar("connected", {"device": "musteval-01"}, STATUS, 1234)
    segments = line.split(" | ")
    assert segments[0] == "linked"
    assert "openai/gpt" in segments
    assert "idle" in segments
    assert "-55dBm" in segments
    assert "workspace 0/20" in segments
    assert "1234 tok" in segments
    assert segments[-1] == "musteval-01"


def test_the_bar_omits_what_it_does_not_know_rather_than_inventing_it():
    """A down link with no status must not blame the provider."""
    assert render_prompt_bar("down", {}, {}, 0) == "down"


def test_an_unreachable_link_shows_no_fact_that_would_be_stale():
    """Every other segment would be the last thing heard before the link died."""
    status = {
        "llm": {"provider": "openai", "model": "m", "available": True},
        "agent": {"busy": False, "queue_depth": 0},
        "wifi": {"rssi": -58},
    }
    assert render_prompt_bar("down", {"device": "m01"}, status, 99) == "down"
    assert render_prompt_bar("rejected", {"device": "m01"}, status, 99) == "refused"
    # A flaky link still carries current facts: the device is answering.
    assert "openai/m" in render_prompt_bar("degraded", {"device": "m01"}, status, 99)


def test_the_bar_reports_the_facts_that_explain_a_trouble():
    status = {
        "llm": {"provider": "openai", "model": "m", "available": False},
        "agent": {"busy": True, "run_id": "r-7", "queue_depth": 2, "max_queue": 2},
        "sys": {"safe_mode": True},
    }
    segments = render_prompt_bar("degraded", {"device": "x"}, status, 5).split(" | ")
    assert segments[0] == "flaky"
    assert "llm down" in segments
    assert "busy r-7" in segments
    assert "queue 2/2" in segments
    assert "SAFE MODE" in segments


def test_a_queue_of_zero_is_not_a_segment():
    status = {"agent": {"busy": False, "run_id": None, "queue_depth": 0, "max_queue": 2}}
    assert "queue" not in render_prompt_bar("connected", {}, status, 0)


async def test_the_prompt_bar_is_visible_and_refreshes_with_the_status():
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        chat = app.query_one("#chat")
        first = chat.bar_line
        assert first.startswith("linked")
        assert "test-agent" in first

        # A status arriving changes the bar without any keystroke: `/status` is
        # an ordinary command and its reply is what feeds the bar.
        app.submit("/status")
        await pilot.pause(0.3)
        after = app.query_one("#chat").bar_line
        assert "openai/gpt" in after and "-55dBm" in after
        await session.close()


async def test_the_prompt_bar_follows_a_link_drop_without_any_frame():
    """A transport drop sends no frame, so only a state hook can redraw the bar.

    `05` §5.1 lists the link state as a bar fact; a bar that only redraws on
    frames would sit at `linked` through an outage.
    """
    from bridge.ui import BridgeApp

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99, backoff=(60,))
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        assert app.query_one("#chat").bar_line.startswith("linked")

        await transport.close()
        for _ in range(50):
            if app.session.state.value == "down":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        assert app.query_one("#chat").bar_line.startswith("down"), (
            app.query_one("#chat").bar_line
        )
        await session.close()


def test_a_narrow_bar_drops_whole_segments_and_never_half_a_fact():
    status = {
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "available": True},
        "agent": {"busy": False, "queue_depth": 0, "max_queue": 2},
        "wifi": {"rssi": -58},
    }
    narrow = render_prompt_bar("connected", {"device": "m01"}, status, 1234, width=40)
    assert narrow == "linked | openai/gpt-4o-mini | idle"
    assert not narrow.endswith("…")


def test_the_link_word_survives_any_width():
    assert render_prompt_bar("connected", {}, {}, 0, width=1) == "linked"


async def test_an_agent_error_is_written_as_an_error_line():
    """`05` §13.2: a line the device refused is `$error`, and it repeats the code
    and message verbatim. The message is the whole diagnostic — `llm` puts the
    provider's rejection body in it — so a bridge that paraphrased it would have
    nothing left to show but a colour.
    """
    from bridge.ui import BridgeApp

    message = 'ollama-cloud: http 400: {"error":"model not found"}'

    def reply_for(line):
        frame = json.loads(line)
        if frame.get("type") == "prompt":
            return [
                encode(Frame(type="agent.start", id="d-3", ts=0,
                             body={"run_id": "r-1", "trigger": "manual",
                                   "source": "prompt", "provider": "ollama-cloud",
                                   "model": "m", "input_bytes": 5,
                                   "queue_depth": 0}, reply_to=frame["id"])),
                encode(Frame(type="agent.error", id="d-4", ts=0,
                             body={"code": "E_LLM_UNAVAILABLE", "message": message,
                                   "retryable": True},
                             reply_to=frame["id"])),
            ]
        return _reply(line)

    session = Session(
        FakeTransport(on_write=reply_for), ack_timeout=1, ping_interval=99
    )
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        field = app.query_one("#chat-input")
        field.focus()
        field.value = "hello"
        await pilot.pause(0.05)
        await pilot.press("enter")
        for _ in range(100):
            if any("E_LLM_UNAVAILABLE" in str(l.text)
                   for l in app.query_one("#chat-log").lines):
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        line = [
            l for l in app.query_one("#chat-log").lines
            if "E_LLM_UNAVAILABLE" in str(l.text)
        ][-1]
        styles = " ".join(str(seg.style) for seg in line._segments)
        assert message in str(line.text)
        assert app.current_theme.error in styles, (str(line.text), styles)
        await session.close()


async def test_every_chat_line_is_classified_and_markdown_is_prose_only():
    """`05` §5.1: the kind decides the style, and only prose is Markdown.

    A kind is not a prefix a caller remembers to type: the marker and the colour
    are added by the one write path, so the plain text a screen reader gets is the
    same text the colour terminal shows. The turn rule is the other half — the
    first prompt has nothing above it to separate, and the second does.
    """
    from bridge.ui import BridgeApp

    def markdown_reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] != "prompt":
            return _reply(line)
        # `## ` is a heading only if the line is parsed; left as text it would
        # print its own hashes, which is how the assertion below can tell.
        return [
            encode(
                Frame(
                    type="agent.final",
                    id="d-9",
                    ts=0,
                    body={
                        "run_id": "r-1",
                        "text": "## Title\n\n- one\n- two",
                        "usage": {},
                        "rounds": 1,
                        "provider": "openai",
                        "model": "gpt",
                        "duration_ms": 3,
                        "truncated": False,
                        "todo_open": False,
                    },
                    reply_to=frame["id"],
                )
            )
        ]

    transport = FakeTransport(on_write=markdown_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        app.submit("first")
        await pilot.pause(0.3)
        app.submit("second")
        await pilot.pause(0.3)
        chat = app.query_one("#chat-log")
        app._chat().write("degraded", kind="warn")
        app._chat().write("refused", kind="error")
        app._chat().write("→ time.now()", kind="tool")
        app._chat().write("waiting", kind="thinking")
        await pilot.pause(0.1)

        lines = [str(line.text) for line in chat.lines]
        body = "\n".join(lines)
        # The markers are added by the writer, so no caller can forget one.
        assert "! degraded" in body
        assert "✗ refused" in body
        # The model's answer is parsed: a heading loses its hashes, a list its
        # bullets of `-`. Printed as text, both would still be there.
        assert "Title" in body
        assert "## " not in body and "- one" not in body

        rules = [line for line in chat.lines if set(str(line.text).strip()) == {"─"}]
        # Exactly one: the boundary before the second prompt, none before the first.
        assert len(rules) == 1, lines
        assert "dim" in " ".join(str(seg.style) for seg in rules[0]._segments)

        styled = {str(line.text).split(" ")[-1]: line for line in chat.lines}
        assert app.current_theme.warning in " ".join(
            str(s.style) for s in styled["degraded"]._segments
        )
        assert app.current_theme.error in " ".join(
            str(s.style) for s in styled["refused"]._segments
        )
    await session.close()

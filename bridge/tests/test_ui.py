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

from bridge.ui.modals import HelpModal, ValuePrompt
from bridge.ui.testing import chat_input
from bridge.ui.render import (
    clip,
    diag_sections,
    render_prompt_bar,
    render_status,
    render_tool_call,
    render_tool_result,
    provider_lines,
    sanitize,
    wifi_known_lines,
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


def test_wifi_list_joins_the_document_with_the_scan():
    status = {"ssid": "HomeLab", "rssi": -51, "state": "connected"}
    scan = {"networks": [{"ssid": "HomeLab", "rssi": -44}, {"ssid": "Cafe", "rssi": -71}]}
    networks = [
        {"ssid": "HomeLab", "priority": 10, "password_ref": "nvs:wifi_home"},
        {"ssid": "Cafe", "priority": 0, "password_ref": "nvs:wifi_cafe"},
        {"ssid": "Cabin", "priority": 5, "password_ref": "nvs:wifi_cabin"},
    ]
    lines = wifi_known_lines(status, scan, networks)
    # Joined wins over the scan: the station is associated with HomeLab, so that is
    # the state a reader is asking about even though the scan also heard it.
    assert "HomeLab" in lines[0] and "joined" in lines[0]
    # A configured network the scan heard is in range with its scanned rssi.
    assert "Cafe" in lines[1] and "in range" in lines[1] and "-71 dBm" in lines[1]
    # A configured network the scan did not return is drawn, not dropped: "known but
    # silent" is exactly the fact `/wifi list` exists to report.
    assert "Cabin" in lines[2] and "not in range" in lines[2]


def test_wifi_list_uses_the_association_rssi_when_the_scan_omits_it():
    # A scan taken while associated frequently does not return the joined AP, so the
    # status frame is the only signal for it.
    lines = wifi_known_lines(
        {"ssid": "HomeLab", "rssi": -63},
        {"networks": []},
        [{"ssid": "HomeLab", "priority": 0, "password_ref": "nvs:wifi_home"}],
    )
    assert "joined" in lines[0] and "-63 dBm" in lines[0]


def test_wifi_list_calls_an_absent_scan_unknown_not_out_of_range():
    # A refused scan (the radio is off, or a run holds it) is not a scan that returned
    # nothing: the second says the network is not audible, the first says nobody
    # looked. Painting `not in range` from an absent scan is a claim the device never
    # made, and it is the one a reader would act on by moving a working AP.
    lines = wifi_known_lines(
        {"state": "off", "ssid": None, "rssi": None},
        None,
        [{"ssid": "HomeLab", "priority": 0}, {"ssid": "Cafe", "priority": 5}],
    )
    assert all("not in range" not in line for line in lines)
    assert all("unknown" in line for line in lines)


def test_wifi_list_says_so_with_no_networks():
    assert wifi_known_lines({}, {}, []) == ["no networks configured"]


def test_provider_rows_name_the_key_and_never_a_model_of_it():
    lines = provider_lines(
        [
            {
                "id": "ollama",
                "base_url": "https://ollama.com/v1",
                "model": "gemma4:31b",
                "api_key_ref": "nvs:key_ollama",
            },
            {
                "id": "groq",
                "base_url": "https://api.groq.com/openai/v1",
                "model": "llama-3.3-70b",
                "api_key_ref": "nvs:key_groq",
            },
        ]
    )
    assert "ollama" in lines[0] and "gemma4:31b" in lines[0]
    assert "key: nvs:key_ollama" in lines[1]
    # The chain order is the fallback order, so the numbering is the fact a reader
    # acts on: 1 is tried first.
    assert lines[2].startswith("  2.") and "groq" in lines[2]
    assert provider_lines([]) == ["no providers configured"]


def test_diag_exposes_the_diagnostic_counters_under_heads():
    sections = diag_sections(STATUS)
    heads = [heading for heading, _lines in sections]
    # The box draws a heading per group, so an empty or missing group is a blank
    # seam a reader cannot scan by.
    assert heads == ["Memory", "Boot", "Tools", "LLM budget"]
    text = "\n".join(line for _heading, lines in sections for line in lines)
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
    """`/usage` is answered from the link's own counters. `/tools` is not local any
    more: it reads `tools.list`, because the descriptions live on the device."""
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
        app.submit("/usage")
        await pilot.pause(0.2)
        assert len(transport.written) == before
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "total tokens" in text
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
    # A call that actually failed: `available` false *with* `last_error` naming why.
    # `available` false alone is a device that has not called yet, which is not a
    # trouble and used to be reported as one.
    status = {
        "llm": {"provider": "openai", "model": "m", "available": False,
                "last_error": "openai: http 401"},
        "agent": {"busy": True, "run_id": "r-7", "queue_depth": 2, "max_queue": 2},
        "sys": {"safe_mode": True},
    }
    segments = render_prompt_bar("degraded", {"device": "x"}, status, 5).split(" | ")
    assert segments[0] == "flaky"
    assert "llm down" in segments
    assert "busy r-7" in segments
    assert "queue 2/2" in segments
    assert "SAFE MODE" in segments


def test_a_device_that_has_not_called_yet_is_not_reported_as_down():
    """`04` §3.9: `available` is "last call succeeded", not "the provider is up".

    A device that boots with a valid chain reports `available: false` with a null
    `last_error` until the first call. Reading that as a failure announced
    `llm down` for a provider that was answering — and a `config.set` on any
    `llm.*` path re-ran `llm_init`, which cleared the flag, so an unrelated write
    produced the same false alarm.
    """
    status = {
        "llm": {"provider": "ollama", "model": "gemma4:31b", "thinking": "max",
                "available": False, "last_error": None},
        "agent": {"busy": False, "queue_depth": 0, "max_queue": 2},
    }
    bar = render_prompt_bar("connected", {"device": "m01"}, status, 99)
    assert "llm down" not in bar
    # The configured model is still named: it is a different fact, and dropping it
    # would make the bar silent about a device that is working.
    assert "ollama/gemma4:31b/max" in bar


def test_the_status_grid_does_not_call_an_uncalled_provider_down():
    status = {"llm": {"provider": "ollama", "model": "gemma4:31b",
                      "available": False, "last_error": None}}
    llm_row = next(line for line in render_status(status) if "LLM" in line)
    assert "available" in llm_row
    assert "[red]no[/red]" not in llm_row
    # And a real failure still says so, in red.
    failed = dict(status, llm={"provider": "ollama", "model": "gemma4:31b",
                               "available": False, "last_error": "http 401"})
    failed_row = next(line for line in render_status(failed) if "LLM" in line)
    assert "[red]no[/red]" in failed_row


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

        # A status arriving changes the bar without any keystroke: `/sys status` is
        # an ordinary command and its reply is what feeds the bar.
        app.submit("/sys status")
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
    """`17` §2: a line the device refused is `$error`, and it repeats the code
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


# -- task and file screen actions (`05` §5.3, §5.5) ----------------------


async def test_running_the_selected_task_sends_the_command():
    """`17` §1: `Ctrl+X` runs the row under the cursor through the same
    `tasks.run` path Enter does, so the two cannot drift."""
    from bridge.ui import BridgeApp

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        sent.append(frame)
        return _reply(line)

    session = Session(FakeTransport(on_write=reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        app.submit("/tasks")
        await pilot.pause(0.2)
        app.action_show("tasks")
        await pilot.pause(0.1)
        app.query_one("#task-table").move_cursor(row=0)
        await app.run_selected_task()
        await pilot.pause(0.1)
        runs = [f for f in sent if f.get("name") == "tasks.run"]
        assert runs and runs[0]["args"] == {"id": "t1"}
        await session.close()


async def test_running_with_no_task_selected_says_so_and_sends_nothing():
    """An empty table is a refusal in the chat, not a `tasks.run` with a made-up id."""
    from bridge.ui import BridgeApp

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        sent.append(json.loads(line))
        return _reply(line)

    session = Session(FakeTransport(on_write=reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        app.action_show("tasks")
        await pilot.pause(0.1)
        before = len([f for f in sent if f.get("name") == "tasks.run"])
        await app.run_selected_task()
        await pilot.pause(0.1)
        assert len([f for f in sent if f.get("name") == "tasks.run"]) == before
        log = app.query_one("#chat-log")
        assert "no task selected" in "\n".join(str(line.text) for line in log.lines)
        await session.close()


async def test_deleting_and_toggling_the_selected_task_send_their_commands():
    """`delete_task` removes and `toggle_task` flips `enabled`; each is refused on an
    empty table rather than sending a request the device would reject."""
    from bridge.ui import BridgeApp

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        sent.append(json.loads(line))
        return _reply(line)

    session = Session(FakeTransport(on_write=reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        app.action_show("tasks")
        await pilot.pause(0.1)
        await app.delete_task()
        await app.toggle_task()
        await pilot.pause(0.1)
        assert not any(f.get("name") in {"tasks.delete", "tasks.set"} for f in sent)
        await session.close()


async def test_toggling_the_selected_task_flips_enabled():
    """The row's own `enabled` is inverted and sent back whole, so the device is not
    asked to merge a partial task."""
    from bridge.ui import BridgeApp

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        sent.append(json.loads(line))
        return _reply(line)

    session = Session(FakeTransport(on_write=reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.session.state.value == "connected":
                break
            await pilot.pause(0.02)
        app.submit("/tasks")
        await pilot.pause(0.2)
        app.action_show("tasks")
        await pilot.pause(0.1)
        app.query_one("#task-table").move_cursor(row=0)
        await app.toggle_task()
        await pilot.pause(0.1)
        set_frames = [f for f in sent if f.get("name") == "tasks.set"]
        assert set_frames, "tasks.set was not sent"
        assert set_frames[0]["args"]["task"]["enabled"] is False
        await session.close()


# -- `/fs put` staging (`05-bridge.md` §7) -----------------------------------


async def _connect(pilot, app) -> None:
    for _ in range(50):
        if app.session.state.value == "connected":
            break
        await pilot.pause(0.02)


async def _fs_put_modal(pilot, app, source: str) -> None:
    """Answer the `Upload from` prompt `/fs put` opens, the way a reader does."""
    for _ in range(120):
        if isinstance(app.screen, ValuePrompt):
            await pilot.pause(0.05)
            app.screen.query_one("#value-input").value = source
            await pilot.press("enter")
            return
        await pilot.pause(0.02)
    raise AssertionError(f"no upload prompt; screen is {app.screen!r}")


def _written(transport) -> list[dict]:
    return [json.loads(line) for line in transport.written]


async def test_fs_put_asks_for_the_host_file_and_chunks_it(tmp_path):
    """`/fs put` was unreachable: `parse` refuses without `upload_bytes`, and only
    the Files screen's `Ctrl+O` ever loaded one — into the sandbox, not userfs.
    The prompt is what makes the documented command runnable, and the frames carry
    the file under the device's RX cap."""
    import base64

    from bridge.commands import FS_PUT_CHUNK_BYTES
    from bridge.ui import BridgeApp

    data = b"z" * (FS_PUT_CHUNK_BYTES * 2 + 10)
    source = tmp_path / "skill.md"
    source.write_bytes(data)
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connect(pilot, app)
        await pilot.pause(0.2)
        app.submit("/fs put /skills/skill/SKILL.md")
        await _fs_put_modal(pilot, app, str(source))
        for _ in range(120):
            puts = [f for f in _written(transport) if f.get("name") == "fs.put"]
            if puts and puts[-1]["args"]["final"]:
                break
            await pilot.pause(0.02)
        puts = [f for f in _written(transport) if f.get("name") == "fs.put"]
        assert [f["args"]["path"] for f in puts] == ["/skills/skill/SKILL.md"] * 3
        assert [f["args"]["offset"] for f in puts] == [
            0, FS_PUT_CHUNK_BYTES, FS_PUT_CHUNK_BYTES * 2
        ]
        assert [f["args"]["final"] for f in puts] == [False, False, True]
        rebuilt = b"".join(base64.b64decode(f["args"]["data"]) for f in puts)
        assert rebuilt == data
        await session.close()


async def test_fs_put_cancelled_at_the_prompt_sends_nothing():
    """Esc is a cancel, not an upload of nothing: the reader gets no error line
    either, the same shape as `Ctrl+O`'s empty answer."""
    from bridge.ui import BridgeApp
    from bridge.ui.testing import chat_lines

    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connect(pilot, app)
        await pilot.pause(0.2)
        before = len(transport.written)
        app.submit("/fs put /skills/skill/SKILL.md")
        for _ in range(120):
            if isinstance(app.screen, ValuePrompt):
                break
            await pilot.pause(0.02)
        await pilot.press("escape")
        for _ in range(120):
            if not isinstance(app.screen, ValuePrompt):
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)
        assert len(transport.written) == before
        text = "\n".join(chat_lines(app))
        assert "no file loaded" not in text and "✗" not in text
        await session.close()


async def test_fs_put_of_an_unreadable_path_names_it_and_sends_nothing(tmp_path):
    """A path that is not there is reported by name in chat, and no frame leaves:
    a silent half-upload would leave the device staging a file forever."""
    from bridge.ui import BridgeApp
    from bridge.ui.testing import chat_lines

    missing = tmp_path / "absent.md"
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _connect(pilot, app)
        await pilot.pause(0.2)
        before = len(transport.written)
        app.submit("/fs put /skills/skill/SKILL.md")
        await _fs_put_modal(pilot, app, str(missing))
        for _ in range(120):
            if str(missing) in "".join(chat_lines(app)):
                break
            await pilot.pause(0.02)
        text = "".join(chat_lines(app))
        assert str(missing) in text
        assert "No such file" in text
        assert not [f for f in _written(transport) if f.get("name") == "fs.put"]
        assert len(transport.written) == before
        await session.close()


def test_the_list_helpers_tolerate_a_non_dict_row():
    """`cfg_validate.c` makes both arrays object arrays, so a scalar can only arrive
    from a document that is already invalid. Drawing a line that names the broken row
    is worse than skipping it: the reader cannot act on `['str']`."""
    lines = wifi_known_lines({}, {}, ["junk", {"ssid": "Cafe", "priority": 1}])
    assert len(lines) == 1 and "Cafe" in lines[0]
    assert provider_lines(["junk", {"id": "groq"}])[0].strip() == "1. groq"
    # The scan is a list of objects too, so the same tolerance applies there: a
    # scalar in it is not a network and must not become an rssi.
    lines = wifi_known_lines({}, {"networks": ["junk", {"ssid": "Cafe", "rssi": -70}]},
                             [{"ssid": "Cafe", "priority": 1}])
    assert "in range" in lines[0] and "-70 dBm" in lines[0]

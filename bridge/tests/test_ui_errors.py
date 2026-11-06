"""L2/L3 error paths: every rejection the bridge can show, shown.

`test_ui.py` and `test_ui_input.py` prove the happy paths. This file proves the
other half of each feature: a device that answers `ok:false`, a link that times
out, a frame it cannot use, and a command the ui must refuse locally. `05-bridge.md`
§4.1 and §7 promise the user sees a reason rather than silence, so each case
asserts visible text, not that a handler ran.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bridge.client import Session
from bridge.client.session import SessionError
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode, reset_ids

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
    "wifi": {"state": "off", "ssid": None, "rssi": None, "ip": None, "failovers": 0},
    "time": {"synced": False, "unix": 0, "tz": "UTC"},
    "llm": {"provider": None, "model": None, "available": False, "last_error": None,
            "calls_hour": 0, "calls_day": 0, "limit_hour": None, "limit_day": None},
    "mcp": [],
    "agent": {"busy": False, "run_id": None, "queue_depth": 0, "max_queue": 2},
    "mqtt": [],
    "geo": None,
    "storage": {"userfs": {"used_bytes": 0, "total_bytes": 100},
                "wsfs": {"used_bytes": 0, "total_bytes": 50}, "workspace_quota_bytes": 20},
    "mem": {"heap_free": 0, "psram_free": 0},
    "sys": {"safe_mode": False, "boot_failures": 0, "tool_calls": 0, "tool_failures": 0},
    "uptime_s": 0,
    "boot_id": "b1",
}

FILES = {
    "/workspace/": [{"name": "notes.md", "dir": False, "bytes": 5}],
    "/workspace/notes.md": "hello",
}


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _result(frame: dict, name: str, result, *, ok: bool = True) -> bytes:
    body = {"name": name, "ok": ok, "duration_ms": 1}
    if ok:
        body["result"] = result
    else:
        body["error"] = result
    return encode(Frame(type="command.result", id="d-9", ts=0, body=body, reply_to=frame["id"]))


def _scripted(overrides: dict[str, object] | None = None, *, drop: set[str] | None = None):
    """A `_reply` that fails one command by name, and can withhold any other."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        kind = frame["type"]
        name = frame.get("name", "")
        if drop and (kind in drop or name in drop):
            return []
        if overrides and name in overrides:
            value = overrides[name]
            if isinstance(value, Exception):
                raise value
            return [_result(frame, name, value, ok=False)]
        if kind == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if kind == "ping":
            return [encode(Frame(type="pong", id="d-2", ts=0, body={}, reply_to=frame["id"]))]
        if kind == "prompt":
            return [encode(Frame(type="agent.start", id="d-3", ts=0,
                                 body={"run_id": "r-1", "trigger": "manual", "source": "prompt",
                                       "provider": "p", "model": "m", "input_bytes": 1,
                                       "queue_depth": 0}, reply_to=frame["id"])),
                    encode(Frame(type="agent.final", id="d-4", ts=0,
                                 body={"run_id": "r-1", "text": "done",
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                 "total_tokens": 2},
                                       "rounds": 1, "provider": "p", "model": "m",
                                       "duration_ms": 1, "truncated": False,
                                       "todo_open": False}, reply_to=frame["id"]))]
        if kind == "command":
            if name == "status":
                return [_result(frame, name, STATUS)]
            if name == "sandbox.list":
                path = (frame.get("args") or {}).get("path", "/workspace/")
                if path in FILES:
                    return [_result(frame, name, {"path": path,
                                                  "entries": FILES[path]})]
                return [_result(frame, name, {"code": "E_STORAGE",
                                              "message": "no such entry"}, ok=False)]
            if name == "sandbox.read":
                path = (frame.get("args") or {}).get("path")
                if path in FILES:
                    return [_result(frame, name, {"path": path, "text": FILES[path],
                                                  "bytes": len(FILES[path]),
                                                  "truncated": False})]
                return [_result(frame, name, {"code": "E_STORAGE",
                                              "message": "no such entry"}, ok=False)]
            if name == "sandbox.write":
                return [_result(frame, name, {"path": (frame.get("args") or {}).get("path"),
                                              "bytes": 1, "created": True})]
            return [_result(frame, name, {"echo": True})]
        return []

    return reply


async def _ready(pilot, app) -> None:
    for _ in range(80):
        if app.session.state.value == "connected":
            break
        await pilot.pause(0.02)
    await pilot.pause(0.1)


def _text(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)


async def _run(reply, body):
    from bridge.ui import BridgeApp

    session = Session(FakeTransport(on_write=reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _ready(pilot, app)
        try:
            await body(app, pilot)
        finally:
            await session.close()


async def test_a_failed_status_shows_the_code_not_silence():
    async def body(app, pilot):
        await app._request("status")
        await pilot.pause(0.1)
        assert "E_STORAGE" in _text(app)

    await _run(_scripted({"status": {"code": "E_STORAGE", "message": "usage failed"}}), body)


async def test_a_timed_out_command_says_so_instead_of_hanging():
    async def body(app, pilot):
        result = await app._request("status")
        # No reply arrives: `_request` must return rather than block the ui.
        assert result is None
        assert "did not answer in time" in _text(app)

    await _run(_scripted(drop={"status"}), body)


async def test_a_command_answered_with_the_wrong_type_is_dropped():
    """`04` §2: only `command.result` answers a command. Anything else is not a
    result, and showing it as one would invent a body the device never sent."""

    def wrong_type(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame["type"] == "command":
            return [encode(Frame(type="event", id="d-9", ts=0,
                                 body={"kind": "noise"}, reply_to=frame["id"]))]
        return []

    async def body(app, pilot):
        assert await app._request("status") is None

    await _run(wrong_type, body)


async def test_a_failed_write_is_reported_and_never_retried():
    """A refused chunk must not be silently retried: staging would splice two
    transfers together (`04` §4.20)."""
    sent: list[str] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame["type"] == "command":
            sent.append(frame["name"])
            return [_result(frame, frame["name"],
                            {"code": "E_STORAGE", "message": "quota"}, ok=False)]
        return []

    async def body(app, pilot):
        await app.save_file("/workspace/a.txt", "x" * 5000)
        await pilot.pause(0.1)
        assert sent.count("sandbox.write") == 3
        assert "E_STORAGE" in _text(app)

    await _run(reply, body)


async def test_a_truncated_file_is_not_opened_for_editing():
    """Editing a partial read would save the cut over the original."""
    sent: list[str] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame["type"] == "command":
            sent.append(frame["name"])
            return [_result(frame, "sandbox.read",
                            {"path": "/workspace/big", "text": "x" * 10,
                             "bytes": 99999, "truncated": True})]
        return []

    async def body(app, pilot):
        await app.view_file("/workspace/big")
        await pilot.pause(0.1)
        # Refused before the editor: a write here would destroy the tail.
        assert "sandbox.write" not in sent
        assert "above the read cap" in _text(app)

    await _run(reply, body)


async def test_browsing_outside_the_sandbox_is_refused_locally():
    """`06` §6.1: the ui refuses before the wire, so a bad path costs no frame."""
    transport = FakeTransport(on_write=_scripted())
    session = Session(transport, ack_timeout=1, ping_interval=99)
    from bridge.ui import BridgeApp

    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _ready(pilot, app)
        before = len(transport.written)
        await app.browse("/workspace/../../etc")
        await pilot.pause(0.1)
        assert len(transport.written) == before
        assert "[error]" in _text(app)
        await session.close()


async def test_a_missing_file_is_reported_as_an_error():
    async def body(app, pilot):
        await app.view_file("/workspace/nope.txt")
        await pilot.pause(0.1)
        assert "E_STORAGE" in _text(app)

    await _run(_scripted(), body)


async def test_deleting_a_directory_asks_before_sending_anything():
    sent: list[str] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame["type"] == "command":
            sent.append(frame["name"])
            return [_result(frame, "sandbox.list",
                            {"path": "/workspace/",
                             "entries": [{"name": "runs", "dir": True, "bytes": 0}]})]
        return []

    async def body(app, pilot):
        await app.browse("/workspace/")
        await pilot.pause(0.1)
        table = app._files().query_one("#files-table")
        table.focus()
        table.move_cursor(row=0)
        # A directory is now listed and its count named before the confirmation;
        # the delete is only sent after `y` (`05` §5.8). Driven by the key rather
        # than the method because `push_screen_wait` needs the binding's worker.
        await pilot.press("ctrl+d")
        await pilot.pause(0.2)
        assert "sandbox.delete" not in sent
        assert app.screen.question

    await _run(reply, body)


async def test_a_task_mutation_failure_is_shown():
    async def body(app, pilot):
        await app._request("tasks.set", {"task": {"id": "t1", "enabled": False}})
        await pilot.pause(0.1)
        assert "E_CONFIG" in _text(app)

    await _run(_scripted({"tasks.set": {"code": "E_CONFIG", "message": "bad task"}}), body)


async def test_an_unknown_log_level_is_reported_not_sent():
    """`05` §5.4: a typo in the filter is a local message, never a frame."""

    async def body(app, pilot):
        app._logs().apply_filter("wifi", "info")
        with pytest.raises(ValueError, match="unknown log level"):
            app._logs().apply_filter("wifi", "nonsense")
        # The refused value left the previous filter in place.
        assert app._logs().buffer.log_filter.min_level == "info"

    await _run(_scripted(), body)


async def test_a_link_rejection_is_shown_as_a_banner():
    async def body(app, pilot):
        app._on_frame(Frame(type="event", id="d-8", ts=0,
                            body={"kind": "link.rejected", "data": {"reason": "auth"}}))
        await pilot.pause(0.05)
        assert "link rejected" in _text(app)

    await _run(_scripted(), body)


async def test_an_agent_error_is_shown_with_its_code():
    async def body(app, pilot):
        app._on_frame(Frame(type="agent.error", id="d-8", ts=0,
                            body={"code": "E_LLM_RATE", "message": "slow down",
                                  "retryable": True}))
        await pilot.pause(0.05)
        text = _text(app)
        assert "E_LLM_RATE" in text and "slow down" in text

    await _run(_scripted(), body)


async def test_a_protocol_event_reaches_the_logs_not_the_chat():
    async def body(app, pilot):
        app._on_frame(Frame(type="event", id="d-8", ts=0,
                            body={"kind": "protocol.error", "data": {"code": "E_PROTOCOL"}}))
        await pilot.pause(0.05)
        # `05` §5.4: an untargeted event belongs in Logs, not the conversation.
        assert "E_PROTOCOL" in "\n".join(app._logs().buffer.visible())
        assert "E_PROTOCOL" not in _text(app)

    await _run(_scripted(), body)

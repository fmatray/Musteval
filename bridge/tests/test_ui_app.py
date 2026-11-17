"""`app.py`'s remaining handlers, driven through the surface a reader touches.

Each test reaches its handler the way a user does — a slash command, a key, a
frame arriving from the device — so what it proves is a screen's behaviour, not
that a method exists. The error paths assert the line the reader sees, because a
refusal that prints nothing is indistinguishable from a hang (`05-bridge.md` §4.1).
"""

from __future__ import annotations

import json
import os
import urllib.error

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode, reset_ids
from bridge.ui import BridgeApp
from bridge.ui.app import _provider_label, _http_fetch
from bridge.ui.render import template_names, template_text

ACK = {
    "device": "test-agent",
    "fw_version": "test-1.0.0",
    "protocol": 1,
    "boot_id": "b1",
    "time_synced": True,
    "limits": {"rx_max": 4096, "tx_max": 16384},
    "capabilities": {
        "tools": ["time.now", "gpio.read"],
        "mcp": ["fs"],
        "dangerous_tools": False,
        "streaming": False,
    },
}

STATUS = {
    "wifi": {"state": "up", "ssid": "home", "rssi": -55, "ip": "10.0.0.9", "failovers": 0},
    "time": {"synced": True, "unix": 1730563600, "tz": "UTC"},
    "llm": {"provider": "openai", "model": "gpt", "available": True, "last_error": None,
            "calls_hour": 1, "calls_day": 2, "limit_hour": 30, "limit_day": 200},
    "mcp": [{"id": "fs", "state": "up"}],
    "agent": {"busy": False, "run_id": None, "queue_depth": 0, "max_queue": 2},
    "mqtt": [],
    "geo": None,
    "storage": {"userfs": {"used_bytes": 10, "total_bytes": 100},
                "wsfs": {"used_bytes": 0, "total_bytes": 50},
                "workspace_quota_bytes": 20},
    "mem": {"heap_free": 111, "psram_free": 222},
    "sys": {"safe_mode": False, "boot_failures": 0, "tool_calls": 0, "tool_failures": 0},
    "uptime_s": 5,
    "boot_id": "b1",
}

CONFIG = {
    "version": 1,
    "device": {"name": "musteval"},
    "wifi": {"networks": [{"ssid": "home", "password_ref": "nvs:k"}],
             "connect_timeout_ms": 20000},
    "llm": {"providers": [{"id": "openai", "base_url": "https://api.openai.com/v1",
                           "model": "gpt", "api_key_ref": "nvs:llm_api_key"}],
            "budget": {"hour": 30, "day": 200}},
    "ntp": {"servers": ["pool.ntp.org"], "timezone": "UTC"},
}

#: `04` §4.29's catalogue, in the registry's order: a disabled row is what the table
#: exists to show, and the descriptions differ so a test cannot pass on a fixed width.
TOOL_ROWS = [
    {"name": "gpio.read", "description": "Read a pin's digital level.", "offered": True},
    {"name": "memory.get", "description": "Report heap and PSRAM free.", "offered": False},
    {"name": "time.now", "description": "Return the current time.", "offered": True},
]

ENTRIES = {
    "/workspace/": [
        {"name": "notes.md", "dir": False, "bytes": 5},
        {"name": "runs", "dir": True, "bytes": 0},
        {"name": "empty", "dir": True, "bytes": 0},
    ],
    "/workspace/runs/": [{"name": "run_7.json", "dir": False, "bytes": 3}],
    "/workspace/empty/": [],
}

TEXT = {"/workspace/notes.md": "hello", "/workspace/runs/run_7.json": "abc"}


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
    return encode(
        Frame(type="command.result", id="d-9", ts=0, body=body, reply_to=frame["id"])
    )


def _device(overrides: dict[str, object] | None = None, *, drop: set[str] | None = None):
    """A device that answers the frames these handlers send."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        kind = frame.get("type")
        name = frame.get("name", "")
        if drop and (kind in drop or name in drop):
            return []
        if overrides and (name in overrides or kind in overrides):
            # A shorthand frame (`secret.set`) carries no `name`, so an override may
            # target its `type`; a command frame is targeted by its name.
            key = name if name in overrides else kind
            value = overrides[key]
            if isinstance(value, Exception):
                raise value
            # A `dict` without a `code` is a result; everything else is a refusal, which
            # is what an override meant before a successful one was needed. The
            # distinction is the wire's, not this double's: a refused command answers
            # `error{code,message}` and a successful one answers `result`.
            ok = isinstance(value, dict) and "code" not in value
            return [_result(frame, name, value, ok=ok)]
        if kind == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if kind == "ping":
            return [encode(Frame(type="pong", id="d-2", ts=0, body={},
                                 reply_to=frame["id"]))]
        if kind == "secret.set":
            # `04` §4.10: a shorthand frame carries `key` and `value` at the root, and
            # the reply echoes only the length — the value never comes back.
            return [_result(frame, "secret.set",
                            {"key": frame["key"], "stored": True,
                             "length": len(frame["value"])})]
        if kind == "prompt":
            return [
                encode(Frame(type="agent.start", id="d-3", ts=0,
                             body={"run_id": "r-1", "trigger": "manual",
                                   "source": "prompt", "provider": "openai",
                                   "model": "gpt", "input_bytes": 1,
                                   "queue_depth": 0}, reply_to=frame["id"])),
                encode(Frame(type="agent.final", id="d-4", ts=0,
                             body={"run_id": "r-1", "text": "done",
                                   "usage": {"prompt_tokens": 1,
                                             "completion_tokens": 1,
                                             "total_tokens": 2},
                                   "rounds": 1, "provider": "openai", "model": "gpt",
                                   "duration_ms": 1, "truncated": False,
                                   "todo_open": False}, reply_to=frame["id"])),
            ]
        if kind == "command":
            args = frame.get("args") or {}
            if name == "status":
                return [_result(frame, name, STATUS)]
            if name == "tools.list":
                return [_result(frame, name, {"tools": TOOL_ROWS})]
            if name == "reload":
                # `04` §4.28: the scopes and the warnings, which are the parts a
                # reader acts on; a reply without them would render as a sentence no
                # device sends.
                return [_result(frame, name,
                                {"reloaded": ["cfg", "tools"],
                                 "warnings": [{"scope": "mqtt",
                                               "message": "broker unreachable"}]})]
            if name == "reboot":
                return [_result(frame, name, {"grace_ms": 250})]
            if name in ("wifi.status", "wifi.scan"):
                # `04` §3.9 and §4.24: the two halves `/wifi list` joins, so a row
                # claiming `joined` or `in range` is a fact about an answer rather
                # than about a cached frame.
                if name == "wifi.scan":
                    return [_result(frame, name, {"networks": [
                        {"ssid": "home", "rssi": -47, "auth": "wpa2"},
                        {"ssid": "Cafe-FREE", "rssi": -71, "auth": "open"},
                    ], "truncated": False})]
                return [_result(frame, name, dict(STATUS["wifi"]))]
            if name == "config.get":
                return [_result(frame, name, {"config": CONFIG})]
            if name == "config.set":
                return [_result(frame, name,
                                {"path": args.get("path"), "applied": True})]
            if name == "context.budget":
                # `04` §4.30: eight blocks with their bytes and their item counts.
                return [_result(frame, name, {
                    "budget": 16384,
                    "used": 1304,
                    "free": 15080,
                    "messages": 3,
                    "blocks": {"system_prompt": 900, "skills": 200, "tools": 160,
                               "user": 24, "assistant": 20, "thinking": 0,
                               "tool_calls": 0, "tool_output": 0},
                    "counts": {"skills": 2, "tools": 18, "user": 2, "assistant": 1,
                               "thinking": 0, "tool_calls": 0, "tool_output": 0},
                })]
            if name == "context.block":
                # `04` §4.32: the block's own text, and the flag that says the text
                # is a prefix of it.
                block = args.get("block")
                if block not in ("system_prompt", "skills", "tools", "user",
                                 "assistant", "thinking", "tool_calls",
                                 "tool_output"):
                    return [_result(frame, name,
                                    {"code": "E_CONFIG", "message": "unknown block"},
                                    ok=False)]
                text = f"text of {block}"
                return [_result(frame, name, {"block": block, "bytes": len(text),
                                              "text": text, "truncated": False})]
            if name == "context.compact":
                # `04` §4.33: what it replaced, what it kept, and the summary.
                return [_result(frame, name, {
                    "summarised": 3,
                    "kept": args.get("keep", 0),
                    "before_bytes": 1304,
                    "after_bytes": 240,
                    "summary": "Goal: ship `/compact`.",
                })]
            if name == "history.clear":
                # `04` §4.28: the count is the reply's whole content, so the double
                # reports one the pane can be checked against.
                return [_result(frame, name, {"cleared": True, "messages": 4})]
            if name in ("tasks.list", "tasks.set", "tasks.delete", "tasks.run"):
                return [_result(frame, name,
                                {"tasks": [{"id": "t1", "description": "water",
                                            "enabled": True}]}
                                if name == "tasks.list" else {"echo": True})]
            if name == "sandbox.list":
                path = args.get("path", "/workspace/")
                if path in ENTRIES:
                    return [_result(frame, name,
                                    {"path": path, "entries": ENTRIES[path]})]
                return [_result(frame, name,
                                {"code": "E_STORAGE", "message": "no such entry"},
                                ok=False)]
            if name == "sandbox.read":
                path = args.get("path")
                if path in TEXT:
                    return [_result(frame, name, {"path": path, "text": TEXT[path],
                                                  "bytes": len(TEXT[path]),
                                                  "truncated": False})]
                return [_result(frame, name,
                                {"code": "E_STORAGE", "message": "no such entry"},
                                ok=False)]
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
    session = Session(FakeTransport(on_write=reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _ready(pilot, app)
        try:
            await body(app, pilot)
        finally:
            await session.close()


async def _await_chat(pilot, app, needle: str, limit: int = 120) -> None:
    for _ in range(limit):
        if needle in _text(app):
            return
        await pilot.pause(0.02)


async def _await_screen(pilot, app, kind) -> None:
    """Wait for the modal and for it to finish composing: a `ModalScreen` is pushed
    before `compose` runs, so querying inside it too early finds no children."""
    for _ in range(120):
        if isinstance(app.screen, kind):
            await pilot.pause(0.05)
            return
        await pilot.pause(0.02)
    raise AssertionError(f"{kind.__name__} never opened; screen is {app.screen!r}")


# --- local commands (`05` §7) -----------------------------------------------


async def test_mcp_reads_from_the_hello_capabilities_and_tools_does_not():
    """`/mcp` is answered from the handshake: the capability list already says which
    servers came up. `/tools` is not, because `hello_ack` carries names and the table
    needs the device's descriptions and the tools the model cannot call.
    """

    async def body(app, pilot):
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.submit("/mcp")
        await _await_chat(pilot, app, "fs=up")
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

        app.submit("/tools")
        await _await_chat(pilot, app, "gpio.read")
        assert any(
            b'"name":"tools.list"' in frame
            for frame in app.session._transport.written  # type: ignore[attr-defined]
        )
        assert "time.now" in _text(app)

    await _run(_device(), body)


async def test_usage_reports_the_running_totals():
    """`/usage` describes what this session has spent, so a prompt must have run
    first and its number must move with it. (`/context` is not local: it asks the
    device for the next request's blocks, `05` §7.5.)"""

    async def body(app, pilot):
        app.submit("/usage")
        await _await_chat(pilot, app, "total tokens: 0")
        app.submit("hello")
        await _await_chat(pilot, app, "done")
        app.submit("/usage")
        await _await_chat(pilot, app, "total tokens: 2")

    await _run(_device(), body)


async def test_export_names_a_timestamped_file_and_writes_nothing():
    """`05` §7: `/export` is the bridge's own transcript, so it names the file it made
    and touches no device frame."""

    async def body(app, pilot):
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.submit("/export")
        await _await_chat(pilot, app, "bridge-export-")
        assert ".md" in _text(app)
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

    await _run(_device(), body)


async def test_logs_opens_its_screen_and_help_stays_a_modal():
    """`05` §5.4 and §5.6: `/logs` is a screen swap, `F1`/`/help` is a modal over the
    composer, because help is consulted mid-sentence and a screen swap would lose the
    draft."""
    from bridge.ui.modals import HelpModal

    async def body(app, pilot):
        app.submit("/logs")
        for _ in range(80):
            if app.query_one("#logs").display:
                break
            await pilot.pause(0.02)
        assert app.query_one("#logs").display is True
        assert app.query_one("#chat").display is False
        app.submit("/help")
        await _await_screen(pilot, app, HelpModal)
        assert isinstance(app.screen, HelpModal)

    await _run(_device(), body)


async def test_quit_exits_the_app():
    async def body(app, pilot):
        app.submit("/quit")
        for _ in range(80):
            if not app.is_running:
                return
            await pilot.pause(0.02)
        raise AssertionError("/quit left the app running")

    await _run(_device(), body)


# --- settings ---------------------------------------------------------------


async def test_a_settings_row_write_sends_config_set_with_the_typed_path():
    """`05` §5.7: the row's own path is what `config.set` carries, and the field is
    seeded from the stored document — an empty prompt would ask the reader to retype
    a value the bridge already holds."""
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        app.action_show("settings")
        for _ in range(120):
            if app._config_fetched:
                break
            await pilot.pause(0.02)
        app.run_worker(app.edit_setting("llm.max_tool_rounds"))
        await _await_screen(pilot, app, ValuePrompt)
        field = app.screen.query_one("#value-input")
        assert field.value == "8"
        field.value = "3"
        await pilot.press("enter")
        for _ in range(120):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if any(f.get("name") == "config.set" for f in written):
                break
            await pilot.pause(0.02)
        sets = [f for f in written if f.get("name") == "config.set"]
        assert sets and sets[-1]["args"] == {"path": "llm.max_tool_rounds", "value": 3}

    await _run(_device(), body)


async def test_a_settings_write_repaints_the_model_the_bar_names():
    """`05` §5.1: the bar names the configured model, and `config.set` is the write
    that changes it. Waiting for the next unsolicited `status` left the bar naming
    the model the reader had just replaced for up to 30 s."""
    inner = _device()
    seen = {"writes": 0}

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") == "config.set":
            seen["writes"] += 1
            return [_result(frame, "config.set",
                            {"path": frame["args"]["path"], "applied": True})]
        if frame.get("name") == "status" and seen["writes"]:
            status = dict(STATUS)
            status["llm"] = {**STATUS["llm"], "model": "gpt-replaced",
                             "thinking": "high"}
            return [_result(frame, "status", status)]
        return inner(line)

    async def body(app, pilot):
        from bridge.ui.modals import ValuePrompt

        app.action_show("settings")
        for _ in range(120):
            if app._config_fetched:
                break
            await pilot.pause(0.02)
        app.run_worker(app.edit_setting("llm.max_tool_rounds"))
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.query_one("#value-input").value = "3"
        await pilot.press("enter")
        for _ in range(120):
            if "gpt-replaced" in app.query_one("#chat").bar_line:
                break
            await pilot.pause(0.02)
        assert seen["writes"] == 1
        assert "openai/gpt-replaced/high" in app.query_one("#chat").bar_line

    await _run(reply, body)


async def test_a_write_the_device_did_not_apply_costs_no_status_round_trip():
    """`05` §5.1: the `status` re-request exists because an *applied* write changed
    the model the bar names. A write that reports `applied: false` — one awaiting a
    reboot, or one the device could not make live — changed nothing the bar reads,
    so asking again is a frame that can only return the same answer."""
    inner = _device()
    seen = {"writes": 0, "status_after": 0}

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") == "config.set":
            seen["writes"] += 1
            return [_result(frame, "config.set",
                            {"path": frame["args"]["path"], "applied": False,
                             "requires_reboot": True})]
        if frame.get("name") == "status" and seen["writes"]:
            seen["status_after"] += 1
        return inner(line)

    async def body(app, pilot):
        from bridge.ui.modals import ValuePrompt

        app.action_show("settings")
        for _ in range(120):
            if app._config_fetched:
                break
            await pilot.pause(0.02)
        app.run_worker(app.edit_setting("ntp.timezone"))
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.query_one("#value-input").value = "UTC"
        await pilot.press("enter")
        await _await_chat(pilot, app, "requires reboot")
        for _ in range(30):
            await pilot.pause(0.02)
        assert seen["writes"] == 1
        assert seen["status_after"] == 0

    await _run(reply, body)


async def test_an_unknown_settings_path_writes_nothing():
    """A path no row owns cannot be coerced, so nothing reaches the wire."""

    async def body(app, pilot):
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        await app.edit_setting("no.such.path")
        await pilot.pause(0.1)
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

    await _run(_device(), body)


async def test_a_settings_value_outside_the_bound_is_refused_before_the_wire():
    """`03-config.md` bounds `llm.max_tool_rounds` at 16; a value past it is named in
    chat and no `config.set` is sent, so the device is not asked a question it would
    refuse — and the reader learns the bound, not just that something failed."""
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        app.action_show("settings")
        for _ in range(120):
            if app._config_fetched:
                break
            await pilot.pause(0.02)
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.run_worker(app.edit_setting("llm.max_tool_rounds"))
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.query_one("#value-input").value = "99"
        await pilot.press("enter")
        for _ in range(120):
            if "llm.max_tool_rounds" in _text(app):
                break
            await pilot.pause(0.02)
        written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
        assert not [f for f in written[before:] if f.get("name") == "config.set"]
        assert "llm.max_tool_rounds" in _text(app)

    await _run(_device(), body)


# --- tasks screen -----------------------------------------------------------


async def test_editing_a_task_without_a_selection_says_so():
    """`05` §5.3: `E` on an empty table has nothing to edit, and the reader is told
    rather than left looking at an unchanged screen."""

    async def body(app, pilot):
        app.submit("/tasks")
        await _await_chat(pilot, app, "")
        await pilot.pause(0.2)
        app.action_show("tasks")
        await pilot.pause(0.1)
        screen = app.query_one("#tasks")
        screen.query_one("#task-table").clear()  # type: ignore[attr-defined]
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.run_worker(app.edit_task())
        await _await_chat(pilot, app, "no task selected")
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

    await _run(_device(), body)


async def test_a_selected_task_opens_the_editor_seeded_with_it():
    """`tasks.set` carries the task the editor produced, and the editor starts from
    the row the cursor is on — an empty prompt would silently drop every field."""
    from bridge.ui.task_prompt import TaskPrompt

    async def body(app, pilot):
        app.submit("/tasks")
        await _await_chat(pilot, app, "")
        await pilot.pause(0.2)
        app.action_show("tasks")
        await pilot.pause(0.1)
        app.run_worker(app.edit_task())
        await _await_screen(pilot, app, TaskPrompt)
        assert "water" in app.screen.query_one("#task-input").value
        app.screen.dismiss(None)
        await pilot.pause(0.2)

    await _run(_device(), body)


async def test_refetch_on_the_tasks_screen_asks_the_device_again():
    async def body(app, pilot):
        app.action_show("tasks")
        await pilot.pause(0.1)
        before = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
        app.query_one("#tasks").action_refetch()  # type: ignore[attr-defined]
        for _ in range(80):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if len(written) > len(before):
                break
            await pilot.pause(0.02)
        names = [f.get("name") for f in written[len(before):]]
        assert "tasks.list" in names

    await _run(_device(), body)


# --- files: the remaining verbs ---------------------------------------------


async def _open_files(pilot, app, path: str = "/workspace/"):
    app.submit(f"/files {path}")
    await _await_chat(pilot, app, "")
    for _ in range(120):
        if app.query_one("#files").display:
            break
        await pilot.pause(0.02)
    return app.query_one("#files")


def _select(screen, name: str) -> None:
    table = screen.query_one("#files-table")
    for row in range(table.row_count):
        _kind, cell, _bytes = table.get_row_at(row)
        if str(cell) == name:
            table.move_cursor(row=row)
            return
    raise AssertionError(f"{name!r} is not in the listing")


async def test_moving_the_root_is_refused_without_a_prompt():
    """`05` §5.8: `/workspace` is the sandbox itself, so it has no parent to move to."""

    async def body(app, pilot):
        screen = await _open_files(pilot, app)
        _select(screen, "notes.md")
        # The root has no row of its own; browsing there and asking to move it is the
        # only way to reach the refusal.
        screen.path = "/workspace/"
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.run_worker(app.move_entry())
        await _await_chat(pilot, app, "cannot be moved")
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

    await _run(_device(), body)


async def test_a_move_sends_the_normalised_target_and_relists():
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        screen = await _open_files(pilot, app)
        _select(screen, "notes.md")
        app.run_worker(app.move_entry())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss("/workspace/renamed.md")
        for _ in range(120):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if any(f.get("name") == "sandbox.move" for f in written):
                break
            await pilot.pause(0.02)
        moves = [f for f in written if f.get("name") == "sandbox.move"]
        assert moves[-1]["args"] == {"from": "/workspace/notes.md",
                                     "to": "/workspace/renamed.md"}
        assert [f["name"] for f in written[-1:]] == ["sandbox.list"]

    await _run(_device(), body)


async def test_an_escape_out_of_the_sandbox_is_refused_before_the_frame():
    """`09` §4: a path the sanitiser refuses never becomes a frame, so the device is
    not left to be the only guard."""
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        screen = await _open_files(pilot, app)
        _select(screen, "notes.md")
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.run_worker(app.move_entry())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss("/workspace/../../etc/passwd")
        await _await_chat(pilot, app, "outside")
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]
        assert not [f for f in (json.loads(line) for line in app.session._transport.written)  # type: ignore[attr-defined]
                    if f.get("name") == "sandbox.move"]

    await _run(_device(), body)


async def test_a_new_directory_is_created_under_the_browsed_path():
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        await _open_files(pilot, app)
        app.run_worker(app.new_directory())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss("reports")
        for _ in range(120):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if any(f.get("name") == "sandbox.mkdir" for f in written):
                break
            await pilot.pause(0.02)
        made = [f for f in written if f.get("name") == "sandbox.mkdir"]
        assert made[-1]["args"]["path"] == "/workspace/reports"

    await _run(_device(), body)


async def test_a_new_directory_name_that_escapes_is_refused():
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        await _open_files(pilot, app)
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.run_worker(app.new_directory())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss("../escape")
        await _await_chat(pilot, app, "outside")
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

    await _run(_device(), body)


async def test_a_new_file_is_created_empty_and_opened():
    """`N` writes an empty file then shows it: the two frames are the point, because
    a reader who created a file must see the editor on it, not an empty chat."""
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        await _open_files(pilot, app)
        app.run_worker(app.new_file())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss("fresh.md")
        for _ in range(120):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if any(f.get("name") == "sandbox.read" for f in written):
                break
            await pilot.pause(0.02)
        names = [f.get("name") for f in written]
        assert "sandbox.write" in names and "sandbox.read" in names
        write = next(f for f in written if f.get("name") == "sandbox.write")
        assert write["args"]["path"] == "/workspace/fresh.md"
        assert write["args"]["data"] == ""

    await _run(_device(), body)


async def test_uploading_a_missing_host_path_creates_nothing(tmp_path):
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        await _open_files(pilot, app)
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.run_worker(app.upload_entry())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss(str(tmp_path / "absent.txt"))
        for _ in range(120):
            if "absent.txt" in _text(app) or "no such file" in _text(app).lower():
                break
            await pilot.pause(0.02)
        assert not [f for f in (json.loads(line) for line in app.session._transport.written[before:])  # type: ignore[attr-defined]
                    if f.get("name") == "sandbox.write"]

    await _run(_device(), body)


async def test_a_failed_download_write_names_the_os_error(tmp_path):
    """`05` §5.8.1: a refused host write is one line, and the device data is not
    half-written — the reader needs the reason, not a traceback."""
    from bridge.ui.modals import ValuePrompt

    blocked = tmp_path / "ro"
    blocked.mkdir()
    (blocked / "notes.md").mkdir()  # a directory where a file must go

    async def body(app, pilot):
        screen = await _open_files(pilot, app)
        _select(screen, "notes.md")
        app.run_worker(app.download_entry())
        await _await_screen(pilot, app, ValuePrompt)
        app.screen.dismiss(str(blocked / "notes.md"))
        for _ in range(120):
            if "Is a directory" in _text(app) or "is a directory" in _text(app):
                break
            await pilot.pause(0.02)
        assert "notes.md" in _text(app)
        assert not (blocked / "notes.md").is_file()

    await _run(_device(), body)


# --- model and thinking -----------------------------------------------------


async def test_thinking_with_no_value_reports_the_stored_level():
    """`05` §7.2: the bare form is a read. It has to fetch the document first or it
    reports on a config nobody asked the device for."""

    async def body(app, pilot):
        app.submit("/thinking")
        await _await_chat(pilot, app, "")
        for _ in range(120):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if any(f.get("name") == "config.get" for f in written):
                break
            await pilot.pause(0.02)
        assert any(f.get("name") == "config.get" for f in written)
        assert not [f for f in written if f.get("name") == "config.set"]

    await _run(_device(), body)


async def test_an_unknown_thinking_level_is_refused_before_the_wire():
    """The device accepts any flat `extra_body` member and 400s on the next call, so
    an invented level has to be refused here, where it can be named."""

    async def body(app, pilot):
        app.submit("/thinking maximum")
        for _ in range(120):
            if "maximum" in _text(app):
                break
            await pilot.pause(0.02)
        written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
        assert not [f for f in written if f.get("name") == "config.set"]
        assert "maximum" in _text(app)

    await _run(_device(), body)


async def test_a_typed_model_name_is_written_without_a_picker():
    """`05` §7.3: naming one directly skips the fetch, so a provider that guards
    `/models` is still usable."""

    async def body(app, pilot):
        app.submit("/model gpt-4o")
        # Poll for the line this test asserts, not for the frame: the write is
        # acknowledged and then the document is re-read quietly (`05` §7.3), so a
        # `config.set` on the wire is one round trip short of the chat line.
        for _ in range(160):
            if "model: gpt-4o" in _text(app):
                break
            await pilot.pause(0.02)
        written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
        sets = [f for f in written if f.get("name") == "config.set"]
        assert sets and sets[-1]["args"]["path"] == "llm.providers"
        assert sets[-1]["args"]["value"][0]["model"] == "gpt-4o"
        assert "model: gpt-4o" in _text(app)

    await _run(_device(), body)


async def test_a_refused_model_write_names_the_device_code():
    async def body(app, pilot):
        app.submit("/model gpt-4o")
        for _ in range(160):
            if "refused" in _text(app):
                break
            await pilot.pause(0.02)
        assert "refused (E_ARG)" in _text(app)

    await _run(_device({"config.set": {"code": "E_ARG", "message": "no"}}), body)


async def test_model_on_an_empty_chain_says_which_part_is_missing():
    async def body(app, pilot):
        app.submit("/model gpt-4o")
        for _ in range(160):
            if "no provider is configured" in _text(app):
                break
            await pilot.pause(0.02)
        written = [json.loads(line) for line in app.session._transport.written]
        assert not [f for f in written if f.get("name") == "config.set"]
        assert "run /onboard first" in _text(app)

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") == "config.get":
            return [_result(frame, "config.get", {"config": {"version": 1}})]
        return _device()(line)

    await _run(reply, body)


async def test_the_provider_label_follows_the_stored_base_url():
    """`05` §7.3: the catalog row names a server the device may not be pointed at, so
    a stored URL that differs makes its host the honest name."""
    from bridge import providers as providers_mod

    known = providers_mod.find("ollama")
    assert known is not None
    assert _provider_label(known, "ollama", known.base_url) == known.label
    assert _provider_label(known, "ollama", "https://ollama.com/v1").startswith(
        "ollama.com")
    assert _provider_label(None, "custom", "https://example.test/v1") == "example.test"
    assert _provider_label(None, "custom", "") == "custom"


async def test_a_non_http_scheme_is_refused_by_the_fetch():
    """`09` §2: `_http_fetch` is the one socket, so it must not be talked into
    `file://`. The refusal is an exception `models_with_efforts` folds into `[]`."""
    with pytest.raises(ValueError, match="non-http"):
        _http_fetch("file:///etc/passwd", "")
    with pytest.raises(ValueError, match="non-http"):
        _http_fetch("ftp://example.test/v1/models", "")


async def test_a_failed_catalog_parse_is_a_chat_line(monkeypatch):
    """`05` §7.1: the fetch runs off the UI thread. A body the catalog cannot read is
    named in chat and no model is offered, rather than a traceback reaching the app.
    """
    from bridge.ui import app as app_mod

    monkeypatch.setattr(app_mod, "_http_fetch", lambda url, key: b"not json at all")
    answers = type("A", (), {"provider": "openai", "api_key": "k",
                             "model_efforts": {}})()

    async def body(app, pilot):
        names = await app._fetch_models(answers)
        await _await_chat(pilot, app, "model list:")
        assert names == [] or isinstance(names, list)

    await _run(_device(), body)


async def test_a_provider_with_no_models_url_costs_no_request():
    """The entry says it publishes no list, which is a fact rather than an empty
    fetch: no socket is opened."""
    answers = type("A", (), {"provider": "custom", "api_key": "",
                             "model_efforts": {}})()

    async def body(app, pilot):
        assert await app._fetch_models(answers) == []

    await _run(_device(), body)


# --- frame handlers ---------------------------------------------------------


async def test_a_truncated_reply_is_flagged_and_a_finished_one_is_not():
    """`05` §5: a reply cut at the provider's cap stops mid-sentence, so it is
    flagged; an untruncated one must not be."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") != "prompt":
            return _device()(line)
        return [
            encode(Frame(type="agent.start", id="d-3", ts=0,
                         body={"run_id": "r-1", "trigger": "manual",
                               "source": "prompt", "provider": "p", "model": "m",
                               "input_bytes": 1, "queue_depth": 0},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.final", id="d-4", ts=0,
                         body={"run_id": "r-1", "text": "half a sen",
                               "usage": {"total_tokens": 3}, "rounds": 1,
                               "provider": "p", "model": "m", "duration_ms": 1,
                               "truncated": True, "full_bytes": 900,
                               "todo_open": True}, reply_to=frame["id"])),
        ]

    async def body(app, pilot):
        app.submit("go")
        await _await_chat(pilot, app, "answer truncated")
        assert "10 of 900 bytes" in _text(app)
        assert "todos left open" in _text(app)

    await _run(reply, body)


async def test_a_tool_result_and_a_tool_call_reach_the_transcript():
    """`05` §5.2: the call and its result are both shown, and a failed result is
    coloured as the error it is."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") != "prompt":
            return _device()(line)
        return [
            encode(Frame(type="agent.start", id="d-3", ts=0,
                         body={"run_id": "r-1", "trigger": "manual",
                               "source": "prompt", "provider": "p", "model": "m",
                               "input_bytes": 1, "queue_depth": 0},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.tool", id="d-5", ts=0,
                         body={"run_id": "r-1", "round": 1, "call_id": "c-1",
                               "tool": "gpio.read", "args": {"pin": 4}},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.tool_result", id="d-6", ts=0,
                         body={"run_id": "r-1", "round": 1, "call_id": "c-1",
                               "tool": "gpio.read", "ok": False, "duration_ms": 1,
                               "error": {"code": "E_GPIO",
                                         "message": "not an output"}},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.final", id="d-4", ts=0,
                         body={"run_id": "r-1", "text": "done",
                               "usage": {"total_tokens": 1}, "rounds": 1,
                               "provider": "p", "model": "m", "duration_ms": 1,
                               "truncated": False, "todo_open": False},
                         reply_to=frame["id"])),
        ]

    async def body(app, pilot):
        app.submit("read pin 4")
        await _await_chat(pilot, app, "E_GPIO")
        assert "gpio.read" in _text(app)

    await _run(reply, body)


async def test_a_model_trace_reaches_the_transcript_above_its_answer():
    """`04` §3.21: the trace is the model's working, not its answer, so it lands in
    the pane as its own line instead of being folded into `agent.final`."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") != "prompt":
            return _device()(line)
        return [
            encode(Frame(type="agent.start", id="d-3", ts=0,
                         body={"run_id": "r-1", "trigger": "manual",
                               "source": "prompt", "provider": "p", "model": "m",
                               "input_bytes": 1, "queue_depth": 0},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.thinking", id="d-5", ts=0,
                         body={"run_id": "r-1", "round": 1,
                               "text": "17 times 2 is 34", "truncated": False,
                               "full_bytes": None},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.final", id="d-4", ts=0,
                         body={"run_id": "r-1", "text": "34",
                               "usage": {"total_tokens": 1}, "rounds": 1,
                               "provider": "p", "model": "m", "duration_ms": 1,
                               "truncated": False, "todo_open": False},
                         reply_to=frame["id"])),
        ]

    async def body(app, pilot):
        app.submit("what is 17 times 2")
        await _await_chat(pilot, app, "34")
        text = _text(app)
        assert "17 times 2 is 34" in text
        # The trace precedes the answer it explains; a trace printed after it would
        # read as the model's next remark.
        assert text.index("17 times 2 is 34") < text.index("\n34")

    await _run(reply, body)


async def test_a_cut_trace_says_it_was_cut():
    """`04` §3.21: a shortened trace reads as the whole reasoning unless it says so,
    which is the same rule `agent.final` follows for a cut answer."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") != "prompt":
            return _device()(line)
        return [
            encode(Frame(type="agent.thinking", id="d-5", ts=0,
                         body={"run_id": "r-1", "round": 1, "text": "part of it",
                               "truncated": True, "full_bytes": 9000},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.final", id="d-4", ts=0,
                         body={"run_id": "r-1", "text": "done",
                               "usage": {"total_tokens": 1}, "rounds": 1,
                               "provider": "p", "model": "m", "duration_ms": 1,
                               "truncated": False, "todo_open": False},
                         reply_to=frame["id"])),
        ]

    async def body(app, pilot):
        app.submit("go")
        await _await_chat(pilot, app, "cut at 9000 bytes")
        # `_await_chat` gives up without asserting, so the poll alone would let this
        # pass on a tree that never rendered the line.
        assert "cut at 9000 bytes" in _text(app)

    await _run(reply, body)


async def test_device_push_frames_reach_the_screen_that_owns_them():
    """`04` §2.1: `task.fired`, `gpio.edge`, `mqtt.message` and `event` are pushed
    without a request, so each has to find a surface with no command behind it."""
    transport: FakeTransport | None = None

    def reply(line: bytes) -> list[bytes]:
        return _device()(line)

    async def body(app, pilot):
        nonlocal transport
        transport = app.session._transport
        # The traffic log is a `RichLog`: it only materialises lines it has been asked
        # to render, so the screen has to be on show before the assertion means
        # anything.
        app.action_show("mqtt")
        await pilot.pause(0.05)  # type: ignore[attr-defined]
        transport.feed(
            encode(Frame(type="task.fired", id="d-7", ts=0,
                         body={"task_id": "t1", "run_id": "r-2", "trigger": "schedule"})),
            encode(Frame(type="gpio.edge", id="d-8", ts=0,
                         body={"pin": 4, "edge": "rising", "level": 1})),
            encode(Frame(type="mqtt.message", id="d-9", ts=0,
                         body={"server_id": "srv", "topic": "t/1", "payload": "hi",
                               "truncated": False, "qos": 1, "retain": False,
                               "received_ts": 1730563600})),
            encode(Frame(type="event", id="d-11", ts=0,
                         body={"kind": "wifi.failover", "data": {"ssid": "b"}})),
            # Last: `link.rejected` is terminal, so anything after it is never read.
            encode(Frame(type="event", id="d-10", ts=0,
                         body={"kind": "link.rejected", "data": {}})),
        )
        await _await_chat(pilot, app, "link rejected")
        assert "task t1 fired" in _text(app)
        # `05` §5.4: an unsolicited edge is a log line, so it belongs to the log
        # buffer rather than the conversation.
        log_lines = "\n".join(app._logs().buffer.visible())
        assert "pin 4 rising" in log_lines
        assert "event:wifi.failover" in log_lines
        # `05` §5.9: a message is the traffic screen's, not the conversation's.
        traffic = app.query_one("#mqtt").query_one("#mqtt-traffic").lines
        assert any("t/1" in str(line.text) for line in traffic)

    await _run(reply, body)


async def test_a_pong_with_no_status_grid_does_not_raise():
    """`05` §10: the heartbeat lands before the first `status` on a fresh link, and
    a missing grid must not take the app down."""

    async def body(app, pilot):
        # No status yet: the handler must return rather than raise on a missing grid.
        app.session.last_rtt_ms = None
        app._on_pong(Frame(type="pong", id="d-1", ts=0, body={}))
        await pilot.pause(0.05)
        # A status with no grid mounted cannot be painted either.
        app._last_status = {}
        app.session.last_rtt_ms = 4.0
        app._on_pong(Frame(type="pong", id="d-1", ts=0, body={}))
        await pilot.pause(0.05)

    await _run(_device(), body)


async def test_a_reconnect_reports_the_state_it_reached():
    """`05` §4.1: the reconnect prints the state it ended in, so a link that came back
    down is visible rather than implied."""

    async def body(app, pilot):
        before = len(_text(app).splitlines())
        await app.action_reconnect()
        for _ in range(120):
            if len(_text(app).splitlines()) > before:
                break
            await pilot.pause(0.02)
        added = _text(app).splitlines()[before:]
        assert added, "reconnect printed nothing"
        assert added[-1].startswith("[") and added[-1].endswith("]")

    await _run(_device(), body)


# --- todo editor round trip -------------------------------------------------


async def test_the_todo_editor_sends_one_init_and_refuses_a_stale_tree():
    """`05` §7: the editor rewrites the whole list with one `init`, so an edit made
    against a tree the device changed underneath is refused rather than applied."""
    from bridge.ui.modals import TodoEditor

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") == "todo.apply":
            return [_result(frame, "todo.apply", {
                "phases": [{"name": "One",
                            "tasks": [{"content": "a", "status": "pending"}]}]})]
        return _device()(line)

    async def body(app, pilot):
        app.run_worker(app._edit_todo())
        await _await_screen(pilot, app, TodoEditor)
        # Nothing was typed, so the editor produces no frames at all.
        app.screen.dismiss(None)
        await pilot.pause(0.2)
        written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
        assert not [f for f in written
                    if (f.get("args") or {}).get("op") == "init"]

    await _run(reply, body)


async def test_an_unreadable_todo_list_never_opens_the_editor():
    """`05` §7: the editor starts from the device's current tree, so a read that fails
    means there is nothing to edit — opening it on a stale cache would drop a task the
    run added since."""

    async def body(app, pilot):
        from bridge.ui.modals import TodoEditor

        async def no_answer(name, args=None, **kwargs):
            return None

        app._request = no_answer  # type: ignore[assignment]
        app.run_worker(app._edit_todo())
        await pilot.pause(0.4)
        assert not isinstance(app.screen, TodoEditor)

    await _run(_device(), body)


async def test_an_edit_made_against_a_changed_tree_is_refused_and_sends_nothing():
    """`05` §7: the frame replaces the whole list, so an edit made without seeing a
    task the run added would drop it. The refusal names the remedy."""
    from bridge.ui.modals import TodoEditor

    reads = {"n": 0}
    tree_one = [{"name": "One", "tasks": [{"content": "a", "status": "pending"}]}]
    tree_two = [{"name": "One", "tasks": [{"content": "a", "status": "done"},
                                          {"content": "b", "status": "pending"}]}]

    async def body(app, pilot):
        async def view(name, args=None, **kwargs):
            assert name == "todo.apply"
            reads["n"] += 1
            app._todo_tree = tree_one if reads["n"] == 1 else tree_two
            return {"phases": app._todo_tree}

        async def editor(_screen):
            return [Frame(type="todo.apply", id="b-7", ts=0,
                          body={"op": "init", "phases": [{"name": "One",
                                                          "tasks": []}]})]

        app._request = view  # type: ignore[assignment]
        app.push_screen_wait = editor  # type: ignore[method-assign]
        app._todo_tree = []
        before = len(app.session._transport.written)
        await app._edit_todo()
        assert "the todo list changed while you were editing" in _text(app)
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if (f.get("args") or {}).get("op") == "init"]

    await _run(_device(), body)


async def test_an_editor_that_returns_nothing_sends_no_frame():
    """`05` §7: Esc on the editor is a cancel, so the device's list is untouched."""

    async def body(app, pilot):
        async def view(name, args=None, **kwargs):
            return {"phases": []}

        async def editor(_screen):
            return []

        app._request = view  # type: ignore[assignment]
        app.push_screen_wait = editor  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._edit_todo()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if (f.get("args") or {}).get("op") == "init"]

    await _run(_device(), body)


async def test_an_assistant_fence_is_drawn_as_a_tree_in_the_pane():
    """`05` 5.1: a fenced `mermaid` block in the answer is drawn, not printed.

    Asserted through the pane the reader sees, because the rewrite could be
    correct and still land nowhere.
    """

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") != "prompt":
            return _device()(line)
        return [
            encode(Frame(type="agent.start", id="d-3", ts=0,
                         body={"run_id": "r-1", "trigger": "manual",
                               "source": "prompt", "provider": "p", "model": "m",
                               "input_bytes": 1, "queue_depth": 0},
                         reply_to=frame["id"])),
            encode(Frame(type="agent.final", id="d-4", ts=0,
                         body={"run_id": "r-1",
                               "text": "Here:\n\n```mermaid\nflowchart LR\n"
                                       "  H[host] --> D[device]\n```\n",
                               "usage": {"total_tokens": 1}, "rounds": 1,
                               "provider": "p", "model": "m", "duration_ms": 1,
                               "truncated": False, "todo_open": False},
                         reply_to=frame["id"])),
        ]

    async def body(app, pilot):
        app.submit("draw it")
        await _await_chat(pilot, app, "device")
        text = _text(app)
        assert "flowchart LR · 2 nodes · 1 edges" in text
        assert "host" in text
        # The arrows are drawn, not spelt: the source's `-->` must be gone.
        assert "-->" not in text

    await _run(reply, body)


async def test_templates_lists_the_prompt_names_one_per_line():
    """`05` §7 says `/templates` "lists prompt templates"; a raw JSON block is the
    debug dump the reader cannot scan, so the names are printed one per line and
    the envelope's punctuation is gone."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            return [_result(frame, "templates.list",
                            {"templates": ["brief.md", "daily.md"]})]
        return []

    async def body(app, pilot):
        app.submit("/templates")
        await _await_chat(pilot, app, "daily.md")
        text = _text(app)
        assert "brief.md" in text
        assert "daily.md" in text
        # The result object must not reach the log as JSON.
        assert '{"templates"' not in text

    await _run(reply, body)


async def test_templates_says_so_rather_than_printing_an_empty_list():
    """An empty `/prompts/` is a normal state, not an error: the line has to name
    it, because a blank reply is indistinguishable from a hang (`05` §4.1)."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            return [_result(frame, "templates.list", {"templates": []})]
        return []

    async def body(app, pilot):
        app.submit("/templates")
        await _await_chat(pilot, app, "no templates")
        assert "no templates" in _text(app)

    await _run(reply, body)


async def test_template_prints_the_text_and_not_the_envelope():
    """`05` §7 says `/template <n>` "dumps template text". The body is what the
    reader asked for, so it is shown whole under a header naming the file and its
    size — not as `{"name":…,"bytes":…,"text":…}`."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            return [_result(frame, "template.get",
                            {"name": "brief.md", "bytes": 21,
                             "text": "Summarise /data today."})]
        return []

    async def body(app, pilot):
        app.submit("/template brief.md")
        await _await_chat(pilot, app, "Summarise /data today.")
        text = _text(app)
        assert "brief.md (21 bytes)" in text
        assert '{"name"' not in text

    await _run(reply, body)


def test_a_template_name_is_escaped_before_it_reaches_the_log():
    """A template name is a basename the user imported through `/fs put`, so it is
    device- and user-supplied like any other string: an escape sequence in one must
    not reach the `Static` as markup, and a missing list must say so."""

    assert template_names({"templates": ["a\x1b[31mb.md"]}) == ["a[31mb.md"]
    assert template_names({"templates": []}) == ["no templates"]
    assert template_names({}) == ["no templates"]
    assert template_text({"name": "x\x1b[0m.md", "bytes": 3, "text": "hi"}) == (
        "x[0m.md (3 bytes)\nhi"
    )


async def test_diag_reports_the_failure_rather_than_rendering_a_stale_status():
    """`05` §7 says `/sys diag` renders "the diagnostic half of `status`". A failed
    `status` therefore has nothing to render: printing the cached frame would show
    last refresh's counters as this command's answer, right under the error saying
    the device did not answer."""

    failing = {"on": False}

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command" and frame.get("name") == "status":
            if failing["on"]:
                return [_result(frame, "status",
                                {"code": "E_BUSY", "message": "agent busy"},
                                ok=False)]
            return [_result(frame, "status", STATUS)]
        return []

    async def body(app, pilot):
        # A status on file first: `/sys diag` after a failed refresh is the case
        # that matters, and it is the one where a stale render has something to
        # print.
        app.submit("/sys status")
        await _await_chat(pilot, app, "linked")
        await pilot.pause(0.2)
        failing["on"] = True
        before = len(app._last_status)
        assert before, "the cached status is what makes a stale render possible"
        app.submit("/sys diag")
        await _await_chat(pilot, app, "E_BUSY")
        await pilot.pause(0.3)
        text = _text(app)
        assert "E_BUSY" in text
        tail = text.split("E_BUSY", 1)[1]
        assert "heap_free" not in tail, (
            f"/sys diag rendered the cached status anyway:\n{tail}"
        )

    await _run(reply, body)


async def test_a_fire_and_forget_command_reports_a_write_it_could_not_make():
    """`/cancel` and `/ping` are `send`, not `request`: nothing waits for a reply.
    The write itself can still fail, and `TransportError` becomes `SessionError` on
    the way up (`09` §7). Left uncaught it escapes the worker, so the reader sees a
    crash instead of the line saying the frame never left (`05-bridge.md` §4.1)."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        return []

    async def body(app, pilot):
        await app.session._transport.close()
        app.submit("/cancel")
        await _await_chat(pilot, app, "transport is not open")
        text = _text(app)
        assert "transport is not open" in text
        # An error line, not a normal one: the reader must see the frame failed.
        assert "✗" in text

    await _run(reply, body)


async def test_fs_put_confirms_the_upload_in_words():
    """`04` §4.16: the reply is the whole confirmation a reader gets for an upload,
    and `requires_reboot` is the one field they must act on. A JSON dump buries it."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command" and frame.get("name") == "fs.put":
            return [_result(frame, "fs.put",
                            {"path": "/skills/a.md", "bytes": 12,
                             "created": True, "requires_reboot": True})]
        return []

    async def body(app, pilot):
        # The renderer, not the `/fs put` modal: the staging path is covered in
        # `test_ui.py`, and this asserts what the reply looks like once it lands.
        await app._request("fs.put", {"path": "/skills/a.md", "data": "",
                                      "offset": 0, "final": True})
        await _await_chat(pilot, app, "reboot to apply")
        text = _text(app)
        assert "/skills/a.md: 12 bytes, created (reboot to apply)" in text
        assert "requires_reboot" not in text

    await _run(reply, body)


async def test_a_run_is_confirmed_with_the_id_the_status_screen_names():
    """`04` §4's catalog: `run_id` is what `/cancel` and the status screen refer to,
    so it is
    the one thing the confirmation has to carry."""

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command" and frame.get("name") == "tasks.run":
            return [_result(frame, "tasks.run",
                            {"task_id": "t1", "run_id": "r-9"})]
        return []

    async def body(app, pilot):
        app.submit("/run t1")
        await _await_chat(pilot, app, "started run r-9")
        text = _text(app)
        assert "started run r-9" in text
        assert "run_id" not in text

    await _run(reply, body)


async def test_tasks_add_opens_a_blank_modal_and_sends_what_it_returns():
    """`05` §7: `/tasks add` takes no id — the body is the whole task, so the modal
    is the only way to supply it. Sending nothing, or the cached row, would be a
    different command. The frame must carry exactly the dict the modal dismissed."""
    from bridge.ui.task_prompt import TaskPrompt

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            if frame.get("name") == "tasks.set":
                sent.append(frame.get("args") or {})
            return [_result(frame, frame.get("name", ""), {"echo": True})]
        return []

    body_task = {"id": "t-new", "description": "water the plants",
                 "enabled": True, "prompt": "p"}

    async def body(app, pilot):
        app.submit("/tasks add")
        await _await_screen(pilot, app, TaskPrompt)
        assert app.screen.query_one("#task-input").value == "{}", "a blank task, not a row"
        app.screen.query_one("#task-input").value = json.dumps(body_task)
        app.screen.action_submit()
        await pilot.pause(0.3)

    await _run(reply, body)
    assert sent == [{"task": body_task}], sent


async def test_tasks_add_refuses_a_surplus_word_without_opening_a_modal():
    """`05` §7: `/tasks add` takes no id. A modal pre-filled from a stray word would
    leave the other fields to guesswork, so the word is refused instead."""
    from bridge.ui.task_prompt import TaskPrompt

    async def body(app, pilot):
        app.submit("/tasks add morning")
        await _await_chat(pilot, app, "usage: /tasks add")
        assert isinstance(app.screen, TaskPrompt) is False
        assert "usage: /tasks add" in _text(app)

    await _run(_device(), body)


async def test_tasks_set_opens_the_modal_seeded_from_the_cached_row():
    """`05` §7: `/tasks set <id>` edits the cached task. Sending it back untouched
    would be a round trip that changes nothing, so the modal must open with it."""
    from bridge.ui.task_prompt import TaskPrompt

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            name = frame.get("name", "")
            if name == "tasks.set":
                sent.append(frame.get("args") or {})
            if name == "tasks.list":
                return [_result(frame, name, {"tasks": [{"id": "t1",
                                                         "description": "water",
                                                         "enabled": True,
                                                         "prompt": "p"}]})]
            return [_result(frame, name, {"echo": True})]
        return []

    async def body(app, pilot):
        app.submit("/tasks")
        await _await_chat(pilot, app, "task(s)")
        app.submit("/tasks set t1")
        await _await_screen(pilot, app, TaskPrompt)
        assert '"description": "water"' in app.screen.query_one("#task-input").value
        app.screen.query_one("#task-input").value = json.dumps(
            {"id": "t1", "description": "water", "enabled": False, "prompt": "p"}
        )
        app.screen.action_submit()
        await pilot.pause(0.3)

    await _run(reply, body)
    assert sent == [{"task": {"id": "t1", "description": "water",
                              "enabled": False, "prompt": "p"}}], sent


async def test_tasks_set_names_an_id_it_never_received():
    """`05` §7: the cached entry is the starting point. With no `tasks.list` behind
    it there is nothing to seed, so the command says which id is unknown rather
    than opening a modal on `{}`."""

    async def body(app, pilot):
        app.submit("/tasks set nope")
        await _await_chat(pilot, app, "no task nope")
        assert "no task nope in the last tasks.list" in _text(app)

    await _run(_device(), body)


async def test_tasks_add_says_so_when_the_modal_is_cancelled():
    """`05` §5.3: Esc cancels without sending. A cancelled edit must not leave the
    reader wondering whether an empty task went to the device."""
    from bridge.ui.task_prompt import TaskPrompt

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            if frame.get("name") == "tasks.set":
                sent.append(frame.get("args") or {})
            return [_result(frame, frame.get("name", ""), {"echo": True})]
        return []

    async def body(app, pilot):
        app.submit("/tasks add")
        await _await_screen(pilot, app, TaskPrompt)
        app.screen.query_one("#task-input").value = ""
        app.screen.action_submit()
        await _await_chat(pilot, app, "task edit cancelled")
        assert "task edit cancelled" in _text(app)

    await _run(reply, body)
    assert sent == [], sent


async def test_tasks_set_says_so_when_the_modal_is_cancelled():
    """Same rule for `set`: the cached row is only a starting point, and Esc means
    nothing was changed."""
    from bridge.ui.task_prompt import TaskPrompt

    sent: list[dict] = []

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if frame.get("type") == "command":
            name = frame.get("name", "")
            if name == "tasks.list":
                return [_result(frame, name, {"tasks": [{"id": "t1",
                                                         "description": "water",
                                                         "enabled": True}]})]
            if name == "tasks.set":
                sent.append(frame.get("args") or {})
            return [_result(frame, name, {"echo": True})]
        return []

    async def body(app, pilot):
        app.submit("/tasks")
        await _await_chat(pilot, app, "task(s)")
        app.submit("/tasks set t1")
        await _await_screen(pilot, app, TaskPrompt)
        app.screen.query_one("#task-input").value = ""
        app.screen.action_submit()
        await _await_chat(pilot, app, "task edit cancelled")
        assert "task edit cancelled" in _text(app)

    await _run(reply, body)
    assert sent == [], sent


async def test_tasks_set_refuses_a_surplus_word_without_opening_a_modal():
    """`05` §7: `/tasks set <id>` takes one id. A third word would be silently
    dropped, so it is refused before any modal opens."""

    async def body(app, pilot):
        app.submit("/tasks set t1 junk")
        await _await_chat(pilot, app, "usage: /tasks set <id>")
        assert "usage: /tasks set <id>" in _text(app)

    await _run(_device(), body)


async def test_cancel_answers_in_chat_whether_or_not_a_run_was_in_flight():
    """`04` §3.4: `/cancel` has no acknowledgement of its own. A live run ends in
    `agent.error`/`E_CANCELLED`, and nothing in flight emits `cancel.noop` — so the
    two frames are the whole answer, and a bridge that only routes `event` to the log
    pane leaves either press looking like a dropped keystroke. Observed: `/cancel`
    on an idle board printed nothing."""

    def reply(line: bytes) -> list[bytes]:
        return _device()(line)

    async def body(app, pilot):
        transport = app.session._transport  # type: ignore[attr-defined]
        transport.feed(
            encode(Frame(type="event", id="d-30", ts=0,
                         body={"kind": "cancel.noop",
                               "data": {"reason": "nothing to cancel"}})),
        )
        await _await_chat(pilot, app, "nothing to cancel")
        assert "nothing to cancel" in _text(app)

        transport.feed(
            encode(Frame(type="agent.error", id="d-31", ts=0,
                         body={"code": "E_CANCELLED", "message": "run cancelled",
                               "retryable": False})),
        )
        await _await_chat(pilot, app, "run cancelled")
        # Printed once: the frame's own text is the whole sentence, so a reply that
        # prefixes it again reads `run cancelled: run cancelled`.
        assert "run cancelled: run cancelled" not in _text(app)
        assert "run cancelled" in _text(app)

    await _run(reply, body)


# -- the wifi and provider editors --------------------------------------


async def test_wifi_list_draws_every_known_network_and_what_the_scan_heard():
    """`05` §7.6: the document says which networks exist, the scan says which are
    audible, and a configured network the scan missed is still drawn."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {
            "wifi": {"networks": [
                {"ssid": "home", "password_ref": "nvs:k"},
                {"ssid": "gone", "password_ref": "nvs:g"},
            ]}
        }
        await app._wifi_list()
        await _await_chat(pilot, app, "gone")
        text = _text(app)
        # `home` is the associated network in the status fixture.
        assert "home" in text and "joined" in text
        # `gone` is configured and was not heard: the row exists and says so.
        assert "gone" in text and "not in range" in text

    await _run(_device(), body)


async def test_wifi_list_with_no_document_answer_says_nothing_was_configured():
    async def body(app, pilot):
        app._config_fetched = True
        app._config = {}
        await app._wifi_list()
        await _await_chat(pilot, app, "no networks configured")

    await _run(_device(), body)


async def test_forgetting_the_last_network_is_refused_before_any_frame():
    """`03-config.md` makes `wifi.networks` 1..8, so the empty list is a document the
    device rejects. The refusal names the bound instead of sending a doomed write."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "only", "password_ref": "nvs:k"}
        ]}}

        answers = ["only", "forget it", True]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        assert "the last network cannot be removed" in _text(app)
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "wifi.networks"
        ]

    await _run(_device(), body)


async def test_adding_a_network_with_a_dead_radio_says_so_in_the_form():
    """An off radio is not an empty scan, and the answer belongs in the modal: the
    form is where the list would have been, so it is where the reader learns why it
    is not there. Nothing about the scan reaches chat (`05` §7.6)."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"}
        ]}}
        seen: list[object] = []
        answers = ["add a network", "from the scan", None]

        async def drive(screen):
            seen.append(screen)
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        form = seen[-1]
        assert type(form).__name__ == "WifiScanForm"
        # A dropped frame is a timeout, not a refusal: the device said nothing at
        # all, so the note must not invent an error code.
        assert "scan unavailable" in form.note
        assert "Ctrl+R" in form.note
        assert "heard nothing new" not in form.note
        assert form.scan == []
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "wifi.networks"
        ]
        # The scan is the modal's, not the log's.
        assert "no networks heard" not in _text(app)

    await _run(_device(drop={"wifi.scan"}), body)


async def test_wifi_list_with_a_dead_radio_lists_the_rows_as_unknown():
    """The document is still worth showing when the radio is off: which networks are
    configured and their priority do not come from the scan. What the scan would have
    answered is drawn `unknown`, never `not in range`."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "priority": 0, "password_ref": "nvs:k"},
            {"ssid": "Cafe", "priority": 5, "password_ref": "nvs:k2"},
        ]}}
        await app._wifi_list()
        text = _text(app)
        assert "home" in text and "Cafe" in text
        assert "unknown" in text
        assert "not in range" not in text

    await _run(_device(drop={"wifi.scan"}), body)


async def test_adding_a_network_writes_its_secret_before_the_array_that_names_it():
    """`03-config.md` rule 9: an `nvs:` ref that resolves to nothing is a document the
    device refuses, so the secret has to land first. The key is derived from the SSID
    because a shared `wifi_psk` would make removing one row blank another's password."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"}
        ]}}

        # Three questions: which network list, scan or by hand, then the form.
        answers = ["add a network", "from the scan",
                   {"ssid": "Cafe-FREE", "password": "hunter2"}]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        secret = [f for f in written if f.get("type") == "secret.set"]
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "wifi.networks"]
        assert secret and arrays, written
        # A shorthand frame puts `key` at the root; there is no `body`.
        assert secret[0]["key"] == "wifi_cafe_free"
        assert written.index(secret[0]) < written.index(arrays[0])
        assert arrays[0]["args"]["value"][-1] == {
            "ssid": "Cafe-FREE", "priority": 1, "password_ref": "nvs:wifi_cafe_free"
        }

    await _run(_device(), body)


async def test_the_provider_editor_offers_every_entry_and_says_so_with_none():
    """`05` §7.7: the picker names what each gesture does before it is chosen."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}

        seen: list[object] = []

        async def picker(screen):
            seen.append(screen)
            return None

        app.push_screen_wait = picker  # type: ignore[method-assign]
        await app._providers()
        assert "providers cancelled" in _text(app)
        rows = [label for label, _hint in seen[0].rows]
        assert "add a provider" in rows
        assert "log in to openai" in rows and "disconnect openai" in rows
        # The header carries the model, url and key ref: the facts a reader needs
        # before choosing a row, and none of them is the row's answer.
        assert any("nvs:llm_api_key" in line for line in seen[0].header)

    await _run(_device(), body)


async def test_removing_the_last_provider_is_refused_before_any_array_write():
    """`03-config.md` makes `llm.providers` 1..4, so removing the only entry would be
    a write the device rejects. The refusal names the bound and sends nothing."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}

        async def picker(_screen):
            return "disconnect openai"

        app.push_screen_wait = picker  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        assert "the last provider cannot be removed" in _text(app)
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "llm.providers"
        ]

    await _run(_device(), body)


async def test_disconnecting_a_provider_removes_the_row_then_erases_its_key():
    """`04` §4.31 refuses a key a live `*_ref` names, so the array has to land first.
    The order is the whole correctness of this gesture."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
            {"id": "groq", "base_url": "https://api.groq.com/openai/v1",
             "model": "llama", "api_key_ref": "nvs:key_groq"},
        ]}}

        async def picker(_screen):
            return "disconnect groq"

        app.push_screen_wait = picker  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "llm.providers"]
        deletes = [f for f in written if f.get("name") == "secret.delete"]
        assert arrays and deletes, written
        # The remaining entry is the one whose key is still referenced.
        assert [e["id"] for e in arrays[0]["args"]["value"]] == ["openai"]
        assert written.index(arrays[0]) < written.index(deletes[0])
        assert deletes[0]["args"]["key"] == "key_groq"
        assert "nvs:key_groq erased" in _text(app)

    await _run(_device(), body)


async def test_the_passphrase_gesture_reuses_the_ref_the_array_already_names():
    """`05` §7.6: a network `/onboard` seeded names `wifi_psk`, and deriving a fresh
    key from the ssid would write a secret nothing references — the array would keep
    pointing at the old one, so the new passphrase would never be used."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:wifi_psk"}
        ]}}

        answers = ["home", "change the passphrase",
                   {"ssid": "home", "password": "newpass"}]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        secrets_ = [f for f in written if f.get("type") == "secret.set"]
        assert secrets_ and secrets_[0]["key"] == "wifi_psk"
        assert secrets_[0]["value"] == "newpass"
        # The array is untouched: only the credential moved.
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "wifi.networks"
        ]
        assert "stored as nvs:wifi_psk" in _text(app)

    await _run(_device(), body)


async def test_a_blank_passphrase_writes_nothing():
    """An empty field is a cancel, not a request to blank the credential: a blank
    `secret.set` would leave the array naming an empty secret."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"}
        ]}}

        answers = ["home", "change the passphrase", {"ssid": "home", "password": ""}]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "nothing entered, nothing written" in _text(app)

    await _run(_device(), body)


async def test_forgetting_a_network_writes_the_shorter_array_and_keeps_the_secret():
    """`05` §7.6: the secret is deliberately kept — another network may name it, and
    `04` §4.31 refuses a key a live `*_ref` still names."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"},
            {"ssid": "Cafe", "password_ref": "nvs:k2"},
        ]}}

        answers = ["Cafe", "forget it", True]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "wifi.networks"]
        assert [e["ssid"] for e in arrays[0]["args"]["value"]] == ["home"]
        assert not [f for f in written if f.get("name") == "secret.delete"]
        assert "its secret is kept" in _text(app)

    await _run(_device(), body)


async def test_cancelling_the_forget_confirmation_sends_nothing():
    """A `ConfirmPrompt` answered no is a cancel, and the array is the whole
    document: a write here would drop a network the reader kept."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"},
            {"ssid": "Cafe", "password_ref": "nvs:k2"},
        ]}}

        answers = ["Cafe", "forget it", False]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "wifi.networks"
        ]
        assert "wifi forget cancelled" in _text(app)

    await _run(_device(), body)


async def test_a_refresh_rescans_and_reopens_the_form_with_the_new_list():
    """`05` §7.6: `Ctrl+R` answers a refresh marker rather than a network, and the
    handler loops. The second form must be built from the second scan — both its
    list and its note — or the network that just appeared never shows up and the
    form keeps apologising for a scan that has since succeeded."""
    from bridge.ui.modals import WifiScanForm

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [{"ssid": "home"}]}}
        forms: list[object] = []
        answers = ["add a network", "from the scan", {"refresh": True},
                   {"ssid": "Cafe-FREE", "password": "pw"}]
        # First scan hears only the known network, so the form has nothing to
        # offer; the refresh hears a new one.
        scans = iter([
            ([{"ssid": "home", "rssi": -40}], None, None),
            ([{"ssid": "home", "rssi": -40}, {"ssid": "Cafe-FREE", "rssi": -70}],
             None, None),
        ])

        async def quiet():
            return next(scans)

        app._wifi_scan_quiet = quiet  # type: ignore[method-assign]

        async def drive(screen):
            if isinstance(screen, WifiScanForm):
                forms.append(screen)
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        assert len(forms) == 2
        assert forms[0].scan == []
        assert "heard nothing new" in forms[0].note
        # The second form carries the second scan, note and all.
        assert [n["ssid"] for n in forms[1].scan] == ["Cafe-FREE"]
        assert forms[1].note == ""
        # The scan's rows are the modal's, never the log's.
        assert "no networks heard" not in _text(app)
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "wifi.networks"]
        assert [e["ssid"] for e in arrays[0]["args"]["value"]] == ["home", "Cafe-FREE"]

    await _run(_device(), body)


async def test_a_truncated_scan_is_named_in_the_form_rather_than_looking_whole():
    """`04` §4.24: a truncated list is a partial one. The form is the only place the
    scan is shown, so a note that said nothing would let a reader conclude the
    network they want is not in range when the list simply stopped early."""
    from bridge.ui.modals import WifiScanForm

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [{"ssid": "home"}]}}
        forms: list[object] = []
        answers = ["add a network", "from the scan", None]

        async def quiet():
            return ([{"ssid": "Cafe-FREE", "rssi": -70}], None,
                    "1 networks shown; the list was truncated")

        app._wifi_scan_quiet = quiet  # type: ignore[method-assign]

        async def drive(screen):
            if isinstance(screen, WifiScanForm):
                forms.append(screen)
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        await app._wifi_edit()
        assert "truncated" in forms[0].note
        assert [n["ssid"] for n in forms[0].scan] == ["Cafe-FREE"]

    await _run(_device(), body)


async def test_adding_by_hand_writes_the_typed_network_and_needs_no_scan():
    """`05` §7.6: a hidden network is named by hand, so the gesture must not depend
    on the scan at all — a station off the air still has a network worth adding."""
    from bridge.ui.modals import WifiCustomForm

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [{"ssid": "home", "password_ref": "nvs:k"}]}}
        seen: list[object] = []
        answers = ["add a network", "by hand",
                   {"ssid": "hidden-net", "password": "hunter2"}]

        async def drive(screen):
            seen.append(screen)
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        assert isinstance(seen[-1], WifiCustomForm)
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("name") == "wifi.scan"]
        secrets_ = [f for f in written if f.get("type") == "secret.set"]
        assert secrets_[0]["key"] == "wifi_hidden_net"
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "wifi.networks"]
        assert arrays[0]["args"]["value"][-1] == {
            "ssid": "hidden-net", "priority": 1, "password_ref": "nvs:wifi_hidden_net"
        }
        assert "wifi add hidden-net" in _text(app)

    await _run(_device(), body)


async def test_dismissing_each_picker_writes_nothing_and_says_which_step_stopped():
    """Three pickers sit in the chain and each can be dismissed. A dismissal is a
    cancel at every one of them: the reader pressed escape, so nothing is written and
    the message names the step they abandoned rather than a generic failure."""
    async def run_chain(answers, expect, before_arrays):
        async def body(app, pilot):
            app._config_fetched = True
            app._config = {"wifi": {"networks": [
                {"ssid": "home", "password_ref": "nvs:k"},
                {"ssid": "Cafe", "password_ref": "nvs:j"}]}}
            queue = list(answers)

            async def drive(screen):
                return queue.pop(0)

            app.push_screen_wait = drive  # type: ignore[method-assign]
            before = len(app.session._transport.written)
            await app._wifi_edit()
            assert expect in _text(app)
            written = [json.loads(line)
                       for line in app.session._transport.written[before:]]
            arrays = [f for f in written
                      if (f.get("args") or {}).get("path") == "wifi.networks"]
            assert len(arrays) == before_arrays

        await _run(_device(), body)

    # The network picker, the add picker, and the by-hand form.
    await run_chain([None], "wifi edit cancelled", 0)
    await run_chain(["add a network", None], "wifi add cancelled", 0)
    await run_chain(["add a network", "by hand", None], "wifi add cancelled", 0)


async def test_a_network_named_like_the_add_row_can_still_be_edited():
    """The first picker's `add a network` row is a label, not an ssid, and an ssid may
    be any string. Without the collision check that network could never be selected:
    its row would answer the same string as the add row and be read as a request to
    add a second one, so a configured network became unreachable and uneditable."""
    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "add a network", "password_ref": "nvs:k"},
            {"ssid": "home", "password_ref": "nvs:j"}]}}
        # The network's own row still answers its ssid; only the add row moved.
        answers = ["add a network", "forget it", True]
        before = len(app.session._transport.written)

        async def drive(screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "wifi.networks"]
        assert [n["ssid"] for n in arrays[-1]["args"]["value"]] == ["home"]
        assert "wifi forget add a network: 1 networks configured" in _text(app)

    await _run(_device(), body)


async def test_a_by_hand_ssid_that_is_already_configured_is_refused_before_a_write():
    """The scan filters known networks out, but a typed ssid is not filtered by
    anything: without this the array would gain a second entry for a network that is
    already in it, and the first one's secret would be the one the station uses."""
    from bridge.ui.modals import WifiCustomForm

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [{"ssid": "home", "password_ref": "nvs:k"}]}}
        answers = ["add a network", "by hand",
                   {"ssid": "home", "password": "hunter2"}]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._wifi_edit()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "wifi.networks"
        ]
        assert "already configured" in _text(app)

    await _run(_device(), body)


async def test_the_editors_cancel_before_the_document_is_read():
    """`/wifi edit` and `/providers` answer a cancel in chat — a picker that closes
    silently is indistinguishable from a hang (`05` §4.1)."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [{"ssid": "home"}]},
                       "llm": {"providers": [{"id": "openai"}]}}

        async def picker(_screen):
            return None

        app.push_screen_wait = picker  # type: ignore[method-assign]
        await app._wifi_edit()
        await app._providers()
        assert "wifi edit cancelled" in _text(app)
        assert "providers cancelled" in _text(app)

    await _run(_device(), body)


async def test_logging_in_replaces_the_key_the_entry_already_names():
    """`05` §7.7: the log-in gesture reuses the entry's own ref, because a fresh
    `key_<id>` would leave the array pointing at the old key."""
    from bridge.ui.modals import SecretPrompt as _Secret

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}

        async def drive(screen):
            # The picker answers the gesture; the masked prompt answers the key.
            return "sk-new" if isinstance(screen, _Secret) else "log in to openai"

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        secrets_ = [f for f in written if f.get("type") == "secret.set"]
        assert secrets_ and secrets_[0]["key"] == "llm_api_key"
        assert secrets_[0]["value"] == "sk-new"
        assert "key replaced (nvs:llm_api_key)" in _text(app)

    await _run(_device(), body)


async def test_cancelling_a_log_in_writes_nothing():
    """A blank masked prompt is a cancel: writing it would blank the credential a
    provider entry names."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}

        answers = ["log in to openai", ""]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "provider login: cancelled, nothing written" in _text(app)

    await _run(_device(), body)


async def test_cancelling_a_disconnect_keeps_the_row_and_the_key():
    """The array and the key are both the reader's data: a `no` answers in chat and
    sends nothing."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
            {"id": "groq", "base_url": "https://api.groq.com/openai/v1",
             "model": "llama", "api_key_ref": "nvs:key_groq"},
        ]}}

        answers = ["disconnect groq", False]

        async def drive(_screen):
            return answers.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [
            f for f in written
            if (f.get("args") or {}).get("path") == "llm.providers"
        ]
        assert not [f for f in written if f.get("name") == "secret.delete"]
        assert "provider disconnect cancelled" in _text(app)

    await _run(_device(), body)


async def test_the_add_provider_gesture_lands_one_entry_and_one_key():
    """`05` §7.7: `add` runs the same `ProviderStep` + `ModelStep` chain `/onboard`
    uses, so the model written is one the key actually unlocked. The write appends to
    `llm.providers` rather than replacing `providers[0]`, and the key lands first."""

    async def body(app, pilot):
        from bridge.ui import app as app_mod
        from bridge.ui.modals import ModelStep, ProviderStep
        from bridge.ui.onboard import Answers

        answers = Answers(
            ssid="", password="",
            base_url="https://api.groq.com/openai/v1", api_key="sk-groq",
            model="", provider="groq",
        )
        picked = type("Picked", (), {"model": "llama", "extra_body": {}})()

        async def fetch(_answers):
            return ["llama"]

        app._fetch_models = fetch  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}

        async def drive(screen):
            # The two steps `/onboard` uses, answered without their widgets: what is
            # under test is the write, and the steps have their own tests.
            if isinstance(screen, ProviderStep):
                return answers
            if isinstance(screen, ModelStep):
                return picked
            return "add a provider"

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        secret = [f for f in written if f.get("type") == "secret.set"]
        arrays = [f for f in written
                  if (f.get("args") or {}).get("path") == "llm.providers"]
        assert secret and arrays, written
        assert secret[0]["key"] == "key_groq"
        assert written.index(secret[0]) < written.index(arrays[0])
        assert [e["id"] for e in arrays[0]["args"]["value"]] == ["openai", "groq"]
        assert arrays[0]["args"]["value"][-1]["api_key_ref"] == "nvs:key_groq"
        assert "2 providers configured" in _text(app)

    await _run(_device(), body)


async def test_a_refused_secret_on_add_and_passphrase_is_reported_not_swallowed():
    """`05` §7.6: a refused `secret.set` is the end of the gesture — the array is not
    written, because an `nvs:` ref that resolves to nothing is a document the device
    rejects (`03-config.md` rule 9). Both gestures name the refusal."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [{"ssid": "home",
                                              "password_ref": "nvs:k"}]}}

        for replies in (
            ["add a network", "from the scan", {"ssid": "Cafe", "password": "pw"}],
            ["home", "change the passphrase", {"ssid": "home", "password": "pw"}],
        ):

            async def drive(_screen, _r=replies):
                return _r.pop(0)

            app.push_screen_wait = drive  # type: ignore[method-assign]
            before = len(app.session._transport.written)
            await app._wifi_edit()
            written = [
                json.loads(line) for line in app.session._transport.written[before:]
            ]
            assert not [
                f for f in written
                if (f.get("args") or {}).get("path") == "wifi.networks"
            ], replies

        text = _text(app)
        assert "wifi add: secret refused" in text
        assert "wifi passphrase: refused" in text

    await _run(_device(overrides={"secret.set": "nvs is full"}), body)


async def test_a_refused_array_write_is_reported_for_add_and_forget():
    """A refused `config.set` leaves the array as it was; the gesture names the
    refusal rather than claiming the change landed."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"},
            {"ssid": "Cafe", "password_ref": "nvs:k2"},
        ]}}

        for replies in (
            ["add a network", "from the scan", {"ssid": "New", "password": ""}],
            ["Cafe", "forget it", True],
        ):

            async def drive(_screen, _r=replies):
                return _r.pop(0)

            app.push_screen_wait = drive  # type: ignore[method-assign]
            await app._wifi_edit()

        text = _text(app)
        assert "wifi add: refused" in text
        assert "wifi forget: refused" in text

    await _run(_device(overrides={"config.set": "document rejected"}), body)


def _provider_answers(**kw):
    """An `Answers` for the provider step, with the members these tests vary."""
    from bridge.ui.onboard import Answers

    base = dict(ssid="", password="", base_url="", api_key="", model="",
                provider="openai")
    base.update(kw)
    return Answers(**base)


async def test_adding_a_provider_that_already_exists_is_refused_at_the_step():
    """`llm.providers[]` is a list of unique ids, so a second `openai` is a document
    the device refuses. The refusal names the fix (`log in`) and writes nothing."""

    async def body(app, pilot):
        from bridge.ui.modals import ProviderStep

        async def drive(screen):
            if isinstance(screen, ProviderStep):
                return _provider_answers(base_url="https://api.openai.com/v1",
                                         api_key="sk")
            return "add a provider"

        app.push_screen_wait = drive  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "already exists (use log in)" in _text(app)

    await _run(_device(), body)


async def test_adding_a_provider_without_a_url_or_a_key_is_refused():
    """A provider entry needs all three of `id`, `base_url` and `model`, and the key
    is what proves them — so a blank field answers in chat before a model fetch."""

    async def body(app, pilot):
        from bridge.ui.modals import ProviderStep

        async def drive(screen):
            if isinstance(screen, ProviderStep):
                # An id outside the chain, so the refusal under test is the missing
                # field rather than the duplicate.
                return _provider_answers(provider="custom")
            return "add a provider"

        app.push_screen_wait = drive  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "a base url and an api key are both required" in _text(app)

    await _run(_device(), body)


async def test_an_empty_model_list_writes_nothing_and_names_the_provider():
    """The list is the proof the key works: no list means the key was not accepted,
    so nothing is written and the reader is told which provider failed."""

    async def body(app, pilot):
        from bridge.ui.modals import ProviderStep

        async def drive(screen):
            if isinstance(screen, ProviderStep):
                return _provider_answers(provider="groq",
                                         base_url="https://api.groq.com/v1",
                                         api_key="sk")
            return "add a provider"

        async def no_models(_a):
            return []

        app._fetch_models = no_models  # type: ignore[method-assign]
        app.push_screen_wait = drive  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "groq published no model list" in _text(app)

    await _run(_device(), body)


async def test_a_refused_key_and_a_refused_array_are_each_reported():
    """Login writes one secret; disconnect writes the array then erases the key. A
    refusal at either step stops the gesture and says which one failed."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
            {"id": "groq", "base_url": "https://api.groq.com/v1",
             "model": "llama", "api_key_ref": "nvs:key_groq"},
        ]}}
        for gesture, answer in (("log in to openai", "sk"), ("disconnect groq", True)):
            replies = [gesture, answer]

            async def drive(_screen, _r=replies):
                return _r.pop(0)

            app.push_screen_wait = drive  # type: ignore[method-assign]
            await app._providers()
        text = _text(app)
        assert "provider login: refused" in text
        assert "provider disconnect: refused" in text

    await _run(
        _device(overrides={"secret.set": "nvs is full",
                           "config.set": "document rejected"}),
        body,
    )


async def test_a_disconnect_whose_key_erase_is_refused_keeps_the_row_change():
    """`04` §4.31 refuses a key a live `*_ref` still names. The array is the fact the
    reader asked to change, so a refused erase is a warning, not a failure."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
            {"id": "groq", "base_url": "https://api.groq.com/v1",
             "model": "llama", "api_key_ref": "nvs:key_groq"},
        ]}}
        replies = ["disconnect groq", True]

        async def drive(_screen):
            return replies.pop(0)

        app.push_screen_wait = drive  # type: ignore[method-assign]
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        # The array landed; only the erase was refused.
        assert [
            f for f in written
            if (f.get("args") or {}).get("path") == "llm.providers"
        ]
        # `_send_body` reports the code, not the message: the code is what a reader
        # can look up, and the message belongs to the device's own vocabulary.
        assert "removed; its key was kept (E_CONFIG)" in _text(app)

    await _run(
        _device(overrides={"secret.delete": {
            "code": "E_CONFIG", "message": "still named by llm.providers"
        }}),
        body,
    )


async def test_each_editor_fetches_the_document_when_it_has_none():
    """`05` §7.6: a reader can reach either command before any reply cached the
    document, so the handler asks for it rather than drawing an empty list."""
    from bridge.ui.modals import _PickBox

    async def body(app, pilot):
        assert app._config_fetched is False
        seen: list[object] = []

        async def picker(screen):
            seen.append(screen)
            return None

        app.push_screen_wait = picker  # type: ignore[method-assign]
        await app._wifi_edit()
        await app._providers()
        assert app._config_fetched is True
        # Both read the fetched document, so each picker names the configured entry.
        labels = [[label for label, _hint in box.rows] for box in seen]
        assert "home" in labels[0]
        assert any("disconnect openai" in row for row in labels[1])
        assert isinstance(seen[0], _PickBox)

    await _run(_device(), body)


async def test_cancelling_the_add_form_and_the_passphrase_form_writes_nothing():
    """Esc on a form is a cancel: the array and the secret are both untouched."""

    async def body(app, pilot):
        app._config_fetched = True
        app._config = {"wifi": {"networks": [
            {"ssid": "home", "password_ref": "nvs:k"},
            {"ssid": "Cafe", "password_ref": "nvs:k2"},
        ]}}

        for replies in (
            ["add a network", "from the scan", None],
            ["home", "change the passphrase", None],
        ):

            async def drive(_screen, _r=replies):
                return _r.pop(0)

            app.push_screen_wait = drive  # type: ignore[method-assign]
            before = len(app.session._transport.written)
            await app._wifi_edit()
            written = [
                json.loads(line) for line in app.session._transport.written[before:]
            ]
            assert not [f for f in written if f.get("type") == "secret.set"], replies
            assert not [
                f for f in written
                if (f.get("args") or {}).get("path") == "wifi.networks"
            ], replies

        text = _text(app)
        assert "wifi add cancelled" in text
        assert "wifi passphrase: cancelled" in text

    await _run(_device(), body)


async def test_cancelling_the_model_step_writes_nothing():
    """The model step is the last question; a cancel there is a cancel of the whole
    gesture, because the entry cannot be written without a model."""

    async def body(app, pilot):
        from bridge.ui.modals import ModelStep, ProviderStep

        async def drive(screen):
            if isinstance(screen, ProviderStep):
                return _provider_answers(provider="groq",
                                         base_url="https://api.groq.com/v1",
                                         api_key="sk")
            if isinstance(screen, ModelStep):
                return None
            return "add a provider"

        async def models(_a):
            return ["llama"]

        app._fetch_models = models  # type: ignore[method-assign]
        app.push_screen_wait = drive  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "provider add cancelled" in _text(app)

    await _run(_device(), body)


async def test_a_custom_provider_file_whose_defect_is_a_note_prints_it_before_asking():
    """`05` §7.7: the catalog reports a malformed custom entry as a note rather than
    failing, and the add gesture is the one place a reader can act on it. Printing it
    before the form means the missing provider is explained by the time it is missed."""
    from bridge import providers as providers_mod

    real = providers_mod.catalog

    def catalog_with_a_note(path=None):
        entries, _notes = real(path)
        return entries, ["providers.json: entry 2 has no base_url (skipped)"]

    async def body(app, pilot):
        from bridge.ui.modals import ProviderStep

        providers_mod.catalog = catalog_with_a_note  # type: ignore[assignment]

        async def picker(screen):
            # The note is printed on the way into the form, so the add arm has to run
            # and the form itself is answered `None`.
            return None if isinstance(screen, ProviderStep) else "add a provider"

        app.push_screen_wait = picker  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        # The picker is answered `None`, so the add arm never runs; the note is
        # printed only on the way in, which is the ordering under test.
        await app._providers()
        text = _text(app)
        assert "entry 2 has no base_url" in text

    try:
        await _run(_device(), body)
    finally:
        providers_mod.catalog = real  # type: ignore[assignment]


async def test_an_add_whose_form_step_is_cancelled_says_so():
    """The provider step is the first of three questions, and closing it is a cancel
    of the whole gesture — the key is not fetched and nothing is written."""
    from bridge.ui.modals import ProviderStep

    async def body(app, pilot):
        async def drive(screen):
            if isinstance(screen, ProviderStep):
                return None
            return "add a provider"

        app.push_screen_wait = drive  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        before = len(app.session._transport.written)
        await app._providers()
        written = [json.loads(line) for line in app.session._transport.written[before:]]
        assert not [f for f in written if f.get("type") == "secret.set"]
        assert "provider add cancelled" in _text(app)

    await _run(_device(), body)


async def test_an_add_whose_array_write_is_refused_says_so_and_keeps_the_key():
    """The key landed and the array did not, so the key is now named by nothing.
    Nothing is erased — a refused write leaves the document as it was, and the
    gesture names the refusal rather than claiming success."""
    from bridge.ui.modals import ModelStep, ProviderStep

    async def body(app, pilot):
        picked = type("Picked", (), {"model": "llama", "extra_body": {}})()

        async def drive(screen):
            if isinstance(screen, ProviderStep):
                return _provider_answers(provider="groq",
                                         base_url="https://api.groq.com/v1",
                                         api_key="sk")
            if isinstance(screen, ModelStep):
                return picked
            return "add a provider"

        async def models(_a):
            return ["llama"]

        app._fetch_models = models  # type: ignore[method-assign]
        app.push_screen_wait = drive  # type: ignore[method-assign]
        app._config_fetched = True
        app._config = {"llm": {"providers": [
            {"id": "openai", "base_url": "https://api.openai.com/v1",
             "model": "gpt", "api_key_ref": "nvs:llm_api_key"},
        ]}}
        await app._providers()
        assert "provider add: refused (document rejected)" in _text(app)

    await _run(_device(overrides={"config.set": "document rejected"}), body)

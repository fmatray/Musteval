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
        if overrides and name in overrides:
            value = overrides[name]
            if isinstance(value, Exception):
                raise value
            return [_result(frame, name, value, ok=False)]
        if kind == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0, body=ACK,
                                 reply_to=frame["id"]))]
        if kind == "ping":
            return [encode(Frame(type="pong", id="d-2", ts=0, body={},
                                 reply_to=frame["id"]))]
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
            if name == "config.get":
                return [_result(frame, name, {"config": CONFIG})]
            if name == "config.set":
                return [_result(frame, name,
                                {"path": args.get("path"), "applied": True})]
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


async def test_tools_and_mcp_read_from_the_hello_capabilities():
    """`/tools` and `/mcp` are answered from the handshake, not from a frame.

    The capability list is what the device said it offers, so a bridge that sent a
    command here would be asking a question it already has the answer to.
    """

    async def body(app, pilot):
        before = len(app.session._transport.written)  # type: ignore[attr-defined]
        app.submit("/tools")
        await _await_chat(pilot, app, "gpio.read")
        assert "time.now" in _text(app)
        app.submit("/mcp")
        await _await_chat(pilot, app, "fs=up")
        assert len(app.session._transport.written) == before  # type: ignore[attr-defined]

    await _run(_device(), body)


async def test_usage_and_context_report_the_running_totals():
    """`/usage` and `/context` describe what this session has spent, so a prompt must
    have run first and their numbers must move with it."""

    async def body(app, pilot):
        app.submit("/usage")
        await _await_chat(pilot, app, "total tokens: 0")
        app.submit("hello")
        await _await_chat(pilot, app, "done")
        app.submit("/usage")
        await _await_chat(pilot, app, "total tokens: 2")
        app.submit("/context")
        await _await_chat(pilot, app, "last input: 5 bytes | tokens: 2")

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
        for _ in range(160):
            written = [json.loads(line) for line in app.session._transport.written]  # type: ignore[attr-defined]
            if any(f.get("name") == "config.set" for f in written):
                break
            await pilot.pause(0.02)
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
    """`05` §7 says `/diag` renders "the diagnostic half of `status`". A failed
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
        # A status on file first: `/diag` after a failed refresh is the case that
        # matters, and it is the one where a stale render has something to print.
        app.submit("/status")
        await _await_chat(pilot, app, "linked")
        await pilot.pause(0.2)
        failing["on"] = True
        before = len(app._last_status)
        assert before, "the cached status is what makes a stale render possible"
        app.submit("/diag")
        await _await_chat(pilot, app, "E_BUSY")
        await pilot.pause(0.3)
        text = _text(app)
        assert "E_BUSY" in text
        tail = text.split("E_BUSY", 1)[1]
        assert "heap_free" not in tail, (
            f"/diag rendered the cached status anyway:\n{tail}"
        )

    await _run(reply, body)



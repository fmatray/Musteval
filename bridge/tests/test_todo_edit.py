"""`05-bridge.md` §7: `/todo edit` is a host modal and a pure plan.

The device has no rename op, so a modify is `append` + `rm` in that order; the
planner is testable without Textual, and the modal is driven through a pilot.
"""

from __future__ import annotations

import json

import pytest

from bridge.client import Session
from bridge.commands import CommandError, todo_edit_frames
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode
from bridge.ui import BridgeApp
from bridge.ui.modals import TodoEditPrompt
from .test_ui import _reply, chat_input


def _args(transport: FakeTransport) -> list[dict]:
    return [
        json.loads(line)["args"]
        for line in transport.written
        if b'"todo.apply"' in line
    ]


def _todo_reply(line: bytes) -> list[bytes]:
    frame = json.loads(line)
    if frame["type"] == "command" and frame["name"] == "todo.apply":
        result = {"phases": [{"name": "auth", "tasks": []}]}
        return [encode(Frame(type="command.result", id="d-9", ts=0,
                             body={"name": "todo.apply", "ok": True, "result": result,
                                   "duration_ms": 1}, reply_to=frame["id"]))]
    return _reply(line)


async def _open(pilot) -> None:
    app = pilot.app
    for _ in range(50):
        if app.session.state.value == "connected":
            break
        await pilot.pause(0.02)
    field = chat_input(app)
    field.focus()
    field.value = "/todo edit"
    await pilot.press("enter")
    for _ in range(80):
        if isinstance(app.screen, TodoEditPrompt):
            return
        await pilot.pause(0.02)
    raise AssertionError("the edit modal did not open")


async def _run_cmd(pilot, text: str) -> None:
    field = chat_input(pilot.app)
    field.focus()
    field.value = text
    await pilot.press("enter")


def test_add_appends_the_content_to_the_phase():
    (frame,) = todo_edit_frames("add", phase="auth", new="wire jwt")
    assert frame.body["args"] == {"op": "append", "phase": "auth", "items": ["wire jwt"]}


def test_modify_appends_before_it_removes_the_old_task():
    frames = todo_edit_frames("modify", phase="auth", task="wire jwt", new="wire oauth")
    assert [f.body["args"] for f in frames] == [
        {"op": "append", "phase": "auth", "items": ["wire oauth"]},
        {"op": "rm", "task": "wire jwt"},
    ]

def test_delete_removes_the_task():
    (frame,) = todo_edit_frames("delete", task="wire jwt")
    assert frame.body["args"] == {"op": "rm", "task": "wire jwt"}

@pytest.mark.parametrize("action, kwargs, match", [
    ("", {"phase": "auth", "new": "x"}, "unknown todo edit action"),
    ("add", {"phase": "  ", "new": "x"}, "add needs a phase"),
    ("add", {"phase": "auth", "new": "  "}, "add needs a phase and a content"),
    ("modify", {"task": "a", "new": "b"}, "modify needs a phase"),
    ("modify", {"phase": "auth", "new": "b"}, "modify needs a phase, a task"),
    ("modify", {"phase": "auth", "task": "a"}, "modify needs a phase, a task and a content"),
    ("delete", {}, "delete needs a task"),
])
def test_planner_refusals(action, kwargs, match):
    with pytest.raises(CommandError, match=match):
        todo_edit_frames(action, **kwargs)


def test_a_refused_modify_never_reaches_the_remove():
    with pytest.raises(CommandError):
        # No phase: the append cannot be built, so the rm must not be either
        todo_edit_frames("modify", task="wire jwt", new="wire oauth")


def test_the_phase_note_says_when_no_phase_is_known():
    assert "type the phase" in TodoEditPrompt()._phase_note()
    assert "auth" in TodoEditPrompt(["auth"])._phase_note()


async def test_add_staged_and_sent():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _open(pilot)
        modal = app.screen
        modal.query_one("#todo-action").value = "add"
        modal.query_one("#todo-phase").value = "auth"
        modal.query_one("#todo-content").value = "wire jwt"
        await pilot.press("enter")
        await pilot.press("ctrl+s")
        for _ in range(80):
            if _args(transport):
                break
            await pilot.pause(0.02)
        assert _args(transport) == [
            {"op": "append", "phase": "auth", "items": ["wire jwt"]}
        ]
        await session.close()


async def test_modify_staged_and_sent():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _open(pilot)
        modal = app.screen
        modal.query_one("#todo-action").value = "modify"
        modal.query_one("#todo-phase").value = "auth"
        modal.query_one("#todo-task").value = "wire jwt"
        modal.query_one("#todo-content").value = "wire oauth"
        await pilot.press("enter")
        await pilot.press("ctrl+s")
        for _ in range(80):
            if _args(transport):
                break
            await pilot.pause(0.02)
        assert _args(transport) == [
            {"op": "append", "phase": "auth", "items": ["wire oauth"]},
            {"op": "rm", "task": "wire jwt"},
        ]
        await session.close()


async def test_delete_staged_and_sent():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _open(pilot)
        modal = app.screen
        modal.query_one("#todo-action").value = "delete"
        modal.query_one("#todo-task").value = "wire jwt"
        await pilot.press("enter")
        await pilot.press("ctrl+s")
        for _ in range(80):
            if _args(transport):
                break
            await pilot.pause(0.02)
        assert _args(transport) == [{"op": "rm", "task": "wire jwt"}]
        await session.close()


async def test_escape_drops_the_plan_and_sends_nothing():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _open(pilot)
        modal = app.screen
        modal.query_one("#todo-action").value = "add"
        modal.query_one("#todo-phase").value = "auth"
        modal.query_one("#todo-content").value = "wire jwt"
        await pilot.press("enter")
        await pilot.press("escape")
        for _ in range(80):
            if not isinstance(app.screen, TodoEditPrompt):
                break
            await pilot.pause(0.02)
        assert not isinstance(app.screen, TodoEditPrompt)
        assert _args(transport) == []
        await session.close()


async def test_the_modal_seeds_its_phases_from_the_last_todo_result():
    transport = FakeTransport(on_write=_todo_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _run_cmd(pilot, "/todo")
        for _ in range(80):
            if app._todo_phases:
                break
            await pilot.pause(0.02)
        await _open(pilot)
        modal = app.screen
        assert "auth" in str(modal.query_one("#todo-phases").render())
        await session.close()

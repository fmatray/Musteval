"""L3 ui: the Files screen (`05-bridge.md` §5.8).

Driven through the pilot against a scripted device: what a user sees when they
open `/files`, walk into a directory, edit a file and delete an entry.
"""

from __future__ import annotations

import json

from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode
from bridge.ui import BridgeApp
from bridge.ui.app import FilesScreen
from bridge.ui.modals import FilePrompt

from .test_ui import _reply
from .test_ui_input import _connected

TREE = {
    "/workspace": [
        {"name": "runs", "dir": True},
        {"name": "notes.txt", "dir": False, "bytes": 11},
    ],
    "/workspace/runs": [
        {"name": "last.jsonl", "dir": False, "bytes": 4},
    ],
}

BODIES = {"/workspace/notes.txt": "hello world"}

SANDBOX_NAMES = ("sandbox.list", "sandbox.read", "sandbox.write", "sandbox.delete")


def _device(line: bytes) -> list[bytes]:
    """A device that answers `sandbox.*` from `TREE`, recording every write."""
    frame = json.loads(line)
    name = frame.get("name")
    if name not in SANDBOX_NAMES:
        return _reply(line)  # hello, ping, status: the shared fake already answers
    reply_to = frame["id"]
    args = frame.get("args") or {}

    def result(body: dict, ok: bool = True) -> list[bytes]:
        return [
            encode(
                Frame(
                    type="command.result",
                    id="d-1",
                    ts=0,
                    body={"name": name, "ok": ok, **body, "duration_ms": 1},
                    reply_to=reply_to,
                )
            )
        ]

    if name == "sandbox.list":
        path = args.get("path", "/workspace/")
        # The screen sends an already-directory path; the keys below are canonical.
        return result(
            {"result": {"path": path, "entries": TREE.get(path.rstrip("/") or "/workspace", [])}}
        )
    if name == "sandbox.read":
        path = args.get("path")
        if path not in BODIES:
            return result({"error": {"code": "E_STORAGE", "message": "no such file"}}, ok=False)
        return result(
            {"result": {"path": path, "text": BODIES[path], "bytes": len(BODIES[path]),
                        "truncated": False}}
        )
    if name == "sandbox.write":
        _device.writes.append(args)
        return result({"result": {"path": args["path"], "created": True, "bytes": 0}})
    _device.deleted.append(args["path"])
    return result({"result": {"path": args["path"], "deleted": True}})


_device.writes = []  # type: ignore[attr-defined]
_device.deleted = []  # type: ignore[attr-defined]


def _app() -> BridgeApp:
    _device.writes = []  # type: ignore[attr-defined]
    _device.deleted = []  # type: ignore[attr-defined]
    session = Session(FakeTransport(on_write=_device), ack_timeout=1, ping_interval=99)
    return BridgeApp(session)


async def _open_files(pilot, app) -> FilesScreen:
    await _connected(pilot)
    app.submit("/files")
    await pilot.pause(0.2)
    return app.query_one(FilesScreen)


def _names(screen: FilesScreen) -> list[str]:
    table = screen.query_one("#files-table")
    return [str(table.get_row_at(row)[1]) for row in range(table.row_count)]


async def test_slash_files_opens_the_screen_at_the_root():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        assert screen.display
        assert not app.query_one("#chat").display
        assert screen.current_path() == "/workspace/"
        assert _names(screen) == ["runs", "notes.txt"]


async def test_enter_on_a_directory_descends_and_a_dotdot_row_returns():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.query_one("#files-table").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert screen.current_path() == "/workspace/runs/"
        assert _names(screen) == ["..", "last.jsonl"]

        screen.query_one("#files-table").focus()
        await pilot.press("enter")  # cursor is on `..`
        await pilot.pause(0.2)
        assert screen.current_path() == "/workspace/"
        assert _names(screen) == ["runs", "notes.txt"]


async def test_u_at_the_root_stays_at_the_root():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.query_one("#files-table").focus()
        await pilot.press("u")
        await pilot.pause(0.2)
        assert screen.current_path() == "/workspace/"


async def test_enter_on_a_file_opens_the_editor_and_ctrl_s_writes_it_back():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=1)  # notes.txt
        await pilot.press("enter")
        await pilot.pause(0.2)

        prompt = app.screen
        assert isinstance(prompt, FilePrompt)
        assert prompt.query_one("#file-body").text == "hello world"

        prompt.query_one("#file-body").text = "goodbye"
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)

        assert _device.writes  # type: ignore[attr-defined]
        written = _device.writes[-1]  # type: ignore[attr-defined]
        assert written["path"] == "/workspace/notes.txt"
        assert written["data"] == "goodbye"
        assert written["final"] is True


async def test_escape_in_the_editor_writes_nothing():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=1)
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not _device.writes  # type: ignore[attr-defined]
        assert screen.display


async def test_d_then_y_deletes_one_entry_and_y_at_the_root_is_impossible():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=1)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert not _device.deleted  # asked first
        await pilot.press("y")
        await pilot.pause(0.2)
        assert _device.deleted == ["/workspace/notes.txt"]  # type: ignore[attr-defined]


async def test_d_on_a_directory_is_refused_without_a_prompt():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=0)  # runs/
        await pilot.press("d")
        await pilot.pause(0.2)
        assert not _device.deleted  # type: ignore[attr-defined]
        assert "device refuses" in "\n".join(
            str(line.text) for line in app.query_one("#chat-log").lines
        )


async def test_n_creates_a_file_in_the_current_directory():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.query_one("#files-table").focus()
        await pilot.press("n")
        await pilot.pause(0.2)
        await pilot.press(*"new.txt")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert _device.writes[-1]["path"] == "/workspace/new.txt"  # type: ignore[attr-defined]
        assert _device.writes[-1]["data"] == ""  # type: ignore[attr-defined]


async def test_a_path_outside_the_sandbox_never_reaches_the_device():
    app = _app()
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.submit("/files /etc/passwd")
        await pilot.pause(0.2)
        assert not app.query_one(FilesScreen).display
        assert "outside /workspace" in "\n".join(
            str(line.text) for line in app.query_one("#chat-log").lines
        )

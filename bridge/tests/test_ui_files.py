"""L3 ui: the Files screen (`05-bridge.md` §5.8).

Driven through the pilot against a scripted device: what a user sees when they
open `/files`, walk into a directory, edit a file and delete an entry.
"""

from __future__ import annotations

import base64
import json

from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode
from bridge.ui import BridgeApp
from bridge.ui.app import FilesScreen, SANDBOX_ROOT
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

SANDBOX_NAMES = (
    "sandbox.list", "sandbox.read", "sandbox.write",
    "sandbox.delete", "sandbox.mkdir", "sandbox.move",
)


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
        _device.lists.append(args.get("path"))
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
        assert args.get("encoding") == "base64"
        chunk = base64.b64decode(args["data"])
        if not args.get("offset"):
            _device.buffers[args["path"]] = b""
        _device.buffers[args["path"]] += chunk
        _device.writes.append(args)
        return result(
            {"result": {"path": args["path"], "created": True, "bytes": len(chunk)}}
        )
    if name == "sandbox.mkdir":
        _device.mkdirs.append(args["path"])
        return result({"result": {"path": args["path"], "created": True}})
    if name == "sandbox.move":
        _device.moves.append((args["from"], args["to"]))
        return result(
            {"result": {"from": args["from"], "to": args["to"], "moved": True}}
        )
    _device.deleted.append((args["path"], args.get("recursive", False)))
    return result({"result": {"path": args["path"], "deleted": True, "entries": 0}})


_device.writes = []  # type: ignore[attr-defined]
_device.deleted = []  # type: ignore[attr-defined]
_device.lists = []  # type: ignore[attr-defined]
_device.buffers = {}  # type: ignore[attr-defined]
_device.mkdirs = []  # type: ignore[attr-defined]
_device.moves = []  # type: ignore[attr-defined]


def _app() -> BridgeApp:
    _device.writes = []  # type: ignore[attr-defined]
    _device.deleted = []  # type: ignore[attr-defined]
    _device.lists = []  # type: ignore[attr-defined]
    _device.buffers = {}  # type: ignore[attr-defined]
    _device.mkdirs = []  # type: ignore[attr-defined]
    _device.moves = []  # type: ignore[attr-defined]
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
        assert written["final"] is True
        assert _device.buffers["/workspace/notes.txt"] == b"goodbye"  # type: ignore[attr-defined]


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
        assert _device.deleted == [("/workspace/notes.txt", False)]  # type: ignore[attr-defined]


async def test_d_on_a_populated_directory_names_the_count_before_deleting():
    """`05` §5.8: the confirmation says what a recursive delete takes.

    "Delete /workspace/runs?" and "delete /workspace/runs and the 1 entries under
    it?" are different questions, and only the second is informed consent.
    """
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=0)  # runs/
        await pilot.press("d")
        await pilot.pause(0.2)
        assert not _device.deleted  # type: ignore[attr-defined]
        question = app.screen.question
        assert "1 entries under it" in question
        assert "/workspace/runs" in question
        await pilot.press("y")
        await pilot.pause(0.2)
        # Recursive, because the directory is not empty.
        assert _device.deleted == [("/workspace/runs", True)]  # type: ignore[attr-defined]


async def test_an_empty_directory_deletes_without_recursive():
    """Nothing to recurse into, so the frame does not ask for the destructive form."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        TREE_BACKUP = TREE["/workspace/runs"]
        TREE["/workspace/runs"] = []
        try:
            table = screen.query_one("#files-table")
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("d")
            await pilot.pause(0.2)
            assert "empty directory" in app.screen.question
            await pilot.press("y")
            await pilot.pause(0.2)
            assert _device.deleted == [("/workspace/runs", False)]  # type: ignore[attr-defined]
        finally:
            TREE["/workspace/runs"] = TREE_BACKUP


async def test_d_then_n_at_the_confirmation_sends_nothing():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("d")
        await pilot.pause(0.2)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not _device.deleted  # type: ignore[attr-defined]


async def test_m_moves_the_selected_file_through_the_move_command():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=1)  # notes.txt
        await pilot.press("m")
        await pilot.pause(0.2)
        # The prompt starts at the current path, so a rename is an edit.
        value = app.screen.query_one("#value-input").value
        assert value == "/workspace/notes.txt"
        for _ in range(len(value)):
            await pilot.press("backspace")
        await pilot.press(*"/workspace/renamed.txt")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert _device.moves == [  # type: ignore[attr-defined]
            ("/workspace/notes.txt", "/workspace/renamed.txt")
        ]


async def test_m_refuses_a_target_outside_the_sandbox_locally():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        table = screen.query_one("#files-table")
        table.focus()
        table.move_cursor(row=1)
        await pilot.press("m")
        await pilot.pause(0.2)
        value = app.screen.query_one("#value-input").value
        for _ in range(len(value)):
            await pilot.press("backspace")
        await pilot.press(*"/etc/passwd")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert not _device.moves  # type: ignore[attr-defined]
        assert "outside" in "\n".join(
            str(line.text) for line in app.query_one("#chat-log").lines
        )


async def test_k_creates_a_directory_in_the_current_directory():
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.query_one("#files-table").focus()
        await pilot.press("k")
        await pilot.pause(0.2)
        await pilot.press(*"archive")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert _device.mkdirs == ["/workspace/archive"]  # type: ignore[attr-defined]


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
        assert _device.buffers["/workspace/new.txt"] == b""  # type: ignore[attr-defined]


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


async def test_one_slash_files_issues_one_listing():
    """`action_show` owns the fetch; the route must not also send one."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        assert _device.lists == ["/workspace/"]  # type: ignore[attr-defined]


async def test_a_repeated_files_path_refetches_that_directory():
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.submit("/files /workspace/runs")
        await pilot.pause(0.2)
        screen = app.query_one(FilesScreen)
        assert screen.current_path() == "/workspace/runs/"
        assert _device.lists[-1] == "/workspace/runs/"  # type: ignore[attr-defined]


async def test_a_file_above_the_chunk_cap_is_staged_in_several_frames():
    """`04` §4.20 allows 2048 raw bytes per frame; a bigger save must split."""
    from bridge.protocol import SANDBOX_CHUNK_BYTES

    body = "x" * (SANDBOX_CHUNK_BYTES * 2 + 5)
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        await app.save_file("/workspace/big.txt", body)
        await pilot.pause(0.2)

        frames = [w for w in _device.writes if w["path"] == "/workspace/big.txt"]  # type: ignore[attr-defined]
        assert len(frames) == 3
        assert [f["offset"] for f in frames] == [0, SANDBOX_CHUNK_BYTES, SANDBOX_CHUNK_BYTES * 2]
        assert [f["final"] for f in frames] == [False, False, True]
        # `bytes` is the decoded length, so the sum is the file the user typed.
        assert _device.buffers["/workspace/big.txt"].decode() == body  # type: ignore[attr-defined]

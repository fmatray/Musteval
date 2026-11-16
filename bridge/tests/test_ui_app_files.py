"""The Files screen's refusal arms (`05-bridge.md` §5.8).

Every key on the screen has a case where it must send nothing: no row under the
cursor, a prompt dismissed, a name that leaves the sandbox, a read that came back
empty. Each one is a guard, so each one is a test — a guard that fires after a
frame is written is a mutation of the sandbox.
"""

from __future__ import annotations

import json

import pytest

from bridge.ui.app import SANDBOX_ROOT, FilesScreen
from bridge.ui.modals import FilePrompt
from bridge.ui.testing import chat_lines

from .test_ui_files import _app, _device, _open_files


def _sandbox_frames(app) -> list[dict]:
    names = ("sandbox.list", "sandbox.read", "sandbox.write", "sandbox.delete",
             "sandbox.mkdir", "sandbox.move")
    return [json.loads(line) for line in app.session._transport.written
            if json.loads(line).get("name") in names]


def _text(app) -> str:
    return "\n".join(chat_lines(app))


async def _none(_screen):
    return None


def _answering(value):
    async def waiter(_screen):
        return value

    return waiter



@pytest.fixture
def no_selection(monkeypatch):
    """A listed directory always carries a `..` row, so the empty-table arm is
    reached by removing the selection rather than by an empty directory."""
    monkeypatch.setattr(FilesScreen, "selected_entry", lambda self: None)


async def test_open_enter_and_delete_with_no_selection_send_nothing(no_selection):
    """`05` §5.8: Enter and `d` act on the cursor row, so with no row they act on
    nothing — a delete that guessed would be the one irreversible mistake."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        before = len(_sandbox_frames(app))
        await app.open_entry()
        await app.delete_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before


async def test_moving_or_downloading_with_no_selection_sends_nothing(no_selection):
    """`05` §5.8.1: both reach for the cursor row first, and neither falls back to
    the directory being browsed."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        before = len(_sandbox_frames(app))
        await app.move_entry()
        await app.download_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before


async def test_a_dismissed_move_prompt_sends_no_move():
    """`05` §5.8: Esc in the move prompt is a cancel, so the entry keeps its name."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _none  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.move_entry()
        await pilot.pause(0.1)
        assert _device.moves == []
        assert len(_sandbox_frames(app)) == before


async def test_a_move_to_the_same_name_sends_no_move():
    """`05` §5.8: the prompt is seeded with the current name, so Enter alone is a
    no-op rather than a move onto itself."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.selected_entry = lambda: ("/workspace/notes.txt", "file")  # type: ignore[assignment]
        app.push_screen_wait = _answering("/workspace/notes.txt")  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.move_entry()
        await pilot.pause(0.1)
        assert _device.moves == []
        assert len(_sandbox_frames(app)) == before


async def test_workspace_itself_cannot_be_moved():
    """`05` §5.8: the root is the sandbox, so moving it would rename the mount point
    every other path is resolved against."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.selected_entry = lambda: (SANDBOX_ROOT, "dir")  # type: ignore[assignment]
        app.push_screen_wait = _answering("/workspace/elsewhere")  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.move_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before
        assert "/workspace cannot be moved" in _text(app)


async def test_a_download_to_a_blank_path_reads_nothing(tmp_path, monkeypatch):
    """`05` §5.8.1: an empty host path is a cancel. Writing there would drop a file
    in the process's own directory."""
    monkeypatch.chdir(tmp_path)
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _none  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.download_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before
        assert list(tmp_path.iterdir()) == []


async def test_a_read_that_returns_nothing_downloads_nothing(tmp_path):
    """A refused `sandbox.read` must not leave an empty host file: an empty file and
    a file the device refused look identical once written."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        real = app._request
        calls = {"n": 0}

        async def refusing(name, args=None, **kwargs):
            if name == "sandbox.read":
                calls["n"] += 1
                return None
            return await real(name, args, **kwargs)

        app._request = refusing  # type: ignore[assignment]
        app.push_screen_wait = _answering(str(tmp_path / "copy.txt"))  # type: ignore[method-assign]
        await app.download_entry()
        await pilot.pause(0.1)
        assert calls["n"] == 1
        assert not (tmp_path / "copy.txt").exists()


async def test_a_new_entry_with_a_blank_name_creates_nothing():
    """`05` §5.8: Esc on the new-file or new-directory prompt is a cancel."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _none  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.new_file()
        await app.new_directory()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before
        assert _device.mkdirs == []


async def test_a_name_that_leaves_the_sandbox_is_refused_before_the_device():
    """`05` §5.8: the join happens on the host, so `..` never reaches the wire."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _answering("../../etc/passwd")  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.new_file()
        await app.new_directory()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before
        assert "not a single entry name" in _text(app)


async def test_an_upload_from_a_blank_path_sends_nothing():
    """`05` §5.8.1: the prompt is empty by default, so Enter alone must not read the
    process's own directory."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _none  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.upload_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before


async def test_a_download_that_the_host_refuses_reports_the_reason(tmp_path):
    """`05` §5.8.1: an unwritable target is reported, not swallowed — the reader has
    to know the copy did not land."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _answering(str(blocker / "x.txt"))  # type: ignore[method-assign]
        await app.download_entry()
        await pilot.pause(0.1)
        text = _text(app).lower()
        assert "downloaded" not in text
        assert "not a directory" in text


async def test_the_editor_is_not_opened_when_the_read_returned_nothing():
    """`05` §5.8: the editor starts from the file's own text; opened on nothing, a
    save would truncate the file."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        real = app._request

        async def refusing(name, args=None, **kwargs):
            if name == "sandbox.read":
                return None
            return await real(name, args, **kwargs)

        app._request = refusing  # type: ignore[assignment]
        app.run_worker(app.edit_file())
        await pilot.pause(0.3)
        assert not isinstance(app.screen, FilePrompt)


async def test_a_file_above_the_read_cap_is_not_opened():
    """`05` §5.8: the editor would save the cut over the original, so a truncated
    read is refused by name instead of shown."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        real = app._request

        async def truncated(name, args=None, **kwargs):
            if name == "sandbox.read":
                return {"text": "prefix", "truncated": True}
            return await real(name, args, **kwargs)

        app._request = truncated  # type: ignore[assignment]
        app.run_worker(app.view_file("/workspace/big.log"))
        await pilot.pause(0.3)
        assert not isinstance(app.screen, FilePrompt)
        assert "above the read cap; not opened" in _text(app)


async def test_a_save_that_changes_nothing_writes_nothing():
    """`05` §5.8: Ctrl+S on an untouched buffer is not a write; a write there would
    bump the file's mtime for every keystroke that decided not to change it."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _answering("hello")  # the file's own text
        await app.view_file("/workspace/notes.md")
        await pilot.pause(0.1)
        assert not [f for f in _sandbox_frames(app) if f.get("name") == "sandbox.write"]


async def test_a_filter_cancel_drops_the_narrowing():
    """`17` §5: Esc in the find prompt drops the filter, so the whole directory
    is back — a cancel that left the last needle applied would hide entries."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.set_needle("notes")
        await pilot.pause(0.1)
        assert "notes" in screen.needle()
        app.push_screen_wait = _none  # type: ignore[method-assign]
        await app.search_files()
        await pilot.pause(0.1)
        assert screen.needle() == ""


async def test_editing_a_directory_reads_nothing():
    """`05` §5.8: Enter on a directory descends; only a file is read. A read of a
    directory path would come back as an error the reader never asked for."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.selected_entry = lambda: ("/workspace/sub", "dir")  # type: ignore[assignment]
        before = len(_sandbox_frames(app))
        await app.edit_file()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before


async def test_an_upload_whose_source_cannot_be_read_reports_it():
    """`05` §5.8.1: an unreadable host path is named, and nothing is written to the
    device — a silent skip would look like an upload that landed."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _answering("/nonexistent/dir/file.txt")  # type: ignore[method-assign]
        before = len(_sandbox_frames(app))
        await app.upload_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before
        text = _text(app).lower()
        assert "no such file" in text or "not found" in text


async def test_an_upload_the_host_refuses_to_read_is_named_not_raised():
    """`05` §5.8.1: a permission error is the host's, not the device's. It must read
    as a line, because a raise out of a worker leaves the reader with no message."""
    app = _app()
    async with app.run_test() as pilot:
        await _open_files(pilot, app)
        app.push_screen_wait = _answering("/etc/shadow")  # type: ignore[method-assign]

        async def denied(send, source, target_dir):
            raise PermissionError("permission denied: /etc/shadow")

        import bridge.transfer as transfer_mod

        original = transfer_mod.upload
        transfer_mod.upload = denied
        try:
            await app.upload_entry()
            await pilot.pause(0.1)
        finally:
            transfer_mod.upload = original
        assert "permission denied" in _text(app)


async def test_every_files_key_on_screen_reaches_the_device_or_the_next_listing():
    """`05` §5.8: each binding names a device verb. Driving the screen's own action
    is what proves the key — a binding that calls nothing looks identical on a
    keymap audit and is dead to a reader."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        sent = lambda: [f.get("name") for f in _sandbox_frames(app)]  # noqa: E731

        before = len(sent())
        screen.action_refetch()
        app.push_screen_wait = _answering(None)  # type: ignore[method-assign]
        screen.action_go_up()
        await pilot.pause(0.25)
        after = sent()[before:]
        assert after.count("sandbox.list") == 2, "refresh and up each list a directory"

        app.push_screen_wait = _answering("")  # type: ignore[method-assign]
        before = len(sent())
        screen.action_new_file()
        screen.action_new_directory()
        screen.action_move_entry()
        await pilot.pause(0.25)
        assert sent()[before:] == [], "a dismissed prompt writes nothing"


async def test_a_file_delete_declined_at_the_prompt_deletes_nothing():
    """`05` §5.8: the confirm is the only thing between the key and an irreversible
    `sandbox.delete`, so declining it must leave the sandbox untouched."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.selected_entry = lambda: ("/workspace/notes.txt", "file")  # type: ignore[assignment]
        before = len(_sandbox_frames(app))
        app.push_screen_wait = _answering(False)  # type: ignore[method-assign]
        await app.delete_entry()
        await pilot.pause(0.1)
        assert len(_sandbox_frames(app)) == before


async def test_a_directory_delete_names_the_count_and_recurses_only_when_there_is_some():
    """`05` §5.8: `recursive` is set only when something is under the directory, and
    the question says how much — "delete runs" and "delete runs and its 47 files"
    must not look the same at the moment the key is pressed."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        asked: list[str] = []

        async def answer(screen):
            asked.append(screen.question)
            return True

        app.push_screen_wait = answer  # type: ignore[method-assign]

        screen.selected_entry = lambda: ("/workspace/empty", "dir")  # type: ignore[assignment]
        await app.delete_entry()
        await pilot.pause(0.15)
        assert asked and "empty" in asked[0]

        screen.selected_entry = lambda: ("/workspace/runs", "dir")  # type: ignore[assignment]
        await app.delete_entry()
        await pilot.pause(0.15)
        frames = [f for f in _sandbox_frames(app) if f.get("name") == "sandbox.delete"]
        assert frames[-1]["args"] == {"path": "/workspace/runs", "recursive": True}
        assert "1 entries" in asked[-1]


async def test_a_delete_reply_reports_how_many_entries_it_took():
    """`04` §4.19: the device names the count in `result.entries`, and the chat line
    repeats it — a bare "ok" leaves the reader unable to tell a one-file delete from
    a directory that took forty with it."""
    app = _app()
    async with app.run_test() as pilot:
        screen = await _open_files(pilot, app)
        screen.selected_entry = lambda: ("/workspace/runs", "dir")  # type: ignore[assignment]
        app.push_screen_wait = _answering(True)  # type: ignore[method-assign]
        await app.delete_entry()
        await pilot.pause(0.3)
        assert "and 1 entries" in _text(app)

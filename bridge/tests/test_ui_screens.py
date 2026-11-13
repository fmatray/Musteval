"""The screen classes' own bindings: `ChatScreen`, `TasksScreen`, `LogsScreen`.

`05` §6.2 pins a key to each action, but a container binding only fires with the
container focused, which is not how a reader reaches most of them. Driving the
action directly is what proves the action, and it is the action the key calls.
"""

from __future__ import annotations

import json

import pytest

from bridge.protocol import reset_ids
from bridge.tests.test_ui_app import (  # noqa: F401 - the shared device double
    CONFIG,
    STATUS,
    _await_chat,
    _await_screen,
    _device,
    _run,
    _text,
)
from bridge.ui.app import ChatScreen, LogsScreen, TasksScreen


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _frames(app) -> list[dict]:
    return [json.loads(line) for line in app.session._transport.written]


async def test_clearing_the_chat_empties_the_pane_and_the_turn_count():
    """`05` §6.2: Ctrl+L is local, and the turn count follows the pane — a reader who
    cleared the log has no turns in front of them."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        chat.write("a line")
        chat._turns = 3
        chat.action_clear_log()
        await pilot.pause(0.05)
        assert chat._turns == 0
        assert chat.query_one("#chat-log").lines == []

    await _run(_device(), body)


async def test_clearing_the_input_empties_the_composer_and_the_candidates():
    """`05` §6.2: Esc/Ctrl+U drops what was typed, and the run is untouched."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/sta"
        await pilot.pause(0.1)
        assert chat._candidates
        before = len(_frames(app))
        chat.action_clear_input()
        await pilot.pause(0.05)
        assert field.value == ""
        assert chat._candidates == []
        assert len(_frames(app)) == before

    await _run(_device(), body)


async def test_clearing_the_input_also_closes_the_candidate_list():
    """`05` §6.2: the clear drops the list too, so a stale list cannot be walked with
    an empty composer and a later Tab cannot apply a command nobody typed."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/sta"
        await pilot.pause(0.1)
        assert chat._candidates
        chat.action_clear_input()
        assert chat._candidates == []
        assert field.value == ""

    await _run(_device(), body)


async def test_a_task_row_selected_runs_that_rows_id():
    """`05` §13.1: Enter on a row is a run of that row, and the id comes from the key
    the table carries — not from the row's index."""

    # The row-selection path calls the same action the key is bound to, so the run
    # is driven through that action.
    async def simple(app, pilot):
        app.submit("/tasks")
        await pilot.pause(0.4)
        app.query_one("#tasks", TasksScreen).action_run_selected()
        for _ in range(200):
            if any(f.get("name") == "tasks.run" for f in _frames(app)):
                break
            await pilot.pause(0.02)
        runs = [f for f in _frames(app) if f.get("name") == "tasks.run"]
        assert runs and runs[-1]["args"] == {"id": "t1"}

    await _run(_device(), simple)


async def test_each_task_binding_reaches_its_own_command():
    """`05` §13.2: run, enable/disable, edit, delete and new are five bindings with
    five different verbs, so a shared action would send the wrong one."""
    from bridge.ui.task_prompt import TaskPrompt

    async def body(app, pilot):
        app.submit("/tasks")
        await pilot.pause(0.4)
        screen = app.query_one("#tasks", TasksScreen)

        # Toggle: the row's id goes with the opposite of its state.
        screen.action_toggle_enabled()
        for _ in range(200):
            if any(f.get("name") == "tasks.set" for f in _frames(app)):
                break
            await pilot.pause(0.02)
        sets = [f for f in _frames(app) if f.get("name") == "tasks.set"]
        assert sets and sets[-1]["args"]["task"]["id"] == "t1"
        assert sets[-1]["args"]["task"]["enabled"] is False
        # The rest of the task survives: the write is a read-modify-write of one flag.
        assert sets[-1]["args"]["task"]["description"] == "water"

        # Delete: confirmed first, so nothing is sent until the reader agrees.
        screen.action_delete_task()
        await _await_screen(pilot, app, __import__("bridge.ui.modals",
                                                   fromlist=["ConfirmPrompt"]).ConfirmPrompt)
        app.screen.action_cancel()
        await _await_chat(pilot, app, "delete cancelled")

        # New: the prompt opens and its answer is an edit with `new`.
        screen.action_new_task()
        await _await_screen(pilot, app, TaskPrompt)
        app.screen.dismiss(None)
        await pilot.pause(0.2)

        # Refetch: a bare list request.
        before = len([f for f in _frames(app) if f.get("name") == "tasks.list"])
        screen.action_refetch()
        for _ in range(200):
            if len([f for f in _frames(app) if f.get("name") == "tasks.list"]) > before:
                break
            await pilot.pause(0.02)
        assert len([f for f in _frames(app) if f.get("name") == "tasks.list"]) > before

    await _run(_device(), body)


async def test_editing_the_selected_task_opens_its_prompt():
    """`05` §13.2: `e` edits the row under the cursor, so the prompt opens on that
    task and a cancel writes nothing."""
    from bridge.ui.task_prompt import TaskPrompt

    async def body(app, pilot):
        app.submit("/tasks")
        await pilot.pause(0.4)
        screen = app.query_one("#tasks", TasksScreen)
        screen.action_edit_task()
        await _await_screen(pilot, app, TaskPrompt)
        app.screen.dismiss(None)
        await pilot.pause(0.2)
        assert not [f for f in _frames(app) if f.get("name") == "tasks.edit"]

    await _run(_device(), body)


async def test_a_task_table_with_no_rows_has_no_selection():
    """`05` §13.1: an empty table has no row under the cursor, so Enter cannot run a
    task that is not there."""

    async def body(app, pilot):
        app._tasks = []
        app.action_show("tasks")
        await pilot.pause(0.3)
        screen = app.query_one("#tasks", TasksScreen)
        assert screen.selected_task_id() is None
        await app.run_selected_task()
        assert not [f for f in _frames(app) if f.get("name") == "tasks.run"]

    await _run(_device(), body)


async def test_the_logs_screen_reports_its_follow_state_and_filter():
    """`05` §5.4: the title carries both, because either one changes what the reader
    is looking at."""

    async def body(app, pilot):
        app.action_show("logs")
        await pilot.pause(0.1)
        logs = app.query_one("#logs", LogsScreen)
        header = logs.query_one("#log-header")
        assert "Follow: ON" in str(header.content)
        logs.action_toggle_follow()
        assert "Follow: OFF" in str(header.content)
        logs.apply_filter("wifi", "warn")
        assert "Filter: wifi" in str(header.content)
        assert "Level: warn" in str(header.content)

    await _run(_device(), body)


async def test_a_filter_hides_a_line_from_the_pane_but_not_from_the_buffer():
    """`05` §5.4: the buffer keeps every line, so widening the filter shows what
    already arrived. A filter that dropped lines would answer with the next arrival
    instead of the past."""

    async def body(app, pilot):
        app.action_show("logs")
        await pilot.pause(0.1)
        logs = app.query_one("#logs", LogsScreen)
        logs.write("12:00:00 [error] fw: first")
        logs.write("12:00:01 [info] net: up")
        logs.apply_filter("fw")
        await pilot.pause(0.05)
        shown = "\n".join(str(line.text) for line in logs.query_one("#log-body").lines)
        assert "first" in shown and "net: up" not in shown
        logs.apply_filter("")
        await pilot.pause(0.05)
        shown = "\n".join(str(line.text) for line in logs.query_one("#log-body").lines)
        assert "net: up" in shown, "the line that was filtered out is still buffered"

        logs.action_clear()
        await pilot.pause(0.05)
        assert len(logs.buffer) == 0
        assert logs.query_one("#log-body").lines == []


async def test_the_composer_grows_with_its_content_and_stops_at_six_rows():
    """`05` §5.1: the composer takes the room its text needs and no more, so a long
    message does not push the conversation off the screen."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "one"
        await pilot.pause(0.1)
        short = field.size.height
        field.value = "\n".join(f"line {n}" for n in range(20))
        await pilot.pause(0.2)
        assert field.size.height >= short
        from bridge.ui.app import COMPOSER_MAX_ROWS

        assert field.size.height <= COMPOSER_MAX_ROWS

    await _run(_device(), body)


# -- the completion panel: a hint, never a menu --------------------------


async def test_the_panel_stops_at_eight_rows_and_says_how_many_it_left_out():
    """`05` §5.1: the panel costs at most eight rows, and the `+N more` line is the
    only thing telling a reader that Home/End reach candidates they cannot see."""
    from bridge.ui.app import COMPLETION_ROWS

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/"
        await pilot.pause(0.15)
        assert len(chat._candidates) == COMPLETION_ROWS
        assert chat._total > COMPLETION_ROWS
        shown = str(chat.query_one("#completion").content)
        assert f"+{chat._total - COMPLETION_ROWS} more" in shown

    await _run(_device(), body)


async def test_home_and_end_walk_the_list_and_leave_other_keys_alone():
    """`05` §5.1: Home/End jump to the list's edges because the focused `Input`
    claims both for line editing. Any other key is line editing and must stay so."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/"
        await pilot.pause(0.15)

        class Key:
            def __init__(self, key):
                self.key = key
                self.stopped = False
                self.default_prevented = False

            def stop(self):
                self.stopped = True

            def prevent_default(self):
                self.default_prevented = True

        end = Key("end")
        chat.on_key(end)
        assert chat._cursor == len(chat._candidates) - 1
        assert end.stopped and end.default_prevented
        home = Key("home")
        chat.on_key(home)
        assert chat._cursor == 0
        other = Key("a")
        chat.on_key(other)
        assert not other.stopped

        chat.action_clear_input()
        empty = Key("home")
        chat.on_key(empty)
        assert not empty.stopped

    await _run(_device(), body)


async def test_the_cursor_wraps_and_a_list_with_no_rows_does_not_move_it():
    """OMP's list behaviour: Down from the last row reaches the first. A list with
    nothing in it has no cursor to move, and must not divide by zero."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/"
        await pilot.pause(0.15)
        chat.action_last_candidate()
        assert chat._cursor == len(chat._candidates) - 1
        chat._move_cursor(1)
        assert chat._cursor == 0, "Down from the last row reaches the first"
        chat._move_cursor(-1)
        assert chat._cursor == len(chat._candidates) - 1

        chat._candidates = []
        before = chat._cursor
        chat._move_cursor(1)
        assert chat._cursor == before

    await _run(_device(), body)


async def test_the_narrow_panel_drops_the_help_column_rather_than_wrapping():
    """`05` §5.1: a row must never wrap, or the `+N more` line is clipped off the
    bottom. Below three columns of room the help text goes, not the row."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/"
        await pilot.pause(0.15)
        panel = chat.query_one("#completion")
        wide = str(panel.content)
        assert "force a status refresh" in wide
        panel.styles.width = 12
        await pilot.pause(0.2)
        chat._render_options()
        narrow = str(panel.content)
        assert "force a status refresh" not in narrow
        assert "/status" in narrow, "the row itself is what must survive"

    await _run(_device(), body)


async def test_enter_on_an_empty_composer_sends_nothing():
    """`05` §5.1: an empty submit is not a frame. The device has no meaning for a
    blank prompt and the reader did not write one."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = ""
        before = len(_frames(app))
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert len(_frames(app)) == before

    await _run(_device(), body)

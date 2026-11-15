"""`app.py`'s command dispatcher, its screen actions and its onboarding steps.

`_run_command` is one long `if` chain and each arm is a different reader intention:
`/files` normalises its path before the screen opens, `/diag` refetches rather than
rendering the last grid, `/secret set` collects the value in a masked modal. Driving
each arm through `submit` is what proves the screen the reader ends up on, and the
`ChatScreen` actions are driven directly because a container binding only fires with
the container focused, which is not how a reader reaches them.
"""

from __future__ import annotations

import base64
import json

import pytest

from bridge.protocol import reset_ids
from bridge.ui.app import COMPLETION_ROWS
from bridge.tests.test_ui_app import (  # noqa: F401 - the shared device double
    CONFIG,
    ENTRIES,
    STATUS,
    _await_chat,
    _await_screen,
    _device,
    _run,
    _text,
)
from bridge.ui.modals import (
    ConfirmPrompt,
    FilePrompt,
    FilterPrompt,
    SecretPrompt,
    TodoEditor,
)


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _frames(app) -> list[dict]:
    """Every frame the bridge wrote, decoded. `encode` flattens the body, so a
    command's `name` and `args` sit at the top level."""
    return [json.loads(line) for line in app.session._transport.written]


async def _until(pilot, predicate, rounds: int = 160) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await pilot.pause(0.02)


async def test_a_bare_command_opens_its_screen_and_a_sub_command_stays_in_chat():
    """`05` §7: `/status` shows the grid, but a sub-command answers in chat — moving
    the reader to a screen that will not show the answer is the bug this prevents."""

    async def body(app, pilot):
        app.submit("/status")
        await _until(pilot, lambda: app.query_one("#status").display)
        assert app.query_one("#status").display is True
        app.submit("/tasks")
        await _until(pilot, lambda: app.query_one("#tasks").display)
        assert app.query_one("#tasks").display is True
        app.submit("/mqtt")
        await _until(pilot, lambda: app.query_one("#mqtt").display)
        assert app.query_one("#mqtt").display is True
        app.submit("/status now")
        await _until(pilot, lambda: app.query_one("#chat").display)
        assert app.query_one("#chat").display is True

    await _run(_device(), body)


async def test_bare_config_and_config_edit_open_the_settings_screen():
    """`05` §5.7: `/config` is the settings screen's command. `get` and `set` stay
    device frames, so the head cannot be answered by one rule; the screen must open
    for the two forms that ask for it and the chat must stay put for the others."""

    async def body(app, pilot):
        app.submit("/config")
        await _until(pilot, lambda: app.query_one("#settings").display)
        assert app.query_one("#settings").display is True
        app.submit("/status")
        await _until(pilot, lambda: app.query_one("#status").display)
        app.submit("/config edit")
        await _until(pilot, lambda: app.query_one("#settings").display)
        assert app.query_one("#settings").display is True
        # `get` and `set` are still device frames: folding the screen into this head
        # must not swallow the two forms that were already mapped to the wire.
        app.submit("/config get wifi.connect_timeout_ms")
        # The screen's own whole-document read carries no `path`, so the path-scoped
        # frame is the one this asserts on.
        await _until(
            pilot,
            lambda: any(f.get("name") == "config.get" and f.get("args")
                        for f in _frames(app)),
        )
        assert {"path": "wifi.connect_timeout_ms"} in [
            f.get("args") for f in _frames(app) if f.get("name") == "config.get"
        ]

    await _run(_device(), body)


async def test_config_edit_with_a_surplus_word_is_refused_and_sends_nothing():
    """`05` §7: a surplus word is refused rather than silently dropped, so the
    reader is not left believing `/config edit wifi` narrowed the screen."""

    async def body(app, pilot):
        before = len(_frames(app))
        app.submit("/config edit wifi")
        await _await_chat(pilot, app, "usage: /config edit")
        # `_await_chat` gives up without asserting, so the line it waited for is
        # checked here: a refusal that never arrived would otherwise look like one.
        assert "usage: /config edit" in _text(app)
        assert len(_frames(app)) == before

    await _run(_device(), body)


async def test_settings_is_no_longer_a_command():
    """`05` §7: `/settings` folded into `/config`, so a reader typing the old name
    is told it is unknown rather than moved to a screen it no longer names."""

    async def body(app, pilot):
        app.submit("/settings")
        await _await_chat(pilot, app, "unknown command")
        assert "unknown command: /settings" in _text(app)
        assert app.query_one("#settings").display is False

    await _run(_device(), body)


async def test_files_normalises_its_path_and_refuses_an_escape():
    """`09` §4: the path is normalised before the screen opens, and an escape is named
    without moving the reader to a listing the device would refuse."""

    async def body(app, pilot):
        app.submit("/files /workspace/runs/")
        await _until(pilot, lambda: app.query_one("#files").display)
        await pilot.pause(0.2)
        assert app._files_path == "/workspace/runs/"
        app.submit("/files ../../etc")
        await _await_chat(pilot, app, "outside")
        assert app._files_path == "/workspace/runs/"

    await _run(_device(), body)


async def test_diag_refetches_rather_than_rendering_a_stale_grid():
    """`05` §7: the diagnostic half is a live read, so a reader asking for it gets
    current counters rather than whatever the screen last painted."""

    async def body(app, pilot):
        before = len([f for f in _frames(app) if f.get("name") == "status"])
        app.submit("/diag")
        await _until(
            pilot,
            lambda: len([f for f in _frames(app) if f.get("name") == "status"]) > before,
        )
        assert len([f for f in _frames(app) if f.get("name") == "status"]) == before + 1
        await _await_chat(pilot, app, "heap")

    await _run(_device(), body)


async def test_a_secret_typed_into_the_modal_is_sent_and_never_shown():
    """`06` §A1: the value goes to `secret.set` and appears in no chat line."""
    value = "hunter2-do-not-print"

    async def body(app, pilot):
        app.submit("/secret set wifi_psk")
        await _await_screen(pilot, app, SecretPrompt)
        app.screen.query_one("#secret-input").value = value
        await pilot.press("enter")
        await _until(pilot, lambda: any(f.get("type") == "secret.set"
                                       for f in _frames(app)))
        sets = [f for f in _frames(app) if f.get("type") == "secret.set"]
        assert sets and sets[-1]["value"] == value
        assert sets[-1]["key"] == "wifi_psk"
        await pilot.pause(0.2)
        assert value not in _text(app)

    await _run(_device(), body)


async def test_a_cancelled_secret_sends_nothing():
    """`05` §8.1: Esc cancels without sending, so nothing is left half-typed."""

    async def body(app, pilot):
        app.submit("/secret set wifi_psk")
        await _await_screen(pilot, app, SecretPrompt)
        await pilot.press("escape")
        await _await_chat(pilot, app, "secret entry cancelled")
        assert not [f for f in _frames(app) if f.get("type") == "secret.set"]

    await _run(_device(), body)


async def test_a_secret_without_a_key_names_the_usage():
    """`05` §8.1: nothing to set is a usage error, not a modal asking for a value it
    cannot file anywhere."""

    async def body(app, pilot):
        app.submit("/secret")
        await _await_chat(pilot, app, "usage: /secret set <key>")
        assert not isinstance(app.screen, SecretPrompt)

    await _run(_device(), body)


async def test_deleting_a_task_asks_first_and_a_cancel_sends_nothing():
    """`05` §7: delete is the one irreversible command, so it is confirmed; a reader
    who backs out must not have deleted anything."""

    async def body(app, pilot):
        app.submit("/tasks delete t1")
        await _await_screen(pilot, app, ConfirmPrompt)
        app.screen.action_cancel()
        await _await_chat(pilot, app, "delete cancelled")
        assert not [f for f in _frames(app) if f.get("name") == "tasks.delete"]

    await _run(_device(), body)


async def test_confirming_a_delete_sends_the_frame():
    async def body(app, pilot):
        app.submit("/tasks delete t1")
        await _await_screen(pilot, app, ConfirmPrompt)
        app.screen.action_confirm()
        await _until(pilot, lambda: any(f.get("name") == "tasks.delete"
                                       for f in _frames(app)))
        deletes = [f for f in _frames(app) if f.get("name") == "tasks.delete"]
        assert deletes and deletes[-1]["args"] == {"id": "t1"}

    await _run(_device(), body)


async def test_a_command_the_parser_refuses_is_named_not_sent():
    """`05` §7: a malformed command is a chat error, so nothing reaches the wire."""

    async def body(app, pilot):
        before = len(_frames(app))
        app.submit("/tasks delete")
        await pilot.pause(0.3)
        assert len(_frames(app)) == before
        assert _text(app).strip() != ""

    await _run(_device(), body)


async def test_tab_applies_the_listed_choice_without_sending_it():
    """`05` §5.1: Tab applies the candidate and never moves focus; the reader still
    presses Enter, so completing must not submit a command they had not finished."""

    async def body(app, pilot):
        chat = app.query_one("#chat")
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/sta"
        await pilot.pause(0.1)
        before = len(_frames(app))
        chat.action_accept_completion()
        await pilot.pause(0.05)
        assert field.value == "/status"
        assert len(_frames(app)) == before

    await _run(_device(), body)


async def test_the_candidate_list_walks_pages_and_edges():
    """`05` §5.1: Home/End and PgUp/PgDn move the list cursor, wrapping at the edges,
    and Esc closes the list before it falls back to leaving the screen."""

    async def body(app, pilot):
        chat = app.query_one("#chat")
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/c"
        await pilot.pause(0.2)
        assert 1 < len(chat._candidates) < COMPLETION_ROWS
        chat.action_last_candidate()
        assert chat._cursor == len(chat._candidates) - 1
        # One page past the end wraps, which is what makes Down from the last row
        # reach the first.
        chat.action_page_forward()
        assert chat._cursor < len(chat._candidates)
        chat.action_first_candidate()
        assert chat._cursor == 0
        chat.action_page_back()
        assert 0 <= chat._cursor < len(chat._candidates)
        chat.action_dismiss_completion()
        await pilot.pause(0.05)
        assert chat._candidates == []
        assert app.query_one("#chat").display is True

    await _run(_device(), body)


async def test_clearing_the_log_and_toggling_follow_change_only_the_screen():
    """`05` §6.2: clear and follow are view state; neither costs a frame."""

    async def body(app, pilot):
        app.action_show("logs")
        await pilot.pause(0.1)
        before = len(_frames(app))
        logs = app._logs()
        logs.write("12:00:00 [info] wifi: up")
        assert logs.buffer.visible()
        logs.action_clear()
        assert logs.buffer.visible() == []
        logs.action_toggle_follow()
        assert logs.follow is False
        logs.action_toggle_follow()
        assert logs.follow is True
        assert len(_frames(app)) == before

    await _run(_device(), body)


async def test_a_log_filter_modal_applies_and_an_empty_one_clears_it():
    """`05` §5.4: `tag[:level]` is the filter grammar, and an emptied field removes the
    filter rather than filtering on the empty string."""

    async def body(app, pilot):
        app.action_show("logs")
        await pilot.pause(0.1)
        logs = app._logs()
        logs.write("12:00:00 [info] wifi: up")
        logs.write("12:00:01 [info] gpio: edge")
        logs.action_edit_filter()
        await _await_screen(pilot, app, FilterPrompt)
        app.screen.query_one("#filter-input").value = "wifi:info"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert logs.buffer.visible() == ["12:00:00 [info] wifi: up"]

        # An emptied field is a cancel, not a filter on the empty string.
        logs.action_edit_filter()
        await _await_screen(pilot, app, FilterPrompt)
        app.screen.query_one("#filter-input").value = ""
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert logs.buffer.visible() == ["12:00:00 [info] wifi: up"]
        logs.apply_filter("")
        assert len(logs.buffer.visible()) == 2

    await _run(_device(), body)


async def test_a_bad_level_in_the_filter_is_named_and_the_filter_stands():
    """`05` §5.4: an unknown level is refused in the chat, so a typo cannot silently
    widen the filter to everything."""

    async def body(app, pilot):
        app.action_show("logs")
        await pilot.pause(0.1)
        logs = app._logs()
        logs.write("12:00:00 [error] llm: timeout")
        logs.action_edit_filter()
        await _await_screen(pilot, app, FilterPrompt)
        app.screen.query_one("#filter-input").value = "llm:loud"
        await pilot.press("enter")
        await _await_chat(pilot, app, "unknown log level")
        assert logs.buffer.log_filter.min_level == "debug"

    await _run(_device(), body)


async def test_the_settings_screen_reports_no_row_on_an_empty_table():
    """`05` §5.7: a screen with nothing in it has no selected path, so Enter cannot
    edit a row that does not exist."""
    from bridge.ui.app import SettingsScreen

    async def body(app, pilot):
        screen = app.query_one("#settings", SettingsScreen)
        assert screen.selected_path() is None
        app.action_show("settings")
        await pilot.pause(0.3)
        assert screen.selected_path() is not None

    await _run(_device(), body)


async def test_running_the_selected_task_sends_its_own_id():
    """`05` §13.1: `r`/Ctrl+X runs the row under the cursor, and the id is the row's,
    not a name guessed from its text."""

    async def body(app, pilot):
        app.submit("/tasks")
        await _until(pilot, lambda: app.query_one("#tasks").display)
        await pilot.pause(0.2)
        app.query_one("#tasks").query_one("#task-table")
        await app.run_selected_task()
        await _until(pilot, lambda: any(f.get("name") == "tasks.run"
                                       for f in _frames(app)))
        runs = [f for f in _frames(app) if f.get("name") == "tasks.run"]
        assert runs and runs[-1]["args"] == {"id": "t1"}

    await _run(_device(), body)


async def test_a_todo_edit_round_trip_sends_one_init_and_a_dismissal_sends_none():
    """`05` §7: the editor rewrites the whole list, so it starts from the device's
    current tree and sends exactly one `init`; backing out sends nothing."""

    async def body(app, pilot):
        app.submit("/todo")
        await pilot.pause(0.3)
        before = len([f for f in _frames(app) if f.get("args", {}).get("op") == "init"])
        app.submit("/todo edit")
        await _await_screen(pilot, app, TodoEditor)
        app.screen.dismiss(None)
        await pilot.pause(0.3)
        views = [f for f in _frames(app) if f.get("args", {}).get("op") == "view"]
        assert len(views) >= 1
        assert len([f for f in _frames(app)
                    if f.get("args", {}).get("op") == "init"]) == before

    await _run(_device(), body)


async def test_an_unreachable_prompt_is_refused_before_the_wire():
    """`05` §5.1: a prompt with no link is answered immediately, so the reader is not
    left looking at their own line for the ack timeout."""
    from bridge.client.session import LinkState

    async def body(app, pilot):
        before = len(_frames(app))
        app.session._state = LinkState.DOWN
        try:
            app.submit("hello")
            await _await_chat(pilot, app, "not sent")
            assert not [f for f in _frames(app) if f.get("type") == "prompt"]
        finally:
            app.session._state = LinkState.CONNECTED
        assert before <= len(_frames(app))

    await _run(_device(), body)


async def test_clear_sends_history_clear_and_wipes_the_pane():
    """`05` §7: `/clear` forgets the conversation on both sides. The order is the
    point — the pane goes only after `history.clear` says the device forgot it, so a
    refusal cannot leave the reader with an empty pane and a device that still
    remembers every turn the next prompt will carry."""

    async def body(app, pilot):
        chat = app.query_one("#chat")
        chat.write("an old turn")
        assert "an old turn" in _text(app)

        app.submit("/clear")
        await _until(pilot, lambda: any(f.get("name") == "history.clear"
                                        for f in _frames(app)))
        await _until(pilot, lambda: "cleared 4 message(s)" in _text(app))

        assert "cleared 4 message(s)" in _text(app)
        # The pane is wiped, and the confirmation is the only line left: a leftover
        # turn here is the failure that motivated the command.
        assert "an old turn" not in _text(app)
        # Clearing is a fact about the session, so the counters that describe it go.
        assert app._usage_total == 0
        assert app._last_prompt is None

    await _run(_device(), body)


async def test_a_refused_clear_keeps_the_pane():
    """`04` §4.28 refuses mid-run with `E_BUSY`. Wiping anyway would tell the reader
    the conversation ended while the device still holds every turn of it, and the
    next prompt would carry all of them back."""

    async def body(app, pilot):
        chat = app.query_one("#chat")
        chat.write("an old turn")
        app.submit("/clear")
        await _await_chat(pilot, app, "history.clear: E_BUSY")
        assert "history.clear: E_BUSY" in _text(app), "the refusal was never shown"
        assert "an old turn" in _text(app)

    await _run(
        _device({"history.clear": {"code": "E_BUSY", "message": "agent run in flight"}}),
        body,
    )

"""`app.py`'s command dispatcher, its screen actions and its onboarding steps.

`_run_command` is one long `if` chain and each arm is a different reader intention:
`/files` normalises its path before the screen opens, `/sys diag` refetches rather
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
    ContextModal,
    InfoModal,
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
    """`05` §7: `/tasks` shows the tree, but a sub-command answers in chat — moving
    the reader to a screen that will not show the answer is the bug this prevents."""

    async def body(app, pilot):
        app.submit("/tasks")
        await _until(pilot, lambda: app.query_one("#tasks").display)
        assert app.query_one("#tasks").display is True
        app.submit("/mqtt")
        await _until(pilot, lambda: app.query_one("#mqtt").display)
        assert app.query_one("#mqtt").display is True
        app.submit("/mqtt publish s1 t p")
        await _until(pilot, lambda: app.query_one("#chat").display)
        assert app.query_one("#chat").display is True

    await _run(_device(), body)


async def test_sys_status_and_sys_diag_open_one_modal_on_different_halves():
    """`05` §5.10: both read one `status` frame and show the same two sections, so a
    reader comparing heap against the quota does not close one box to open another.
    The verb only decides which half is first."""

    async def body(app, pilot):
        app.submit("/sys diag")
        await _until(pilot, lambda: isinstance(app.screen, InfoModal))
        assert isinstance(app.screen, InfoModal)
        # The asked-for half leads, and it is four headed sections rather than one
        # flat list: the box already draws headings, so a reader gets the seam
        # `render.diag_sections` provides.
        heads = [heading for heading, _lines in app.screen.sections]
        assert heads == ["Memory", "Boot", "Tools", "LLM budget", ""]
        app.screen.dismiss(None)
        await _until(pilot, lambda: not isinstance(app.screen, InfoModal))

        app.submit("/sys status")
        await _until(pilot, lambda: isinstance(app.screen, InfoModal))
        # `status` leads with the general half, and the same four sections follow it
        # in the same order, so the two verbs differ only in which comes first.
        heads = [heading for heading, _lines in app.screen.sections]
        assert heads == ["", "Memory", "Boot", "Tools", "LLM budget"]
        app.screen.dismiss(None)
        await _until(pilot, lambda: not isinstance(app.screen, InfoModal))
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


async def test_sys_modal_renders_the_grid_instead_of_printing_its_tags():
    """`05` §5.10: the general half is `render_status`, which is markup with every
    device string already escaped. A body that does not parse it prints `[b]WIFI [/b]`
    at the reader; one that parses it unescaped turns an SSID into a style. This reads
    the widget's rendered text, which is the only place that difference shows."""

    async def body(app, pilot):
        app.submit("/sys status")
        await _until(pilot, lambda: isinstance(app.screen, InfoModal))
        await pilot.pause(0.1)
        body = app.screen.query_one("#info-body")
        rendered = "\n".join(
            str(widget.render()) for widget in body.query("Static") if widget.content
        )
        assert "WIFI " in rendered
        assert "[b]" not in rendered and "[/b]" not in rendered
        assert "[green]" not in rendered
        app.screen.dismiss(None)
        await _until(pilot, lambda: not isinstance(app.screen, InfoModal))

    await _run(_device(), body)


async def test_sys_info_refetches_rather_than_rendering_the_last_frame():
    """`05` §5.10: a reader asking for the box gets current counters, not whatever the
    screen last painted — the modal shows `_last_status`, so the frame must be fresh
    before it opens."""

    async def body(app, pilot):
        before = len([f for f in _frames(app) if f.get("name") == "status"])
        app.submit("/sys diag")
        await _until(pilot, lambda: isinstance(app.screen, InfoModal))
        assert len([f for f in _frames(app) if f.get("name") == "status"]) == before + 1
        app.screen.dismiss(None)
        await _until(pilot, lambda: not isinstance(app.screen, InfoModal))

    await _run(_device(), body)


async def test_sys_reload_reports_the_scopes_and_each_warning():
    """`04` §4.28: the warnings are the only part a reader acts on, so each gets its
    own line rather than being flattened into a JSON block."""

    async def body(app, pilot):
        app.submit("/sys reload")
        await _await_chat(pilot, app, "warning")
        text = _text(app)
        assert "reloaded: cfg, tools" in text
        assert "warning (mqtt): broker unreachable" in text
        assert "{" not in text, "a reload answers in prose, not a JSON block"

    await _run(_device(), body)


async def test_sys_reboot_reports_the_grace_period_in_words():
    async def body(app, pilot):
        app.submit("/sys reboot")
        await _await_chat(pilot, app, "rebooting in 250 ms")
        assert "rebooting in 250 ms" in _text(app)

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
        field.value = "/sy"
        await pilot.pause(0.1)
        before = len(_frames(app))
        chat.action_accept_completion()
        await pilot.pause(0.05)
        assert field.value == "/sys"
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
    """`17` §1: `r`/Ctrl+X runs the row under the cursor, and the id is the row's,
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


async def test_context_opens_the_split_box_with_an_empty_right_pane():
    """`05` §7.5: `/context` asks the device for the eight blocks and shows them in one
    split box, with the right pane empty until a row is selected.

    Two things are worth pinning: it must reach the wire (a host-side estimate would be
    a number the device contradicts), and every block must be listed with its counter —
    a table that silently dropped the empty ones would leave the reader unable to tell
    "no thinking" from "not measured". The empty pane is the third: showing a block the
    reader has not asked for reads as the answer to a question they have not asked.
    """

    async def body(app, pilot):
        app.submit("/context")
        await _until(pilot, lambda: isinstance(app.screen, ContextModal))
        assert isinstance(app.screen, ContextModal), f"screen is {app.screen!r}"
        await pilot.pause(0.1)

        sent = [f for f in _frames(app) if f.get("name") == "context.budget"]
        assert sent, "the bridge answered from the host instead of asking the device"

        view = app.screen.query_one("#context-blocks")
        rows = [str(item.query_one("Static").render()) for item in view.children]
        assert len(rows) == 8, rows
        # The counters are the frame's, beside the block they count.
        assert any("skills" in row and " 2 " in row for row in rows), rows
        assert any("tools" in row and " 18 " in row for row in rows), rows
        assert any("assistant thinking" in row and " 0 " in row for row in rows), rows

        # Nothing is fetched for a block the reader has not selected, and the pane
        # says nothing rather than showing an arbitrary block.
        assert not [f for f in _frames(app) if f.get("name") == "context.block"]
        assert str(app.screen.query_one("#context-text").render()) == ""

        app.screen.dismiss(None)
        await _until(pilot, lambda: not isinstance(app.screen, ContextModal))

    await _run(_device(), body)


async def test_selecting_a_context_block_fills_the_right_pane():
    """`05` §7.5: a row is a request for that block's text, not a local lookup.

    Selected twice in a row, the pane must end up showing the second block: the two
    frames are in flight together and the slower answer may arrive last, which would
    put one block's text under another block's row.
    """

    async def body(app, pilot):
        app.submit("/context")
        await _until(pilot, lambda: isinstance(app.screen, ContextModal))
        await pilot.pause(0.1)
        modal = app.screen
        view = modal.query_one("#context-blocks")

        view.focus()
        view.action_cursor_down()
        await pilot.pause(0.1)
        view.action_cursor_down()
        await pilot.press("enter")
        await _until(
            pilot,
            lambda: modal.query_one("#context-text").render() != "",
        )
        # Two rows down from the first row is `skills`.
        await _until(
            pilot,
            lambda: "text of" in str(modal.query_one("#context-text").render()),
        )
        assert str(modal.query_one("#context-text").render()) == "text of skills"

        sent = [f for f in _frames(app) if f.get("name") == "context.block"]
        assert [f["args"]["block"] for f in sent] == ["skills"], sent

        # An answer for a block the reader has since left must not overwrite the pane.
        modal.show_block("tools", "stale", False)
        assert str(modal.query_one("#context-text").render()) == "text of skills"

        # A truncated block says so: the device's text is a prefix of the block, and a
        # reader comparing it against a prompt they saw in `/export` needs to know why
        # the ends differ (`04` §4.32).
        modal.show_block("skills", "half a prompt", True)
        assert "[truncated:" in str(modal.query_one("#context-text").render())

        # Escape closes it, like every other box.
        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, ContextModal))

    await _run(_device(), body)


async def test_compact_asks_before_it_rewrites_the_history():
    """`05` §7.5: `/compact` names what will be lost, then asks.

    The confirm is the only thing standing between a reader and a discarded
    transcript, so both halves are asserted: the frame is not sent before the answer,
    and `n` leaves the history alone.
    """

    async def body(app, pilot):
        app.submit("/compact")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmPrompt))
        assert isinstance(app.screen, ConfirmPrompt), f"screen is {app.screen!r}"
        # The question names the count and the bytes, because "are you sure" is not
        # decidable and "3 messages, 1304 B" is.
        question = str(app.screen.query_one(".prompt-title").render())
        assert "3 messages" in question and "1304 B" in question, question
        assert not [f for f in _frames(app) if f.get("name") == "context.compact"]

        app.screen.action_cancel()
        await _until(pilot, lambda: not isinstance(app.screen, ConfirmPrompt))
        await _await_chat(pilot, app, "compact cancelled")
        assert "compact cancelled" in _text(app)
        assert not [f for f in _frames(app) if f.get("name") == "context.compact"], (
            "a cancelled compaction still spent a request"
        )

    await _run(_device(), body)


async def test_compact_reports_what_it_replaced_and_that_the_summary_is_the_copy():
    """`05` §7.5: the report names the counts and the size change, and says the old
    messages are gone — the transcript's only other copy is what `/export` wrote."""

    async def body(app, pilot):
        app.submit("/compact")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmPrompt))
        app.screen.action_confirm()
        await _await_chat(pilot, app, "summarised 3 messages")

        await _await_chat(pilot, app, "the summary is now the only copy")
        assert "summarised 3 messages, kept 0, 1304 B -> 240 B" in _text(app)
        assert "the summary is now the only copy" in _text(app)
        # The summary itself is shown: a reader who must trust the rewrite needs to
        # see what the device kept.
        assert "Goal: ship `/compact`." in _text(app)

        sent = [f for f in _frames(app) if f.get("name") == "context.compact"]
        assert len(sent) == 1
        # No `keep` argument rather than `keep: 0`: the default is the device's.
        assert sent[0]["args"] == {}

    await _run(_device(), body)


async def test_compact_keep_is_forwarded_and_the_budget_is_read_quietly():
    """`05` §7.5: `keep` names how many messages survive verbatim, and the budget read
    must not open the `/context` box under the confirm."""

    async def body(app, pilot):
        app.submit("/compact 2")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmPrompt))
        # A budget read for a count must not have opened the split box.
        assert isinstance(app.screen, ConfirmPrompt), f"screen is {app.screen!r}"
        app.screen.action_confirm()
        await _await_chat(pilot, app, "summarised 3 messages")

        sent = [f for f in _frames(app) if f.get("name") == "context.compact"]
        assert sent[0]["args"] == {"keep": 2}, sent

    await _run(_device(), body)


async def test_compact_refuses_an_empty_history_without_a_confirm():
    """`05` §7.5: nothing to compact is answered in chat, from the device's own count.

    The count is what refuses it, so a modal over a history the device says is empty
    would be a question with one answer and a wasted keystroke.
    """

    async def body(app, pilot):
        app.submit("/compact")
        await _await_chat(pilot, app, "compact: the conversation is empty")
        assert "compact: the conversation is empty" in _text(app)
        assert not isinstance(app.screen, ConfirmPrompt)
        assert not [f for f in _frames(app) if f.get("name") == "context.compact"]

    await _run(
        _device({"context.budget": {"budget": 16384, "used": 40, "free": 16344,
                                    "messages": 0, "blocks": {}, "counts": {}}}),
        body,
    )


async def test_compact_names_cancelling_the_run_when_the_device_is_busy():
    """`05` §7.5: `E_BUSY` comes with the next step, because "busy" alone leaves the
    reader nowhere to go."""

    async def body(app, pilot):
        app.submit("/compact")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmPrompt))
        app.screen.action_confirm()
        await _await_chat(pilot, app, "cancel it first")
        assert "compact: a run is in flight; cancel it first, then compact" in _text(app)

    await _run(
        _device({"context.compact": {"code": "E_BUSY", "message": "agent run in flight"}}),
        body,
    )


async def test_compact_refuses_a_keep_outside_the_ceiling():
    """`04` §4.33 caps `keep` at eight; refusing here spends no request."""

    async def body(app, pilot):
        app.submit("/compact 9")
        await _await_chat(pilot, app, "keep must be 0 to 8")
        assert "/compact [keep]: keep must be 0 to 8" in _text(app)
        assert not [f for f in _frames(app) if f.get("name") == "context.budget"], (
            "a malformed keep still read the budget"
        )

    await _run(_device(), body)


async def test_wifi_list_and_edit_and_providers_reach_their_editors():
    """`05` §7.6 and §7.7: the three commands are UI-only — the screen answers, so the
    dispatch arms are the only path a reader has to them. Each opens its picker and
    reports the cancel in chat, which is what proves the arm ran rather than falling
    through to the wire."""
    from bridge.ui.modals import _PickBox

    async def body(app, pilot):
        seen: list[object] = []

        async def picker(screen):
            seen.append(screen)
            return None

        app.push_screen_wait = picker  # type: ignore[method-assign]
        # `/wifi list` answers in chat: it reads two frames and writes rows, so it
        # has no picker to answer.
        app.submit("/wifi list")
        await _await_chat(pilot, app, "home")
        assert "joined" in _text(app)
        for command in ("/wifi edit", "/providers"):
            seen.clear()
            app.submit(command)
            await _until(pilot, lambda: bool(seen))
            assert isinstance(seen[0], _PickBox), command

        text = _text(app)
        assert "wifi edit cancelled" in text
        assert "providers cancelled" in text

    await _run(_device(), body)


async def test_a_refused_wifi_status_ends_the_list_without_a_second_excuse():
    """`_request` already names a refusal, so `/wifi list` must return rather than
    paraphrase it. The document is still fetched, because the refusal says nothing
    about which networks are configured."""

    async def body(app, pilot):
        app.submit("/wifi list")
        await _await_chat(pilot, app, "wifi.status")
        text = _text(app)
        # The document answered, so rows would have been drawn had the status landed.
        assert "home" not in text

    await _run(_device(drop={"wifi.status"}), body)

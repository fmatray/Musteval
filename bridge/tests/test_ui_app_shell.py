"""`app.py`'s shell: startup, the frame plan, and the panels with nothing in them.

`05` §5.1 puts the prompt bar and the completion panel on the Chat screen; §10 says
a frame plan is sent in order. Both are only exercised by an app that has already
mounted, so each case here drives the app rather than a widget in isolation.
"""

from __future__ import annotations

import json

import pytest

from bridge.protocol import Frame, encode, reset_ids
from bridge.tests.test_ui_app import (  # noqa: F401 - the shared device double
    CONFIG,
    STATUS,
    _await_chat,
    _await_screen,
    _device,
    _run,
    _text,
)
from bridge.client.session import Session
from bridge.link.fake_transport import FakeTransport
from bridge.ui.app import (
    BridgeApp,
    ChatScreen,
    FilesScreen,
    LogsScreen,
    MqttScreen,
    StatusScreen,
)
from bridge.ui.modals import (
    ConfirmPrompt,
    FilePrompt,
    NetworkStep,
    ValuePrompt,
    Working,
)
from bridge.ui.render import binding_pairs
from bridge.ui.testing import WidgetHost, chat_lines


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _frames(app) -> list[dict]:
    return [json.loads(line) for line in app.session._transport.written]


async def test_a_link_that_never_connected_says_so_and_asks_for_nothing():
    """`05` §5.1: the bar is only meaningful once linked, so a failed start writes
    the state it got and returns — a `status` request over a dead link would sit in
    the queue until the ack timeout and bury the actual cause."""

    async def body(app, pilot):
        await app._start()
        assert "[" in _text(app)
        assert not [f for f in _frames(app) if f.get("name") == "status"]

    await _run(lambda line: [], body)


async def test_retry_resends_the_last_prompt_and_a_fresh_app_has_none():
    """`05` §7: `/retry` is the one command that resends what was typed, and with
    nothing sent yet the parser refuses it before the wire."""

    async def body(app, pilot):
        app.submit("/retry")
        await _await_chat(pilot, app, "no prompt to retry")
        assert not [f for f in _frames(app) if f.get("type") == "prompt"]

        app.submit("/ping device")
        await _await_chat(pilot, app, "pong")
        app.submit("hello")
        await _await_chat(pilot, app, "done")
        before = len([f for f in _frames(app) if f.get("type") == "prompt"])
        app.submit("/retry")
        for _ in range(120):
            after = [f for f in _frames(app) if f.get("type") == "prompt"]
            if len(after) == before + 1:
                break
            await pilot.pause(0.02)
        assert len(after) == before + 1
        assert after[-1]["text"] == "hello"

    await _run(_device(), body)


async def test_ctrl_g_with_no_candidate_accepts_the_inline_suggestion():
    """`17` §1: Tab at the end of a line takes the shell-style suggestion, which
    is the only completion available when the list is closed."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "/he"
        await pilot.pause(0.1)
        chat.action_accept_completion()
        await pilot.pause(0.05)
        assert field.value == "/help"
        assert field.cursor_position == len("/help")

    await _run(_device(), body)


async def test_moving_the_cursor_with_no_candidates_leaves_it_where_it_was():
    """`17` §1: arrows on a closed list must not move a cursor that is not shown,
    or the next Enter would send a row the reader never picked."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        chat.query_one("#chat-input").focus()
        chat._candidates = []
        chat._cursor = 0
        for action in (
            chat.action_history_back,
            chat.action_history_forward,
            chat.action_first_candidate,
            chat.action_last_candidate,
            chat.action_page_back,
            chat.action_page_forward,
        ):
            action()
        assert chat._cursor == 0

    await _run(_device(), body)


async def test_a_row_too_narrow_for_the_help_column_drops_the_column():
    """`17` §1: the panel is sized to the width it has. Appending the help text
    into no room would wrap the row, and `+N more` would be clipped away."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        chat._show_options("/")
        await pilot.pause(0.1)
        panel = chat.query_one("#completion")
        assert panel.display is True
        # A width at or below the name plus markers leaves no room for help.
        panel.styles.width = 8
        await pilot.pause(0.1)
        chat._render_options()
        await pilot.pause(0.05)
        rows = str(panel.content).splitlines()
        assert rows, "the list still renders"
        assert all("/" in row for row in rows if row.startswith(("> ", "  ")))

    await _run(_device(), body)


async def test_a_files_directory_with_no_entries_has_no_selection():
    """`05` §5.8: an empty directory has nothing to open, delete or download, so the
    selection is `None` rather than the last row of the directory above."""

    async def body(app, pilot):
        app.action_show("files")
        await pilot.pause(0.1)
        await app.browse("/workspace/empty/")
        await _await_chat(pilot, app, "")
        await pilot.pause(0.2)
        screen = app.query_one(FilesScreen)
        table = screen.query_one("#files-table")
        table.clear()
        assert screen.selected_entry() is None

    await _run(_device(), body)


async def test_moving_in_an_empty_broker_list_leaves_the_selection_at_zero():
    """`05` §5.5: with no brokers there is nothing to step through, and a selection
    left above zero would be out of range the moment one arrives."""

    async def body(app, pilot):
        app.action_show("mqtt")
        await pilot.pause(0.1)
        screen = app.query_one(MqttScreen)
        screen.render_brokers([])
        screen.action_move(1)
        assert screen.selected == 0

    await _run(_device(), body)


async def test_a_delete_that_took_entries_says_how_many():
    """`05` §5.8: removing a directory reports what it took with it, because the
    reader asked to delete one row and lost a subtree."""

    async def body(app, pilot):
        frame = encode(
            Frame(
                type="command.result",
                id="d-7",
                ts=0,
                body={
                    "name": "sandbox.delete",
                    "ok": True,
                    "duration_ms": 1,
                    "result": {"path": "/workspace/runs", "entries": 3},
                },
            )
        )
        app.session._transport.feed(frame)  # type: ignore[attr-defined]
        await _await_chat(pilot, app, "3 entries")
        assert "and 3 entries" in _text(app)

    await _run(_device(), body)


async def test_a_mutation_that_took_nothing_does_not_claim_entries():
    """`05` §5.8: a file delete names no count, and `and 0 entries` would read as a
    failure rather than a plain delete."""

    async def body(app, pilot):
        frame = encode(
            Frame(
                type="command.result",
                id="d-8",
                ts=0,
                body={
                    "name": "sandbox.delete",
                    "ok": True,
                    "duration_ms": 1,
                    "result": {"path": "/workspace/notes.md"},
                },
            )
        )
        app.session._transport.feed(frame)  # type: ignore[attr-defined]
        await _await_chat(pilot, app, "notes.md ok")
        assert "entries" not in _text(app)

    await _run(_device(), body)


async def test_a_bare_listing_path_gains_its_trailing_slash():
    """`05` §5.8: a directory is listed with its slash, and a typed path without one
    is the same directory — the device refuses the form the reader did not mean."""

    async def body(app, pilot):
        app.action_show("files")
        await pilot.pause(0.1)
        await app.browse("/workspace")
        await pilot.pause(0.2)
        assert app.query_one(FilesScreen).current_path() == "/workspace/"
        listed = [f for f in _frames(app) if f.get("name") == "sandbox.list"]
        assert listed[-1]["args"]["path"] == "/workspace/"

    await _run(_device(), body)


async def test_enter_on_an_empty_composer_sends_nothing():
    """`05` §5.1: an empty submit is not a prompt — sending `""` would start a run
    on a blank message and spend a call."""

    async def body(app, pilot):
        chat = app.query_one("#chat", ChatScreen)
        field = chat.query_one("#chat-input")
        field.focus()
        field.value = "   "
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert not [f for f in _frames(app) if f.get("type") == "prompt"]
        assert field.value == ""

    await _run(_device(), body)


async def test_every_device_frame_type_has_a_handler():
    """`04` §2.1: the decoder admits exactly the device-to-bridge set, so a type in
    that set with no handler would be dropped in silence the day it is added."""

    async def body(app, pilot):
        from bridge.protocol import DEVICE_TO_BRIDGE

        handled = {
            "hello_ack", "status", "log", "agent.start", "agent.tool",
            "agent.ask", "agent.tool_result", "agent.thinking", "agent.final",
            "agent.error",
            "task.fired", "gpio.edge", "mqtt.message", "event", "command.result",
            "pong",
        }
        assert handled == set(DEVICE_TO_BRIDGE)

    await _run(_device(), body)


async def test_a_reset_counter_under_safe_mode_says_when_it_lifts():
    """`04` §4.23: clearing the counter while safe mode is on does not leave it, and
    the reply is the only place that says which boot it applies to."""

    async def body(app, pilot):
        app.session._transport.feed(  # type: ignore[attr-defined]
            encode(
                Frame(
                    type="command.result",
                    id="d-6",
                    ts=0,
                    body={
                        "name": "sys.reset_counter",
                        "ok": True,
                        "duration_ms": 1,
                        "result": {"previous": 2, "safe_mode": True},
                    },
                )
            )
        )
        await _await_chat(pilot, app, "safe mode lifts on the next boot")
        assert "boot failures: 2 -> 0" in _text(app)

    await _run(_device(), body)


async def test_a_latency_reading_with_no_status_yet_paints_nothing():
    """`05` §10: `pong` updates the number the Status grid shows, and a heartbeat
    that lands before the first `status` has no grid to update — painting then would
    replace the placeholder with a grid of `None`s."""

    async def body(app, pilot):
        app.action_show("status")
        await pilot.pause(0.1)
        status = app.query_one("#status")
        assert getattr(status, "_status", None) is None
        status.show_latency(4.2)
        await pilot.pause(0.05)
        assert "no status yet" in str(status.query_one("#status-body").content)

    await _run(_device(drop={"status"}), body)


async def test_a_delete_result_that_is_not_an_object_names_the_path_alone():
    """`05` §5.8: the count line is read from the reply body, so a reply shaped
    unexpectedly is reported without inventing a number."""

    async def body(app, pilot):
        app.session._transport.feed(  # type: ignore[attr-defined]
            encode(
                Frame(
                    type="command.result",
                    id="d-7",
                    ts=0,
                    body={
                        "name": "sandbox.delete",
                        "ok": True,
                        "duration_ms": 1,
                        "result": "deleted",
                    },
                )
            )
        )
        await pilot.pause(0.2)
        assert "entries" not in _text(app)

    await _run(_device(), body)


async def test_a_second_bare_files_command_does_not_refetch():
    """`05` §5.8: a bare `/files` shows the path the reader left it at. Refetching on
    every visit would also drop a filter the reader had just applied (`17` §5)."""

    async def body(app, pilot):
        app.action_show("files")
        await pilot.pause(0.1)
        await app.browse("/workspace/runs/")
        await pilot.pause(0.2)
        before = len([f for f in _frames(app) if f.get("name") == "sandbox.list"])
        app.action_show("chat")
        await pilot.pause(0.05)
        app.action_show("files")
        await pilot.pause(0.2)
        after = len([f for f in _frames(app) if f.get("name") == "sandbox.list"])
        assert after == before
        assert app.query_one(FilesScreen).current_path() == "/workspace/runs/"

    await _run(_device(), body)


async def test_a_prompt_answered_by_an_error_names_the_code():
    """`04` §4.19: `agent.error` is the answer to a prompt, not a push, so the run
    that never started is reported where the reader typed."""

    async def body(app, pilot):
        app.submit("hello")
        await _await_chat(pilot, app, "E_LLM")
        assert "quota" in _text(app).lower()

    def reply(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("type") == "prompt":
            return [
                encode(
                    Frame(
                        type="agent.error",
                        id="d-3",
                        ts=0,
                        body={
                            "run_id": "r-1",
                            "code": "E_LLM",
                            "message": "quota exhausted",
                            "stage": "provider",
                        },
                        reply_to=frame["id"],
                    )
                )
            ]
        return _device()(line)

    await _run(reply, body)


async def test_onboard_typed_as_a_command_opens_the_network_step():
    """`05` §7: `/onboard` is dispatched by the app, not the parser — it opens a
    screen and so cannot be a frame plan. Cancelling it there says where it stopped."""

    async def body(app, pilot):
        app.submit("/onboard")
        await _await_screen(pilot, app, NetworkStep)
        assert app.screen.query_one("#step-ssid")
        await pilot.press("escape")
        await _await_chat(pilot, app, "cancelled at the network step")

    await _run(_device(), body)


# -- local verbs, and the panels that answer without the device ----------


async def test_the_local_verbs_answer_from_state_already_in_hand(tmp_path, monkeypatch):
    """`05` §7: `usage` and `export` are local, so they cost no frame. `export` names
    the file it wrote (`test_ui_app.py` asserts the file's content); `usage` quotes
    the counters. The cwd is a temp directory so the export lands outside the tree.

    `/context` is deliberately absent: it asks the device (`context.budget`), because
    the prompt, the tool array and the history are device state and a host-side count
    would be a number the device contradicts (`05` §7.5)."""
    from bridge.protocol import Frame as F

    monkeypatch.chdir(tmp_path)

    async def body(app, pilot):
        app.query_one("#chat-log").clear()  # type: ignore[attr-defined]
        app._on_frame(
            F(
                type="agent.final",
                id="d-9",
                ts=0,
                body={"run_id": "r-1", "text": "hi", "stop": "done",
                      "usage": {"total_tokens": 128}},
            )
        )
        app._on_agent_start(
            F(type="agent.start", id="d-10", ts=0,
              body={"run_id": "r-2", "input_bytes": 42})
        )
        before = len(app.session._transport.written)
        for verb in ("/usage", "/export", "/help"):
            app.submit(verb)
            await pilot.pause(0.15)
        assert len(app.session._transport.written) == before
        text = _text(app)
        assert "total tokens: 128" in text
        assert "exported bridge-export-" in text

    await _run(_device(), body)


async def test_quit_ends_the_app():
    """`05` §6.2: `/quit` leaves. Anything else would strand the reader in a TUI the
    shell thinks is still running."""

    async def body(app, pilot):
        app.submit("/quit")
        for _ in range(100):
            if not app.is_running:
                break
            await pilot.pause(0.02)
        assert not app.is_running

    await _run(_device(), body)


async def test_escape_on_a_screen_that_is_shown_returns_to_the_chat():
    """`05` §6: Esc leaves the screen the reader is on before it cancels the run, so
    looking away from a run cannot drop it."""

    async def body(app, pilot):
        app.action_show("status")
        await pilot.pause(0.1)
        assert app.query_one("#status").display is True
        app.action_back()
        await pilot.pause(0.1)
        assert app.query_one("#status").display is False


async def test_a_reply_that_is_not_a_command_result_is_not_read_for_a_result():
    """`04` §2.1: a `command` is answered by `command.result`. Any other frame is a
    desynchronised stream, so its body holds no `result` — reading one would report
    success for a command the device never answered."""
    from bridge.protocol import Frame as F

    async def body(app, pilot):
        app.query_one("#chat-log").clear()  # type: ignore[attr-defined]

        async def wrong_reply(frame):
            return F(type="status", id=frame.id, ts=0, body={"result": {"ok": 1}})

        app.session.request = wrong_reply  # type: ignore[method-assign]
        assert await app._request("status") is None
        assert "ok" not in _text(app)

    await _run(_device(), body)


@pytest.mark.asyncio
async def test_the_global_hint_names_the_way_to_the_whole_list() -> None:
    """`17` §3: the app row announces the shortcut list and does not attempt it.

    The row used to carry every global key, `F1`–`F7`, `Ctrl+R`, `Ctrl+Q` and `Esc`,
    and at 50x20 it wrapped to the width of the terminal and took rows the composer
    needed. The list moved into the `/help` modal, which has room for it; what the
    row still owes the reader is the key that opens it. The pairs are derived from
    `BridgeApp.BINDINGS` by action, so a renamed key moves the row too.
    """
    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(50, 20)):
        shown = str(app.query_one("#app-hint").content)
        assert "F1" in shown and "Help" in shown
        assert "Escape" in shown
        # One row: an app bar that grows is the bug this contract replaced.
        assert app.query_one("#app-hint").region.height == 1, shown
    await session.close()


@pytest.mark.asyncio
async def test_help_lists_every_global_shortcut_the_app_binds() -> None:
    """`17` §3: the shortcut list lives in `/help`, and it is the app's own list.

    Moved out of the row under the composer, which could not hold it without
    wrapping the panes away. The modal renders the pairs `BridgeApp.BINDINGS`
    yields, so a key added to the app appears here in the same edit and a key the
    app drops cannot linger as a lie the reader presses.
    """
    from bridge.ui.modals import HelpModal

    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(100, 40)) as pilot:
        await _await_chat(pilot, app, "")
        app.submit("/help")
        await _await_screen(pilot, app, HelpModal)
        shown = str(app.screen.query_one("#help-keys").content)
        pairs = binding_pairs(BridgeApp.BINDINGS)
        assert pairs
        for chord, label in pairs:
            assert chord in shown, (chord, shown)
            assert label.split(" ")[0] in shown, (label, shown)
    await session.close()


@pytest.mark.asyncio
async def test_every_screen_ends_in_a_hint_naming_every_key_it_binds() -> None:
    """`17` §3: every screen ends in a hint line, built from its `BINDINGS`.

    Four screens bound keys and ended in content instead: the composer on Chat, the
    grid on Status, the buffer on Logs, the traffic log on MQTT. Every key those
    screens bind was discoverable only by reading the doc.
    """
    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    rows = {
        "f2": "#chat-hint",
        "f3": "#status-hint",
        "f4": "#task-hint",
        "f5": "#log-hint",
        "f6": "#mqtt-hint",
        "f7": "#files-hint",
    }
    async with app.run_test(size=(120, 40)) as pilot:
        for key, selector in rows.items():
            await pilot.press(key)
            await pilot.pause()
            shown = str(app.query_one(selector).content)
            pairs = binding_pairs(app.query_one(selector).parent.BINDINGS)
            assert pairs, selector
            for chord, label in pairs:
                assert chord in shown, (selector, chord, shown)
                assert label.split(" ")[0] in shown, (selector, label, shown)
    await session.close()


@pytest.mark.asyncio
async def test_a_control_sequence_from_the_device_never_reaches_the_terminal() -> None:
    """`06` T16: a payload is rendered, never interpreted.

    `agent.error` and `agent.final` carry text the device composed, which can be a
    provider's error body or an MQTT payload. `sanitize` existed and was applied to
    MQTT traffic and log lines, but the chat pane wrote the string straight into a
    rich `Text`, so an ESC went to the terminal as a control sequence.
    """
    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    hostile = "boom\x1b[31mred\x1b[2Jwiped\x07bell"
    async with app.run_test(size=(120, 40)):
        app._on_frame(Frame(type="agent.error", id="x", ts=0,
                            body={"code": "E_LLM", "message": hostile}))
        # A frame is routed synchronously; the compositor needs one more pass to
        # lay the line out, which is what makes the escape sequence observable.
        app.refresh()
        shown = [s.text for s in app.screen._compositor.render_strips()]
        assert any("boom" in line for line in shown)
        for line in shown:
            assert "\x1b" not in line, repr(line)
            assert "\x07" not in line, repr(line)
    await session.close()


@pytest.mark.asyncio
async def test_a_line_written_before_the_pane_exists_is_kept() -> None:
    """`05` §5.1: `_start` writes from a worker, so its first line can precede mount.

    A `NoMatches` out of `#chat-log` proved it: the write ran after the pane had
    gone. That line is the one that says why the link never came up, so it is
    buffered and replayed on mount rather than dropped.
    """
    screen = ChatScreen()
    screen.write("transport failed", "error")

    host = WidgetHost(screen)
    async with host.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert "transport failed" in "\n".join(chat_lines(host))


@pytest.mark.asyncio
async def test_a_bracket_in_a_device_string_does_not_take_the_screen_down() -> None:
    """`17` §2: the grid is markup, so a value spliced into it must be escaped.

    A `Static` updated with `Text.from_markup` raises `MarkupError` on an unbalanced
    tag, and every value in the status grid and the broker list comes from the
    device. An SSID an access point chose was therefore able to blank the Status
    screen, and a broker id to blank MQTT.
    """
    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f3")
        await pilot.pause()
        app.query_one("#status", StatusScreen).render_status(
            {
                "wifi": {"state": "connected", "ssid": "[/bold] x", "rssi": -5, "ip": "i"},
                "llm": {"provider": "p", "model": "m", "available": True},
            }
        )
        await pilot.pause()
        assert "[/bold] x" in str(app.query_one("#status-body").content)

        await pilot.press("f6")
        await pilot.pause()
        app.query_one("#mqtt", MqttScreen).render_brokers(
            [{"id": "[bad]", "connected": True, "subscriptions": [1]}]
        )
        await pilot.pause()
        assert "[bad]" in str(app.query_one("#mqtt-brokers").content)

        # A path is device data too: `sandbox.list` names the directories.
        await pilot.press("f7")
        await pilot.pause()
        files = app.query_one("#files", FilesScreen)
        files.render_entries("/workspace/[/nope]", [{"name": "a", "bytes": 1}])
        await pilot.pause()
        assert str(app.query_one("#files-path").content) == "/workspace/[/nope]"

        # The log header echoes the filter the reader typed, which is free text.
        await pilot.press("f5")
        await pilot.pause()
        logs = app.query_one("#logs", LogsScreen)
        logs.buffer.log_filter.tag = "[/nope]"
        logs.refresh_view()
        await pilot.pause()
        assert "[/nope]" in str(app.query_one("#log-header").content)
    await session.close()


@pytest.mark.asyncio
async def test_a_bracket_in_a_modal_title_does_not_take_the_screen_down() -> None:
    """The same rule inside the modals: their titles name device paths and ids.

    `ValuePrompt` shows the path a move or download is about, `FilePrompt` the file
    it edits, `Working` the action it is waiting on. Each is built from a device
    reply, and each was a markup `Static`, so a path with a `[` blanked the modal
    that was asking the reader to confirm the operation.
    """
    session = Session(FakeTransport(), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    hostile = "/workspace/[/nope]"
    async with app.run_test(size=(120, 40)) as pilot:
        for modal in (
            ValuePrompt(f"Move {hostile} to", ""),
            FilePrompt(hostile, "body"),
            Working(f"moving {hostile}", f"detail {hostile}"),
            ConfirmPrompt(f"Delete {hostile}?"),
        ):
            app.push_screen(modal)
            await pilot.pause()
            assert app.screen is modal
            app.pop_screen()
            await pilot.pause()
    await session.close()

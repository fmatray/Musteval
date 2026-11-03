"""L3 ui: the help catalogue, the composer suggester and the Esc stack.

`05-bridge.md` §5.6 and §6: the help screen is derived from one table, so a
command the parser accepts but the table omits is a red test rather than a gap
the user finds by typing it.
"""

from __future__ import annotations

import pytest

from bridge.client import Session
from bridge.commands import LOCAL_COMMANDS
from bridge.link import FakeTransport
from bridge.ui import BridgeApp
from bridge.ui.help import COMMANDS, HELP_TEXT, suggest
from bridge.ui.modals import ConfirmPrompt, SecretPrompt
from bridge.ui.render import SlashHighlighter, json_payload, render_json_block, sanitize

from .test_ui import _reply


# -- the catalogue is the parser's ---------------------------------------


def test_every_device_command_is_documented():
    documented = {command.name for command in COMMANDS if not command.local}
    for name in ("status", "reload", "reboot", "tasks", "run", "config", "secret",
                 "templates", "template", "mqtt", "geo", "fs", "retry", "ping", "cancel"):
        assert name in documented, name


def test_every_local_command_is_documented():
    documented = {command.name for command in COMMANDS if command.local}
    assert documented == set(LOCAL_COMMANDS)


def test_diag_is_a_device_command_not_a_local_one():
    """`05` §7 maps `/diag` to the `status` frame, so it costs one round trip."""
    diag = next(command for command in COMMANDS if command.name == "diag")
    assert diag.local is False
    assert "diag" not in LOCAL_COMMANDS


def test_help_text_lists_every_head():
    for command in COMMANDS:
        assert command.name in HELP_TEXT, command.name
    assert len(HELP_TEXT.splitlines()) > len(COMMANDS)


async def test_diag_requests_status_and_renders_the_counters():
    """`/diag` reuses the `status` frame but prints only the diagnostic half."""
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        app.submit("/diag")
        for _ in range(60):
            if "safe_mode" in _log(app):
                break
            await pilot.pause(0.02)
        assert any(b'"name":"status"' in line for line in transport.written)
        assert "safe_mode" in _log(app)
        await session.close()


def _log(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)


def test_help_fits_an_eighty_column_terminal():
    assert max(len(line) for line in HELP_TEXT.splitlines()) <= 78


# -- the suggester -------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("/sta", "/status"),
        ("/temp", "/template"),
        ("/config g", "/config get"),
        ("/config s", "/config set"),
        ("/mqtt p", "/mqtt publish"),
        ("/fs p", "/fs put"),
        ("/secret s", "/secret set"),
        ("/tasks de", "/tasks delete"),
        ("/tasks dis", "/tasks disable"),
    ],
)
def test_suggest_completes_a_unique_prefix(typed, expected):
    assert suggest(typed) == expected


@pytest.mark.parametrize("typed", ["", "hello", "/", "/zzz", "/t", "/status", "/status ", "/tasks "])
def test_suggest_declines_what_it_cannot_complete(typed):
    assert suggest(typed) is None


# -- modals and the Esc stack -------------------------------------------


async def _app(pilot):
    for _ in range(50):
        if pilot.app.session.state.value == "connected":
            break
        await pilot.pause(0.02)
    await pilot.pause(0.2)


async def test_escape_on_chat_cancels_the_run():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        await pilot.press("escape")
        for _ in range(50):
            if any(b'"type":"cancel"' in line for line in transport.written):
                break
            await pilot.pause(0.02)
        assert any(b'"type":"cancel"' in line for line in transport.written)
        await session.close()


async def test_escape_leaves_a_non_chat_screen():
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        app.action_show("status")
        await pilot.pause(0.1)
        assert app.query_one("#status").display is True
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app.query_one("#chat").display is True
        await session.close()


async def test_secret_modal_masks_and_never_echoes():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        app.submit("/secret set wifi")
        for _ in range(50):
            if isinstance(app.screen, SecretPrompt):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, SecretPrompt)
        # The mask is the widget's own `password=True`, not a rendering decision.
        assert app.screen.query_one("#secret-input").password is True
        await pilot.press(*"hunter2")
        await pilot.press("enter")
        for _ in range(50):
            if any(b'"type":"secret.set"' in line for line in transport.written):
                break
            await pilot.pause(0.02)
        sent = [line for line in transport.written if b'"type":"secret.set"' in line]
        assert sent and b"hunter2" in sent[0]
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "hunter2" not in text
        await session.close()


async def test_escape_cancels_the_secret_prompt_without_sending():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        app.submit("/secret set wifi")
        for _ in range(50):
            if isinstance(app.screen, SecretPrompt):
                break
            await pilot.pause(0.02)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not any(b'"type":"secret.set"' in line for line in transport.written)
        text = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
        assert "cancelled" in text
        await session.close()


async def test_delete_asks_before_it_sends():
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        app.submit("/tasks delete t1")
        for _ in range(50):
            if isinstance(app.screen, ConfirmPrompt):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, ConfirmPrompt)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not any(b"tasks.delete" in line for line in transport.written)
        await session.close()


# -- highlighting never changes the text --------------------------------


def test_a_scalar_result_stays_one_line():
    assert render_json_block("done") == "done"
    assert render_json_block(None) == "null"
    assert render_json_block({"ok": True}) == '{"ok":true}'


def test_a_long_result_is_a_json_block_not_an_escape_sequence():
    payload = {"k": "\x1b[2J" * 40}
    block = render_json_block(payload)
    assert not isinstance(block, str)
    # The escape survives into the Syntax source but is escaped by rich, so it
    # cannot clear the screen; the sanitised line form proves the intent.
    assert sanitize(json_payload(payload)) == json_payload(payload).replace("\x1b", "")


def test_the_slash_head_is_emphasised_and_placeholders_dimmed():
    highlighter = SlashHighlighter(frozenset({"config"}))
    text = highlighter("/config set <path> <value>")
    spans = {(span.start, span.end): span.style for span in text.spans}
    assert spans[(0, 7)] == "bold"
    assert spans[(12, 18)] == "dim"
    assert highlighter("plain prompt").spans == []
    assert highlighter("/unknown").spans == []

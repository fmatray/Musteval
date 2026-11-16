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
from bridge.ui.help import (
    COMMANDS,
    HELP_TEXT,
    SUBCOMMAND_HELP,
    _SUBCOMMANDS,
    options,
    suggest,
)
from bridge.ui.modals import (
    ConfirmPrompt,
    FilePrompt,
    FilterPrompt,
    HelpModal,
    InfoModal,
    SecretPrompt,
    ValuePrompt,
)
from bridge.ui.task_prompt import TaskPrompt
from bridge.ui.render import SlashHighlighter, json_payload, render_json_block, sanitize

from .test_ui import _reply


# -- the catalogue is the parser's ---------------------------------------


def test_every_device_command_is_documented():
    documented = {command.name for command in COMMANDS if not command.local}
    for name in ("sys", "tasks", "run", "config", "secret",
                 "templates", "template", "mqtt", "geo", "todo", "fs", "retry", "ping",
                 "cancel", "model"):
        assert name in documented, name


def test_every_local_command_is_documented():
    """`/tools` left this set when it became a `tools.list` read: it is a device
    command now, and the doc-lock gate below checks it is still a row."""
    documented = {command.name for command in COMMANDS if command.local}
    assert documented == set(LOCAL_COMMANDS)
    assert "tools" not in LOCAL_COMMANDS
    tools_command = next(command for command in COMMANDS if command.name == "tools")
    assert tools_command.local is False


def test_sys_is_a_device_command_covering_both_frame_shapes():
    """`05` §7 gives `/sys` every former top-level head, `diag` included, so the
    whole family is one row and none of its verbs is local."""
    sys_command = next(command for command in COMMANDS if command.name == "sys")
    assert sys_command.local is False
    assert "sys" not in LOCAL_COMMANDS
    for verb in ("status", "diag", "reload", "reboot"):
        assert verb not in LOCAL_COMMANDS


def test_help_text_lists_every_head():
    """A row is a line whose first cell is the head. Substring matching would pass on
    `/tool` because `/tools` contains it, so the row itself is what is checked."""
    heads = {
        line.split()[0]
        for line in HELP_TEXT.splitlines()
        if line.startswith("  /")
    }
    for command in COMMANDS:
        assert f"/{command.name}" in heads, command.name
    assert len(HELP_TEXT.splitlines()) > len(COMMANDS)


async def test_sys_diag_requests_status_and_shows_the_counters():
    """`05` §5.10: `/sys diag` reads the `status` frame and shows its counters in the
    info modal, not in chat."""
    transport = FakeTransport(on_write=_reply)
    session = Session(transport, ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    async with app.run_test() as pilot:
        await _app(pilot)
        app.submit("/sys diag")
        for _ in range(80):
            if isinstance(app.screen, InfoModal):
                break
            await pilot.pause(0.02)
        assert any(b'"name":"status"' in line for line in transport.written)
        assert isinstance(app.screen, InfoModal)
        body = "\n".join(str(w.content) for w in app.screen.query("Static"))
        assert "safe_mode" in body
        await session.close()


def _log(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)


def test_help_fits_an_eighty_column_terminal():
    assert max(len(line) for line in HELP_TEXT.splitlines()) <= 78


# -- the suggester -------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("/sy", "/sys"),
        ("/temp", "/template"),
        ("/config g", "/config get"),
        ("/config s", "/config set"),
        ("/mqtt p", "/mqtt publish"),
        ("/fs p", "/fs put"),
        ("/secret s", "/secret set"),
        ("/lo", "/logs"),
        ("/tasks de", "/tasks delete"),
        ("/tasks dis", "/tasks disable"),
    ],
)
def test_suggest_completes_a_unique_prefix(typed, expected):
    assert suggest(typed) == expected


@pytest.mark.parametrize("typed", ["", "hello", "/", "/zzz", "/t", "/sys", "/sys ", "/tasks "])
def test_suggest_declines_what_it_cannot_complete(typed):
    assert suggest(typed) is None


# -- the completion list -------------------------------------------------


def test_options_lists_every_command_for_a_bare_slash():
    assert [candidate.text for candidate in options("/")] == [
        f"/{command.name}" for command in COMMANDS
    ]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("/sy", ["/sys"]),
        ("/tasks ad", ["/tasks add"]),
        ("/ping r", ["/ping router"]),
        ("/ping ", ["/ping device", "/ping router", "/ping llm", "/ping ntp"]),
    ],
)
def test_options_filters_a_unique_prefix(typed, expected):
    assert [candidate.text for candidate in options(typed)] == expected


def test_sys_usage_names_every_verb():
    """`05-bridge.md` §7: the five verbs are the whole surface of `/sys`."""
    sys_command = next(command for command in COMMANDS if command.name == "sys")
    assert sys_command.usage == "/sys status|diag|reload|reboot|reset-counter"


def test_every_sys_verb_has_its_own_line():
    rows = {candidate.text: candidate.help for candidate in options("/sys ")}
    assert list(rows) == [
        "/sys status", "/sys diag", "/sys reload", "/sys reboot", "/sys reset-counter"
    ]
    assert all(rows.values()), rows


def test_ping_usage_names_every_target():
    """`05-bridge.md` §7: the four targets are the whole surface of `/ping`."""
    ping = next(command for command in COMMANDS if command.name == "ping")
    assert ping.usage == "/ping device|router|llm|ntp"


def test_a_head_with_no_subcommands_offers_nothing_after_a_space():
    """`05` §5.1: the list answers "which form does what" only where forms exist.
    Falling back to the head's summary would offer `/clear clear the pane as the
    thing to type, which is not a form the parser accepts."""
    assert options("/clear ") == []
    assert options("/clear x") == []


def test_options_keeps_an_exact_match():
    assert [candidate.text for candidate in options("/logs")] == ["/logs"]


def test_options_lists_subcommands_after_a_head_and_a_space():
    assert [candidate.text for candidate in options("/tasks ")] == [
        f"/tasks {name}" for name in _SUBCOMMANDS["tasks"]
    ]


def test_options_filters_a_partial_subcommand():
    assert [candidate.text for candidate in options("/wifi ")] == [
        "/wifi scan",
        "/wifi status",
        "/wifi connect",
        "/wifi disconnect",
        "/wifi reconnect",
    ]

    assert [candidate.text for candidate in options("/config s")] == ["/config set"]


@pytest.mark.parametrize("typed", ["hello", "/zzz", "/sys status ", "/tasks zz"])
def test_options_declines_what_it_cannot_complete(typed):
    assert options(typed) == []


def test_every_candidate_carries_the_catalogue_summary():
    """`05` §5.1: the list's help column is the help screen's own text, so a
    command cannot be documented in one place and described differently here."""
    summaries = {command.name: command.summary for command in COMMANDS}
    for candidate in options("/"):
        assert candidate.help == summaries[candidate.text[1:]]
        assert candidate.help


def test_every_subcommand_candidate_has_help_to_show():
    """A blank column would be the silent half of this feature, so it fails here
    rather than in the panel."""
    for head, subcommands in _SUBCOMMANDS.items():
        candidates = options(f"/{head} ")
        assert [candidate.text for candidate in candidates] == [
            f"/{head} {name}" for name in subcommands
        ]
        assert all(candidate.help for candidate in candidates), head


def test_every_subcommand_in_the_list_has_its_own_help_line():
    """`05` §5.1: the list says which form does what, not ten copies of the head.

    `SUBCOMMAND_HELP` is the owner of that prose. A verb added to `_SUBCOMMANDS`
    and never described here would silently show the head's summary instead, which
    reads as an answer and is not one — so this test fails on it.
    """
    for head, subcommands in _SUBCOMMANDS.items():
        described = SUBCOMMAND_HELP[head]
        assert set(described) == set(subcommands), head
        lines = {described[verb] for verb in subcommands}
        assert len(lines) == len(subcommands), f"{head} repeats a help line"


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


def test_every_modal_can_be_left_with_escape():
    """`ModalScreen` suppresses app bindings, so Esc must come from the modal.

    This is the invariant a bare `ModalScreen` subclass would silently break: the
    user is trapped and the awaiting `push_screen_wait` worker never resolves.
    """
    from bridge.ui.modals import ConfirmPrompt, PromptBox, SecretPrompt

    # Subclass `BINDINGS` replace rather than extend at class level; Textual
    # merges over the MRO at runtime, which the pilot test below observes.
    assert "escape" in {binding.key for binding in PromptBox.BINDINGS}
    for modal in (SecretPrompt, ConfirmPrompt):
        assert issubclass(modal, PromptBox)


@pytest.mark.parametrize(("screen_name", "expected"), [("secret", None), ("confirm", False)])
async def test_escape_resolves_the_awaiting_worker(screen_name, expected):
    from bridge.ui.modals import ConfirmPrompt, SecretPrompt

    modal = SecretPrompt("wifi") if screen_name == "secret" else ConfirmPrompt("delete t1?")
    session = Session(FakeTransport(on_write=_reply), ack_timeout=1, ping_interval=99)
    app = BridgeApp(session)
    result: dict[str, object] = {}
    async with app.run_test() as pilot:
        await _app(pilot)

        def wait_modal() -> None:
            async def inner() -> None:
                result["value"] = await app.push_screen_wait(modal)

            app.run_worker(inner())

        # `pop_screen` leaves this future unresolved on Textual 8.2.8, which is
        # why the modal owns the binding instead of the app.
        wait_modal()
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert result.get("value", "<hung>") == expected
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


# -- modal geometry (`05` §5.2) -----------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SecretPrompt("openai_api_key"),
        lambda: ConfirmPrompt("Delete?"),
        lambda: FilterPrompt("level"),
        lambda: ValuePrompt("key", "value"),
        lambda: FilePrompt("/workspace/a.md", "body"),
        lambda: HelpModal({name: "row" for name in ("Device",)}),
        lambda: TaskPrompt(),
    ],
)
async def test_every_modal_is_centred(factory):
    """`05` §5.2: a modal sits in the middle of the screen.

    This is a real regression: `_CSS` selected `Screen`, which does not match a
    `ModalScreen` in Textual 8.2 because the subclass carries its own
    `DEFAULT_CSS`. Every modal rendered at `(0, 0)` while the rule still read
    `align: center middle`.
    """
    from bridge.ui.testing import ModalCanvas

    app = ModalCanvas()
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(factory())
        await pilot.pause(0.2)
        box = app.screen.query_one(".prompt-box, .help-box")
        left = box.region.x
        right = 100 - (box.region.x + box.region.width)
        top = box.region.y
        bottom = 40 - (box.region.y + box.region.height)
        # A cell boundary can leave half a cell on either side; anything larger
        # means the box is not centred.
        assert abs(left - right) <= 1, (left, right)
        assert abs(top - bottom) <= 1, (top, bottom)

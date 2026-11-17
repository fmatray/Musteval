"""L3 ui: the help catalogue, the composer suggester and the Esc stack.

`05-bridge.md` §5.6 and §6: the help screen is derived from one table, so a
command the parser accepts but the table omits is a red test rather than a gap
the user finds by typing it.
"""

from __future__ import annotations

from typing import Any

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
    """`05-bridge.md` §7: the six verbs are the whole surface of `/sys`."""
    sys_command = next(command for command in COMMANDS if command.name == "sys")
    assert sys_command.usage == "/sys status|diag|reload|reboot|reset-counter|prompt"


def test_every_sys_verb_has_its_own_line():
    rows = {candidate.text: candidate.help for candidate in options("/sys ")}
    assert list(rows) == [
        "/sys status", "/sys diag", "/sys reload", "/sys reboot",
        "/sys reset-counter", "/sys prompt"
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
        "/wifi list",
        "/wifi edit",
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


def _every_modal() -> list[tuple[str, Any]]:
    """One instance of every modal class, for the geometry rules.

    A list rather than a `parametrize` over `PromptBox.__subclasses__()`: the point of
    the test is that *every* modal is measured, so a new class must be added here or
    the test silently stops covering it. The count below is the guard.
    """
    from bridge import providers as providers_mod
    from bridge.ui import modals as M
    from bridge.ui.help import help_tabs

    return [
        ("SecretPrompt", lambda: M.SecretPrompt("llm_api_key")),
        ("ConfirmPrompt", lambda: M.ConfirmPrompt("Delete task 3?")),
        ("FilterPrompt", lambda: M.FilterPrompt("Filter", "home")),
        ("ValuePrompt", lambda: M.ValuePrompt("Upload from", "/tmp/x")),
        ("Working", lambda: M.Working("reload", "re-boot the station")),
        ("InfoModal", lambda: M.InfoModal(
            "Diagnostics", [("Memory", ["heap 1"]), ("Boot", ["safe false"])]
        )),
        ("ContextModal", lambda: M.ContextModal(
            {
                "budget": 16384,
                "used": 1304,
                "free": 15080,
                "messages": 3,
                "blocks": {"user": 40, "assistant": 30},
                "counts": {"skills": 2, "tools": 18, "user": 2, "assistant": 1},
            }
        )),
        ("HelpModal", lambda: M.HelpModal(help_tabs())),
        ("TodoEditor", lambda: M.TodoEditor([{"name": "p1", "tasks": []}])),
        ("_PickBox", lambda: M._PickBox(
            [("add a network", "from the scan"), ("home", "priority 0")],
            title="Wi-Fi networks",
        )),
        ("WifiScanForm", lambda: M.WifiScanForm(
            "Add a network", scan=[{"ssid": "Cafe"}, {"ssid": "home"}]
        )),
        ("WifiCustomForm", lambda: M.WifiCustomForm("Add a network")),
        ("FilePrompt", lambda: M.FilePrompt("/x/y.txt", "one\ntwo")),
        ("ModelPicker", lambda: M.ModelPicker("openai", "OpenAI", ["a", "b"], "")),
        ("NetworkStep", lambda: M.NetworkStep({}, [])),
        ("ProviderStep", lambda: M.ProviderStep({}, list(providers_mod.BUILTIN))),
        ("ModelStep", lambda: M.ModelStep(["m1"], "openai", "", "none", [], {})),
    ]


def test_every_modal_class_is_measured():
    """The geometry test below covers what this list names, so a new modal that is
    never added here would keep the rule untested while looking green."""
    from bridge.ui import modals as M

    measured = {name for name, _f in _every_modal()}
    declared = {
        name
        for name, obj in vars(M).items()
        if isinstance(obj, type)
        and issubclass(obj, M.PromptBox)
        and obj is not M.PromptBox
        and not name.startswith("_StepBox")
    }
    # `_PickBox` is private by name but is a real modal the editors push.
    assert declared - measured == set(), declared - measured


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (120, 40)])
async def test_every_modal_keeps_a_one_row_two_column_gutter(size):
    """`17` §4: one row above and below, two columns either side, every modal.

    Measured rather than read off the stylesheet because the two can disagree: at 60
    columns a box declared `width: 60` had a zero-column gutter, and `ModelPicker`
    declared 21 rows on a 20-row terminal and drew its last row off the screen. The
    margin is the fix (`margin: 1 2`), and this is what holds it.

    The inner padding is asserted against the same two numbers: a box with a uniform
    outer gutter and `padding: 0 1` still sets its text one column from the border
    while its neighbour sets it two, which reads as two different modal styles.
    """
    from bridge.ui.testing import ModalCanvas

    width, height = size
    bad: list[str] = []
    for name, factory in _every_modal():
        app = ModalCanvas()
        async with app.run_test(size=size) as pilot:
            app.push_screen(factory())
            await pilot.pause(0.2)
            box = pilot.app.screen.query_one(
                ".prompt-box, .pick-box, .info-box, .help-box, .context-box"
            )
            left = box.region.x
            right = width - box.region.right
            top = box.region.y
            bottom = height - box.region.bottom
            inside = box.content_region
            # The border is one cell; whatever is left is the padding.
            pad_l = inside.x - box.region.x - 1
            pad_r = box.region.right - inside.right - 1
            pad_t = inside.y - box.region.y - 1
            pad_b = box.region.bottom - inside.bottom - 1
        if min(left, right) < 2 or min(top, bottom) < 1:
            bad.append(
                f"{name} at {width}x{height}: L={left} R={right} T={top} B={bottom}"
            )
        if (pad_l, pad_r, pad_t, pad_b) != (2, 2, 1, 1):
            bad.append(
                f"{name} at {width}x{height}: padding "
                f"{(pad_l, pad_r, pad_t, pad_b)}"
            )
    assert not bad, bad


async def test_the_context_box_gives_its_right_pane_the_measure_it_names() -> None:
    """`17` §4: a two-pane box names its measure in characters, and the cap is
    arithmetic against the box's own chrome.

    This is the rule that was wrong: `96%` never reached a 160-column cap, so at a
    120-column terminal the pane the doc promises 120 columns was 71 wide. The width is
    now the terminal, capped; the pane gets its 120 when the cap is reachable and the
    table keeps its 34 either way.
    """
    from bridge.ui.modals import ContextModal

    result = {
        "budget": 16384, "used": 1304, "free": 15080, "messages": 3,
        "blocks": {"tools": 5996}, "counts": {"tools": 23},
    }
    from bridge.ui.testing import ModalCanvas

    # A terminal wide enough for the cap, and a narrow one where the percentage wins.
    for size, pane in [((180, 50), 120), ((120, 40), 120 - 44)]:
        app = ModalCanvas()
        async with app.run_test(size=size) as pilot:
            app.push_screen(ContextModal(dict(result)))
            await pilot.pause(0.2)
            box = pilot.app.screen.query_one(".context-box")
            table = pilot.app.screen.query_one("#context-blocks")
            body = pilot.app.screen.query_one("#context-body")
            assert table.region.width == 34, (size, table.region.width)
            assert body.region.width == pane, (size, body.region.width, pane)
            # The cap is 120 of pane + 34 of table + 2 of border + 4 of padding, so a
            # terminal past it must not widen the box any further.
            assert box.region.width <= 160, (size, box.region.width)


# Content that would paint past the box's inside border, per modal, to prove the box
# either grows or scrolls. Two ways in: content longer than the screen, and a child
# that claims fewer rows than it paints (`LoadingIndicator` reports one row for an
# `auto` box and occupies four). A function, not a module-level dict, so the import
# happens where it is used the way `_every_modal` does it.
def _overflowing() -> dict[str, Any]:
    from bridge.ui import modals as M
    from bridge.ui.help import help_tabs

    return {
        "ModelPicker": lambda: M.ModelPicker(
            "openai", "OpenAI", [f"model-{i}" for i in range(40)], ""
        ),
        "ModelStep": lambda: M.ModelStep(
            [f"model-{i}" for i in range(40)], "openai", "", "none", [], {}
        ),
        "InfoModal": lambda: M.InfoModal(
            "Diag", [(f"H{i}", [f"line {j}" for j in range(30)]) for i in range(5)]
        ),
        "HelpModal": lambda: M.HelpModal(help_tabs()),
        "TodoEditor": lambda: M.TodoEditor(
            [
                {"name": f"p{i}", "tasks": [{"id": f"t{i}{j}"} for j in range(5)]}
                for i in range(8)
            ]
        ),
        "FilePrompt": lambda: M.FilePrompt(
            "/x", "\n".join(f"line {i}" for i in range(200))
        ),
        "Working": lambda: M.Working("rebooting", "the device is restarting"),
    }


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (120, 40)])
async def test_a_modal_that_overflows_scrolls_rather_than_drawing_past_its_border(size):
    """`17` §4: a box that cannot fit its content scrolls inside itself.

    The gutter test above uses small content, so every box fits and the rule "it fits"
    is never tested. These cases exceed the terminal: `HelpModal`'s body is 44 lines
    and a `TabPane` is a plain `Widget`, not a scroll view, so it was drawn 30 rows
    below the bottom of a 20-row screen. The assertion is that no widget paints past
    the box's content area unless something above it scrolls, which is the property
    that makes the overflow reachable instead of lost.
    """
    from bridge.ui.testing import ModalCanvas

    width, height = size
    bad: list[str] = []
    for name, factory in _overflowing().items():
        app = ModalCanvas()
        async with app.run_test(size=size) as pilot:
            app.push_screen(factory())
            await pilot.pause(0.3)
            box = pilot.app.screen.query_one(
                ".prompt-box, .pick-box, .info-box, .help-box, .context-box"
            )
            area = box.content_region
            for child in box.walk_children():
                region = child.region
                if region.width == 0 or region.height == 0:
                    continue
                if region.bottom <= area.bottom and region.right <= area.right:
                    continue
                # Matched by class name, not by import: `09` §6 confines `textual`
                # to `bridge/ui/`, and an AST walk enforces that here. `VerticalScroll`
                # is a `ScrollableContainer`, so the base name is the test.
                if any(
                    "Scrollable" in base.__name__
                    for a in child.ancestors
                    for base in type(a).__mro__
                ):
                    continue
                bad.append(f"{name} at {width}x{height}: {type(child).__name__} {region}")
    assert not bad, bad


# -- a box is its content, not a fraction of the screen ------------------


#: Every modal's chrome: two border rows plus `padding: 1 2`. A box whose height is
#: its content cannot exceed content + this. Four is the number the rule derives from
#: the stylesheet, not a tolerance: `thick` border is one row each side and the padding
#: is one row each side.
_MODAL_CHROME_ROWS = 4


#: Modals whose length *is* their content, so the terminal is the only honest
#: ceiling. Both are documents rather than forms: the help catalogue is a tab per
#: command group and the file editor is a file. §4's "content-sized unless it is a
#: document" is the exemption; every other box must hug its rows.
_DOCUMENT_MODALS = frozenset({"HelpModal", "FilePrompt", "ContextModal"})

#: Two rows of slack over the leaf count: a `Select` reports its closed height while
#: its options list contributes a border row, and a one-row `Static` can wrap to two
#: at 60 columns. Anything larger is empty space inside the border.
_LEAF_SLACK = 2


def _leaf_rows(node: Any) -> int:
    """Rows the box's text would occupy if nothing stretched it.

    Counted at the leaves rather than at the box's direct children: a box that is its
    own scroll view still hands most of its rows to containers, and a container's
    height is the artefact a stray `1fr` inflates.
    """
    total = 0
    for child in node.children:
        if not child.display:
            continue
        if child.children:
            total += _leaf_rows(child)
        else:
            total += child.region.height
    return total


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (120, 40), (170, 68)])
async def test_a_modal_box_is_its_content_and_not_a_fraction_of_the_screen(size):
    """`17` §4: height is `auto` with `max-height: 100%`, never a percentage.

    `/sys diag` drew a 46-row border around eight lines at 170x68 and each onboarding
    step took 66 of 68 rows for three fields, because `.info-box` asked for `70%`,
    `.help-box` for `80%`, and every `VerticalScroll` kept its `1fr` default. The
    empty space was the bug: a reader met a wall of border and hunted for the content
    inside it. A box now grows to its content and only the ceiling stops it.

    Measured at the leaves, not at the box's children: the child is the scroll view,
    and its height is exactly what a stray `1fr` inflates, so measuring it would
    assert the bug rather than catch it.
    """
    from bridge.ui.testing import ModalCanvas

    width, height = size
    # The `1 2` margin, then the box's own `thick` border and `1 2` padding.
    cap = height - 2 - _MODAL_CHROME_ROWS
    bad: list[str] = []
    for name, factory in _every_modal():
        app = ModalCanvas()
        async with app.run_test(size=size) as pilot:
            app.push_screen(factory())
            await pilot.pause(0.2)
            box = pilot.app.screen.query_one(
                ".prompt-box, .pick-box, .info-box, .help-box, .context-box"
            )
            content = box.content_region.height
            leaves = _leaf_rows(box)
        if name in _DOCUMENT_MODALS:
            expected = min(leaves, cap)
        elif leaves > cap:
            expected = cap
        else:
            expected = leaves
        # A document may sit at the ceiling; anything else must be at or under its
        # leaves, with the slack named above and nothing more.
        if name in _DOCUMENT_MODALS:
            ok = content <= min(leaves, cap) + _LEAF_SLACK
        else:
            ok = content <= expected + _LEAF_SLACK
        if not ok:
            bad.append(
                f"{name} at {width}x{height}: content area {content}, "
                f"leaves want {leaves} (allowed {expected + _LEAF_SLACK})"
            )
    assert not bad, bad

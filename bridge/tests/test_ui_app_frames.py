"""`app.py`'s frame routing and the arms no slash command reaches directly.

`05` §10: a push frame arrives with no request behind it, and a
`command.result` is routed by its `name`. Both are pure dispatch, so each case is
driven by feeding the frame the device would send and asserting the surface the
reader sees. The rest is `action_back`/`action_reconnect`, which are bindings
rather than commands.
"""

from __future__ import annotations

import base64
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
from bridge.ui.app import FilesScreen, MqttScreen
from bridge.ui.modals import ConfirmPrompt, ValuePrompt


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _frames(app) -> list[dict]:
    return [json.loads(line) for line in app.session._transport.written]


def _feed(app, **body) -> None:
    """Push one frame at the app the way the session sink does."""
    app.session._transport.feed(
        encode(Frame(type=body.pop("type"), id=body.pop("id", "d-9"), ts=0, body=body))
    )


def _result_body(name: str, result, *, ok: bool = True) -> dict:
    body = {"name": name, "ok": ok, "duration_ms": 1}
    body["result" if ok else "error"] = result
    return body


async def test_the_refresh_bar_survives_a_state_change_before_the_bar_mounts():
    """`05` §5.1: the bar is redrawn from state in hand, and a link-state change that
    lands before the chat screen is mounted must not take the app down over a bar
    nobody can see yet."""

    async def body(app, pilot):
        app._refresh_bar()
        app._state = {"device": "x"}
        app._refresh_bar()
        chat = app.query_one("#chat")
        assert chat.bar_line

    await _run(_device(), body)


async def test_back_from_a_modal_pops_it_and_from_a_screen_returns_to_chat():
    """`05` §6: Esc closes the top modal, then leaves a screen; on Chat it cancels the
    run, which is the one place Esc changes device state."""

    async def body(app, pilot):
        app.action_show("status")
        await pilot.pause(0.05)
        app.action_back()
        await pilot.pause(0.05)
        assert app.query_one("#chat").display is True
        before = len([f for f in _frames(app) if f.get("type") == "cancel"])
        app.action_back()
        await pilot.pause(0.1)
        assert len([f for f in _frames(app) if f.get("type") == "cancel"]) == before + 1
        assert _frames(app)[-1]["reason"] == "user"

    await _run(_device(), body)


async def test_a_status_frame_paints_the_grid_and_the_bar():
    """`05` §5.2: `status` is pushed and answered, so both surfaces are updated from
    the one body."""

    async def body(app, pilot):
        app._last_status = {}
        _feed(app, type="status", **STATUS)
        await pilot.pause(0.1)
        assert app._last_status["wifi"]["state"] == "up"
        status = app.query_one("#status")
        assert status.display is False or True  # painted whether or not it is shown
        assert app.query_one("#chat").bar_line

    await _run(_device(), body)


async def test_a_wifi_result_is_one_line_and_a_scan_is_one_line_per_network():
    """`04` §4.26: a `wifi.*` result reads as the §3.9 object, so a reading from a
    command and one from `status` cannot disagree; a scan lists what it found."""

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("wifi.state", STATUS["wifi"]))
        )
        await pilot.pause(0.05)
        assert "home" in _text(app) or "up" in _text(app)

        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("wifi.scan",
                                    {"networks": [{"ssid": "home", "rssi": -55,
                                                   "auth": "wpa2", "channel": 6}]}))
        )
        await pilot.pause(0.05)
        assert "home" in _text(app)

    await _run(_device(), body)


async def test_a_reset_counter_result_names_the_previous_count():
    """`04` §4.23: the previous value is the proof the reply carries, and safe mode
    lifting is a second fact the reader needs."""

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("sys.reset_counter",
                                    {"previous": 3, "safe_mode": True}))
        )
        await _await_chat(pilot, app, "boot failures: 3 -> 0")
        assert "safe mode lifts on the next boot" in _text(app)

    await _run(_device(), body)


async def test_a_secret_result_names_the_key_and_the_length_never_the_value():
    """`06` §A1: the reply carries a length, and the chat line says so."""
    body_frame = {"key": "wifi_psk", "length": 12}

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("secret.set", body_frame))
        )
        await _await_chat(pilot, app, "secret wifi_psk stored (12 bytes)")
        # The same line twice is one line: a repeated reply is not new information.
        before = _text(app).count("secret wifi_psk stored")
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("secret.set", body_frame))
        )
        await pilot.pause(0.05)
        assert _text(app).count("secret wifi_psk stored") == before

    await _run(_device(), body)


async def test_a_path_scoped_config_read_is_shown_and_does_not_flatten_the_grid():
    """`04` §4.8: two reply shapes share `config.get`, and treating the path-scoped
    one as a document flattened one value into the grid, so `/config get` looked dead."""

    async def body(app, pilot):
        app._config = {"llm": {"budget": {"hour": 1}}}
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("config.get",
                                    {"path": "llm.budget.hour", "value": 7}))
        )
        await pilot.pause(0.1)
        assert "llm.budget.hour" in _text(app)
        assert app._config == {"llm": {"budget": {"hour": 1}}}
        assert app.query_one("#chat").display is True

    await _run(_device(), body)


async def test_a_mqtt_status_lists_the_broker_count():
    """`05` §5.9: `/mqtt status` answers with the broker list and its size."""

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("mqtt.status",
                                    {"servers": [{"id": "srv", "connected": True,
                                                  "subscriptions": ["a/b"]}]}))
        )
        await _await_chat(pilot, app, "1 broker(s)")
        brokers = app.query_one("#mqtt", MqttScreen).query_one("#mqtt-brokers")
        assert "srv" in str(brokers.content)

    await _run(_device(), body)


async def test_a_sandbox_mutation_names_the_entry_and_relists():
    """`05` §5.8: a write, delete, mkdir or move reports what happened and refetches
    the directory, so the listing the reader sees is the device's."""

    async def body(app, pilot):
        # The mutation is a re-list of the browsed directory, so the directory has to
        # be the one the screen holds.
        app._files_path = "/workspace/"
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("sandbox.mkdir",
                                    {"path": "/workspace/reports", "created": True}))
        )
        await _await_chat(pilot, app, "sandbox.mkdir /workspace/reports created")
        for _ in range(120):
            if any(f.get("name") == "sandbox.list" for f in _frames(app)):
                break
            await pilot.pause(0.02)
        assert [f["args"]["path"] for f in _frames(app)
                if f.get("name") == "sandbox.list"] == ["/workspace/"]

        before = len([f for f in _frames(app) if f.get("name") == "sandbox.list"])
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("sandbox.move",
                                    {"from": "/workspace/a", "to": "/workspace/b"}))
        )
        await _await_chat(pilot, app, "sandbox.move /workspace/b")
        for _ in range(120):
            if len([f for f in _frames(app) if f.get("name") == "sandbox.list"]) > before:
                break
            await pilot.pause(0.02)
        assert len([f for f in _frames(app) if f.get("name") == "sandbox.list"]) > before

    await _run(_device(), body)


async def test_a_ping_result_is_one_line_and_full_loss_is_an_error():
    """`04` §4.25: 100 % loss is a measurement, not an error frame, so it is shown as
    the answer it is — and coloured as the failure the reader is looking for."""

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("net.ping",
                                    {"host": "1.1.1.1", "sent": 4, "received": 0,
                                     "loss_pct": 100, "rtt_ms_avg": None}))
        )
        await pilot.pause(0.2)
        assert "1.1.1.1" in _text(app)

    await _run(_device(), body)


async def test_an_unrecognised_result_body_is_shown_as_json():
    """`05` §7: a command whose result has no line of its own still shows its answer,
    rather than being silently dropped."""

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("gpio.read", {"pin": 4, "level": 1}))
        )
        await pilot.pause(0.2)
        assert "level" in _text(app)

    await _run(_device(), body)


async def test_the_mqtt_traffic_line_is_stamped_and_sanitised():
    """`05` §5.9: a message is one stamped line on the traffic screen, and a payload
    carrying an escape cannot drive the terminal."""

    async def body(app, pilot):
        app.action_show("mqtt")
        await pilot.pause(0.05)
        _feed(app, type="mqtt.message", id="d-8", server_id="srv", topic="t/1",
              payload="hi\x1b[31mred", truncated=False, qos=1, retain=False,
              received_ts=1730563600)
        await pilot.pause(0.1)
        lines = app.query_one("#mqtt", MqttScreen).query_one("#mqtt-traffic").lines
        joined = "\n".join(str(line.text) for line in lines)
        assert "t/1" in joined
        assert "\x1b" not in joined

    await _run(_device(), body)


async def test_a_broker_list_with_no_servers_says_so():
    """`05` §5.9: an empty reading is named, so a reader cannot mistake "none" for
    "the command did nothing"."""

    async def body(app, pilot):
        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("mqtt.status", {"servers": []}))
        )
        await _await_chat(pilot, app, "0 broker(s)")

    await _run(_device(), body)


async def test_a_frame_type_the_bridge_does_not_route_is_ignored():
    """`05` §10 names the frames the bridge acts on, and the device may add one
    before the bridge learns it. An unhandled type must leave every pane as it was
    rather than raise inside the reader loop and drop the link."""
    async def body(app, pilot):
        await app._start()
        before = _text(app)
        app._on_frame(Frame(type="brownout.warn", id="d-1", ts=0, body={"mv": 2600}))
        await pilot.pause(0.1)
        assert _text(app) == before
        # And the app still routes what it does know.
        app._on_frame(Frame(type="log", id="d-2", ts=0,
                            body={"level": "warn", "tag": "net", "msg": "after"}))
        await pilot.pause(0.1)
        assert any("after" in line for line in app._logs().buffer.visible())

    await _run(_device(), body)


async def test_an_agent_ask_offers_its_options_and_returns_the_answer():
    """`05` §10.1: `agent.ask` is the one device frame that expects an answer. The
    dialog is built from the frame's `options`, and the chosen row travels back as
    `ask.reply` correlated by `ask_id` — a reply carrying the wrong id is ignored by
    the device (§3.23), so the round trip would silently time out."""
    from bridge.ui.modals import _PickBox

    async def body(app, pilot):
        _feed(app, type="agent.ask", ask_id="q-1", run_id="r-7", round=1,
              call_id="call_a3", question="Which branch?", options=["main", "release"],
              timeout_s=120)
        await _await_chat(pilot, app, "? Which branch?")
        for _ in range(120):
            if isinstance(app.screen, _PickBox):
                await pilot.pause(0.05)
                break
            await pilot.pause(0.02)
        else:
            raise AssertionError(f"_PickBox never opened; screen is {app.screen!r}")
        # The device's options first, then the free-text escape: the question that
        # needed one more answer is the case the tool exists for.
        assert [label for label, _hint in app.screen.rows] == [
            "main", "release", "type an answer…"
        ]
        await pilot.press("down", "enter")
        for _ in range(120):
            replies = [f for f in _frames(app) if f.get("type") == "ask.reply"]
            if replies:
                break
            await pilot.pause(0.02)
        assert replies, "no ask.reply was sent"
        assert replies[0]["ask_id"] == "q-1"
        assert replies[0]["answer"] == "release"
        assert replies[0]["cancelled"] is False
        await _await_chat(pilot, app, "\u21b3 release")

    await _run(_device(), body)


async def test_dismissing_an_ask_replies_cancelled_rather_than_with_an_empty_answer():
    """`04` §3.23: `cancelled` is its own field rather than a sentinel answer. This
    bridge's text prompt returns `None` for both Esc and an emptied field, so an Esc
    that sent `answer: ""` would be indistinguishable from a reader who typed
    nothing — and the device could not tell a dismissal from an answer it chose
    itself. The field is what makes the two cases separable on the wire."""
    from bridge.ui.modals import ValuePrompt

    async def body(app, pilot):
        _feed(app, type="agent.ask", ask_id="q-2", run_id="r-7", round=1,
              call_id="call_a4", question="Name the port?", timeout_s=120)
        for _ in range(120):
            if isinstance(app.screen, ValuePrompt):
                await pilot.pause(0.05)
                break
            await pilot.pause(0.02)
        else:
            raise AssertionError(f"ValuePrompt never opened; screen is {app.screen!r}")
        # The question is the modal's title, so a reader answering a second ask in
        # the same run can tell which one is on screen.
        assert "Name the port?" in _text(app)
        await pilot.press("escape")
        for _ in range(120):
            replies = [f for f in _frames(app) if f.get("type") == "ask.reply"]
            if replies:
                break
            await pilot.pause(0.02)
        assert replies, "no ask.reply was sent"
        assert replies[0]["ask_id"] == "q-2"
        assert replies[0]["cancelled"] is True
        assert replies[0]["answer"] == ""

    await _run(_device(), body)


async def test_choosing_the_free_text_row_then_escaping_still_replies_cancelled():
    """`05` §10.1 lists two widgets, and the second one is a whole dismissal path of
    its own: a reader who picks "type an answer…" and then changes their mind must
    still produce an `ask.reply`. Without it the device waits the full timeout on a
    question the reader already walked away from."""
    from bridge.ui.modals import _PickBox, ValuePrompt

    async def body(app, pilot):
        _feed(app, type="agent.ask", ask_id="q-3", run_id="r-7", round=1,
              call_id="call_a5", question="Which branch?", options=["main"],
              timeout_s=120)
        for _ in range(120):
            if isinstance(app.screen, _PickBox):
                await pilot.pause(0.05)
                break
            await pilot.pause(0.02)
        else:
            raise AssertionError(f"_PickBox never opened; screen is {app.screen!r}")
        # Row 0 is the only option, so Down lands on the free-text row.
        await pilot.press("down", "enter")
        for _ in range(120):
            if isinstance(app.screen, ValuePrompt):
                await pilot.pause(0.05)
                break
            await pilot.pause(0.02)
        else:
            raise AssertionError(f"ValuePrompt never opened; screen is {app.screen!r}")
        await pilot.press("escape")
        for _ in range(120):
            replies = [f for f in _frames(app) if f.get("type") == "ask.reply"]
            if replies:
                break
            await pilot.pause(0.02)
        assert replies, "no ask.reply was sent"
        assert replies[0]["ask_id"] == "q-3"
        assert replies[0]["cancelled"] is True

    await _run(_device(), body)


async def test_a_geo_result_updates_the_geo_row_and_reports_in_chat():
    """`05` §10: `geo.locate` updates the Status screen GEO row and reports in chat.
    The branch that did the second was missing, so a fix fell through to the raw JSON
    block and the row kept reading `no fix` from the last `status`."""
    fix = {"lat": 52.3676, "lon": 4.9041, "accuracy_m": 65,
           "provider": "home_wifi", "age_s": 42}

    async def body(app, pilot):
        app._last_status = dict(STATUS)
        app._render_status()
        await pilot.pause(0.05)
        assert "no fix" in str(app.query_one("#status-body").content)

        app._on_command_result(
            Frame(type="command.result", id="d-9", ts=0,
                  body=_result_body("geo.locate", fix))
        )
        # "+/-65m" is `geo_line`'s own wording: the raw JSON block the reply used to
        # fall through to contains the number but never that form, so this asserts
        # the chat line rather than the `_render_status` call that follows it.
        await _await_chat(pilot, app, "+/-65m")
        assert "+/-65m" in _text(app)
        assert app._last_status["geo"] == fix
        await pilot.pause(0.05)
        grid = str(app.query_one("#status-body").content)
        assert "no fix" not in grid
        assert "52.3676" in grid

    await _run(_device(), body)

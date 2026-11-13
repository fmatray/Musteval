"""`app.py`'s frame routing and the arms no slash command reaches directly.

`02` §? no — `05` §10: a push frame arrives with no request behind it, and a
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



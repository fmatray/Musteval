"""L3 ui: the `/onboard` wizard (`05-bridge.md` §7), driven through the pilot.

What a reader sees and what reaches the device, against a scripted transport that
records every frame. The ordering rule — both `secret.set` frames before any
`config.set` naming their `nvs:` key — is asserted on the wire, not on the plan.
"""

from __future__ import annotations

import json

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge import providers
from bridge.protocol import Frame, encode, reset_ids
from bridge.ui import BridgeApp
from bridge.ui import onboard

from .test_ui import _reply
from .test_ui_input import _connected


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


class Device:
    """Answers like the shared fake, and remembers every frame in order."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def __call__(self, line: bytes) -> list[bytes]:
        frame = json.loads(line)
        self.seen.append(frame)
        return _reply(line)

    def order(self) -> list[str]:
        out = []
        for frame in self.seen:
            if frame["type"] == "secret.set":
                out.append(f"secret:{frame['key']}")
            elif frame["type"] == "command":
                out.append(f"{frame['name']}:{frame.get('args', {}).get('path', '')}")
        return out


def _app(device: Device) -> BridgeApp:
    return BridgeApp(Session(FakeTransport(on_write=device), ack_timeout=1, ping_interval=99))


async def _fill(pilot, widget: str, text: str) -> None:
    """Type `text` into one field of the grouped form (`05` §13.6).

    The form is one dialog, so a test fills fields instead of answering prompts
    in sequence; the ordering rule is asserted on the wire either way.
    """
    await pilot.pause(0.05)
    app = pilot.app
    field = app.screen.query_one(widget)
    field.focus()
    field.value = text
    await pilot.pause(0.02)


async def _submit(pilot) -> None:
    await pilot.pause(0.05)
    pilot.app.screen.query_one("#onboard-ssid").focus()
    await pilot.press("ctrl+s")


async def test_onboard_writes_both_secrets_before_the_config():
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        for widget, answer in (
            ("#onboard-ssid", "home"),
            ("#onboard-password", "hunter2"),
            ("#onboard-api-key", "sk-test"),
            ("#onboard-model", "gpt-4o-mini"),
        ):
            await _fill(pilot, widget, answer)
        await _submit(pilot)
        await pilot.pause(0.3)
        await pilot.press("y")
        await pilot.pause(0.4)
    order = device.order()
    secret_rows = [i for i, row in enumerate(order) if row.startswith("secret:")]
    config_rows = [i for i, row in enumerate(order) if row.startswith("config.set:")]
    assert len(secret_rows) == 2 and config_rows
    assert max(secret_rows) < min(config_rows), order


async def test_onboard_refuses_without_a_link():
    app = BridgeApp(
        Session(FakeTransport(on_write=lambda line: []), ack_timeout=1, ping_interval=99)
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await app._run_command("/onboard")
        await pilot.pause(0.2)
        log = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
    assert "needs a live link" in log


async def test_cancelling_the_confirm_writes_nothing():
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        for widget, answer in (
            ("#onboard-ssid", "home"),
            ("#onboard-password", "hunter2"),
            ("#onboard-api-key", "sk-test"),
            ("#onboard-model", "gpt"),
        ):
            await _fill(pilot, widget, answer)
        await _submit(pilot)
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.3)
    assert not [row for row in device.order()
                if row.startswith(("secret:", "config.set:"))]


async def test_a_half_filled_domain_says_which_pair_it_needs():
    """`03` `## Full schema` makes `password_ref` required, so an ssid alone
    writes nothing; the report must name the pair, not call the answer empty."""
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        await _fill(pilot, "#onboard-ssid", "home")
        await _submit(pilot)
        for _ in range(40):
            await pilot.pause(0.05)
        log = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
    assert "an ssid and a password" in log
    assert "every answer was empty" not in log
    # Reads (the handshake's `status`, the wizard's `wifi.scan`) and nothing else:
    # no `secret.set` and no `config.set` was sent.
    assert not [row for row in device.order()
                if row.startswith(("secret:", "config.set:"))]


# --- the planner rules (`05` §7.1), asserted without a terminal ------------

SCAN = {
    "networks": [
        {"ssid": "HomeLab", "rssi": -47, "auth": "wpa2"},
        {"ssid": "Cafe-FREE", "rssi": -71, "auth": "open"},
    ],
    "truncated": False,
}


class ScanDevice(Device):
    """The same fake, answering `wifi.scan` with a fixed list."""

    def __call__(self, line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "command" and frame["name"] == "wifi.scan":
            self.seen.append(frame)
            return [
                encode(
                    Frame(
                        type="command.result",
                        id="d-9",
                        ts=0,
                        body={"name": "wifi.scan", "ok": True, "result": SCAN,
                              "duration_ms": 1},
                        reply_to=frame["id"],
                    )
                )
            ]
        return super().__call__(line)

    def args(self, path: str) -> object:
        """The value of the last `config.set` for `path`, or None."""
        found = None
        for frame in self.seen:
            if (
                frame["type"] == "command"
                and frame["name"] == "config.set"
                and frame.get("args", {}).get("path") == path
            ):
                found = frame["args"].get("value")
        return found


def _config_value(plan, path: str):
    """The value a plan writes to `path`, or None. Several `config` rows share a kind,
    so the plan is searched rather than folded into a dict."""
    for kind, body in plan:
        if kind == "config" and path in body:
            return body[path]
    return None


def _answers(**kwargs) -> onboard.Answers:
    base = dict(ssid="", password="", base_url="", api_key="", model="")
    base.update(kwargs)
    return onboard.Answers(**base)


def test_a_scanned_network_is_written_by_the_whole_array():
    """`04` §4.9 creates a missing object but never an array, so the write is the
    whole `wifi.networks` list even for one entry."""
    answers = _answers(ssid="HomeLab", password="pw", auth="wpa2")
    plan = onboard.writes(answers)
    assert plan[0] == ("secret", {"key": onboard.WIFI_KEY, "value": "pw"})
    assert ("config", {"wifi.networks": [
        {"ssid": "HomeLab", "password_ref": f"nvs:{onboard.WIFI_KEY}"}
    ]}) in plan


def test_an_open_network_is_refused_with_a_reason():
    """`04` §4.24: the station thresholds at WPA2_PSK, so an open SSID would look
    like a successful write and leave the device offline."""
    answers = _answers(ssid="Cafe-FREE", password="pw", auth="open")
    assert not onboard.network_writable(answers)
    assert [kind for kind, _ in onboard.writes(answers)] == []


def test_a_hand_typed_ssid_carries_no_auth_and_is_written():
    """An empty `auth` is the reader asserting a name no scan returned."""
    answers = _answers(ssid="hidden-net", password="pw")
    assert onboard.network_writable(answers)


def test_a_region_writes_the_tz_and_its_pool_server():
    """One region choice, two keys: the TZ string `setenv` applies and the server."""
    tz, server = providers.timezone_for("US / Pacific")
    answers = _answers(timezone=tz, ntp_server=server)
    plan = onboard.writes(answers)
    assert ("config", {"ntp.timezone": "PST8PDT,M3.2.0,M11.1.0"}) in plan
    assert ("config", {"ntp.servers": ["0.north-america.pool.ntp.org"]}) in plan


def test_thinking_lands_in_extra_body_with_the_token_param():
    """`03` §Provider compatibility names `token_param` per provider, and the level
    travels in `extra_body` — never as a top-level key the firmware would drop."""
    entry = providers.find("groq")
    answers = _answers(
        base_url=entry.base_url,
        api_key="sk-test",
        model="openai/gpt-oss-120b",
        provider=entry.id,
        token_param=providers.TOKEN_PARAM[entry.id],
        extra_body=entry.extra_body("medium"),
    )
    provider = _config_value(onboard.writes(answers), "llm.providers")[0]
    assert provider["token_param"] == "max_completion_tokens"
    assert provider["extra_body"] == {"reasoning_effort": "medium"}
    assert provider["api_key_ref"] == f"nvs:{onboard.LLM_KEY}"


def test_a_provider_without_thinking_writes_no_extra_body():
    entry = providers.find("ollama")
    answers = _answers(
        base_url=entry.base_url, api_key="x", model="qwen2.5:7b",
        provider=entry.id, extra_body=entry.extra_body("high"),
    )
    provider = _config_value(onboard.writes(answers), "llm.providers")[0]
    assert "extra_body" not in provider


def test_narration_does_not_promise_a_write_that_will_not_be_sent():
    answers = _answers(ssid="Cafe-FREE", password="pw", auth="open")
    lines = onboard.narration(answers, {})
    assert any("cannot join open" in line for line in lines)
    assert not any("wifi.networks" in line for line in lines)


async def test_the_scan_feeds_the_ssid_dropdown():
    """The scan's list is what the reader chooses from, and the choice is what gets
    written — asserted on the wire, not on the widget."""
    device = ScanDevice()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        for _ in range(40):
            await pilot.pause(0.05)
            try:
                form = app.screen.query_one("#onboard-ssid-select")
            except Exception:
                continue
            break
        options = [value for _, value in form._options]
        assert "HomeLab" in options and "Cafe-FREE" in options

        form.value = "HomeLab"
        await _fill(pilot, "#onboard-password", "hunter2")
        await _fill(pilot, "#onboard-api-key", "sk-test")
        await _fill(pilot, "#onboard-model", "gpt")
        await _submit(pilot)
        await pilot.pause(0.3)
        await pilot.press("y")
        await pilot.pause(0.4)

    assert device.args("wifi.networks") == [
        {"ssid": "HomeLab", "password_ref": f"nvs:{onboard.WIFI_KEY}"}
    ]

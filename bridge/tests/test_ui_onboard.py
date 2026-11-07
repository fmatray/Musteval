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



async def _open_form(pilot) -> bool:
    """Push the wizard and wait for its fields. False if it never mounted."""
    pilot.app.run_worker(pilot.app.onboard())
    for _ in range(40):
        await pilot.pause(0.05)
        try:
            pilot.app.screen.query_one("#onboard-provider")
        except Exception:
            continue
        return True
    return False


async def test_choosing_a_provider_does_not_crash_when_it_takes_no_level():
    """A provider with no thinking member must not raise on selection.

    The picker is mounted for every provider and hidden for those that take no
    level, because a widget that is absent cannot be shown when the choice changes.
    This is the crash: `query` returns a `DOMQuery`, not the widget.
    """
    app = _app(Device())
    async with app.run_test() as pilot:
        await _connected(pilot)
        assert await _open_form(pilot)
        select = pilot.app.screen.query_one("#onboard-provider")

        select.value = "openrouter"
        await pilot.pause(0.15)
        assert pilot.app.screen.query_one("#onboard-thinking").display is True

        select = pilot.app.screen.query_one("#onboard-provider")
        select.value = "ollama-cloud"
        await pilot.pause(0.15)
        assert pilot.app.screen.query_one("#onboard-thinking").display is False


async def test_a_hidden_picker_contributes_no_extra_body():
    """A level chosen for one provider must not survive into a provider that takes
    none: the picker is hidden, not reset, so `answers` has to gate on the entry."""
    app = _app(Device())
    async with app.run_test() as pilot:
        await _connected(pilot)
        assert await _open_form(pilot)

        pilot.app.screen.query_one("#onboard-provider").value = "openrouter"
        await pilot.pause(0.15)
        pilot.app.screen.query_one("#onboard-thinking").value = "high"
        await pilot.pause(0.05)
        assert pilot.app.screen.answers().extra_body == {"reasoning_effort": "high"}

        pilot.app.screen.query_one("#onboard-provider").value = "ollama-cloud"
        await pilot.pause(0.15)
        assert pilot.app.screen.answers().extra_body == {}


async def _open_with(pilot, **kwargs) -> bool:
    """Push the wizard with explicit `OnboardPrompt` arguments."""
    from bridge.ui.modals import OnboardPrompt

    provider_rows = (kwargs.pop("current", {}) or {})
    pilot.app.push_screen(OnboardPrompt(provider_rows, **kwargs))
    for _ in range(60):
        await pilot.pause(0.05)
        try:
            pilot.app.screen.query_one("#onboard-model-select")
        except Exception:
            continue
        return True
    return False


async def _stub_fetch(names: list[str]):
    """A fetch callback standing in for the provider's own endpoint."""

    async def fetch(entry, api_key: str) -> list[str]:
        fetch.calls.append((entry.id, api_key))
        return list(names)

    fetch.calls = []
    return fetch


async def test_the_fetched_list_is_selected_not_just_offered():
    """A list in the dropdown with nothing selected is an empty answer.

    `Select.set_options` resets the selection, so the value has to be set again
    afterwards. The symptom when it is not: a model list is visible, the reader
    accepts it, and `answers()` still reads no model, which drops the whole
    `llm.providers` write.
    """
    fetch = await _stub_fetch(["m-one", "m-two"])
    app = BridgeApp(
        Session(FakeTransport(on_write=Device()), ack_timeout=1, ping_interval=99)
    )
    async with app.run_test() as pilot:
        assert await _open_with(pilot, providers=providers.catalog()[0], fetch_models=fetch)
        pilot.app.screen.query_one("#onboard-provider").value = "openrouter"
        for _ in range(40):
            await pilot.pause(0.05)
            if pilot.app.screen.query_one("#onboard-model-select").value == "m-one":
                break
        select = pilot.app.screen.query_one("#onboard-model-select")
        answers = pilot.app.screen.answers()

    assert select.value == "m-one"
    assert answers.model == "m-one"
    assert fetch.calls == [("openrouter", "")]


async def test_the_list_loads_when_the_form_opens():
    """The configured provider's list is there before the reader touches anything.

    They may accept every pre-filled value and press `Ctrl+S`, so a blank dropdown
    is not a field waiting to be filled — it is a write that will be skipped.
    """
    fetch = await _stub_fetch(["m-one", "m-two"])
    app = BridgeApp(
        Session(FakeTransport(on_write=Device()), ack_timeout=1, ping_interval=99)
    )
    current = {
        "llm": {
            "providers": [
                {"id": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                 "model": "m-two", "api_key_ref": "nvs:llm_api_key"}
            ]
        }
    }
    async with app.run_test() as pilot:
        assert await _open_with(
            pilot, current=current, providers=providers.catalog()[0], fetch_models=fetch
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if fetch.calls:
                break
        select = pilot.app.screen.query_one("#onboard-model-select")

    assert fetch.calls == [("openrouter", "")]
    assert select.value == "m-two", "the configured model is kept when still offered"


async def test_typing_a_key_refetches_the_list():
    """Most providers answer 401 without a key, so the list has to follow the key."""
    fetch = await _stub_fetch(["m-one"])
    app = BridgeApp(
        Session(FakeTransport(on_write=Device()), ack_timeout=1, ping_interval=99)
    )
    async with app.run_test() as pilot:
        assert await _open_with(pilot, providers=providers.catalog()[0], fetch_models=fetch)
        pilot.app.screen.query_one("#onboard-provider").value = "openrouter"
        await pilot.pause(0.1)
        before = len(fetch.calls)
        await _fill(pilot, "#onboard-api-key", "sk-test")
        for _ in range(40):
            await pilot.pause(0.05)
            if len(fetch.calls) > before:
                break

    assert fetch.calls[-1] == ("openrouter", "sk-test")


def test_a_started_provider_without_a_model_says_it_will_skip():
    """A half-filled LLM domain must narrate the skip, not do nothing quietly."""
    answers = onboard.Answers(
        ssid="", password="", base_url="https://openrouter.ai/api/v1",
        api_key="sk-test", model="", provider="openrouter",
    )
    assert onboard.writes(answers) == [("secret", {"key": onboard.LLM_KEY, "value": "sk-test"})]
    assert any("skips llm.providers" in line for line in onboard.narration(answers, {}))


async def test_a_flattened_config_still_prefills_the_form():
    """`app.py` holds the config flattened, so a nested read found nothing.

    Every field opened empty and the provider was blank, which is what left the model
    list unloaded. The form has to read the shape the app actually holds, or the
    pre-fill it promises never happens.
    """
    from bridge.ui.settings import flatten

    nested = {
        "wifi": {"networks": [{"ssid": "HomeLab", "password_ref": "nvs:wifi_psk"}]},
        "llm": {"providers": [{"id": "openrouter",
                               "base_url": "https://openrouter.ai/api/v1",
                               "model": "m-one", "api_key_ref": "nvs:llm_api_key"}]},
        "ntp": {"timezone": "CET-1CEST,M3.5.0,M10.5.0/3"},
    }
    fetch = await _stub_fetch(["m-one", "m-two"])
    app = BridgeApp(
        Session(FakeTransport(on_write=Device()), ack_timeout=1, ping_interval=99)
    )
    async with app.run_test() as pilot:
        assert await _open_with(
            pilot, current=flatten(nested), providers=providers.catalog()[0],
            fetch_models=fetch,
        )
        for _ in range(40):
            await pilot.pause(0.05)
            if fetch.calls:
                break
        screen = pilot.app.screen
        provider = screen.query_one("#onboard-provider")
        ssid = screen.query_one("#onboard-ssid-select")
        base_url = screen.query_one("#onboard-base-url")
        answers = screen.answers()

    assert provider.value == "openrouter"
    assert ssid.value == "HomeLab"
    assert base_url.value == "https://openrouter.ai/api/v1"
    assert answers.model == "m-one"
    assert fetch.calls == [("openrouter", "")]

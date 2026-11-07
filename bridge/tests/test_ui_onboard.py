"""L3 ui: the four `/onboard` steps (`05-bridge.md` §7), driven through the pilot.

The wizard is three screens plus a time zone, and each step is tested before the
next is asked for. What that means for these tests: a step's assertions are about
the frames it sends and the line its own test produced, not about a plan built from
all four. The ordering rule that survived the split — a `secret.set` frame precedes
any `config.set` naming its `nvs:` key — is asserted on the wire.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge import providers
from bridge.protocol import reset_ids
from bridge.ui import BridgeApp
from bridge.ui import onboard
from bridge.ui.modals import ModelStep, NetworkStep, ProviderStep

from .test_ui import _reply


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


class Device:
    """Answers like the shared fake, and remembers every frame in order."""

    def __init__(self, status: dict | None = None) -> None:
        self.seen: list[dict] = []
        self.status = status or {
            "wifi": {"state": "connected", "ssid": "HomeLab", "rssi": -50, "ip": "1.2.3.4"},
            "time": {"synced": True, "unix": 1730563200, "tz": "UTC0"},
        }

    def __call__(self, line: bytes) -> list[bytes]:
        frame = json.loads(line)
        self.seen.append(frame)
        if frame.get("name") == "status":
            result = {"name": "status", "ok": True, "result": self.status,
                      "duration_ms": 0}
            return [json.dumps({"v": 1, "type": "command.result", "id": "d-9", "ts": 0,
                                "reply_to": frame["id"], **result}).encode() + b"\n"]
        return _reply(line)

    def writes(self) -> list[dict]:
        """Only the frames that change the device, ignoring the handshake and probes."""
        return [f for f in self.seen if f["type"] in ("secret.set",)
                or f.get("name") in ("config.set", "reload")]

    def order(self) -> list[str]:
        out = []
        for frame in self.seen:
            if frame["type"] == "secret.set":
                out.append(f"secret:{frame['key']}")
            elif frame["type"] == "command":
                out.append(f"{frame['name']}:{frame.get('args', {}).get('path', '')}")
        return out


def _app(device: Device) -> BridgeApp:
    return BridgeApp(
        Session(FakeTransport(on_write=device), ack_timeout=1, ping_interval=99)
    )


async def _mounted(pilot, selector: str) -> bool:
    """Wait for a widget to be composed and mounted, not merely present."""
    for _ in range(80):
        await pilot.pause(0.05)
        try:
            pilot.app.screen.query_one(selector).query_one("#label")
        except Exception:
            continue
        return True
    return False


def _answer(**kwargs):
    """`Answers` with every field named, so a new field cannot silently default."""
    base = {
        "ssid": "", "password": "", "base_url": "", "api_key": "", "model": "",
        "skip_network": False, "skip_provider": False, "skip_model": False,
    }
    base.update(kwargs)
    return onboard.Answers(**base)


# --- the pass option ------------------------------------------------------


def test_a_passed_network_step_writes_neither_the_array_nor_the_zone():
    """Ctrl+G means "keep what the device has", so nothing is sent for it."""
    a = _answer(ssid="HomeLab", password="pw", auth="wpa2",
                timezone="CET-1CEST,M3.5.0,M10.5.0/3",
                ntp_server="0.europe.pool.ntp.org", skip_network=True)
    assert onboard.writes_network(a) == []
    assert onboard.writes_time(a) == []


def test_a_passed_provider_step_keeps_the_stored_provider_and_writes_no_secret():
    a = _answer(base_url="https://a/v1", api_key="k", model="m",
                provider="openrouter", skip_provider=True)
    plan = onboard.writes_provider(a)
    assert [kind for kind, _ in plan] == ["config"], "no key was typed, none is written"
    assert plan[0][1]["llm.providers"] == [{
        "id": "openrouter", "base_url": "https://a/v1", "model": "m",
        "api_key_ref": "nvs:llm_api_key",
    }]


def test_a_passed_model_step_writes_nothing_at_all():
    """The provider is already written with its model; a rewrite would be a no-op."""
    a = _answer(base_url="https://a/v1", api_key="k", model="m", skip_model=True)
    assert onboard.writes_provider(a) == []


def test_every_step_passed_writes_nothing():
    a = _answer(ssid="HomeLab", password="pw", auth="wpa2", base_url="https://a/v1",
                api_key="k", model="m", timezone="UTC0",
                skip_network=True, skip_provider=True, skip_model=True)
    assert onboard.writes(a) == []


# --- the pure plan --------------------------------------------------------


def test_a_network_writes_its_secret_before_the_array_naming_it():
    """`03-config.md` rule 9: a `nvs:` reference whose key is missing is rejected."""
    plan = onboard.writes_network(
        _answer(ssid="HomeLab", password="pw", auth="wpa2")
    )
    kinds = [kind for kind, _ in plan]
    assert kinds == ["secret", "config"]
    assert plan[0][1] == {"key": "wifi_psk", "value": "pw"}
    assert plan[1][1] == {
        "wifi.networks": [{"ssid": "HomeLab", "password_ref": "nvs:wifi_psk"}]
    }


def test_an_open_network_is_refused_rather_than_written():
    """`04` §4.24: the station thresholds at WPA2, so the join would fail silently."""
    assert onboard.writes_network(_answer(ssid="Cafe", password="x", auth="open")) == []


def test_a_hand_typed_ssid_carries_no_auth_and_is_written():
    plan = onboard.writes_network(_answer(ssid="Hidden", password="pw", auth=""))
    assert plan[1][1]["wifi.networks"][0]["ssid"] == "Hidden"


def test_a_provider_writes_its_key_before_the_array_naming_it():
    plan = onboard.writes_provider(
        _answer(base_url="https://x/v1", api_key="sk", model="m", provider="custom")
    )
    assert [kind for kind, _ in plan] == ["secret", "config"]
    assert plan[1][1]["llm.providers"][0]["model"] == "m"


def test_a_provider_without_a_model_writes_nothing():
    """`03-config.md` requires `model`; there is no half-configured provider."""
    assert onboard.writes_provider(_answer(base_url="u", api_key="k", model="")) == []


def test_thinking_lands_in_extra_body_with_the_token_param():
    a = _answer(base_url="https://api.groq.com/openai/v1", api_key="k", model="m",
                provider="groq", extra_body={"reasoning_effort": "medium"})
    provider = onboard.writes_provider(a)[1][1]["llm.providers"][0]
    assert provider["extra_body"] == {"reasoning_effort": "medium"}
    assert providers.TOKEN_PARAM["groq"] == "max_completion_tokens"


def test_a_region_writes_the_tz_and_its_pool_server():
    plan = onboard.writes_time(
        _answer(timezone="CET-1CEST,M3.5.0,M10.5.0/3",
                ntp_server="0.europe.pool.ntp.org")
    )
    assert plan == [
        ("config", {"ntp.timezone": "CET-1CEST,M3.5.0,M10.5.0/3"}),
        ("config", {"ntp.servers": ["0.europe.pool.ntp.org"]}),
    ]


def test_a_zone_naming_no_region_writes_no_server():
    """Guessing a pool would replace a working server with a European one."""
    plan = onboard.writes_time(_answer(timezone="XYZ-5", ntp_server=""))
    assert plan == [("config", {"ntp.timezone": "XYZ-5"})]


def test_narration_names_one_step_at_a_time():
    """The zone rides with the network, because one `reload` applies both."""
    a = _answer(ssid="HomeLab", password="pw", auth="wpa2", model="m",
                base_url="u", api_key="k", timezone="UTC0")
    assert onboard.narration(a, {}, "network") == [
        "creates wifi.networks (1 entry)", "stores nvs:wifi_psk",
        "sets ntp.timezone UTC0",
    ]
    assert onboard.narration(a, {}, "provider") == [
        "creates llm.providers (1 entry)", "provider model m", "stores nvs:llm_api_key"
    ]


def test_narration_does_not_promise_a_write_that_will_not_be_sent():
    """An open network is narrated as skipped, never as created or replaced."""
    lines = onboard.narration(_answer(ssid="Cafe", password="x", auth="open"), {},
                              "network")
    assert any("skips Cafe" in line for line in lines), lines
    assert not any("creates" in line or "replaces" in line or "stores" in line
                   for line in lines), lines


# --- the verdicts ---------------------------------------------------------


def test_a_joined_network_is_reported_from_status_not_from_the_write():
    """`config.set` ok means the document changed, not that the station joined."""
    assert onboard.verdict_network(
        {"wifi": {"state": "connected", "ssid": "HomeLab"}}
    ) == "network: joined HomeLab"


def test_a_failed_join_says_which_two_things_to_check():
    said = onboard.verdict_network({"wifi": {"state": "failed"}})
    assert "passphrase" in said and "WPA2" in said


def test_a_provider_that_publishes_no_list_is_named_as_such():
    a = _answer(base_url="https://ollama.com/v1", api_key="k", provider="ollama-cloud")
    assert "publishes no model list" in onboard.verdict_provider([], a)


def test_a_refused_key_is_distinguished_from_an_unreachable_host():
    a = _answer(base_url="https://openrouter.ai/api/v1", api_key="k",
                provider="openrouter")
    said = onboard.verdict_provider([], a)
    assert "key is refused or the host is unreachable" in said


def test_a_model_absent_from_the_list_is_reported_before_the_write():
    a = _answer(base_url="u", api_key="k", model="gpt-9")
    said = onboard.verdict_model(["gpt-4o-mini", "gpt-4o"], a)
    assert "is not in the provider's list of 2" in said


def test_a_zone_the_device_still_holds_old_is_reported():
    said = onboard.verdict_time(
        {"time": {"tz": "UTC0", "synced": True}}, "CET-1CEST,M3.5.0,M10.5.0/3"
    )
    assert "did not apply" in said


def test_the_zone_the_device_holds_is_reported_as_applied():
    said = onboard.verdict_time(
        {"time": {"tz": "CET-1CEST,M3.5.0,M10.5.0/3", "synced": False}},
        "CET-1CEST,M3.5.0,M10.5.0/3",
    )
    assert "applied" in said and "not synced" in said


# --- the screens ----------------------------------------------------------


async def test_the_network_step_offers_the_scan_and_keeps_the_configured_ssid():
    app = _app(Device())
    scan = [{"ssid": "StoneBox", "rssi": -53, "auth": "wpa2"},
            {"ssid": "HomeLab", "rssi": -70, "auth": "wpa2"}]
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({"wifi.networks": [{"ssid": "HomeLab"}]}, scan))
        assert await _mounted(pilot, "#step-ssid-select")
        select = app.screen.query_one("#step-ssid-select")
        values = [value for _, value in select._options]
        answered = app.screen.answer()
    assert values == ["StoneBox", "HomeLab"]
    assert answered.ssid == "HomeLab", "the configured ssid wins over the scan order"


async def test_a_scanned_network_arrives_with_its_auth_word():
    app = _app(Device())
    scan = [{"ssid": "StoneBox", "rssi": -53, "auth": "wpa2"}]
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({}, scan))
        assert await _mounted(pilot, "#step-ssid-select")
        app.screen.query_one("#step-password").value = "pw"
        answered = app.screen.answer()
    assert answered.ssid == "StoneBox" and answered.auth == "wpa2"


async def test_choosing_a_provider_fills_the_base_url_and_keeps_an_unknown_id():
    """`id` is free text, so a device configured elsewhere holds one no row names.

    A `Select` whose value is absent from its options raises on mount, which made the
    wizard unusable on a device that already had a provider.
    """
    entries, _ = providers.catalog()
    app = _app(Device())
    current = {"llm.providers": [{"id": "p1", "base_url": "https://a/v1",
                                 "model": "m"}]}
    async with app.run_test() as pilot:
        app.push_screen(ProviderStep(current, entries))
        assert await _mounted(pilot, "#step-provider")
        select = app.screen.query_one("#step-provider")
        ids = [row[1] for row in select._options]
        assert select.value == "p1" and "p1" in ids
        assert app.screen.query_one("#step-base-url").value == "https://a/v1"
        select.value = "openrouter"
        await pilot.pause(0.3)
        assert app.screen.query_one("#step-base-url").value.endswith("/api/v1")
        answered = app.screen.answer()
    assert answered.provider == "openrouter"


async def test_the_model_step_offers_the_fetched_list_and_selects_the_first():
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(ModelStep(models=["a/one", "b/two"], provider="openrouter"))
        assert await _mounted(pilot, "#step-model-select")
        select = app.screen.query_one("#step-model-select")
        answered = app.screen.answer()
    assert select.value == "a/one"
    assert answered["model"] == "a/one"


async def test_the_model_step_hides_thinking_where_the_provider_takes_none():
    """`05` §7.1: a level the provider rejects turns every call into a 400."""
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(ModelStep(models=["gpt-4o"], provider="openai"))
        assert await _mounted(pilot, "#step-model-select")
        assert not app.screen.query("#step-thinking")
        answered = app.screen.answer()
    assert answered["thinking"] == "none"


async def test_the_network_step_translates_the_stored_zone_back_to_a_region():
    """`CET-1CEST,...` is what `ntp.timezone` holds; the dropdown holds place names."""
    app = _app(Device())
    tz = "CET-1CEST,M3.5.0,M10.5.0/3"
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({"ntp": {"timezone": tz}}, []))
        assert await _mounted(pilot, "#step-region")
        select = app.screen.query_one("#step-region")
        values = [value for _, value in select._options]
        answered = app.screen.answer()
    assert select.value == "Europe / Western"
    assert tz not in values, "the raw TZ string is not a row"
    assert (answered.timezone, answered.ntp_server) == providers.timezone_for(
        "Europe / Western"
    )


async def test_the_network_step_offers_a_pass_only_when_both_halves_are_stored():
    """Ctrl+G must not walk a reader past a domain the device has not got."""
    app = _app(Device())
    configured = {
        "wifi.networks": [{"ssid": "HomeLab", "password_ref": "nvs:wifi_psk"}],
        "ntp": {"timezone": "UTC0"},
    }
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep(configured, []))
        assert await _mounted(pilot, "#step-region")
        assert app.screen.can_pass()
        assert "Ctrl+G keeps both" in str(app.screen.query_one("#step-pass").render())


async def test_the_network_step_offers_no_pass_without_a_stored_network():
    """The zone alone is half the step; passing would leave the station unwritten."""
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({"ntp": {"timezone": "UTC0"}}, []))
        assert await _mounted(pilot, "#step-region")
        assert not app.screen.can_pass(), "no stored network: nothing to keep"
        assert "Ctrl+G" not in str(app.screen.query_one("#step-pass").render())


async def test_the_provider_step_passes_only_over_a_stored_provider():
    entries, _ = providers.catalog()
    app = _app(Device())
    stored = {"llm.providers": [{"id": "p1", "base_url": "https://a/v1",
                                 "model": "m"}]}
    async with app.run_test() as pilot:
        app.push_screen(ProviderStep(stored, entries))
        assert await _mounted(pilot, "#step-provider")
        assert app.screen.can_pass()
        assert "Ctrl+G keeps it" in str(app.screen.query_one("#step-pass").render())


async def test_the_provider_step_offers_no_pass_without_a_stored_provider():
    entries, _ = providers.catalog()
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(ProviderStep({}, entries))
        assert await _mounted(pilot, "#step-provider")
        assert not app.screen.can_pass()
        assert "Ctrl+G" not in str(app.screen.query_one("#step-pass").render())


async def test_pressing_pass_marks_the_step_and_sends_nothing():
    app = _app(Device())
    configured = {
        "wifi.networks": [{"ssid": "HomeLab", "password_ref": "nvs:wifi_psk"}],
        "ntp": {"timezone": "UTC0"},
    }
    box: list[object] = []
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep(configured, []), box.append)
        assert await _mounted(pilot, "#step-region")
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
    assert box and isinstance(box[0], onboard.Answers), box
    answered = box[0]
    assert answered.skip_network is True
    assert onboard.writes_network(answered) == []


async def test_a_stored_zone_the_table_lacks_keeps_its_own_row():
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({"ntp": {"timezone": "XYZ-5"}}, []))
        assert await _mounted(pilot, "#step-region")
        select = app.screen.query_one("#step-region")
        answered = app.screen.answer()
    assert select.value == "XYZ-5"
    assert (answered.timezone, answered.ntp_server) == ("XYZ-5", ""), \
        "no pool is guessed for an unknown zone"


# --- the wizard, end to end over the wire ---------------------------------


async def test_the_wizard_tests_each_step_before_asking_the_next():
    """The order is the dependency order, and each test is the next step's unlock.

    Driven through `onboard()` with the three screens answered by name, so the wire
    records what the app actually sends and in what order.
    """
    device = Device()
    app = _app(device)
    seen: list[str] = []

    answers = {
        "ssid": "HomeLab", "password": "pw", "auth": "wpa2",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "sk",
        "model": "a/one", "provider": "openrouter",
        "timezone": "UTC0", "ntp_server": "",
    }

    async def fake_screen(screen):
        name = type(screen).__name__
        seen.append(name)
        if isinstance(screen, NetworkStep):
            return _answer(ssid="HomeLab", password="pw", auth="wpa2",
                           timezone="UTC0")
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1", api_key="sk",
                           provider="openrouter")
        if isinstance(screen, ModelStep):
            return {"model": "a/one", "thinking": "none"}
        return None

    async def fake_fetch(a):
        return ["a/one", "b/two"]

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._fetch_models = fake_fetch  # type: ignore[method-assign]
        app._config = {}
        app._config_fetched = True
        await app.onboard()

    order = device.order()
    assert "config.set:wifi.networks" in order, order
    # A reload follows the network write, before any provider frame.
    network_at = order.index("config.set:wifi.networks")
    reload_at = order.index("reload:")
    provider_at = order.index("config.set:llm.providers")
    assert network_at < reload_at < provider_at, order
    # The zone is written in step 1 and applied by the same reload.
    assert order.index("config.set:ntp.timezone") < reload_at, order
    # And a status read follows the reload, which is step 1's test.
    assert "status:" in order[reload_at:provider_at], order
    assert seen == ["NetworkStep", "ProviderStep", "ModelStep"]


async def test_passing_every_step_sends_only_the_document_read():
    """The reported use: re-run the wizard to inspect one domain, change none.

    A pass must not reload either. `reload` is step 1's test, and a test whose write
    did not happen would report on a document nobody touched.
    """
    device = Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(ssid="HomeLab", password="pw", auth="wpa2",
                           timezone="UTC0", skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1", provider="openrouter",
                           skip_provider=True)
        if isinstance(screen, ModelStep):
            return {"model": "", "thinking": "none", "skip_model": True}
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._config = {}
        app._config_fetched = True
        await app.onboard()

    assert device.writes() == [], device.writes()
    assert "reload:" not in device.order(), "a passed step has no write to apply"


async def test_cancelling_the_network_step_writes_nothing():
    device = Device()
    app = _app(device)

    async def fake_screen(screen):
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._config = {}
        app._config_fetched = True
        await app.onboard()

    assert device.writes() == [], "a cancelled first step writes nothing"


async def test_onboard_refuses_without_a_link():
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.session._set_state(app.session.state.__class__.DOWN)
        await app.onboard()
    assert device.writes() == [], "nothing is written to a link that cannot answer"


async def test_the_wizard_prefills_from_a_config_it_fetched_itself():
    """`_config` arrives only by a `config.get` reply, and only settings asked.

    A fresh process opened the wizard on `{}`: every field looked unconfigured on a
    device that had one, the provider was blank, and the model list never loaded.
    """
    stored = {"llm": {"providers": [{"id": "p1", "base_url": "https://a/v1",
                                     "model": "m"}]}}

    def device(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") == "config.get" and not frame.get("args"):
            body = {"name": "config.get", "ok": True, "result": {"config": stored},
                    "duration_ms": 0}
            return [json.dumps({"v": 1, "type": "command.result", "id": "d-1", "ts": 0,
                                "reply_to": frame["id"], **body}).encode() + b"\n"]
        return _reply(line)

    app = BridgeApp(Session(FakeTransport(on_write=device), ack_timeout=1,
                            ping_interval=99))
    seen: list[dict] = []

    async def capture(screen):
        seen.append(dict(screen.current))
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app._config_fetched is False
        app.push_screen_wait = capture  # type: ignore[method-assign]
        await app.onboard()
        visible = [name for name in ("chat", "status", "tasks", "logs", "settings",
                                     "mqtt", "files")
                   if app.query_one(f"#{name}").display]

    assert seen, "the form was opened"
    assert seen[0].get("llm.providers"), "the config arrived before the form was shown"
    assert visible == ["chat"], "the wizard is a modal, not a detour through settings"

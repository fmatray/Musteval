"""L3 ui: the four `/onboard` steps (`05-bridge.md` §7), driven through the pilot.

The wizard is three screens plus a time zone, and each step is tested before the
next is asked for. What that means for these tests: a step's assertions are about
the frames it sends and the line its own test produced, not about a plan built from
all four. The ordering rule that survived the split — a `secret.set` frame precedes
any `config.set` naming its `nvs:` key — is asserted on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge import providers
from bridge.protocol import reset_ids
from bridge.ui import BridgeApp
from bridge.ui import onboard
from bridge.ui.app import _http_fetch
from bridge.ui.modals import ModelPicker, ModelStep, NetworkStep, ProviderStep
from bridge.ui.testing import ModalHost, chat_lines

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
    """`custom` is the only built-in with no `models_url`; `ollama-cloud` serves an
    OpenAI-shaped `/v1/models` and is listed from it (`05` §7.1)."""
    a = _answer(base_url="https://example.invalid/v1", api_key="k", provider="custom")
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


# --- the branches a complete answer never reaches -------------------------


def test_a_model_without_a_base_url_writes_no_provider():
    """`03` rule 9: an entry with no `base_url` is a document the device refuses."""
    a = _answer(base_url="", api_key="k", model="m", provider="custom")
    assert onboard.writes_provider(a) == []


def test_a_token_param_is_written_only_when_the_reader_supplied_one():
    """`03` §Provider compatibility names `token_param` per provider, so an absent
    one stays absent: writing a member the provider rejects is a 400 on every call."""
    a = _answer(base_url="u", api_key="k", model="m", provider="custom", token_param="")
    entry = onboard.writes_provider(a)[1][1]["llm.providers"][0]
    assert "token_param" not in entry


def test_a_stored_provider_with_no_id_is_reported_rather_than_guessed():
    _frames, note = onboard.thinking_writes("high", [{"base_url": "u"}])
    assert note == "the stored provider has no id; run /onboard first"


def test_a_stored_provider_outside_the_catalog_is_reported_by_name():
    _frames, note = onboard.thinking_writes("high", [{"id": "retired"}])
    assert note == "provider 'retired' is not in this bridge's catalog"
    assert onboard.thinking_line("high", [{"id": "retired"}]) == (
        "thinking: retired is not in this bridge's catalog"
    )


def test_a_provider_with_no_thinking_levels_says_so_both_ways():
    _frames, note = onboard.thinking_writes("high", [{"id": "custom"}])
    assert note == "custom takes no thinking level; nothing to set"
    assert onboard.thinking_line("none", [{"id": "custom"}]) == (
        "thinking: custom takes no thinking level"
    )


def test_turning_thinking_off_writes_the_level_rather_than_deleting_the_member():
    """`05` §7.2: a stale explicit level must not survive a reader turning thinking
    off, so `none` is written, not dropped."""
    frames, note = onboard.thinking_writes(
        "none", [{"id": "openai", "extra_body": {"reasoning_effort": "high"}}]
    )
    assert note == ""
    assert frames[0][1]["llm.providers"][0]["extra_body"] == {"reasoning_effort": "none"}


def test_status_without_a_wifi_block_is_named_as_missing():
    """A missing block is not "off": the device answered, just not about the radio."""
    assert onboard.verdict_network({"wifi": "unknown"}) == (
        "network: the device did not report a wifi block"
    )


def test_a_connecting_radio_is_reported_with_the_ssid_it_is_trying():
    assert onboard.verdict_network(
        {"wifi": {"state": "connecting", "ssid": "Cafe"}}
    ) == "network: still connecting to Cafe"
    assert onboard.verdict_network({"wifi": {"state": "connecting"}}) == (
        "network: still connecting to the configured ssid"
    )


def test_a_provider_verdict_without_a_base_url_or_a_key_names_the_missing_one():
    assert onboard.verdict_provider([], _answer(base_url="")) == "provider: no base url"
    assert onboard.verdict_provider([], _answer(base_url="u", api_key="")) == (
        "provider: no key typed, so the list cannot be checked"
    )


def test_the_narration_names_a_replacement_when_a_chain_already_exists():
    a = _answer(base_url="u", api_key="k", model="m", provider="custom")
    lines = onboard.narration(
        a, {"llm.providers": [{"id": "openai"}, {"id": "groq"}]}, "provider"
    )
    assert "replaces llm.providers (2 entries → 1)" in lines


def test_the_narration_names_a_server_replacement_when_servers_exist():
    a = _answer(timezone="UTC0", ntp_server="pool.ntp.org")
    lines = onboard.narration(a, {"ntp": {"servers": ["a", "b"]}}, "network")
    assert "replaces ntp.servers (2 → 1)" in lines


def test_a_time_verdict_without_a_block_or_a_zone_is_named():
    assert onboard.verdict_time({"time": "unknown"}, "UTC0") == (
        "time: the device did not report a time block"
    )
    assert onboard.verdict_time({"time": {"tz": None}}, "UTC0") == (
        "time: the device reports no zone"
    )


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
    """`05` §7.1: a level the provider rejects turns every call into a 400.

    Corrected when the OpenAI row gained its levels: that surface does take
    `reasoning_effort`, so `openai` is no longer an example of a row with no member.
    Anthropic's compatibility layer ignores the member, which is.
    """
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(ModelStep(models=["claude-sonnet-4-6"], provider="anthropic"))
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


async def test_a_fully_stored_network_step_says_the_pass_keeps_both_halves():
    """`can_pass` still distinguishes a satisfied step; it no longer gates the key."""
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


async def test_the_network_step_passes_even_with_half_the_step_stored():
    """The key must always work; the line says what passing would leave unset.

    A silent refusal was the reported bug: the reader pressed Ctrl+G, the screen
    did nothing, and nothing on it explained why. The cost is stated instead.
    """
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({"ntp": {"timezone": "UTC0"}}, []))
        assert await _mounted(pilot, "#step-region")
        assert not app.screen.can_pass(), "no stored network: nothing to keep"
        note = str(app.screen.query_one("#step-pass").render())
    assert "no stored network" in note and "unconfigured" in note, note


async def test_the_network_pass_note_names_the_half_that_is_missing():
    """A configured network with no zone must not be reported as no network."""
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep(
            {"wifi.networks": [{"ssid": "HomeLab", "password_ref": "nvs:wifi_psk"}]}, []
        ))
        assert await _mounted(pilot, "#step-region")
        note = str(app.screen.query_one("#step-pass").render())
    assert "zone" in note and "network" not in note.split("no stored ")[1], note


async def test_the_network_pass_note_names_both_when_both_are_absent():
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({}, []))
        assert await _mounted(pilot, "#step-region")
        note = str(app.screen.query_one("#step-pass").render())
    assert "nothing stored" in note and "unconfigured" in note, note


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


async def test_the_provider_step_passes_without_a_stored_provider():
    """Same rule as the network step: the key works, the line names the cost."""
    entries, _ = providers.catalog()
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(ProviderStep({}, entries))
        assert await _mounted(pilot, "#step-provider")
        assert not app.screen.can_pass()
        note = str(app.screen.query_one("#step-pass").render())
    assert "nothing stored" in note and "unconfigured" in note, note


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


async def test_pressing_pass_on_an_unconfigured_step_dismisses_it():
    """The reported bug, at the key: Ctrl+G inside the focused ssid Input.

    A screen binding is reached by bubbling from the focused widget, so this asserts
    the key path rather than the action in isolation.
    """
    app = _app(Device())
    box: list[object] = []
    async with app.run_test() as pilot:
        app.push_screen(NetworkStep({}, []), box.append)
        assert await _mounted(pilot, "#step-region")
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
    assert box, "Ctrl+G must dismiss an unconfigured step, not ignore it"
    assert isinstance(box[0], onboard.Answers) and box[0].skip_network is True


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


async def test_passing_an_unconfigured_step_still_continues():
    """The reported bug: Ctrl+G on an unconfigured step was a silent no-op.

    The key must reach the next step; what it leaves unset is the step line's job to
    say, not the key's job to enforce.
    """
    device = Device()
    app = _app(device)
    seen: list[str] = []

    async def fake_screen(screen):
        seen.append(type(screen).__name__)
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(skip_provider=True)
        if isinstance(screen, ModelStep):
            return {"model": "", "thinking": "none", "skip_model": True}
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._config = {}
        app._config_fetched = True
        await app.onboard()

    assert seen == ["NetworkStep", "ProviderStep", "ModelStep"], seen
    assert device.writes() == []


async def test_the_progress_screen_names_the_action_in_flight():
    """A step's test is a round trip; the reader is told which one is running."""
    from bridge.ui.modals import Working

    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(Working("reloading the device", "re-running the boot sequence"))
        for _ in range(40):
            await pilot.pause(0.05)
            if app.screen.query("#working-action"):
                break
        title = str(app.screen.query_one("#working-action").render())
        detail = str(app.screen.query_one("#working-detail").render())
        assert isinstance(app.screen, Working)
    assert title == "reloading the device"
    assert "boot sequence" in detail


async def test_the_progress_screen_has_no_escape():
    """A cancel here would dismiss the screen its caller is still awaiting."""
    from bridge.ui.modals import Working

    assert "escape" not in {b.key for b in Working.BINDINGS}


async def test_the_reload_shows_a_progress_screen_while_it_waits():
    """`reload` blocks on net_connect; without the screen it looks hung.

    Records every screen pushed while the reload is in flight, so a missing
    progress screen fails here rather than being excused by an `or`.
    """
    from bridge.ui.modals import Working

    device = Device()
    app = _app(device)
    pushed: list[str] = []
    real_push = app.push_screen

    def recording_push(screen, *args, **kwargs):
        pushed.append(type(screen).__name__)
        return real_push(screen, *args, **kwargs)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(ssid="HomeLab", password="pw", auth="wpa2",
                           timezone="UTC0", skip_network=False)
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app.push_screen = recording_push  # type: ignore[method-assign]
        app._config = {"wifi.networks": [], "wifi.connect_timeout_ms": 5000}
        app._config_fetched = True
        await app.onboard()
        # Checked while the app is still running: after `run_test` returns there is
        # no screen stack to ask.
        assert pushed.count("Working") >= 2, pushed
        assert not isinstance(app.screen, Working), "the screen is popped after the wait"

    assert "reload:" in device.order(), device.order()


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


def test_a_level_write_is_the_whole_array_and_keeps_other_members():
    """`05` §7.2: a level change is not a reconfiguration.

    `04` §4.9 will not create an index, so the path is the array; and the array form
    is what preserves the entry's other members, including `extra_body` members the
    catalog does not model.
    """
    chain = [
        {
            "id": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "m",
            "api_key_ref": "nvs:llm_api_key",
            "extra_body": {"reasoning_effort": "low", "seed": 7},
        },
        {"id": "openrouter", "base_url": "u", "model": "b", "api_key_ref": "nvs:k"},
    ]
    plan, why = onboard.thinking_writes("high", chain)
    assert why == ""
    assert [kind for kind, _ in plan] == ["config"]
    written = plan[0][1]["llm.providers"]
    assert written[0]["extra_body"] == {"seed": 7, "reasoning_effort": "high"}
    assert written[0]["model"] == "m"
    # The rest of the chain is untouched: the chain is a fallback list, not one entry.
    assert written[1] == chain[1]


def test_a_provider_with_no_off_switch_refuses_none_rather_than_approximating():
    """Corrected when the `thinking_budget` strategy was removed. The old test drove
    `anthropic` as a budget provider; that surface ignores `reasoning_effort` and its
    thinking member is nested, which `03-config.md`'s flat-scalar rule forbids, so the
    row now takes no level at all.

    The rule the test existed for is `05` §7.2's: where a provider cannot say "off",
    `none` is refused rather than approximated, because Ollama resolves an unsupported
    name to the model's *default*, and several models default to their highest effort.
    """
    plan, why = onboard.thinking_writes(
        "none",
        [{"id": "ollama-cloud", "extra_body": {"seed": 7}, "model": "m"}],
    )
    assert plan is None
    assert "not offered" in why and "low" in why and "high" in why

    # The row with neither an off switch nor an on switch refuses too, rather than
    # writing a member its provider ignores.
    plan, why = onboard.thinking_writes("none", [{"id": "anthropic", "model": "m"}])
    assert plan is None
    assert "takes no thinking level" in why

    # And where the surface does take "off", the member is written rather than the
    # entry's `extra_body` being dropped: a stale level must not survive. `openai`
    # offers all five; `groq` is the row that does not offer `none`.
    plan, why = onboard.thinking_writes(
        "none", [{"id": "openai", "model": "m"}]
    )
    assert why == ""
    assert plan[0][1]["llm.providers"][0]["extra_body"] == {
        "reasoning_effort": "none"
    }


def test_a_level_the_provider_does_not_take_is_refused_on_the_host():
    """The device accepts any flat member and turns every call into a 400, so the
    refusal has to happen where it can name the provider (`05` §7.2)."""
    # `anthropic`, not `openai`: corrected when the OpenAI row gained its levels —
    # that surface takes `reasoning_effort`, so it is no longer the example of a row
    # with nothing to set. Anthropic's compatibility layer ignores the member, which
    # is a row that genuinely takes no level.
    plan, why = onboard.thinking_writes("high", [{"id": "anthropic", "model": "m"}])
    assert plan is None
    assert "takes no thinking level" in why
    plan, why = onboard.thinking_writes("maximal", [{"id": "groq", "model": "m"}])
    assert plan is None
    assert "not offered" in why and "high" in why
    # Not "the provider does not take it": Ollama's `max` is a real level on some of
    # its models and absent from the row, and the reader is owed that distinction.
    plan, why = onboard.thinking_writes("max", [{"id": "ollama-cloud", "model": "m"}])
    assert plan is None
    assert "not offered" in why and "takes" not in why
    plan, why = onboard.thinking_writes("high", [])
    assert plan is None and "/onboard" in why


async def test_the_model_step_is_handed_the_stored_level():
    """`05` §13.6: re-running the wizard must not be a silent downgrade.

    The assertion is on the step the app *built*, not on the answer a fake screen
    returned: a fake that answers `high` would pass whatever the app seeded.
    """
    device = Device()
    app = _app(device)
    seen: list[ModelStep] = []

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(ssid="HomeLab", password="pw", auth="wpa2",
                           timezone="UTC0", skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://api.groq.com/openai/v1",
                           provider="groq", api_key="k")
        if isinstance(screen, ModelStep):
            seen.append(screen)
        return None

    async def fake_fetch(_answers):
        return ["llama-3.3-70b"]

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._fetch_models = fake_fetch  # type: ignore[method-assign]
        app._config = {
            "llm.providers": [
                {
                    "id": "groq",
                    "base_url": "https://api.groq.com/openai/v1",
                    "model": "llama-3.3-70b",
                    "api_key_ref": "nvs:llm_api_key",
                    "extra_body": {"reasoning_effort": "high"},
                }
            ]
        }
        app._config_fetched = True
        await app.onboard()

    assert len(seen) == 1
    assert seen[0].thinking == "high"


async def test_the_model_step_offers_a_host_file_providers_levels():
    """Reading `BUILTIN` in the step made a custom entry look like a provider with no
    thinking member, so its levels were never offered."""
    custom = providers.Provider(
        id="local-thinker",
        label="Local",
        base_url="http://localhost:8000/v1",
        thinking=providers.THINKING_REASONING_EFFORT,
    )
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(models=["m"], provider="local-thinker", model="m", entries=[custom])
        )
        assert await _mounted(pilot, "#step-thinking")


async def test_the_model_step_offers_a_level_for_ollama_cloud():
    """`03-config.md` §Provider compatibility records `reasoning_effort` for both
    Ollama rows, so the step must show the picker for the entry `/onboard` offers.
    The row used to be `THINKING_NONE` and the picker was absent (`05` §7.1)."""
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(
                models=["gemma4:31b", "gpt-oss:120b"],
                provider="ollama-cloud",
                model="gemma4:31b",
                thinking="high",
                entries=list(providers.BUILTIN),
            )
        )
        assert await _mounted(pilot, "#step-thinking")


#: A slice of the real `GET https://ollama.com/v1/models` body, recorded 2026-11-07.
_OLLAMA_LIST = (
    b'{"object":"list","data":[{"id":"gemma4:31b","object":"model"},'
    b'{"id":"gpt-oss:120b","object":"model"}]}'
)


async def test_the_wizard_fetches_ollama_cloud_models_without_a_key(monkeypatch):
    """`05` §7.1: the cloud list is public, so step 2 offers names before the key is
    proved. The fetch is asserted on the URL step 2 asks for, not on a fake's answer.

    Spied on `models_with_efforts`, which is the fetch the step now makes: it reads the
    same list and keeps each model's published efforts for the level picker.
    """
    asked: list[tuple[str, str]] = []
    app = _app(Device())
    original = providers.models_with_efforts

    def spy(entry, key, fetch=None):
        def recording(url, k):
            asked.append((url, k))
            return _OLLAMA_LIST

        return original(entry, key, recording)

    async with app.run_test():
        monkeypatch.setattr(providers, "models_with_efforts", spy)
        answers = _answer(provider="ollama-cloud", api_key="", base_url="https://ollama.com/v1")
        names = await app._fetch_models(answers)
        assert names == ["gemma4:31b", "gpt-oss:120b"]
        assert asked == [("https://ollama.com/v1/models", "")]
        assert answers.model_efforts == {"gemma4:31b": (), "gpt-oss:120b": ()}


def test_the_fetch_refuses_a_url_that_is_not_http():
    """`_http_fetch` is the one socket the bridge opens, and `urllib` reads `file://`
    for real, so the scheme is pinned before the request is built."""
    for url in ("file:///etc/passwd", "ftp://127.0.0.1/x", "/etc/passwd", ""):
        with pytest.raises(ValueError, match="refusing a non-http scheme"):
            _http_fetch(url, "")


async def test_a_stored_level_the_row_does_not_offer_survives_a_re_run():
    """`05` §13.6: a re-run must not be a silent downgrade.

    Ollama names its levels per model, so `max` is a real stored setting that the
    `ollama-cloud` row does not list. The step used to fall back to its first row
    (`low`) and `answers.extra_body` is rebuilt from the picker, so accepting step 3
    wrote `low` over `max`.
    """
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(
                models=["glm-5.3-flash"],
                provider="ollama-cloud",
                model="glm-5.3-flash",
                thinking="max",
                entries=list(providers.BUILTIN),
            )
        )
        assert await _mounted(pilot, "#step-thinking")
        picker = app.screen.query_one("#step-thinking")
        assert picker.value == "max"
        assert app.screen.answer()["thinking"] == "max"


def test_an_absent_member_on_an_ollama_entry_reads_as_the_default_not_none():
    """Ollama's surface has no off switch, so an absent `reasoning_effort` means the
    model thinks at its own default — `glm-5.3-flash` defaults to `max`. Reporting
    `none` would claim thinking was off when nothing was off (`05` §7.2)."""
    chain = [{"id": "ollama-cloud", "model": "glm-5.3-flash"}]
    said = onboard.thinking_line("none", chain)
    assert "the provider's default" in said
    assert "none" not in said.split("(")[0]
    assert "low" in said and "high" in said

    # A stored level is still reported as itself, even one the row does not offer.
    assert onboard.thinking_line("none", [{"id": "ollama-cloud",
                                           "extra_body": {"reasoning_effort": "max"}}]
                                 ).startswith("thinking: max")


async def test_the_levels_narrow_to_the_model_that_publishes_them():
    """`05` §7.1: levels are a property of the model. OpenRouter publishes each
    model's own `supported_efforts`, so a model that takes only `low`/`high` must not
    be offered `xhigh` — the provider would answer 400 on the first prompt."""
    app = _app(Device())
    efforts = {"openai/gpt-narrow": ("low", "high"),
               "openai/gpt-wide": ("low", "medium", "high", "xhigh")}
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(
                models=["openai/gpt-narrow", "openai/gpt-wide"],
                provider="openrouter",
                model="openai/gpt-narrow",
                entries=list(providers.BUILTIN),
                efforts=efforts,
            )
        )
        assert await _mounted(pilot, "#step-thinking")
        picker = app.screen.query_one("#step-thinking")
        assert [value for _, value in picker._options] == ["low", "high"]

        # Choosing the wider model re-narrows to its own set.
        app.screen.query_one("#step-model-select").value = "openai/gpt-wide"
        await pilot.pause(0.1)
        picker = app.screen.query_one("#step-thinking")
        assert [value for _, value in picker._options] == [
            "low", "medium", "high", "xhigh"
        ]


async def test_a_model_step_without_published_efforts_offers_the_row_set():
    """Every provider but OpenRouter says nothing about a model's levels, so the
    row's set is the answer rather than an empty picker."""
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(
                models=["gemma4:31b"],
                provider="ollama-cloud",
                model="gemma4:31b",
                entries=list(providers.BUILTIN),
            )
        )
        assert await _mounted(pilot, "#step-thinking")
        picker = app.screen.query_one("#step-thinking")
        assert [value for _, value in picker._options] == ["low", "high"]


async def test_the_manual_model_field_completes_from_the_fetched_list():
    """`05` §7.1: the list can hold hundreds of names, so the field completes from it
    instead of making the reader scroll a menu. A completion, not a filter: a name the
    list lacks stays submittable."""
    app = _app(Device())
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(
                models=["anthropic/claude-sonnet-4-6", "openai/gpt-wide"],
                provider="openrouter",
                model="",
                entries=list(providers.BUILTIN),
            )
        )
        await pilot.pause(0.1)
        field = app.screen.query_one("#step-model")
        assert await field.suggester.get_suggestion("anth") == "anthropic/claude-sonnet-4-6"
        # A name the list does not hold is not invented into one.
        assert await field.suggester.get_suggestion("zzz") is None


async def test_a_typed_model_name_also_narrows_the_picker():
    """The typed field wins in `answer()`, so a name typed instead of picked must
    narrow the same way — otherwise the picker offers a level from the previous
    model while the new name is the one written to the device."""
    app = _app(Device())
    efforts = {"a/narrow": ("low", "high"), "b/wide": ("low", "high", "xhigh")}
    async with app.run_test() as pilot:
        app.push_screen(
            ModelStep(
                models=["a/narrow", "b/wide"],
                provider="openrouter",
                model="a/narrow",
                entries=list(providers.BUILTIN),
                efforts=efforts,
            )
        )
        assert await _mounted(pilot, "#step-thinking")
        app.screen.query_one("#step-model").value = "b/wide"
        await pilot.pause(0.1)
        picker = app.screen.query_one("#step-thinking")
        assert [value for _, value in picker._options] == ["low", "high", "xhigh"]
        assert app.screen.answer()["model"] == "b/wide"


# -- `/model` (`05` §7.3) --------------------------------------------------


def test_provider_entry_names_both_refusals_from_one_place():
    """`/model`'s picker and its writer refuse for the same two reasons, so the
    wording lives in one function: a reader must never see two vocabularies."""
    assert onboard.provider_entry(None) == (
        None,
        "no provider is configured; run /onboard first",
    )
    assert onboard.provider_entry([]) == (
        None,
        "no provider is configured; run /onboard first",
    )
    assert onboard.provider_entry(["not an object"]) == (
        None,
        "the stored provider entry is not an object; run /onboard first",
    )
    entry = {"id": "ollama", "model": "gemma4:cloud"}
    assert onboard.provider_entry([entry, {"id": "second"}]) == (entry, "")


def test_model_writes_keeps_every_other_member_of_the_entry():
    """`05` §7.3: a model change is not a reconfiguration.

    The whole `llm.providers` array is written because `04` §4.9 will not create a
    missing index, so anything this function drops from the entry is lost on the
    device. `base_url`, `api_key_ref`, `token_param` and an `extra_body` the catalog
    does not model must all survive; a test that only checked `model` would pass
    while the device went dark.
    """
    chain = [
        {
            "id": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "old",
            "api_key_ref": "nvs:llm_api_key",
            "token_param": "max_completion_tokens",
            "timeout_ms": 45000,
            "extra_body": {"reasoning_effort": "low", "seed": 7},
        },
        {"id": "openrouter", "base_url": "u", "model": "b", "api_key_ref": "nvs:k"},
    ]
    plan, why = onboard.model_writes("new", chain)
    assert why == ""
    assert [kind for kind, _ in plan] == ["config"]
    written = plan[0][1]["llm.providers"]
    assert written[0] == {**chain[0], "model": "new"}
    # The chain is a fallback list: the second entry is not this entry's business.
    assert written[1] == chain[1]


def test_model_writes_trims_the_name_it_is_given():
    """A trailing space is a name the device would store verbatim and then fail to
    resolve against the provider, which is a 404 on every later prompt."""
    plan, why = onboard.model_writes("  llama3  ", [{"id": "ollama", "model": "m"}])
    assert why == ""
    assert plan[0][1]["llm.providers"][0]["model"] == "llama3"


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_model_writes_refuses_a_blank_name_on_the_host(name):
    """`03-config.md` requires 1..64 characters, so a blank write is a document the
    device rejects; refusing here costs no round trip and can name the fix."""
    plan, why = onboard.model_writes(name, [{"id": "ollama", "model": "m"}])
    assert plan is None
    assert "/model" in why


def test_model_writes_refuses_without_a_provider():
    plan, why = onboard.model_writes("m", [])
    assert plan is None
    assert "/onboard" in why

    plan, why = onboard.model_writes("m", None)
    assert plan is None
    assert "/onboard" in why


def test_model_writes_refuses_an_entry_that_is_not_an_object():
    """`config.get` can answer anything; a scalar first entry is not a provider, and
    `dict()` on it would raise instead of reporting."""
    plan, why = onboard.model_writes("m", ["not-an-entry"])
    assert plan is None
    assert "not an object" in why


# -- the model picker itself (`05` §7.3) -----------------------------------
#
# On `ModalHost`, not `BridgeApp`: the picker's contract is the value it dismisses
# with, and `BridgeApp` would drag the whole session into a test of one widget.


class _PickerApp(ModalHost):
    def __init__(self, models: list[str], current: str = "", provider: str = "ollama") -> None:
        super().__init__(ModelPicker(provider, provider, models, current))


@contextlib.asynccontextmanager
async def _picker(models: list[str], current: str = "", provider: str = "ollama"):
    app = _PickerApp(models, current, provider)
    async with app.run_test(size=(100, 40)) as pilot:
        for _ in range(60):
            if app.screen is app.modal:
                break
            await pilot.pause(0.02)
        assert app.screen is app.modal
        yield pilot, app


async def test_the_model_picker_takes_a_typed_name_over_the_selection():
    """`05` §7.3: the field sits beside the list, so a name the provider does not
    publish is still one keystroke away. The device's own entry is that case —
    `gemma4:cloud` is absent from the cloud list, so a selection-only picker would
    move the reader off a name that works.
    """
    async with _picker(["kimi-k2.6", "glm-5.3"], "gemma4:cloud") as (pilot, app):
        assert app.modal.query_one("#step-model").value == "gemma4:cloud"
        app.modal.query_one("#step-model").value = "typed-model"
        await pilot.pause()
        app.modal.action_submit()
        await pilot.pause()
    assert app.dismissed["value"] == "typed-model"


async def test_the_model_picker_returns_the_selection_when_the_field_is_blank():
    async with _picker(["kimi-k2.6", "glm-5.3"]) as (pilot, app):
        app.modal.query_one("#step-model-select").value = "glm-5.3"
        await pilot.pause()
        app.modal.query_one("#step-model").value = ""
        app.modal.action_submit()
        await pilot.pause()
    assert app.dismissed["value"] == "glm-5.3"


async def test_the_model_picker_cancels_to_none():
    async with _picker(["a"], "a") as (pilot, app):
        app.modal.action_cancel()
        await pilot.pause()
    assert app.dismissed["value"] is None


async def test_the_model_picker_is_just_a_field_when_there_is_no_list():
    """The common case, not the edge: measured keyless, only `openrouter` and
    `ollama` publish `/models`; OpenAI, Mistral and Anthropic answer 401 and Groq
    403, so for most providers the field is the whole picker and the stored name
    must already be in it."""
    async with _picker([], "gpt-4o-mini", provider="openai") as (pilot, app):
        assert not app.modal.query("#step-model-select")
        assert app.modal.query_one("#step-model").value == "gpt-4o-mini"
        app.modal.action_submit()
        await pilot.pause()
    assert app.dismissed["value"] == "gpt-4o-mini"


# -- `/model` through the app (`05` §7.3) ----------------------------------


class _ProviderDevice(Device):
    """A device that holds one provider entry and answers `config.get` for it."""

    def __init__(self, entry: dict) -> None:
        super().__init__()
        self.entry = dict(entry)

    def __call__(self, line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") == "config.get":
            self.seen.append(frame)
            body = {"name": "config.get", "ok": True,
                    "result": {"config": {"llm": {"providers": [dict(self.entry)]}}},
                    "duration_ms": 0}
            return [json.dumps({"v": 1, "type": "command.result", "id": "d-9", "ts": 0,
                                "reply_to": frame["id"], **body}).encode() + b"\n"]
        if frame.get("name") == "config.set":
            self.seen.append(frame)
            value = frame["args"]["value"]
            self.entry = dict(value[0])
            body = {"name": "config.set", "ok": True, "result": {}, "duration_ms": 0}
            return [json.dumps({"v": 1, "type": "command.result", "id": "d-9", "ts": 0,
                                "reply_to": frame["id"], **body}).encode() + b"\n"]
        return super().__call__(line)


async def test_slash_model_with_a_name_writes_only_that_member():
    """The no-fetch route, and the one a reader uses for a model the provider does
    not publish — the device's own `gemma4:cloud` is exactly that. The whole entry is
    written back, so `base_url` and `api_key_ref` must survive verbatim."""
    device = _ProviderDevice({
        "id": "ollama", "base_url": "https://ollama.com/v1", "model": "old",
        "api_key_ref": "nvs:llm_api_key", "timeout_ms": 120000,
    })
    app = _app(device)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config_quiet = True
        await app._model(["gemma4:cloud"])
        await pilot.pause()
    written = [f for f in device.seen if f.get("name") == "config.set"][-1]
    entry = written["args"]["value"][0]
    assert entry["model"] == "gemma4:cloud"
    assert entry["base_url"] == "https://ollama.com/v1"
    assert entry["api_key_ref"] == "nvs:llm_api_key"
    assert entry["timeout_ms"] == 120000


async def test_slash_model_refuses_a_blank_name_before_any_frame():
    device = _ProviderDevice({"id": "ollama", "base_url": "https://ollama.com/v1",
                              "model": "old", "api_key_ref": "nvs:k"})
    app = _app(device)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config_quiet = True
        app.action_show("chat")
        await pilot.pause(0.1)
        before = len(device.seen)
        await app._model(["   "])
        await pilot.pause()
        assert not [f for f in device.seen[before:] if f.get("name") == "config.set"]
        assert any("model:" in line for line in chat_lines(app))


async def test_slash_model_refuses_when_no_provider_is_stored():
    """An empty chain is the fresh-device state; `/onboard` is the fix, so the line
    must name it rather than open a picker over nothing."""
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config_quiet = True
        app._config = {}
        app._config_fetched = True
        app.action_show("chat")
        await pilot.pause(0.1)
        await app._model([])
        await pilot.pause()
        lines = chat_lines(app)
    assert any("run /onboard" in line for line in lines), lines


async def test_the_model_picker_lists_the_stored_name_when_the_provider_omits_it():
    """The board's own state: `gemma4:cloud` is stored while the cloud list carries
    23 other names. If the list omitted it, the highlight would sit on a model the
    reader never chose and clearing the field would adopt it."""
    async with _picker(["kimi-k2.6", "glm-5.3"], "gemma4:cloud") as (pilot, app):
        select = app.modal.query_one("#step-model-select")
        offered = [value for _, value in select._options]
        assert offered[0] == "gemma4:cloud"
        assert select.value == "gemma4:cloud"
        # The published names are still all there.
        assert {"kimi-k2.6", "glm-5.3"} <= set(offered)
        app.modal.action_submit()
        await pilot.pause()
    assert app.dismissed["value"] == "gemma4:cloud"

"""`app.py`'s `/onboard` branches: the ones a happy path never reaches.

`05` §7 makes each step's test the unlock for the next, so every branch here is a
step that did not unlock: a cancelled screen, a step passed with Ctrl+G, a refusal
from the device. The screens are stubbed with `push_screen_wait` — the suite's
existing pattern — because the modal's own behaviour is `modals.py`'s test, and what
is under test here is the decision `app.py` takes on its answer.
"""

from __future__ import annotations

import json

import pytest

from bridge import providers
from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import reset_ids
from bridge.ui import BridgeApp
from bridge.ui import onboard
from bridge.ui.modals import ModelStep, NetworkStep, ProviderStep
from bridge.ui.testing import chat_lines

from .test_ui import _reply


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


class _Device:
    """Answers `status` from a fixed body and records what was written."""

    def __init__(self, status: dict | None = None) -> None:
        self.seen: list[dict] = []
        self.status = status if status is not None else {
            "wifi": {"state": "connected", "ssid": "HomeLab", "rssi": -50,
                     "ip": "1.2.3.4"},
            "time": {"synced": True, "unix": 1730563200, "tz": "UTC0"},
        }

    def __call__(self, line: bytes) -> list[bytes]:
        import json

        frame = json.loads(line)
        self.seen.append(frame)
        if frame.get("name") == "status":
            return [json.dumps({
                "v": 1, "type": "command.result", "id": "d-9", "ts": 0,
                "reply_to": frame["id"], "name": "status", "ok": True,
                "result": self.status, "duration_ms": 0,
            }).encode() + b"\n"]
        return _reply(line)

    def writes(self) -> list[dict]:
        return [f for f in self.seen
                if f["type"] == "secret.set"
                or f.get("name") in ("config.set", "reload")]


def _app(device) -> BridgeApp:
    return BridgeApp(Session(FakeTransport(on_write=device), ack_timeout=1,
                             ping_interval=99))


def _answer(**kwargs):
    base = {"ssid": "", "password": "", "base_url": "", "api_key": "", "model": "",
            "skip_network": False, "skip_provider": False, "skip_model": False}
    base.update(kwargs)
    return onboard.Answers(**base)


async def test_onboard_refuses_at_once_on_a_link_that_cannot_answer():
    """`05` §7: the refusal names the bar's word, so the reader is not left waiting
    for a reply from a link that is down."""
    from bridge.client.session import LinkState

    device = _Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.session._state = LinkState.DOWN
        before = len(device.writes())
        await app.onboard()
        text = "\n".join(chat_lines(app))
        assert "not sent" in text
        assert len(device.writes()) == before
        app.session._state = LinkState.CONNECTED


async def test_a_config_that_was_never_fetched_is_read_before_the_form():
    """`05` §7.1: the form pre-fills from the device's document, so a fresh process
    reads it first — quietly, so the wizard stays a modal over the current screen."""

    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(skip_provider=True)
        if isinstance(screen, ModelStep):
            return {"model": "", "thinking": "none", "skip_model": True}
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config_fetched = False
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        # One read, and no jump to the settings grid.
        reads = [f for f in device.seen if f.get("name") == "config.get"]
        assert len(reads) == 1
        assert app._config_fetched is True
        assert app.query_one("#chat").display is True


async def test_a_cancelled_network_step_stops_the_wizard():
    """`05` §7: Esc on a step ends the wizard, and nothing after it is asked for."""
    device = _Device()
    app = _app(device)
    seen: list[str] = []

    async def fake_screen(screen):
        seen.append(type(screen).__name__)
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        assert seen == ["NetworkStep"]
        assert "cancelled at the network step" in "\n".join(chat_lines(app))
        assert device.writes() == []


async def test_a_cancelled_provider_step_stops_the_wizard():
    device = _Device()
    app = _app(device)
    seen: list[str] = []

    async def fake_screen(screen):
        seen.append(type(screen).__name__)
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        assert seen == ["NetworkStep", "ProviderStep"]
        assert "cancelled at the provider step" in "\n".join(chat_lines(app))


async def test_a_cancelled_model_step_writes_no_provider():
    """`05` §7.3: `03` makes `model` required, so a provider written before the model
    exists is a document the device refuses — hence no write on cancel."""
    device = _Device()
    app = _app(device)
    seen: list[str] = []

    async def fake_screen(screen):
        seen.append(type(screen).__name__)
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1",
                           provider="openrouter", skip_provider=True)
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        assert seen == ["NetworkStep", "ProviderStep", "ModelStep"]
        assert "cancelled at the model step" in "\n".join(chat_lines(app))
        assert [f for f in device.writes()
                if f.get("name") == "config.set"] == []


async def test_a_passed_provider_keeps_the_stored_one_and_fetches_nothing():
    """`05` §7.2: a pass has no key to fetch with, so no socket is opened and the
    step claims nothing."""
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1",
                           provider="openrouter", skip_provider=True)
        if isinstance(screen, ModelStep):
            return {"model": "", "thinking": "none", "skip_model": True}
        return None

    async def no_fetch(answers):
        raise AssertionError("a passed provider must not be fetched")

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._fetch_models = no_fetch  # type: ignore[method-assign]
        await app.onboard()
        assert "provider kept as stored" in "\n".join(chat_lines(app))


async def test_a_network_that_cannot_be_written_is_not_written_and_not_reloaded():
    """`05` §7.1: an empty plan means nothing to reload and nothing to test, so the
    line reports what the device already says rather than claiming a test that never
    ran."""
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            # No ssid and no zone: both halves of the plan are empty.
            return _answer(ssid="", password="", auth="wpa2", timezone="")
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        assert [f for f in device.writes() if f.get("name") == "reload"] == []
        assert [f for f in device.writes() if f.get("name") == "config.set"] == []
        assert "safe mode" in "\n".join(chat_lines(app))


async def test_a_refused_network_write_stops_before_the_reload():
    """`05` §7.1: a refused write is not reloaded, because the document did not
    change and the test would report on nothing."""
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(ssid="NewNet", password="pw", auth="wpa2",
                           timezone="UTC0")
        return None

    async def refuse(kind, body, timeout=None):
        return False, "E_NVS"

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._send_body = refuse  # type: ignore[method-assign]
        await app.onboard()
        assert [f for f in device.seen if f.get("name") == "reload"] == []
        assert "refused (E_NVS)" in "\n".join(chat_lines(app))


async def test_a_reload_that_does_not_answer_stops_the_wizard():
    """`05` §7.1: the reload is the step's test. Without an answer there is no
    verdict to print, so the wizard stops rather than guessing."""
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(ssid="NewNet", password="pw", auth="wpa2",
                           timezone="UTC0")
        return None

    async def reload_unanswered(kind, body, timeout=None):
        if body.get("name") == "reload":
            return False, "the device did not answer in time"
        return await real_send(kind, body, timeout)

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        before = len([f for f in device.seen if f.get("name") == "status"])
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        real_send = app._send_body
        app._send_body = reload_unanswered  # type: ignore[method-assign]
        await app.onboard()
        # No status read: there is no verdict to print for a boot sequence the
        # device never reported on.
        assert len([f for f in device.seen if f.get("name") == "status"]) == before
        assert "reload: refused (the device did not answer in time)" in \
            "\n".join(chat_lines(app))


async def test_the_model_step_seeds_its_level_from_the_stored_entry():
    """`05` §13.6: re-running the wizard must not be a silent downgrade for a reader
    who set a thinking level, so the seed is the stored `extra_body`."""
    device = _Device()
    app = _app(device)
    seeds: list[str] = []

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://api.openai.com/v1", provider="openai",
                           api_key="k", skip_provider=False)
        assert isinstance(screen, ModelStep)
        seeds.append(screen.thinking)
        return {"model": "gpt-4o", "thinking": "high", "skip_model": True}

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {"llm.providers": [{
            "id": "openai", "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o", "api_key_ref": "nvs:llm_api_key",
            "extra_body": {"reasoning_effort": "high"},
        }]}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._fetch_models = _no_models  # type: ignore[method-assign]
        await app.onboard()
        assert seeds == ["high"]


async def test_a_model_step_that_keeps_the_stored_name_claims_no_verdict():
    """`05` §7.3: `Ctrl+G` on the model step carries the stored name forward and
    writes nothing, because the name was never checked against the fetched list."""
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1",
                           provider="openrouter", skip_provider=True)
        return {"model": "m", "thinking": "none", "skip_model": True}

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        assert "model kept as stored" in "\n".join(chat_lines(app))
        assert [f for f in device.writes()
                if f.get("name") == "config.set"] == []


async def test_an_empty_model_name_stops_before_the_provider_write():
    """`03`: `model` is required, so an empty one is no write at all."""
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1",
                           provider="openrouter", skip_provider=True)
        return {"model": "", "thinking": "none", "skip_model": False}

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        assert [f for f in device.writes()
                if f.get("name") == "config.set"] == []


async def test_a_refused_provider_write_is_named_and_stops():
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://openrouter.ai/api/v1",
                           provider="openrouter", api_key="k", skip_provider=False)
        return {"model": "gpt-4o", "thinking": "none", "skip_model": False}

    async def refuse(kind, body, timeout=None):
        return False, "E_ARG"

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        app._fetch_models = _no_models  # type: ignore[method-assign]
        app._send_body = refuse  # type: ignore[method-assign]
        await app.onboard()
        assert "refused (E_ARG)" in "\n".join(chat_lines(app))


async def _no_models(answers):
    return []


async def test_a_complaint_about_the_provider_file_is_shown_before_the_form(tmp_path, monkeypatch):
    """`05` §7.2: a malformed custom file must not cost the reader onboarding, but it
    must not be silent either — a dropped entry looks like a provider that does not
    exist."""
    bad = tmp_path / "musteval" / "providers.json"
    bad.parent.mkdir()
    bad.write_text("{not json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        text = "\n".join(chat_lines(app))
        assert "providers.json" in text, "the note names the file it could not read"


async def test_a_malformed_provider_file_is_named_once_not_once_per_step(tmp_path, monkeypatch):
    """`05` §7.2: two steps read the same catalog, so a complaint belongs to the step
    that first read it. Printing it twice reads as two broken files."""
    bad = tmp_path / "musteval" / "providers.json"
    bad.parent.mkdir()
    bad.write_text("{not json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    device = _Device()
    app = _app(device)

    async def fake_screen(screen):
        if isinstance(screen, NetworkStep):
            return _answer(skip_network=True)
        if isinstance(screen, ProviderStep):
            return _answer(base_url="https://x/v1", api_key="k", provider="custom",
                           skip_provider=False)
        if isinstance(screen, ModelStep):
            return {"model": "m", "thinking": "none", "skip_model": False}
        return None

    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app._config = {}
        app._config_fetched = True
        app.push_screen_wait = fake_screen  # type: ignore[method-assign]
        await app.onboard()
        text = "\n".join(chat_lines(app))
        assert text.count("providers.json") == 1


async def test_a_truncated_scan_says_so_before_the_network_form():
    """`05` §7.2: `wifi.scan` caps the list, and a form that silently shows a prefix
    reads as "these are all the networks" — the SSID that was cut looks absent."""
    device = _Device()
    inner = device.__call__

    def scan_too(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame.get("name") != "wifi.scan":
            return inner(line)
        device.seen.append(frame)
        return [json.dumps({
            "v": 1, "type": "command.result", "id": "d-9", "ts": 0,
            "reply_to": frame["id"], "name": "wifi.scan", "ok": True,
            "result": {"networks": [{"ssid": "A"}, {"ssid": "B"}], "truncated": True},
            "duration_ms": 0,
        }).encode() + b"\n"]

    app = BridgeApp(Session(FakeTransport(on_write=scan_too), ack_timeout=1,
                            ping_interval=99))
    async with app.run_test() as pilot:
        async def cancel(_screen):
            return None

        app.push_screen_wait = cancel  # type: ignore[method-assign]
        await app._onboard_network()
        await pilot.pause(0.1)
        assert "2 networks shown; the list was truncated" in chat_lines(app)
        await app.session.close()

"""`/model`, `/thinking` and the one socket the bridge opens.

`05` §7.2/§7.3: both commands are a read-modify-write of `llm.providers`, so each
branch is a way of losing a stored member. The list fetch is a plain GET that must
never be handed a `file://` URL, which is the scheme guard `_http_fetch` exists for.
"""

from __future__ import annotations

import json

import pytest

from bridge.protocol import reset_ids
from bridge.tests.test_ui_app import (  # noqa: F401 - the shared device double
    CONFIG,
    STATUS,
    _device,
    _run,
    _text,
)
from bridge.ui.app import _http_fetch, _provider_label
from bridge import providers as providers_mod
from bridge.ui import onboard


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _config_with(entry: dict) -> dict:
    return {**CONFIG, "llm": {"providers": [entry]}}


OLLAMA = {
    "id": "ollama",
    "base_url": "https://ollama.com/v1",
    "model": "gemma4:31b",
    "api_key_ref": "nvs:llm_api_key",
}


def _written(app) -> list[dict]:
    return [json.loads(line) for line in app.session._transport.written]


def _sets(app) -> list[dict]:
    return [f for f in _written(app) if f.get("name") == "config.set"]


# -- `_http_fetch` ------------------------------------------------------


def test_a_non_http_scheme_is_refused_before_the_socket_opens():
    """`06` §4: the URL comes from a provider entry, which the device stores. A
    `file://` there would turn one command into a local read."""
    with pytest.raises(ValueError, match="non-http scheme"):
        _http_fetch("file:///etc/passwd", "")


def test_a_url_with_no_scheme_is_refused_too():
    with pytest.raises(ValueError, match="\\(none\\)"):
        _http_fetch("ollama.com/v1/models", "")


def test_the_key_is_sent_only_when_there_is_one(monkeypatch):
    """`06` A1: `/model` lists without a key on purpose, so a guarded provider yields
    no list rather than a 401 the reader cannot act on."""
    import urllib.request

    seen: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"data": []}'

    def fake_urlopen(request, timeout=None):
        seen.append({"url": request.full_url,
                     "auth": request.get_header("Authorization"), "timeout": timeout})
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _http_fetch("https://ollama.com/v1/models", "secret-key") == b'{"data": []}'
    assert seen[0]["auth"] == "Bearer secret-key"
    assert seen[0]["timeout"] == 10

    seen.clear()
    _http_fetch("https://ollama.com/v1/models", "")
    assert seen[0]["auth"] is None


# -- `_provider_label` --------------------------------------------------


def test_a_stored_host_that_differs_from_the_catalog_row_is_named_by_its_host():
    """`05` §7.3: a device can carry `id: ollama` pointed at the cloud. Calling that
    "Ollama (local)" points the reader at a server that is not there."""
    known = providers_mod.find("ollama")
    assert known is not None
    label = _provider_label(known, "ollama", "https://ollama.com/v1")
    assert label == "ollama.com"


def test_a_stored_url_matching_the_row_keeps_the_row_label():
    known = providers_mod.find("ollama")
    assert known is not None
    assert _provider_label(known, "ollama", known.base_url) == known.label
    assert _provider_label(known, "ollama", "") == known.label


def test_an_unknown_provider_with_no_url_falls_back_to_its_id():
    """A `custom` entry has no catalog row at all, so its id is the only name left."""
    assert _provider_label(None, "custom", None) == "custom"
    assert _provider_label(None, "", None) == "provider"


# -- `/model` ----------------------------------------------------------


async def test_naming_a_model_writes_it_without_asking_the_provider():
    """`05` §7.3: the typed name is the route when the list is empty, so a provider
    that guards `/models` is still usable."""
    entry = dict(OLLAMA)

    async def body(app, pilot):
        app._config = _config_with(entry)
        app._config_fetched = True
        await app._model(["llama3:8b"])
        sets = _sets(app)
        assert sets, "the write was sent"
        written = sets[-1]["args"]
        assert written["path"] == "llm.providers"
        providers = written["value"]
        assert providers[0]["model"] == "llama3:8b"
        # Every other member survives the read-modify-write.
        assert providers[0]["id"] == "ollama"
        assert providers[0]["api_key_ref"] == "nvs:llm_api_key"

    await _run(_device(), body)


async def test_a_model_with_no_provider_configured_names_the_reason():
    """`05` §7.3: an empty chain is reported as its own sentence, not a write of an
    entry the device never had."""
    async def body(app, pilot):
        app._config = {**CONFIG, "llm": {"providers": []}}
        app._config_fetched = True
        await app._model(["llama3:8b"])
        assert "model:" in _text(app)
        assert not _sets(app)

    await _run(_device(), body)


async def test_a_model_whose_write_is_refused_reports_the_code():
    """`04` §4.9: the device answers `E_ARG` for a provider that is not in the chain,
    and the reader has to see the code to know which half was wrong."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True

        async def refuse(kind, body_, timeout=None):
            return False, "E_ARG"

        app._send_body = refuse  # type: ignore[assignment]
        await app._model(["llama3:8b"])
        assert "model: refused (E_ARG)" in _text(app)

    await _run(_device(), body)


async def test_a_model_pick_cancelled_writes_nothing():
    """`05` §7.3: Esc on the picker leaves the stored model alone."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True

        async def no_list(*a, **k):
            return []

        async def cancelled(_screen):
            return None

        app._model_list = no_list  # type: ignore[assignment]
        app.push_screen_wait = cancelled  # type: ignore[method-assign]
        await app._model([])
        assert "model: cancelled" in _text(app)
        assert not _sets(app)

    await _run(_device(), body)


async def test_a_model_with_no_base_url_offers_no_list():
    """`05` §7.3: the URL comes from the entry, and the catalog row is the fallback;
    with neither, one GET would go to a path with no host."""
    async def body(app, pilot):
        app._config = _config_with({"id": "custom", "model": ""})
        app._config_fetched = True
        names = await app._model_list({"id": "custom"}, "custom", "custom", None)
        assert names == []

    await _run(_device(), body)


async def test_the_list_url_is_the_entrys_own_base_url(monkeypatch):
    """`05` §7.3: the device can store `https://ollama.com/v1` while the catalog row
    says `127.0.0.1`; asking the catalog would query a server that is not there."""
    asked: list[str] = []

    def fake_fetch(url, key):
        asked.append(url)
        return b'{"data": [{"id": "gemma4:31b"}]}'

    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True
        monkeypatch.setattr("bridge.ui.app._http_fetch", fake_fetch)
        names = await app._model_list(
            dict(OLLAMA), "ollama", "ollama.com", providers_mod.find("ollama")
        )
        assert names == ["gemma4:31b"]
        assert asked == ["https://ollama.com/v1/models"]

    await _run(_device(), body)


# -- `/thinking` -------------------------------------------------------


async def test_the_bare_form_reads_the_stored_level_and_writes_nothing():
    """`05` §7.2: the read form reports; only the argument form writes."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True
        await app._thinking([])
        assert not _sets(app)
        assert _text(app).strip()

    await _run(_device(), body)


async def test_a_level_the_provider_cannot_express_is_refused_before_the_write():
    """`05` §7.2: the device accepts any flat `extra_body` member, so a level written
    where the provider rejects it turns every call into a 400."""
    async def body(app, pilot):
        app._config = _config_with(
            {"id": "custom", "base_url": "https://example.test/v1", "model": "m"}
        )
        app._config_fetched = True
        await app._thinking(["high"])
        assert not _sets(app)
        assert _text(app).strip()

    await _run(_device(), body)


async def test_a_thinking_write_that_is_refused_reports_it():
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True

        async def refuse(kind, body_, timeout=None):
            return False, "E_ARG"

        app._send_body = refuse  # type: ignore[assignment]
        await app._thinking(["high"])
        assert "thinking: refused (E_ARG)" in _text(app)

    await _run(_device(), body)


async def test_a_thinking_write_that_lands_is_echoed_in_the_conversation():
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True
        await app._thinking(["high"])
        sets = _sets(app)
        assert sets, "the write was sent"
        assert "thinking: high" in _text(app)

    await _run(_device(), body)


async def test_a_model_this_provider_cannot_take_is_refused_before_the_write(monkeypatch):
    """`05` §7.3: the write is a read-modify-write of the one stored entry, so a
    name the provider cannot serve is refused here rather than at the first prompt."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True
        monkeypatch.setattr(
            "bridge.ui.app.onboard.model_writes", lambda chosen, chain: (None, "no entry")
        )
        await app._model(["llama3:8b"])
        assert "model: no entry" in _text(app)
        assert not _sets(app)

    await _run(_device(), body)


async def test_a_screen_the_reader_already_moved_to_is_not_popped_by_the_spinner():
    """`05` §7.1: `_working` pops itself in a `finally`, so a raise inside the body
    must not leave the spinner on top of the next step. The other half of that rule:
    when the body has already moved the reader somewhere, the spinner is not the top
    screen and popping it would close the screen they are now reading."""
    from bridge.ui.modals import Working

    async def body(app, pilot):
        from bridge.ui.modals import ModelPicker

        async with app._working("writing the model", "config.set (x)"):
            assert isinstance(app.screen, Working)
            app.push_screen(ModelPicker("ollama", "Ollama", ["a"], "a"))
            await pilot.pause(0.05)
        await pilot.pause(0.05)
        assert isinstance(app.screen, ModelPicker), "the step must survive"

    await _run(_device(), body)

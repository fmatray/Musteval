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


def test_a_body_turns_the_fetch_into_a_post(monkeypatch):
    """`05` §7.1: `POST /api/show` names the model in the request body, so the fetch
    must carry one — a GET there answers for no model and the level set is lost."""
    import json as json_mod
    import urllib.request

    seen: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"thinking": {"values": [false, "low", "max"]}}'

    def fake_urlopen(request, timeout=None):
        seen.append(
            {
                "data": request.data,
                "method": request.get_method(),
                "content_type": request.get_header("Content-type"),
            }
        )
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = _http_fetch("https://ollama.com/api/show", "", {"model": "m"})
    assert body
    assert seen[0]["method"] == "POST"
    assert json_mod.loads(seen[0]["data"]) == {"model": "m"}
    assert seen[0]["content_type"] == "application/json"

    # No body is still the plain GET every other caller makes.
    seen.clear()
    _http_fetch("https://ollama.com/v1/models", "")
    assert seen[0]["method"] == "GET"
    assert seen[0]["data"] is None
    assert seen[0]["content_type"] is None


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


async def test_cancelling_the_bare_form_writes_nothing():
    """`05` §7.2: the bare form offers the levels; only a chosen one is written, so a
    dismissed picker must leave `extra_body` untouched."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True

        async def cancel(screen):
            return None

        app.push_screen_wait = cancel  # type: ignore[method-assign]
        await app._thinking([])
        assert not _sets(app)
        assert "thinking: cancelled" in _text(app)

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


# -- the refresh after a write must not navigate (`05` §7.2, §7.3) -------


def _settings_shown(app) -> bool:
    """Whether the config grid is the screen the reader is looking at."""
    return app.query_one("#settings").display


async def _let_the_refresh_land(pilot, app) -> None:
    """Give the post-write `config.get` worker time to answer, then move the reader
    there if it is going to. A quiet read never does, so this returns with the grid
    hidden; a navigating one has shown it well inside the loop."""
    for _ in range(50):
        await pilot.pause(0.02)
        if _settings_shown(app):
            return


async def test_a_thinking_write_does_not_move_the_reader_to_the_config_grid():
    """`05` §7.2: `/thinking` reads the document as an input, so the `config.get`
    it sends after the write must not switch screens. The whole-document reply
    navigates unless the read is quiet, and this command's answer belongs in the
    chat pane the reader is already looking at."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True
        await app._thinking(["high"])
        await _let_the_refresh_land(pilot, app)
        assert "thinking: high" in _text(app), "the answer landed in chat"
        assert not _settings_shown(app), "the reader was moved onto the grid"

    await _run(_device(), body)


async def test_a_model_write_does_not_move_the_reader_to_the_config_grid():
    """`05` §7.3: the same rule as `/thinking`. `/model llama3:8b` asked for a
    model, not for the settings grid, so its own refresh is quiet too."""
    async def body(app, pilot):
        app._config = _config_with(dict(OLLAMA))
        app._config_fetched = True
        await app._model(["llama3:8b"])
        await _let_the_refresh_land(pilot, app)
        assert "model: llama3:8b" in _text(app), "the answer landed in chat"
        assert not _settings_shown(app), "the reader was moved onto the grid"

    await _run(_device(), body)


# -- the level set is the model's, not the row's (`05` §7.1) ----------------


async def test_a_thinking_write_offers_what_the_model_publishes(monkeypatch):
    """The reported defect: `deepseek` answers `POST /api/show` with
    `[false, "low", "high", "max"]`, and the old row-wide set offered only `low` and
    `high`, so `max` was refused on the host before any frame was sent."""
    entry = {**OLLAMA, "model": "deepseek-v4.1-flash"}

    asked: list[tuple[str, object]] = []

    def fake_levels_for(provider, model, key="", fetch=None):
        asked.append((provider.id, model))
        return ("none", "low", "high", "max")

    monkeypatch.setattr(providers_mod, "levels_for", fake_levels_for)

    async def body(app, pilot):
        app._config = _config_with(entry)
        app._config_fetched = True
        await app._thinking(["max"])
        assert asked == [("ollama", "deepseek-v4.1-flash")]
        sets = _sets(app)
        assert sets, "the write was sent"
        assert sets[-1]["args"]["value"][0]["extra_body"]["reasoning_effort"] == "max"
        assert "thinking: max" in _text(app)

    await _run(_device(), body)


async def test_the_thinking_picker_offers_the_levels_the_model_publishes(monkeypatch):
    """`05` §7.2: the rows are the set the chosen model publishes, not the provider
    row's, so `/thinking` cannot offer `max` at a model that refuses it."""
    entry = {**OLLAMA, "model": "deepseek-v4.1-flash"}

    monkeypatch.setattr(
        providers_mod,
        "levels_for",
        lambda provider, model, key="", fetch=None: ("none", "low", "high", "max"),
    )

    async def body(app, pilot):
        app._config = _config_with(entry)
        app._config_fetched = True
        rows: list[list[tuple[str, str]]] = []

        async def capture(screen):
            rows.append(list(screen.rows))
            return None

        app.push_screen_wait = capture  # type: ignore[method-assign]
        await app._thinking([])
        assert [[label for label, _ in r] for r in rows] == [
            ["none", "low", "high", "max"]
        ]
        assert not _sets(app), "an offered level is not a written one"

    await _run(_device(), body)


async def test_a_provider_that_cannot_answer_falls_back_to_the_row(monkeypatch):
    """`levels_for` returning nothing means the provider has no per-model endpoint or
    the fetch failed. The row's set is then the answer, so the picker is never empty
    just because the network was.

    The model is one whose published set is *narrower* than the row, so the fallback
    is observable: the row's whole ladder appears, which a per-model answer could
    never produce."""
    entry = {**OLLAMA, "model": "glm-5.3"}

    monkeypatch.setattr(
        providers_mod,
        "levels_for",
        lambda provider, model, key="", fetch=None: ("low", "high"),
    )

    async def body(app, pilot):
        app._config = _config_with(entry)
        app._config_fetched = True
        rows: list[list[str]] = []

        async def capture(screen):
            rows.append([label for label, _ in screen.rows])
            return None

        app.push_screen_wait = capture  # type: ignore[method-assign]
        await app._thinking([])
        assert rows == [["low", "high"]]
        # Now the same entry with no answer from the provider: the row's set, which
        # the answer above cannot have produced.
        monkeypatch.setattr(
            providers_mod,
            "levels_for",
            lambda provider, model, key="", fetch=None: (),
        )
        await app._thinking([])
        assert rows[-1] == ["none", "low", "medium", "high", "xhigh", "max"]

    await _run(_device(), body)


# -- `/thinking`: the bare form is a picker, the typed form is not -----------


async def test_the_bare_thinking_form_opens_a_picker_and_writes_the_chosen_level():
    """`05` §7.2: with no argument the command offers the levels instead of only
    reporting one. A report is a level the reader has to retype into a second
    command, which is the case the picker exists for.

    The row chosen is the frame written: an `_PickBox` answers its own label, so a
    picker that answered anything else would be indistinguishable from a cancel.
    """

    async def body(app, pilot):
        from bridge.ui.modals import _PickBox

        rows: list[list[tuple[str, str]]] = []

        async def picker(screen):
            rows.append(list(screen.rows))
            return "high"

        app.push_screen_wait = picker  # type: ignore[method-assign]
        app.submit("/thinking")
        for _ in range(200):
            if _sets(app):
                break
            await pilot.pause(0.02)
        assert rows, "the bare form must open the picker, not print a sentence"
        assert [label for label, _ in rows[0]] == list(
            providers_mod.find("openai").thinking_levels
        )
        assert "high" in _text(app)
        wrote = _sets(app)
        assert wrote, "the chosen level must be written"
        extra = wrote[0]["args"]["value"][0]["extra_body"]
        assert providers_mod.find("openai").level_of(extra) == "high"

    await _run(_device(), body)


async def test_a_typed_thinking_level_skips_the_picker():
    """`05` §7.2: one argument is the direct write, so no modal may open on that
    path — a reader who typed the level has already chosen it."""

    async def body(app, pilot):
        opened: list[object] = []

        async def picker(screen):
            opened.append(screen)
            return None

        app.push_screen_wait = picker  # type: ignore[method-assign]
        app.submit("/thinking low")
        for _ in range(200):
            if "thinking: low" in _text(app):
                break
            await pilot.pause(0.02)
        assert not opened, "/thinking <level> must not open a modal"
        assert "thinking: low" in _text(app)
        assert _sets(app)

    await _run(_device(), body)


async def test_the_bare_thinking_form_reports_when_there_is_nothing_to_offer():
    """`05` §7.2: a provider this bridge cannot describe has no rows, so the picker
    would be a box with nothing in it. The sentence that names why is the answer."""

    entry = {**OLLAMA, "id": "not-in-the-catalog"}
    device = _device({"config.get": _config_with(entry)})

    async def body(app, pilot):
        opened: list[object] = []

        async def picker(screen):
            opened.append(screen)
            return None

        app.push_screen_wait = picker  # type: ignore[method-assign]
        app.submit("/thinking")
        for _ in range(200):
            if "not in this bridge's catalog" in _text(app):
                break
            await pilot.pause(0.02)
        assert not opened, "a provider with no levels must not open an empty box"
        assert "not in this bridge's catalog" in _text(app)
        assert not _sets(app)

    await _run(device, body)

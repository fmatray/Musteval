"""`app.py`'s error arms: what the chat says when a step is refused or unanswered.

`05` §7: a step has to name the device's own code, because a refused `config.set`
and an unanswered one are different problems. These arms only run when the device
fails, so each is driven by a device that fails in exactly that way.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bridge.client import Session
from bridge.client.session import SessionError
from bridge.link import FakeTransport
from bridge.protocol import Frame, decode_line, encode, reset_ids
from bridge.tests.test_ui_app import (  # noqa: F401 - the shared device double
    CONFIG,
    STATUS,
    _await_chat,
    _await_screen,
    _device,
    _run,
    _text,
)
from bridge.ui import BridgeApp
from bridge.ui.modals import (
    ConfirmPrompt,
    ContextModal,
    FilePrompt,
    SecretPrompt,
    ValuePrompt,
)


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


async def _app_with(reply, body):
    """Run `body` against a device that answers with `reply`."""
    return await _run(reply, body)


async def test_a_send_that_raises_is_an_error_line_and_stops_the_step():
    """`05` §7: a step stops at the first refusal, and the exception is the reason."""

    async def body(app, pilot):
        before = len(app.session._transport.written)

        async def boom(*_args, **_kwargs):
            raise SessionError("the link dropped")

        app.session.request = boom  # type: ignore[assignment]
        ok, why = await app._send_body("config", {"llm.budget.hour": 1})
        assert (ok, why) == (False, "the link dropped")
        assert len(app.session._transport.written) == before

    await _run(_device(), body)


async def test_a_send_that_times_out_names_the_timeout_not_the_device():
    """`05` §7: an unanswered step and a refused one are different problems."""

    async def body(app, pilot):
        async def slow(*_args, **_kwargs):
            raise asyncio.TimeoutError

        app.session.request = slow  # type: ignore[assignment]
        ok, why = await app._send_body("config", {"llm.budget.hour": 1})
        assert (ok, why) == (False, "the device did not answer in time")

    await _run(_device(), body)


async def test_a_reply_of_the_wrong_type_is_not_read_as_success():
    """`04` §2: only `command.result` answers a command, so anything else is a
    protocol problem rather than an `ok`."""

    async def body(app, pilot):
        async def wrong(frame, **_kwargs):
            return Frame(type="status", id="d-3", ts=0, body={})

        app.session.request = wrong  # type: ignore[assignment]
        ok, why = await app._send_body("config", {"llm.budget.hour": 1})
        assert (ok, why) == (False, "unexpected reply")

    await _run(_device(), body)


async def test_a_refusal_with_no_code_still_names_one():
    """`04` §4.3: a refusal carries a code, and one that does not must not read as a
    blank reason in the chat."""

    async def body(app, pilot):
        async def bare(frame, **_kwargs):
            return Frame(type="command.result", id="d-3", ts=0,
                         body={"name": "config.set", "ok": False})

        app.session.request = bare  # type: ignore[assignment]
        ok, why = await app._send_body("config", {"llm.budget.hour": 1})
        assert (ok, why) == (False, "no code")

    await _run(_device(), body)


async def test_a_plain_string_error_is_used_as_the_code():
    """`04` §4.3 allows `error` as a code rather than an object, and the chat names
    whichever shape arrived."""

    async def body(app, pilot):
        async def coded(frame, **_kwargs):
            return Frame(type="command.result", id="d-3", ts=0,
                         body={"name": "config.set", "ok": False, "error": "E_ARG"})

        app.session.request = coded  # type: ignore[assignment]
        ok, why = await app._send_body("config", {"llm.budget.hour": 1})
        assert (ok, why) == (False, "E_ARG")

    await _run(_device(), body)


async def test_a_refused_step_in_a_plan_stops_the_plan_and_names_the_code():
    """`05` §7: the wizard stops at the first refused frame, so a later write that
    depends on the earlier one is never sent."""

    async def body(app, pilot):
        sent: list[str] = []

        async def refuse(kind, body_dict, timeout=None):
            sent.append(kind)
            return False, "E_ARG"

        app._send_body = refuse  # type: ignore[assignment]
        ok = await app._apply([("config", {"a": 1}), ("config", {"b": 2})])
        assert ok is False
        assert sent == ["config"]
        await _await_chat(pilot, app, "onboard: refused (E_ARG)")

    await _run(_device(), body)


async def test_a_scan_that_cannot_run_falls_back_to_a_typed_ssid():
    """`05` §7.1: a scan the device cannot answer must not stop the wizard — the ssid
    is typed instead, and the note says so."""

    async def body(app, pilot):
        async def broken(frame, **kwargs):
            raise SessionError("no such command")

        app.session.request = broken  # type: ignore[assignment]
        networks, notes = await app._onboard_scan()
        assert networks == []
        assert notes == ["scan unavailable; type the ssid"]

    await _run(_device(), body)


async def test_quiet_reports_a_reply_that_is_not_a_result_as_no_result():
    """`05` §7.5: a frame that answers a command with something other than
    `command.result` is a link-level surprise, and the note must say that rather than
    reading `ok` off a body that has none — which would be a `KeyError`-shaped crash
    on a reply the protocol allows."""
    async def body(app, pilot):
        async def not_a_result(_frame, **_kwargs):
            return Frame(type="pong", id="d-9", ts=0, body={})

        app.session.request = not_a_result  # type: ignore[assignment]
        result, note = await app._quiet("context.budget")
        assert result is None
        assert note == "the device sent no result"

    await _run(_device(), body)


async def test_quiet_returns_the_result_of_a_reply_that_succeeded():
    """The other half of the same contract: `ok` and a `result` is a result."""

    async def body(app, pilot):
        result, note = await app._quiet("context.budget")
        assert note is None
        assert isinstance(result, dict) and result["budget"] == 16384

    await _run(_device(), body)


async def test_a_block_read_that_fails_says_so_and_leaves_the_pane_alone():
    """`05` §7.5: a `context.block` that cannot be answered is reported in chat and
    the right pane keeps its prompt, because a pane that half-fills is a block the
    reader cannot tell from a complete one."""

    async def body(app, pilot):
        app.submit("/context")
        await _await_screen(pilot, app, ContextModal)
        modal = app.screen

        async def broken(_frame, **_kwargs):
            raise SessionError("nothing on the wire")

        # Broken after the box opened: the budget read is what opens it, so breaking
        # the link first would test that the box never appears instead of the pane.
        app.session.request = broken  # type: ignore[assignment]
        # Row 1 is `skills`; `Enter` selects the row the cursor is on, which is the
        # gesture a reader uses rather than a handler called directly.
        table = modal.query_one("#context-blocks")
        table.focus()
        table.move_cursor(row=1)
        await pilot.press("enter")
        await _await_chat(pilot, app, "the block could not be read")
        assert "the block could not be read" in _text(app)
        assert "text of" not in str(modal.query_one("#context-text").render())
        modal.dismiss(None)

    await _run(_device(), body)


async def test_a_refused_block_names_the_code_rather_than_echoing_it():
    """`04` §4.32: an unknown block is `E_CONFIG`. The code is named so a reader can
    quote it, and the pane is not filled."""

    async def body(app, pilot):
        app.submit("/context")
        await _await_screen(pilot, app, ContextModal)
        modal = app.screen

        async def refused(_frame, **_kwargs):
            return Frame(type="command.result", id="d-4", ts=0,
                         body={"name": "context.block", "ok": False,
                               "error": {"code": "E_CONFIG", "message": "unknown"}})

        app.session.request = refused  # type: ignore[assignment]
        modal.selected = "nope"
        modal.post_message(ContextModal.RequestBlock("nope"))
        await _await_chat(pilot, app, "block refused (E_CONFIG)")
        assert "block refused (E_CONFIG)" in _text(app)
        modal.dismiss(None)

    await _run(_device(), body)


async def test_a_block_answer_for_a_closed_box_is_dropped():
    """`05` §7.5: the reader can close the box while the frame is in flight, and a
    chat line for a block they have dismissed answers a question they withdrew."""

    async def body(app, pilot):
        app.submit("/context")
        await _await_screen(pilot, app, ContextModal)
        app.screen.dismiss(None)
        await pilot.pause(0.1)
        # The message still reaches the app: nothing is waiting for it, and it must
        # not raise or paint into the screen that replaced the box.
        app.post_message(ContextModal.RequestBlock("skills"))
        await pilot.pause(0.3)
        assert "reading the block" not in _text(app)

    await _run(_device(), body)


async def test_a_refused_scan_names_the_code_and_the_fallback():
    """`05` §7.1: a refusal is not the same as an unreachable device, so the note
    carries the device's own code."""

    async def body(app, pilot):
        async def refused(frame, **_kwargs):
            return Frame(type="command.result", id="d-3", ts=0,
                         body={"name": "wifi.scan", "ok": False,
                               "error": {"code": "E_RADIO", "message": "off"}})

        app.session.request = refused  # type: ignore[assignment]
        networks, notes = await app._onboard_scan()
        assert networks == []
        assert notes == ["scan refused (E_RADIO); type the ssid"]

    await _run(_device(), body)


async def test_a_truncated_scan_says_how_many_it_shows():
    """`04` §4.27: a scan the device cut short is reported, because a short list
    otherwise reads as a weak signal rather than a cap."""

    async def body(app, pilot):
        async def cut(frame, **_kwargs):
            return Frame(type="command.result", id="d-3", ts=0,
                         body={"name": "wifi.scan", "ok": True,
                               "result": {"networks": [{"ssid": "a"}, {"ssid": "b"}],
                                          "truncated": True}})

        app.session.request = cut  # type: ignore[assignment]
        networks, notes = await app._onboard_scan()
        assert [n["ssid"] for n in networks] == ["a", "b"]
        assert notes == ["2 networks shown; the list was truncated"]

    await _run(_device(), body)


async def test_a_model_fetch_that_raises_is_a_chat_line_and_no_list():
    """`05` §7.3: the fetch runs off the UI thread, so an exception is named rather
    than reaching the app."""

    async def body(app, pilot):
        from bridge.ui import app as app_mod
        answers = type("A", (), {"provider": "openai", "api_key": "k",
                                 "model_efforts": {}})()

        def boom(*_args, **_kwargs):
            raise RuntimeError("the provider is unreachable")

        original = app_mod.providers_mod.models_with_efforts
        app_mod.providers_mod.models_with_efforts = boom
        try:
            names = await app._fetch_models(answers)
        finally:
            app_mod.providers_mod.models_with_efforts = original
        assert names == []
        assert "model list: the provider is unreachable" in _text(app)

    await _run(_device(), body)


async def test_a_refused_thinking_write_names_the_code_and_leaves_the_chat():
    """`05` §7.2: `config.set` answering no is the device refusing the level, and the
    line carries the code."""

    async def body(app, pilot):
        app._config_fetched = False
        app._config = {"llm": {"providers": [
            {"id": "ollama", "base_url": "https://ollama.com/v1", "model": "m",
             "api_key_ref": "nvs:k"}]}}
        app._config_fetched = True

        async def refuse(kind, body_dict, timeout=None):
            return False, "E_ARG"

        app._send_body = refuse  # type: ignore[assignment]
        await app._thinking(["high"])
        assert "thinking: refused (E_ARG)" in _text(app)

    await _run(_device(), body)


async def test_a_refused_model_write_names_the_code():
    """`05` §7.3: the typed name is written back whole, and a refusal names its code."""

    async def body(app, pilot):
        app._config = {"llm": {"providers": [
            {"id": "ollama", "base_url": "https://ollama.com/v1", "model": "m",
             "api_key_ref": "nvs:k"}]}}
        app._config_fetched = True

        async def refuse(kind, body_dict, timeout=None):
            return False, "E_NVS"

        app._send_body = refuse  # type: ignore[assignment]
        await app._model(["gpt-4o"])
        assert "model: refused (E_NVS)" in _text(app)

    await _run(_device(), body)


async def test_a_provider_with_no_base_url_and_no_catalog_row_has_no_list():
    """`05` §7.3: the URL comes from the entry's own `base_url`, so an entry with
    neither that nor a catalog row publishes nothing and opens no socket."""

    async def body(app, pilot):
        names = await app._model_list({"id": "custom"}, "custom", "custom", None)
        assert names == []

    await _run(_device(), body)


async def test_a_secret_request_that_raises_is_an_error_line():
    """`05` §8.1: the store write is a request, so a failed one is reported and the
    pending value is cleared rather than retried with it."""

    async def body(app, pilot):
        async def boom(frame, **_kwargs):
            raise SessionError("the device refused the key")

        app.session.request = boom  # type: ignore[assignment]
        app._pending_secret = "hunter2"
        await app._request_secret(
            Frame(type="secret.set", id="b-1", ts=0, body={"key": "wifi_psk"})
        )
        assert app._pending_secret is None
        assert "the device refused the key" in _text(app)

    await _run(_device(), body)


async def test_a_secret_request_that_times_out_clears_the_pending_value():
    """`05` §8.1: a timed-out write is not retried with the value still in hand."""

    async def body(app, pilot):
        async def slow(frame, **_kwargs):
            raise asyncio.TimeoutError

        app.session.request = slow  # type: ignore[assignment]
        app._pending_secret = "hunter2"
        await app._request_secret(
            Frame(type="secret.set", id="b-1", ts=0, body={"key": "wifi_psk"})
        )
        assert app._pending_secret is None
        assert "did not answer in time" in _text(app)

    await _run(_device(), body)


async def test_a_second_secret_asks_again_and_writes_its_own_value():
    """`05` §8.1: the mask is per entry, so the second `/secret set` must open its own
    modal. A value left in hand after the first write made it skip the modal and file
    the first secret under the second key — one wrong key, silently."""
    first, second = "first-value", "second-value"

    async def body(app, pilot):
        app.submit("/secret set wifi_psk")
        await _await_screen(pilot, app, SecretPrompt)
        app.screen.query_one("#secret-input").value = first
        await pilot.press("enter")
        for _ in range(160):
            if any(f.get("type") == "secret.set"
                   for f in [json.loads(l) for l in app.session._transport.written]):
                break
            await pilot.pause(0.02)

        app.submit("/secret set llm_api_key")
        for _ in range(160):
            if isinstance(app.screen, SecretPrompt):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, SecretPrompt), "the second entry skipped its modal"
        assert app.screen.query_one("#secret-input").value == ""
        app.screen.query_one("#secret-input").value = second
        await pilot.press("enter")
        for _ in range(160):
            sets = [f for f in [json.loads(l) for l in app.session._transport.written]
                    if f.get("type") == "secret.set"]
            if len(sets) >= 2:
                break
            await pilot.pause(0.02)
        sets = [f for f in [json.loads(l) for l in app.session._transport.written]
                if f.get("type") == "secret.set"]
        assert [(f["key"], f["value"]) for f in sets] == [
            ("wifi_psk", first), ("llm_api_key", second)]

    await _run(_device(), body)

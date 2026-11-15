"""`/tool list|enable|disable` — the one route to `tools.disabled`.

`05` §7.4: the array is what `tools_offered` consults before anything else, so the
only effect that matters is the array written. Everything here asserts that array or
the sentence refusing to touch it; a test that the command "ran" would pass on a
write of the wrong key.
"""

from __future__ import annotations

import pytest

from bridge.protocol import reset_ids
from bridge.tests.test_ui_app import (  # noqa: F401 - the shared device double
    CONFIG,
    _device,
    _run,
    _text,
)


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


OFFERED = ["gpio.read", "gpio.write", "memory.get", "time.now"]


def _config_with_disabled(disabled: list[str]) -> dict:
    return {**CONFIG, "tools": {"disabled": disabled}}


def _written(app) -> list[dict]:
    import json

    return [json.loads(line) for line in app.session._transport.written]


def _sets(app) -> list[dict]:
    return [f for f in _written(app) if f.get("name") == "config.set"]


def _armed(app, disabled: list[str]) -> None:
    app._config = _config_with_disabled(disabled)
    app._config_fetched = True
    app._state["capabilities"] = {"tools": list(OFFERED)}


# -- list ---------------------------------------------------------------


async def test_list_marks_the_disabled_names_and_names_every_other_one():
    """The set that matters is offered-plus-disabled: a name in the array is exactly
    the one the model will not be shown, so leaving it out would hide the effect."""

    async def body(app, pilot):
        _armed(app, ["memory.get"])
        await app._tool(["list"])
        lines = _text(app).splitlines()
        assert "memory.get (disabled)" in lines
        assert "gpio.read" in lines
        assert "time.now" in lines
        assert not _sets(app), "a read must not write"

    await _run(_device(), body)


async def test_list_includes_a_disabled_name_the_offered_set_no_longer_carries():
    """A name turned off is absent from the next `hello_ack`, so reading only the
    offered set would make a disabled tool invisible and unenablable."""

    async def body(app, pilot):
        app._config = _config_with_disabled(["fs.delete"])
        app._config_fetched = True
        app._state["capabilities"] = {"tools": ["gpio.read"]}
        await app._tool(["list"])
        assert "fs.delete (disabled)" in _text(app)

    await _run(_device(), body)


async def test_list_fetches_the_config_when_nothing_has_yet():
    """`/tool` is reachable before any screen has read the document, so the array is
    read on demand rather than assumed to be in hand."""

    async def body(app, pilot):
        app._state["capabilities"] = {"tools": list(OFFERED)}
        app._config_fetched = False
        await app._tool(["list"])
        assert app._config_fetched, "the read must fetch rather than treat absence as empty"
        assert "gpio.read" in _text(app)

    await _run(_device(), body)


async def test_an_empty_registry_says_so_rather_than_printing_nothing():
    """A blank answer reads as a broken command; the device can genuinely offer no
    tools, and the sentence is what tells the two apart."""

    async def body(app, pilot):
        _armed(app, [])
        app._state["capabilities"] = {}
        await app._tool(["list"])
        assert "no tools" in _text(app)

    await _run(_device(), body)


# -- disable / enable ---------------------------------------------------


async def test_disable_writes_the_whole_array_with_the_name_appended():
    """`04` §4.9: a set replaces its value, so the array is written whole rather than
    one index at a time — an index write needs the entry to exist."""

    async def body(app, pilot):
        _armed(app, ["memory.get"])
        await app._tool(["disable", "gpio.read"])
        (frame,) = _sets(app)
        assert frame["args"] == {
            "path": "tools.disabled",
            "value": ["memory.get", "gpio.read"],
        }
        assert "tool disable gpio.read" in _text(app)

    await _run(_device(), body)


async def test_enable_removes_the_name_and_leaves_the_rest():
    async def body(app, pilot):
        _armed(app, ["memory.get", "gpio.read"])
        await app._tool(["enable", "memory.get"])
        (frame,) = _sets(app)
        assert frame["args"]["value"] == ["gpio.read"]

    await _run(_device(), body)


async def test_enable_of_a_name_that_is_not_disabled_is_refused_without_a_write():
    """Nothing to do is not a reason to write: a redundant `config.set` rewrites the
    flash config for no change and reports success either way."""

    async def body(app, pilot):
        _armed(app, [])
        await app._tool(["enable", "gpio.read"])
        assert not _sets(app)
        assert "not disabled" in _text(app)

    await _run(_device(), body)


async def test_disable_of_a_name_already_disabled_is_refused_without_a_write():
    """A second append would put the same name in twice, and the array carries no
    duplicate rule the validator could catch it with."""

    async def body(app, pilot):
        _armed(app, ["gpio.read"])
        await app._tool(["disable", "gpio.read"])
        assert not _sets(app)
        assert "already disabled" in _text(app)

    await _run(_device(), body)


async def test_a_refused_write_names_the_device_code_and_leaves_the_array_alone():
    async def body(app, pilot):
        _armed(app, [])

        async def refuse(kind, payload, timeout=None):
            return False, "E_CONFIG"

        app._send_body = refuse  # type: ignore[assignment]
        await app._tool(["disable", "gpio.read"])
        assert "tool disable: refused (E_CONFIG)" in _text(app)

    await _run(_device(), body)


# -- usage --------------------------------------------------------------


@pytest.mark.parametrize(
    "rest",
    [["bogus"], ["disable"], ["disable", "a", "b"], ["enable"], []],
)
async def test_a_form_outside_the_table_states_the_usage_and_sends_nothing(rest):
    async def body(app, pilot):
        _armed(app, [])
        await app._tool(list(rest))
        assert "usage: /tool" in _text(app)
        assert not _sets(app)

    await _run(_device(), body)


async def test_the_command_reaches_the_tool_handler_rather_than_parse():
    """`/tool` is a UI verb: the array is an input the device only hands over whole,
    so a `parse` arm would have nothing to build a frame from."""

    async def body(app, pilot):
        _armed(app, ["gpio.read"])
        app.submit("/tool list")
        for _ in range(120):
            if "gpio.read (disabled)" in _text(app):
                break
            await pilot.pause(0.02)
        assert "gpio.read (disabled)" in _text(app)

    await _run(_device(), body)

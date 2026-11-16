"""`/tool list|enable|disable` — the one route to `tools.disabled`.

`05` §7.4: the array is what `tools_offered` consults before anything else, so the
only effect that matters is the array written. Everything here asserts that array or
the sentence refusing to touch it; a test that the command "ran" would pass on a
write of the wrong key.
"""

from __future__ import annotations

import pytest

from bridge.protocol import reset_ids
from bridge.tests import test_ui_app
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


# -- the table ----------------------------------------------------------


def _rows(app) -> list[list[str]]:
    """The table as cells: three columns, padded to a common width.

    Read from the rendered text rather than the frame, because the padding and the
    sort are the bridge's own work — a frame test would pass while the table stayed
    ragged or unsorted.
    """
    # The connect banner and any reply are chat lines too, so a row is a line whose
    # first cell is one of the catalogue's names.
    known = {row["name"] for row in test_ui_app.TOOL_ROWS}
    return [
        cells
        for line in _text(app).splitlines()
        if (cells := line.split()) and cells[0] in known
    ]


async def test_list_renders_one_row_per_tool_in_name_order():
    """`05` §7.4: a table, sorted by name. The device returns its registry order, so
    the sort is the bridge's, and a reader scanning for a name relies on it."""

    async def body(app, pilot):
        await app._tool(["list"])
        assert [row[0] for row in _rows(app)] == ["gpio.read", "memory.get", "time.now"]
        assert not _sets(app), "a read must not write"

    await _run(_device(), body)


async def test_a_disabled_tool_is_a_row_with_its_state_and_description():
    """The row the table exists for: a tool the model cannot call now, still named,
    with the device's own description so the reader knows what they turned off."""

    async def body(app, pilot):
        await app._tool(["list"])
        (row,) = [cells for cells in _rows(app) if cells[0] == "memory.get"]
        assert row[1] == "disabled"
        assert row[2:] == "Report heap and PSRAM free.".split()

    await _run(_device(), body)


async def test_the_description_column_is_read_from_the_device_not_a_host_table():
    """A description the host cannot know: an MCP server writes its own, so any
    host-side copy would drift from what the model was shown."""

    async def body(app, pilot):
        await app._tool(["list"])
        (row,) = [cells for cells in _rows(app) if cells[0] == "gpio.read"]
        assert row[1] == "enabled"
        assert row[2:] == "Read a pin's digital level.".split()

    await _run(_device(), body)


async def test_list_asks_the_device_for_the_catalogue():
    """The frame is the contract: reading the cached `hello_ack` would print the
    advertised set with no descriptions, which is the divergence this frame removes."""

    async def body(app, pilot):
        app._state["capabilities"] = {"tools": ["gpio.read"]}
        await app._tool(["list"])
        assert any(f.get("name") == "tools.list" for f in _written(app))

    await _run(_device(), body)


async def test_an_empty_catalogue_says_so_rather_than_printing_nothing(monkeypatch):
    """A blank answer reads as a broken command; the device can genuinely carry no
    tools, and the sentence is what tells the two apart."""

    monkeypatch.setattr(test_ui_app, "TOOL_ROWS", [])

    async def body(app, pilot):
        await app._tool(["list"])
        assert _text(app).splitlines()[-1] == "no tools"

    await _run(_device(), body)


# -- /tools -------------------------------------------------------------


async def test_tools_and_tool_list_print_the_same_table():
    """`05` §7.4: one surface, one frame. The two commands answered differently
    before this, which is the defect the frame exists to make impossible."""

    async def body(app, pilot):
        await app._tool(["list"])
        from_list = _text(app)
        await app._run_command("/tools")
        assert _text(app).startswith(from_list)

    await _run(_device(), body)


async def test_tools_spends_no_second_round_trip():
    """`/tools` is the read form: one `tools.list`, never a `config.set`."""

    async def body(app, pilot):
        await app._run_command("/tools")
        assert not _sets(app)
        assert sum(1 for f in _written(app) if f.get("name") == "tools.list") == 1

    await _run(_device(), body)


# -- completion ---------------------------------------------------------


def test_completion_offers_the_advertised_and_the_disabled_names():
    """`05` §7.4: a disabled tool is absent from `hello_ack` by design, and it is
    exactly the name `enable` must accept, so both sources are unioned."""

    from bridge.ui.help import options

    names = ("gpio.read", "memory.get")
    assert [c.text for c in options("/tool enable ", names)] == [
        "/tool enable gpio.read",
        "/tool enable memory.get",
    ]
    assert [c.text for c in options("/tool disable mem", names)] == [
        "/tool disable memory.get"
    ]


def test_completion_narrows_on_a_partial_tool_name():
    from bridge.ui.help import options

    assert [c.text for c in options("/tool enable gpio", ("gpio.read", "gpio.write"))] == [
        "/tool enable gpio.read",
        "/tool enable gpio.write",
    ]


def test_completion_offers_the_verb_then_nothing_until_a_name_is_typed():
    """`/tool en` is a partial verb, so the verb list answers; once a verb and a space
    are in, the candidates are tool names and an empty prefix offers them all."""

    from bridge.ui.help import options

    assert [c.text for c in options("/tool en")] == ["/tool enable"]
    assert [c.text for c in options("/tool enable ")] == []


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
    so a `parse` arm would have nothing to build a frame from. Typed, not called, so
    the route through `submit` is what this covers."""

    async def body(app, pilot):
        _armed(app, ["memory.get"])
        app.submit("/tool list")
        for _ in range(120):
            if "memory.get" in _text(app):
                break
            await pilot.pause(0.02)
        (row,) = [line.split() for line in _text(app).splitlines()
                  if line.split()[:1] == ["memory.get"]]
        assert row[1] == "disabled"

    await _run(_device(), body)


async def test_typing_a_verb_and_a_space_lists_the_device_and_disabled_names():
    """`05` §5.1: completion runs on every keystroke, so both sources must already be
    on the app. A disabled tool is absent from `hello_ack` by design and is exactly
    the name `enable` accepts, so the panel must offer it too — read from the panel,
    because a helper-only test would pass with the wiring missing."""

    async def body(app, pilot):
        _armed(app, ["memory.get"])
        app.query_one("#chat-input").value = "/tool enable gp"
        await pilot.pause(0.1)
        import re

        panel = str(app.query_one("#completion").content)
        assert re.findall(r"/tool enable \S+", panel) == [
            "/tool enable gpio.read",
            "/tool enable gpio.write",
        ]

    await _run(_device(), body)

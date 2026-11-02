"""Cross-document invariants (`AGENTS.md`, non-regression rule 6).

Each test here defends one fact stated in two documents. They are cheap and they
catch the drift a reviewer cannot: a frame type added to the protocol and never
given a UI surface, or an example that no longer decodes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bridge.protocol import DEVICE_TO_BRIDGE, Frame, ProtocolError, decode_line, encode

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Body of `heading` up to the next heading of the same or a higher level."""
    match = re.search(rf"^(#+)\s+{re.escape(heading)}\s*$", text, re.M)
    assert match, f"heading {heading!r} not found"
    level = len(match.group(1))
    rest = text[match.end():]
    stop = re.search(rf"^#{{1,{level}}}\s", rest, re.M)
    return rest[: stop.start()] if stop else rest


def _table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)
    return rows[1:]  # drop the header row


def _json_fences(text: str) -> list[str]:
    return re.findall(r"```json\n(.*?)```", text, re.S)


def _protocol_directions() -> tuple[set[str], set[str]]:
    rows = _table_rows(_section(_read("04-protocol.md"), "2.1 Allowed directions"))
    to_device = {row[0].strip("`") for row in rows if row[1] not in ("—", "-")}
    to_bridge = {row[0].strip("`") for row in rows if row[2] not in ("—", "-")}
    return to_device, to_bridge


# -- 04 §2.1 vs the code ------------------------------------------------


def test_the_direction_table_matches_the_protocol_catalog():
    from bridge.protocol import BRIDGE_TO_DEVICE

    to_device, to_bridge = _protocol_directions()
    assert to_device == set(BRIDGE_TO_DEVICE)
    assert to_bridge == set(DEVICE_TO_BRIDGE)


def test_every_direction_row_has_exactly_one_direction():
    rows = _table_rows(_section(_read("04-protocol.md"), "2.1 Allowed directions"))
    for row in rows:
        bridge_to_device, device_to_bridge = row[1], row[2]
        assert ("—" in (bridge_to_device, device_to_bridge)), f"{row[0]} has two directions"


# -- 04 §2.1 vs 05 §10 --------------------------------------------------


def test_every_device_to_bridge_frame_has_a_ui_surface():
    """`05-bridge.md` §10 must cover exactly the device `→` bridge set."""
    _, to_bridge = _protocol_directions()
    rows = _table_rows(_section(_read("05-bridge.md"), "10. Frame-to-UI Traceability"))
    surfaces = {row[0].strip("`") for row in rows}
    assert surfaces == to_bridge, f"missing={to_bridge - surfaces} extra={surfaces - to_bridge}"


# -- 04 §3 examples decode ---------------------------------------------


def test_every_protocol_example_decodes_to_its_own_heading_type():
    """`AGENTS.md` non-regression rule 3: the docs are the fixture corpus."""
    text = _read("04-protocol.md")
    catalog = text[text.index("## 3. Message catalog"):]
    checked = 0
    for match in re.finditer(
        r"^### 3\.\d+ (.*?)\n(.*?)(?=^### 3\.|^## )",
        catalog,
        re.M | re.S,
    ):
        names = re.findall(r"`([a-z_.]+)`", match.group(1))
        body = match.group(2)
        for block in _json_fences(body):
            frame = decode_line(block.strip().encode())
            assert isinstance(frame, Frame)
            assert frame.type in names, f"§{names} example decodes as {frame.type}"
            checked += 1
    assert checked >= 15, f"only {checked} examples checked"


def test_an_example_with_an_unknown_type_is_rejected():
    bad = b'{"v":1,"type":"nonsense","id":"a-1","ts":0}'
    with pytest.raises(ProtocolError):
        decode_line(bad)


def test_encode_round_trips_every_catalog_type():
    for kind in DEVICE_TO_BRIDGE:
        frame = decode_line(encode(Frame(type=kind, id="d-1", ts=0, body={})))
        assert frame.type == kind


# -- 03 config examples respect their own bounds -----------------------


def _config_bounds() -> dict[str, tuple[float, float]]:
    """Numeric bounds from `03-config.md`'s Full schema table, keyed by dotted path."""
    text = _read("03-config.md")
    section = _section(text, "Full schema")
    bounds: dict[str, tuple[float, float]] = {}
    for row in _table_rows(section):
        key, rng = row[0].strip("`"), row[-1]
        numbers = re.findall(r"-?\d+(?:\.\d+)?", rng)
        if len(numbers) >= 2:
            bounds[key] = (float(numbers[0]), float(numbers[1]))
    return bounds


def _walk(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path, value
            yield from _walk(value, path)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, prefix)


def test_every_config_example_stays_inside_its_documented_bounds():
    bounds = _config_bounds()
    assert bounds, "no numeric bounds parsed from 03-config.md"
    text = _read("03-config.md")
    checked = 0
    for block in _json_fences(text):
        try:
            example = json.loads(block)
        except json.JSONDecodeError:
            continue
        for path, value in _walk(example):
            if path in bounds and isinstance(value, (int, float)) and not isinstance(value, bool):
                low, high = bounds[path]
                assert low <= value <= high, f"{path}={value} outside [{low}, {high}]"
                checked += 1
    assert checked > 0, "no bounded field was exercised"

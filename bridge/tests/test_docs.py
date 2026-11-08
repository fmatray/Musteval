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
from bridge.ui import app as ui

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

# `03-config.md`'s Full schema table, transcribed by hand into what a test can
# check (`AGENTS.md`, cross-doc invariant 3). Every entry restates one row; the
# test below fails if a row is added to the doc and not to this map, so the two
# cannot drift. Prose parsing was tried first and was wrong: `wifi.retry_backoff_ms`
# reads "1..6 entries, 100..60000 each", whose first pair is a length, not a value.
#
# `[]` means "every element"; a bare `[]`-keyed entry (`wifi.networks[]`) bounds the
# list's own length.
BOUNDS: dict[str, tuple[float, float]] = {
    "version": (1, 1),
    "device.name": (1, 32),
    "wifi.connect_timeout_ms": (1000, 120000),
    "wifi.retry_backoff_ms[]": (100, 60000),
    "wifi.networks[]": (1, 8),
    "wifi.networks[].ssid": (1, 32),
    "wifi.networks[].priority": (0, 100),
    "ntp.servers[]": (1, 4),
    "ntp.sync_interval_s": (60, 86400),
    "ntp.sync_timeout_ms": (100, 60000),
    "llm.providers[]": (1, 4),
    "llm.providers[].model": (1, 64),
    "llm.providers[].timeout_ms": (1000, 120000),
    "llm.providers[].max_output_tokens": (16, 8192),
    "llm.max_tool_rounds": (1, 16),
    "llm.max_history_messages": (0, 32),
    "llm.max_calls_per_hour": (0, 100000),
    "llm.max_calls_per_day": (0, 1000000),
    "mcp.servers[]": (0, 4),
    "mcp.servers[].timeout_ms": (1000, 60000),
    "agent.max_input_bytes": (256, 4096),
    "tools.http_allow_hosts[]": (0, 8),
    "tools.memory_max_bytes": (256, 4096),
    "sys.safe_mode_after": (2, 10),
    "serial.max_line_bytes": (512, 4096),
    "geo.cache_ttl_s": (60, 86400),
    "geo.timeout_ms": (1000, 15000),
    "geo.max_bssids": (4, 32),
    "geo.providers[]": (1, 4),
    "mqtt.max_servers": (1, 8),
    "mqtt.servers[].port": (1, 65535),
    "mqtt.servers[].host": (1, 128),
    "mqtt.servers[].client_id": (1, 64),
    "mqtt.servers[].username": (1, 64),
    "mqtt.servers[].keepalive_s": (10, 300),
    "mqtt.servers[].topics[]": (0, 8),
    "mqtt.servers[].topics[].filter": (1, 128),
}

# Rows whose bounds are not one numeric pair. Listed so a new row is noticed.
NON_NUMERIC_ROWS = {
    "device.log_level",
    "wifi.networks[].password_ref",
    "ntp.timezone",
    "llm.providers[].id",
    "llm.providers[].base_url",
    "llm.providers[].api_key_ref",
    "llm.providers[].token_param",
    "llm.providers[].extra_body",
    "mcp.servers[].id",
    "mcp.servers[].url",
    "mcp.servers[].enabled",
    "mcp.servers[].auth_header_ref",
    "agent.system_prompt",
    "tools.allow_dangerous",
    "geo.enabled",
    "geo.providers[].id",
    "geo.providers[].kind",
    "geo.providers[].url",
    "geo.providers[].api_key_ref",
    "mqtt.servers[]",
    "mqtt.servers[].id",
    "mqtt.servers[].tls",
    "mqtt.servers[].password_ref",
    "mqtt.servers[].qos",
    "mqtt.servers[].topics[].qos",
    "serial.baud",
}


def _schema_rows() -> list[str]:
    """Every key row of the Full schema table, which spans `## config.json`."""
    text = _read("03-config.md")
    start = text.index("### Full schema")
    end = text.index("\n## ", start)
    section = text[start:end]
    # Only the schema table's rows are backticked keys; the provider-compatibility
    # table below it is a matrix of names and booleans.
    return [row[0].strip("`") for row in _table_rows(section) if row[0].startswith("`")]


def _walk(node, prefix=""):
    """Yield `(path, value)` with `[]` on every list, so paths match the schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            yield path, value
            yield from _walk(value, path)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                yield from _walk(item, f"{prefix}[]")
            else:
                yield f"{prefix}[]", item


# `wifi.retry_backoff_ms` is the one row whose table key names the list but whose
# bound applies to each entry, so it is encoded as the `[]` form.
KEY_ALIASES = {"wifi.retry_backoff_ms": "wifi.retry_backoff_ms[]"}


def test_the_bounds_map_covers_exactly_the_documented_rows():
    """A row added to `03-config.md` and not encoded here fails this test."""
    rows = {KEY_ALIASES.get(row, row) for row in _schema_rows()}
    encoded = set(BOUNDS) | NON_NUMERIC_ROWS
    assert rows - encoded == set(), f"uncoded rows: {sorted(rows - encoded)}"
    assert encoded - rows == set(), f"rows that no longer exist: {sorted(encoded - rows)}"


#: The schema array rows the `/settings` screen cannot show as one line: each is a
#: nested object, so editing it needs a form rather than a text field
#: (`05-bridge.md` §5.7).
OBJECT_ARRAY_PREFIXES = ("wifi.networks[]", "llm.providers[]", "mcp.servers[]",
                         "mqtt.servers[]", "geo.providers[]")


def test_the_settings_rows_cover_exactly_the_editable_schema_rows():
    """`05` §5.7: a schema key with no row is a setting the user cannot reach."""
    from bridge.ui.settings import ROWS

    # Not `KEY_ALIASES`: that map turns one row's key into its per-entry bound
    # form for `BOUNDS`, it does not rename the schema path.
    documented = set(_schema_rows())
    editable = {
        path
        for path in documented
        if not any(path == prefix or path.startswith(prefix + ".") for prefix in OBJECT_ARRAY_PREFIXES)
    }
    assert {row.path for row in ROWS} == editable


def test_a_length_bound_is_not_mistaken_for_a_value_bound():
    """`wifi.retry_backoff_ms` bounds its entries, not the list length."""
    assert BOUNDS["wifi.retry_backoff_ms[]"] == (100, 60000)
    assert "wifi.retry_backoff_ms" not in BOUNDS


def test_every_config_example_stays_inside_its_documented_bounds():
    """`AGENTS.md`, cross-doc invariant 3: the docs are the fixture corpus."""
    text = _read("03-config.md")
    seen: set[str] = set()
    for block in _json_fences(text):
        try:
            example = json.loads(block)
        except json.JSONDecodeError:
            continue
        for path, value in _walk(example):
            if path not in BOUNDS:
                continue
            low, high = BOUNDS[path]
            if isinstance(value, bool):
                continue
            if isinstance(value, list):
                actual = len(value)
            elif isinstance(value, (int, float)):
                actual = value
            else:
                continue
            assert low <= actual <= high, f"{path}={actual} outside [{low}, {high}]"
            seen.add(path)
    missing = set(BOUNDS) - seen
    assert len(seen) >= 20, f"only {len(seen)} bounded paths exercised: {sorted(seen)}"
    assert not missing or len(missing) < len(BOUNDS), "the walk exercises nothing"


# -- `09-bridge-architecture.md` §6: the two import laws -----------------


#: `09` §6: the import name, and the one package allowed to use it. Spelled in
#: two pieces because the law's own grep must print nothing, this file included.
LAWS = {"text" + "ual": "ui", "ser" + "ial": "link"}


@pytest.mark.parametrize(("module", "home"), LAWS.items())
def test_the_import_laws_hold_everywhere(module, home):
    """`09` §6 states both as grep laws; this is the grep, indented imports included."""
    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(rf"^\s*(?:import|from)\s+{module}\b")
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(root).parts[0] != home
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.match(line)
    ]
    assert offenders == [], f"{module} imported outside bridge/{home}/"


# -- `05-bridge.md` §6.2 vs `bridge/ui/app.py` --------------------------


#: The screen each §6.2 row group belongs to. A row names one screen, so the
#: table and the class are the same fact said twice.
SCREEN_CLASSES = {
    "Chat": "ChatScreen",
    "Status": "StatusScreen",
    "Tasks": "TasksScreen",
    "Logs": "LogsScreen",
    "MQTT": "MqttScreen",
    "Settings": "SettingsScreen",
    "Files": "FilesScreen",
}


def test_the_binding_tables_match_the_screens_they_describe():
    """A `Ctrl` key in the table is a binding in code, and every binding is in the
    table. Keys a widget owns (`Up`, `Enter`, `Esc`) are not class bindings, so only
    `Ctrl` chords are compared — the half that has drifted before."""
    rows = _table_rows(_section(_read("05-bridge.md"), "6.2 Screen-Specific Bindings"))
    documented: dict[str, set[str]] = {}
    for screen, key, *_ in rows:
        if screen not in SCREEN_CLASSES:  # `Editor` rows describe a modal
            continue
        for letter in re.findall(r"`Ctrl\+([A-Z])`", key):
            documented.setdefault(screen, set()).add(letter.lower())
    assert documented, "§6.2 documents no Ctrl binding"

    for screen, letters in documented.items():
        cls = getattr(ui, SCREEN_CLASSES[screen])
        actual = {b.key[5:] for b in cls.BINDINGS if b.key.startswith("ctrl+")}
        assert actual == letters, f"{screen}: code {sorted(actual)}, doc {sorted(letters)}"


# -- `02-firmware.md` §7.1 vs `docs/tools/*.md` vs the registry -------------


FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
SPEC = FIRMWARE / "components" / "tools" / "tools_spec.c"

#: `02` §7.1: these two are declarative `/tools/*.json` files, not built-ins, so
#: their schema lives in the image and never in the compiled registry.
DECLARATIVE = {"weather_now", "web_search"}


def _registry_schemas() -> dict[str, str]:
    """The `S_*` macro each built-in tool names, expanded to its JSON text."""
    source = SPEC.read_text(encoding="utf-8")
    # Line-continued macros are joined first, so a multi-line schema is one literal.
    source = re.sub(r"\\\n", "", source)
    macros = {
        name: "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', body)).encode().decode("unicode_escape")
        for name, body in re.findall(r"^#define\s+(S_[A-Z0-9_]+)\s+(.*)$", source, re.M)
    }
    return {
        tool: macros[schema]
        for tool, schema in re.findall(
            r'\{"([a-z0-9._]+)",\s*"[^"]*",\s*TOOLS_GATE_\w+,\s*(S_[A-Z0-9_]+)\}', source
        )
    }


def _documented_schemas() -> dict[str, str]:
    """Every `**Schema sent to the model**` fence, keyed by its own section heading."""
    found: dict[str, str] = {}
    for path in sorted((DOCS / "tools").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^## `([a-z0-9._]+)`\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M):
            fence = re.search(
                r"\*\*Schema sent to the model\*\*\s*\n\s*```json\n(.*?)```",
                match.group(2),
                re.S,
            )
            if fence:
                found[match.group(1)] = fence.group(1).strip()
    return found


def test_every_builtin_tool_schema_matches_its_documented_block():
    """`02` §7.1 names each domain file as the schema's owner, so the registry and
    the fence are one fact said twice. Six schemas had drifted before this test
    existed — a model following the doc would have sent arguments the gate refused."""
    registry, documented = _registry_schemas(), _documented_schemas()
    assert documented.keys() - registry.keys() == DECLARATIVE
    assert not registry.keys() - documented.keys(), "a built-in ships with no doc section"
    mismatched = {
        tool: (registry[tool], documented[tool])
        for tool in registry.keys() & documented.keys()
        if json.loads(registry[tool]) != json.loads(documented[tool])
    }
    assert not mismatched, "\n".join(
        f"{tool}\n  code: {code}\n  doc : {doc}" for tool, (code, doc) in mismatched.items()
    )

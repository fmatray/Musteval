"""Data model for the `/settings` screen: one row per scalar `config.json` key.

`03-config.md` §config.json §Full schema is the authority for every path, kind,
default and bound below, in table order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The only range form the bounds cells state: `1000..120000`.
_RANGE = re.compile(r"(\d+)\.\.(\d+)")
_INTEGER = re.compile(r"[+-]?\d+")
#: Enum values are the backticked words of a bounds cell.
_BACKTICKED = re.compile(r"`([^`]+)`")

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class ConfigRow:
    """One editable leaf of `config.json` as the settings screen lists it."""

    path: str
    kind: str
    default: object
    bound: str
    section: str


def _row(path: str, kind: str, default: object, bound: str) -> ConfigRow:
    """Build a row whose section is its first path segment."""
    return ConfigRow(path, kind, default, bound, path.split(".")[0])


#: `wifi.networks[]`, `llm.providers[]`, `mcp.servers[]` and their children are
#: nested objects, so they need a form rather than a row. `string[]` is the
#: table's simple list of strings, kept apart from `object[]`.
ROWS: tuple[ConfigRow, ...] = (
    # `version` has no default in the table; `1` is its only legal value.
    _row("version", "int", 1, "must be `1`"),
    _row("device.name", "string", "musteval", "1..32 chars, `[A-Za-z0-9_-]`"),
    _row("device.log_level", "enum", "info", "`none` `error` `warn` `info` `debug`"),
    _row("wifi.connect_timeout_ms", "int", 15000, "1000..120000"),
    _row("wifi.retry_backoff_ms", "int[]", [1000, 2000, 5000, 10000], "1..6 entries, 100..60000 each"),
    _row("ntp.servers[]", "string[]", ["pool.ntp.org"], "1..4 hostnames or IPv4"),
    _row("ntp.timezone", "string", "UTC0", "POSIX TZ string"),
    _row("ntp.sync_interval_s", "int", 3600, "60..86400"),
    _row("ntp.sync_timeout_ms", "int", 3000, "100..60000"),
    _row(
        "llm.max_tool_rounds",
        "int",
        8,
        "1..16; also bounds the close-out round described in `02-firmware.md` §4.1, "
        "so a value of 1 can leave a task open and set `todo_open`",
    ),
    _row("llm.max_history_messages", "int", 8, "0..32"),
    _row(
        "llm.max_calls_per_hour",
        "int",
        100,
        "0..100000; `0` disables the window. Bounds `llm.providers[]` attempts per UTC "
        "hour, across the whole chain (`E_LLM_RATE`)",
    ),
    _row("llm.max_calls_per_day", "int", 1000, "0..1000000; `0` disables the window. Bounds attempts per UTC day"),
    _row("agent.system_prompt", "string", "/prompts/system.md", "path starting `/`"),
    _row("agent.max_input_bytes", "int", 3072, "256..`serial.max_line_bytes`"),
    _row(
        "tools.allow_dangerous",
        "bool",
        False,
        "switch read by the write tools; the list is `06-security.md` §6, not restated here",
    ),
    _row("tools.http_allow_hosts[]", "string[]", [], "0..8 hostnames; exact match, no wildcards"),
    _row("tools.memory_max_bytes", "int", 2048, "256..4096; total bytes across the `memory` NVS namespace"),
    _row("sys.safe_mode_after", "int", 3, "2..10; consecutive failed boots before safe mode"),
    _row("serial.baud", "int", 115200, "`115200` or `921600` only"),
    _row("serial.max_line_bytes", "int", 4096, "512..4096, must match `04-protocol.md` RX limit"),
    # `geo` and `mqtt` have their own tables in §config.json; their nested arrays are
    # forms, so only the scalars they own appear here.
    _row("geo.enabled", "bool", False, "gates `geo.locate`"),
    _row("geo.cache_ttl_s", "int", 900, "60..86400"),
    _row("geo.timeout_ms", "int", 5000, "1000..15000, per provider attempt"),
    _row("geo.max_bssids", "int", 12, "4..32"),
    _row("mqtt.max_servers", "int", 4, "1..8"),
)


def sections() -> tuple[str, ...]:
    """Distinct sections in first-appearance order."""
    return tuple(dict.fromkeys(row.section for row in ROWS))


def rows_for(section: str) -> tuple[ConfigRow, ...]:
    """One section's rows, in table order."""
    return tuple(row for row in ROWS if row.section == section)


def _range(bound: str) -> tuple[int, int] | None:
    match = _RANGE.search(bound)
    return (int(match[1]), int(match[2])) if match else None


def coerce(row: ConfigRow, text: str) -> object:
    """Parse user input to the row's JSON type, or raise `ValueError` naming it."""
    raw = text.strip()
    if row.kind == "int":
        if not _INTEGER.fullmatch(raw):
            raise ValueError(f"{row.path} expects an integer, got {text!r}")
        value = int(raw)
        # Only the bounds cell's literal `min..max` or backticked values are checked;
        # cross-field rules such as `agent.max_input_bytes` vs `serial.max_line_bytes`
        # belong to the device, not to this screen.
        limits = _range(row.bound)
        if limits:
            if not limits[0] <= value <= limits[1]:
                raise ValueError(f"{row.path} must be {limits[0]}..{limits[1]}, got {value}")
            return value
        allowed = [token for token in _BACKTICKED.findall(row.bound) if _INTEGER.fullmatch(token)]
        if allowed and raw not in allowed:
            raise ValueError(f"{row.path} expects one of {', '.join(allowed)}, got {text!r}")
        return value
    if row.kind == "bool":
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(f"{row.path} expects true or false, got {text!r}")
    if row.kind == "enum":
        allowed = _BACKTICKED.findall(row.bound)
        if raw not in allowed:
            raise ValueError(f"{row.path} expects one of {', '.join(allowed)}, got {text!r}")
        return raw
    if row.kind in ("int[]", "string[]"):
        parts = [part.strip() for part in raw.split(",")] if raw else []
        if row.kind == "string[]":
            return parts
        if all(_INTEGER.fullmatch(part) for part in parts):
            return [int(part) for part in parts]
        raise ValueError(f"{row.path} expects comma-separated integers, got {text!r}")
    if row.kind == "string":
        return text
    raise ValueError(f"{row.path}: unsupported kind {row.kind!r}")


def render_value(row: ConfigRow, value: object) -> str:
    """`coerce`'s inverse: booleans as `true`/`false`, lists comma-separated, `None` as `-`."""
    if value is None:
        return "-"
    if row.kind == "bool":
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def flatten(config: object, prefix: str = "") -> dict[str, object]:
    """Dotted-path lookup table over the nested config `config.get` returns.

    Arrays are indexed (`wifi.retry_backoff_ms.0`), matching `04` §4.8, and also
    kept whole under their own path so a list row finds its value.
    """
    if not isinstance(config, dict):
        return {prefix: config} if prefix else {}
    flat: dict[str, object] = {}
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten(value, path))
        elif isinstance(value, list):
            flat[path] = value
            for index, item in enumerate(value):
                flat.update(flatten(item, f"{path}.{index}"))
        else:
            flat[path] = value
    return flat


def lookup(flat: dict[str, object], path: str) -> object | None:
    """The value for a schema path; the table writes arrays as `foo[]`, the wire as `foo`."""
    if path in flat:
        return flat[path]
    return flat.get(path.removesuffix("[]"))

"""Chat log rendering: sanitising, clipping and the tool-call block."""

from __future__ import annotations

import json
import re
from typing import Any

from rich.highlighter import Highlighter
from rich.syntax import Syntax
from rich.text import Text

#: `05-bridge.md` §5.5: payloads are never interpreted as terminal escapes.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_PLACEHOLDER = re.compile(r"<[^<>]*>")

PAYLOAD_CLIP = 120
RESULT_CLIP = 80

#: Result bodies are structured; `None` means "one line is enough".
JSON_CLIP_ROWS = 12


def sanitize(text: str) -> str:
    """Strip control characters so a broker payload cannot move the cursor."""
    return _CONTROL.sub("", text)


def clip(text: str, limit: int = PAYLOAD_CLIP) -> str:
    text = sanitize(text).replace("\r", "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_tool_call(frame_body: dict[str, Any]) -> str:
    args = frame_body.get("args") or {}
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items()) or ""
    return f"→ {frame_body.get('tool')}({rendered})"


def render_tool_result(frame_body: dict[str, Any]) -> str:
    tool = frame_body.get("tool")
    duration = frame_body.get("duration_ms", 0)
    if frame_body.get("ok"):
        return f"  ✓ {tool} ({duration}ms) → {clip(str(frame_body.get('result')), RESULT_CLIP)}"
    error = frame_body.get("error") or {}
    return f"  ✗ {tool} ({duration}ms) {error.get('code')}: {clip(str(error.get('message')), RESULT_CLIP)}"


def render_status(status: dict[str, Any]) -> list[str]:
    """The `05-bridge.md` §5.2 grid, one line per row."""
    wifi = status.get("wifi") or {}
    clock = status.get("time") or {}
    llm = status.get("llm") or {}
    agent = status.get("agent") or {}
    mem = status.get("mem") or {}
    storage = status.get("storage") or {}
    mqtt = status.get("mqtt") or []
    geo = status.get("geo")
    userfs = storage.get("userfs") or {}
    wsfs = storage.get("wsfs") or {}

    brokers = ", ".join(
        f"{b.get('id')} {'connected' if b.get('connected') else 'down'}" for b in mqtt
    ) or "none"
    geo_line = (
        f"{geo.get('lat')}, {geo.get('lon')} (+/-{geo.get('accuracy_m')}m) age {geo.get('age_s')}s"
        if geo
        else "no fix"
    )
    busy = f"Busy ({agent.get('run_id')})" if agent.get("busy") else "Idle"

    return [
        f"WIFI:  {wifi.get('state')} ({wifi.get('ssid')}) | RSSI: {wifi.get('rssi')}dBm | IP: {wifi.get('ip')}",
        f"TIME:  {clock.get('unix')} | TZ: {clock.get('tz')} | Synced: {clock.get('synced')}",
        f"LLM:   {llm.get('provider')} ({llm.get('model')}) | Available: {llm.get('available')}",
        f"AGENT: {busy} | Queue: {agent.get('queue_depth')}/{agent.get('max_queue')}",
        f"MEM:   Heap: {mem.get('heap_free')} | PSRAM: {mem.get('psram_free')}",
        f"DISK:  userfs {userfs.get('used_bytes')}/{userfs.get('total_bytes')} | wsfs {wsfs.get('used_bytes')}/{wsfs.get('total_bytes')} (quota {storage.get('workspace_quota_bytes')})",
        f"MQTT:  {brokers}",
        f"GEO:   {geo_line}",
        f"BOOT:  {status.get('boot_id')} | Uptime: {status.get('uptime_s')}s",
    ]


#: `05-bridge.md` §5.1: the prompt bar's segment separators. A plain `|` rather
#: than a powerline glyph, because a codepoint a terminal cannot draw would take
#: the fact with it.
PROMPT_SEPARATOR = " | "

#: Short forms for the bar, which has one line and many facts. The Status screen
#: keeps the long words; this is the same value, said in less.
_LINK_WORDS = {
    "connected": "linked",
    "connecting": "linking",
    "wait_ack": "linking",
    "degraded": "flaky",
    "rejected": "refused",
    "down": "down",
}


#: States in which no fact from the device can be current.
_UNREACHABLE = frozenset({"down", "connecting", "wait_ack", "rejected"})


def render_prompt_bar(
    link: str,
    ack: dict[str, Any],
    status: dict[str, Any],
    tokens: int,
    width: int | None = None,
) -> str:
    """The prompt bar above the composer, per `05-bridge.md` §5.1.

    Every segment is a fact the bridge already holds: the link state it is
    tracking, the `hello_ack` fields it stored, the last `status` frame and the
    running token total. Nothing here costs a frame, so the bar never turns a
    keystroke into a round trip.
    """
    llm = status.get("llm") or {}
    agent = status.get("agent") or {}
    wifi = status.get("wifi") or {}
    storage = status.get("storage") or {}
    wsfs = storage.get("wsfs") or {}
    sysinfo = status.get("sys") or {}

    word = _LINK_WORDS.get(link, link)
    # Without a link every other fact is stale, and a stale fact printed beside
    # `down` reads as current. The link word alone is the honest bar.
    if link in _UNREACHABLE:
        return word
    segments = [word]

    # Nothing is claimed before the device has said it: an empty `status` leaves
    # the bar with the link state alone rather than "no model", which would blame
    # the provider for a bridge that has simply not heard back yet.
    model = llm.get("model")
    if model:
        provider = llm.get("provider") or "?"
        segments.append(f"{provider}/{model}")
        # A provider that is down is a fact worth the space; a working one is not.
        if not llm.get("available", True):
            segments.append("llm down")

    if agent:
        if agent.get("busy"):
            segments.append(f"busy {agent.get('run_id')}")
        else:
            segments.append("idle")
        queue = agent.get("queue_depth") or 0
        if queue:
            segments.append(f"queue {queue}/{agent.get('max_queue')}")

    rssi = wifi.get("rssi")
    if rssi is not None:
        segments.append(f"{rssi}dBm")

    quota = storage.get("workspace_quota_bytes")
    if quota:
        used = wsfs.get("used_bytes") or 0
        segments.append(f"workspace {used}/{quota}")

    if sysinfo.get("safe_mode"):
        segments.append("SAFE MODE")

    if tokens:
        segments.append(f"{tokens} tok")

    device = ack.get("device")
    if device:
        segments.append(str(device))

    line = PROMPT_SEPARATOR.join(str(segment) for segment in segments)
    if width is None or len(line) <= width:
        return line
    # Drop whole trailing segments rather than clipping mid-word: a bar ending
    # `-58dB…` reads as a broken fact. The link word is never dropped.
    # At least one segment stays: an empty bar would say less than a clipped
    # one, and the link word is the fact that explains the rest.
    while len(segments) > 1 and len(PROMPT_SEPARATOR.join(segments)) > width:
        segments.pop()
    return PROMPT_SEPARATOR.join(str(segment) for segment in segments)


def render_diag(status: dict[str, Any]) -> list[str]:
    """`/diag`: the diagnostic half of `status`, per `05-bridge.md` §7."""
    mem = status.get("mem") or {}
    sysinfo = status.get("sys") or {}
    llm = status.get("llm") or {}
    return [
        f"heap_free: {mem.get('heap_free')}",
        f"psram_free: {mem.get('psram_free')}",
        f"safe_mode: {sysinfo.get('safe_mode')}",
        f"boot_failures: {sysinfo.get('boot_failures')}",
        f"tool_calls: {sysinfo.get('tool_calls')}",
        f"tool_failures: {sysinfo.get('tool_failures')}",
        f"llm calls hour: {llm.get('calls_hour')}/{llm.get('limit_hour')}",
        f"llm calls day: {llm.get('calls_day')}/{llm.get('limit_day')}",
    ]


# -- syntax highlighting -------------------------------------------------
#
# Colour never carries meaning on its own: the plain text is identical with or
# without a highlight, so a monochrome terminal loses nothing (`05-bridge.md` §5).


def json_payload(value: Any) -> str:
    """Compact JSON for a tool result, or `str(value)` when it is not JSON-able."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def render_json_block(value: Any) -> Any:
    """A result body as a highlighted JSON block, or a clipped line if it is scalar.

    Returning a `Syntax` renderable is what makes the block highlighted; a string
    keeps the log readable for the common one-value result.
    """
    if isinstance(value, str):
        return clip(value, RESULT_CLIP)
    if value is None:
        return "null"
    rendered = json_payload(value)
    if len(rendered) <= RESULT_CLIP:
        return rendered
    return Syntax(
        rendered,
        "json",
        theme="ansi_dark",
        background_color="default",
        word_wrap=True,
        line_numbers=False,
    )


class SlashHighlighter(Highlighter):
    """Colour the command head in the composer, and dim the usage placeholders.

    `heads` is passed in so `render` holds no copy of the command catalogue:
    `help.COMMANDS` is its only owner (`05-bridge.md` §7).
    """

    def __init__(self, heads: frozenset[str]) -> None:
        super().__init__()
        self._heads = heads

    def highlight(self, text: Text) -> None:
        raw = text.plain
        if not raw.startswith("/"):
            return
        head = raw[1:].split(" ", 1)[0]
        if head not in self._heads:
            return
        text.stylize("bold", 0, len(head) + 1)  # the slash and the head
        for match in _PLACEHOLDER.finditer(raw):
            text.stylize("dim", match.start(), match.end())

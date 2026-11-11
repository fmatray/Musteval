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


def ping_line(result: dict[str, Any]) -> str:
    """`04-protocol.md` §4.25's answer as one line, for a command result and for the
    `net.ping` tool result alike: both carry the same eight members, so they are
    worded once here rather than twice with a chance to drift."""
    received = result.get("received", 0)
    sent = result.get("sent", 0)
    loss = result.get("loss_pct", 0)
    line = (
        f"ping {result.get('target')} {result.get('host')}: "
        f"{received}/{sent}, {loss}% loss"
    )
    if loss < 100:
        line += (
            f", rtt {result.get('rtt_min_ms')}/{result.get('rtt_avg_ms')}"
            f"/{result.get('rtt_max_ms')} ms"
        )
    return line


def render_tool_result(frame_body: dict[str, Any]) -> str:
    tool = frame_body.get("tool")
    duration = frame_body.get("duration_ms", 0)
    if frame_body.get("ok"):
        result = frame_body.get("result")
        # `net.ping` shares its result shape with the `net.ping` command, so the
        # reader gets the same sentence in both places.
        if tool == "net.ping" and isinstance(result, dict):
            return f"  ✓ {tool} ({duration}ms) → {ping_line(result)}"
        return f"  ✓ {tool} ({duration}ms) → {clip(str(result), RESULT_CLIP)}"
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

    def verdict(value: bool | None, good: str = "up", bad: str = "down") -> str:
        """`05` §13.2: a verdict word, coloured where colour is available."""
        if value is None:
            return f"[dim]{bad}[/dim]"
        return f"[green]{good}[/green]" if value else f"[red]{bad}[/red]"

    return [
        f"[b]WIFI [/b] {wifi.get('state')} ({wifi.get('ssid')}) "
        f"| rssi {wifi.get('rssi')}dBm | ip {wifi.get('ip')}",
        f"[b]TIME [/b] unix {clock.get('unix')} | tz {clock.get('tz')} "
        f"| synced {verdict(clock.get('synced'), 'yes', 'no')}",
        f"[b]LLM  [/b] {llm.get('provider')} ({llm.get('model')}) "
        f"| available {verdict(llm.get('available'), 'yes', 'no')}",
        f"[b]AGENT[/b] {busy} | queue {agent.get('queue_depth')}/{agent.get('max_queue')}",
        f"[b]MEM  [/b] heap {mem.get('heap_free')} | psram {mem.get('psram_free')}",
        f"[b]DISK [/b] userfs {userfs.get('used_bytes')}/{userfs.get('total_bytes')} "
        f"| wsfs {wsfs.get('used_bytes')}/{wsfs.get('total_bytes')} "
        f"(quota {storage.get('workspace_quota_bytes')})",
        f"[b]MQTT [/b] {brokers}",
        f"[b]GEO  [/b] {geo_line}",
        f"[b]BOOT [/b] {status.get('boot_id')} | uptime {status.get('uptime_s')}s",
    ]


#: `05-bridge.md` §5.1: the prompt bar's segment separators. A plain `|` rather
#: than a powerline glyph, because a codepoint a terminal cannot draw would take
#: the fact with it.
PROMPT_SEPARATOR = " | "

#: Short forms for the bar, which has one line and many facts. The Status screen
#: keeps the long words; this is the same value, said in less.
LINK_WORDS = {
    "connected": "linked",
    "connecting": "linking",
    "wait_ack": "linking",
    "degraded": "flaky",
    "rejected": "refused",
    "down": "down",
}


#: `04` §3.9 `wifi.state`, said in the bar's short words. Two characters for a
#: working network, because the bar has one line and the 80-column layout has
#: only a few spare columns; a failure that costs a word is a failure worth it.
WIFI_WORDS = {
    "connected": "up",
    "up": "up",  # pre-§3.9 firmware and the test fixtures
    "connecting": "wifi linking",
    "failed": "wifi failed",
    "off": "wifi off",
}

#: States in which no fact from the device can be current.
UNREACHABLE = frozenset({"down", "connecting", "wait_ack", "rejected"})


def format_uptime(seconds: int) -> str:
    """One segment, in the unit that matters at that magnitude: `45s`, `12m05s`."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{rest:02d}m"
    days, rest = divmod(hours, 24)
    return f"{days}d{rest}h"


#: `05` §13.2: the bar's semantic slots. Colour never carries a fact alone — the
#: plain text is identical with or without it — so a monochrome terminal loses
#: nothing but the emphasis.
SEGMENT_STYLES = {
    "ok": "green",
    "warn": "yellow",
    "bad": "red bold",
    "muted": "dim",
}

#: The link word's own slot, because it is the one segment that must be legible
#: before anything else on the bar is read.
_LINK_SLOTS = {"linked": "ok", "degraded": "warn", "refused": "bad", "down": "bad"}


def _bar_segments(
    link: str,
    ack: dict[str, Any],
    status: dict[str, Any],
    tokens: int,
) -> list[tuple[str, str]]:
    """The bar as `(text, slot)` pairs, in drop order.

    One list feeds both the plain string and the styled one, so the fact rules
    cannot differ between a coloured terminal and a pipe.
    """
    llm = status.get("llm") or {}
    agent = status.get("agent") or {}
    wifi = status.get("wifi") or {}
    storage = status.get("storage") or {}
    wsfs = storage.get("wsfs") or {}
    sysinfo = status.get("sys") or {}

    word = LINK_WORDS.get(link, link)
    # Without a link every other fact is stale, and a stale fact printed beside
    # `down` reads as current. The link word alone is the honest bar.
    if link in UNREACHABLE:
        return [(word, _LINK_SLOTS.get(link, "bad"))]
    segments: list[tuple[str, str]] = [
        (word, _LINK_SLOTS.get(link, "ok"))
    ]

    # The bar drops from the right, so the leftmost segments are the ones that
    # survive a narrow terminal. Safe mode explains a failure better than any
    # other fact, so it goes first; uptime goes next because it is the fact that
    # dates every other one.
    if sysinfo.get("safe_mode"):
        segments.append(("SAFE MODE", "bad"))

    uptime = status.get("uptime_s")
    if isinstance(uptime, int) and not isinstance(uptime, bool):
        segments.append((format_uptime(max(uptime, 0)), "muted"))

    # Nothing is claimed before the device has said it: an empty `status` leaves
    # the bar with the link state alone rather than "no model", which would blame
    # the provider for a bridge that has simply not heard back yet.
    model = llm.get("model")
    if model:
        provider = llm.get("provider") or "?"
        segments.append((f"{provider}/{model}", "muted"))
        # A provider that is down is a fact worth the space; a working one is not.
        if not llm.get("available", True):
            segments.append(("llm down", "bad"))

    if agent:
        if agent.get("busy"):
            segments.append((f"busy {agent.get('run_id')}", "warn"))
        else:
            segments.append(("idle", "muted"))
        queue = agent.get("queue_depth") or 0
        if queue:
            segments.append((f"queue {queue}/{agent.get('max_queue')}", "warn"))

    # Wifi: the state is its own segment, because a missing rssi used to be the
    # whole report and silence is not a fault report. The connected word is two
    # characters: the bar is one line and the 80-column layout is already full,
    # where a longer word would push the device name off the end.
    state = wifi.get("state")
    if state:
        slot = "ok" if WIFI_WORDS.get(state) == "up" else "bad"
        segments.append((WIFI_WORDS.get(state, f"wifi {state}"), slot))
    rssi = wifi.get("rssi")
    if rssi is not None:
        segments.append((f"{rssi}dBm", "muted" if int(rssi) > -70 else "warn"))
    failovers = wifi.get("failovers") or 0
    if failovers:
        segments.append(
            (f"{failovers} failover" + ("s" if failovers > 1 else ""), "warn")
        )

    quota = storage.get("workspace_quota_bytes")
    if quota:
        used = wsfs.get("used_bytes") or 0
        segments.append((f"workspace {used}/{quota}", "muted"))

    if tokens:
        segments.append((f"{tokens} tok", "muted"))

    device = ack.get("device")
    if device:
        segments.append((str(device), "muted"))
    return segments


def _fit(segments: list[tuple[str, str]], width: int | None) -> list[tuple[str, str]]:
    """Drop whole trailing segments until the bar fits. Never the first one."""
    if width is None:
        return segments
    while len(segments) > 1 and len(PROMPT_SEPARATOR.join(t for t, _ in segments)) > width:
        segments.pop()
    return segments


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

    The plain string is what a test and a pipe see. `render_prompt_text` styles
    the same segments; neither owns a rule the other lacks.
    """
    fitted = _fit(_bar_segments(link, ack, status, tokens), width)
    return PROMPT_SEPARATOR.join(text for text, _ in fitted)


def render_prompt_text(
    link: str,
    ack: dict[str, Any],
    status: dict[str, Any],
    tokens: int,
    width: int | None = None,
) -> Text:
    """The bar as styled text: same characters, same order, same drops."""
    fitted = _fit(_bar_segments(link, ack, status, tokens), width)
    out = Text()
    for index, (text, slot) in enumerate(fitted):
        if index:
            out.append(PROMPT_SEPARATOR, style=SEGMENT_STYLES["muted"])
        out.append(text, style=SEGMENT_STYLES.get(slot, ""))
    return out


def key_label(key: str) -> str:
    """`ctrl+d` → `Ctrl+D`, `enter` → `Enter`: how a key is written to a reader."""
    parts = [part for part in key.split("+") if part]
    spelled = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt"}
    out: list[str] = []
    for part in parts:
        if part in spelled:
            out.append(spelled[part])
        elif len(part) == 1:
            out.append(part.upper())
        else:
            out.append(part.capitalize())
    return "+".join(out)


def binding_pairs(
    bindings: list[Any], extra: list[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """`05` §13.3: the hint line from a screen's own bindings, plus the keys it
    handles outside `BINDINGS`. One source, so a hint cannot name a dead key."""
    pairs = [(key_label(binding.key), binding.description) for binding in bindings]
    pairs.extend(extra)
    return pairs


def hint_bar(pairs: list[tuple[str, str]]) -> Text:
    """`05` §13.3: the one hint line every screen ends in.

    Built from the same `(key, label)` pairs the screen's `BINDINGS` declare, so the
    hint cannot name a key the screen no longer binds. The key takes `$accent` and
    the verb `$text-muted`, which reads as a key and a verb at a glance.
    """
    out = Text()
    for index, (key, label) in enumerate(pairs):
        if index:
            out.append("  ", style=SEGMENT_STYLES["muted"])
        out.append(key, style="bold")
        out.append(f" {label}", style=SEGMENT_STYLES["muted"])
    return out


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

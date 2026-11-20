"""Chat log rendering: sanitising, clipping and the tool-call block."""

from __future__ import annotations

import json
import re
from typing import Any

from rich.highlighter import Highlighter
from rich.markup import escape
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


def wifi_state_line(result: dict[str, Any]) -> str:
    """`04-protocol.md` §4.26's answer as one line. The §3.9 object is the same one
    `status` carries, so the two readings of the radio cannot word it differently."""
    state = str(result.get("state") or "off")
    ssid = result.get("ssid")
    if not ssid:
        # `off`, `connecting` and `failed` have no identity to report, and printing
        # `None` where a name belongs reads as a device fault.
        return f"wifi {state}"
    return f"wifi {state} ({sanitize(str(ssid))}) rssi {result.get('rssi')} dBm ip {result.get('ip')}"


def template_names(result: dict[str, Any]) -> list[str]:
    """`04-protocol.md` §4.11's answer, one name per line.

    The list is `/prompts/*.md` basenames, so a name is user-written (a file the
    user imported through `/fs put`) and goes through `sanitize` like any other.
    """
    names = result.get("templates")
    if not names:
        return ["no templates"]
    return [sanitize(str(name)) for name in names]


def template_text(result: dict[str, Any]) -> str:
    """`04-protocol.md` §4.12's answer: the body, headed by the file it came from.

    The body is a prompt a model will read, so it is shown verbatim rather than
    clamped: `TOOLS_TEXT_MAX` is the tool surface's limit, not this one.
    """
    name = sanitize(str(result.get("name") or ""))
    text = str(result.get("text") or "")
    return f"{name} ({result.get('bytes')} bytes)\n{text}"


def geo_line(result: dict[str, Any]) -> str:
    """`04-protocol.md` §4.13's answer as one line.

    `provider` is the configured entry id, which comes from the config file, so it
    is sanitized like any other device-supplied string. The place and locale
    members are the provider's own words, so each goes through `sanitize` too, and
    an absent one is left out rather than printed as an empty bracket.

    The return stays one line because the caller writes it as one chat row.
    """
    def text(*keys: str) -> str:
        """The first of `keys` the provider actually sent, sanitised."""
        for key in keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return sanitize(value).strip()
        return ""

    def labelled(value: str, code: str, fmt: str) -> str:
        """`value (code)` when both arrived; whichever one arrived alone otherwise."""
        return fmt.format(value, code) if value and code else value or code

    age = result.get("age_s")
    line = (
        f"geo {result.get('lat')}, {result.get('lon')} "
        f"(+/-{result.get('accuracy_m')}m)"
    )
    provider = result.get("provider")
    if provider:
        line += f" via {sanitize(str(provider))}"
    if age is not None:
        line += f", age {age}s"

    region = labelled(text("region"), text("region_code"), "{} ({})")
    country = labelled(text("country"), text("country_code"), "{} [{}]")
    place = ", ".join(p for p in (text("city"), region, country) if p)

    segments = [place] if place else []
    if result.get("in_eu") is True:
        segments.append("EU")
    if dial := text("country_calling_code"):
        segments.append(dial)
    if postal := text("postal"):
        segments.append(postal)
    if clock := " ".join(p for p in (text("timezone"), text("utc_offset")) if p):
        segments.append(clock)
    if money := " ".join(p for p in (text("currency"), text("currency_name")) if p):
        segments.append(money)
    if languages := text("languages"):
        segments.append(languages)

    if segments:
        line += " — " + " · ".join(segments)

    return line


def tool_table(result: dict[str, Any]) -> list[str]:
    """`04` §4.29's answer as one table, sorted by name.

    Three columns: the name, whether the model may call it now, and its description.
    A row the model cannot call is the reason this frame exists, so the state column
    sits before the prose where it stays readable on a narrow terminal. Every field is
    device or network text — an MCP server writes its own description — so all three
    go through `sanitize`, and the caller writes the lines with `markup=False`.
    """
    rows = sorted(
        (
            sanitize(str(row.get("name") or "")),
            "enabled" if row.get("offered") else "disabled",
            sanitize(str(row.get("description") or "")),
        )
        for row in result.get("tools") or []
        if isinstance(row, dict)
    )
    if not rows:
        return ["no tools"]
    names = max([len(name) for name, _, _ in rows] + [len("name")])
    states = max([len(state) for _, state, _ in rows] + [len("state")])
    header = f"  {'name'.ljust(names)}  {'state'.ljust(states)}  description"
    return [header] + [
        f"  {name.ljust(names)}  {state.ljust(states)}  {desc}"
        for name, state, desc in rows
    ]


#: `04` §4.30's eight blocks, in the order the document lists them, with the label the
#: modal shows. The order is the prompt's own shape — what the model is told, then what
#: was said, then what was called — so a reader can see where the bytes went.
CONTEXT_BLOCKS = (
    ("system_prompt", "system prompt"),
    ("skills", "skills"),
    ("tools", "tools"),
    ("user", "user messages"),
    ("assistant", "assistant messages"),
    ("thinking", "assistant thinking"),
    ("tool_calls", "tool calls"),
    ("tool_output", "tool outputs"),
)


def context_blocks(
    result: dict[str, Any],
) -> list[tuple[str, str, str, str, str]]:
    """`04` §4.30's answer as the left pane's columns.

    One row per block, in the order the document lists them, as
    `(key, label, size, share, counter)`: what the block is, how many bytes it takes,
    what share of `LLM_BODY_MAX` that is, and how many items it holds.

    The four columns are one row because they answer one question — a reader looking
    at "tools 5200 31% 18" is deciding what to drop, and splitting the numbers across
    two rows or two screens would make them compare a number against a label.

    The counter is the item count from `counts`, not the byte count: a reader looking
    at "skills 3" is asking how many skills are in the index. A block with no counter
    gets an em dash rather than a zero, because a zero is a measured answer and
    `system_prompt` is one document. The size is `blocks`, and a reply that omits
    either map still lists all eight rows — a block missing from the list reads as one
    the device cannot report, and the reader cannot tell that from a measured nothing.
    """
    counts = result.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    blocks = result.get("blocks")
    if not isinstance(blocks, dict):
        blocks = {}
    try:
        budget = int(result.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    rows: list[tuple[str, str, str, str, str]] = []
    for key, label in CONTEXT_BLOCKS:
        if key in counts:
            try:
                counter = str(int(counts.get(key) or 0))
            except (TypeError, ValueError):
                counter = "—"
        else:
            counter = "—"
        # `17` §4: the device's value is data, and a string where a number belongs is
        # the reply's, not the bridge's to crash on — the compositor is behind this.
        # A block the reply did not measure is an em dash, not a zero: eight rows are
        # drawn from the document's list, and one the frame omitted has no size to
        # show rather than a measured none.
        measure, share = "—", "—"
        try:
            size = int(blocks[key])
        except (KeyError, TypeError, ValueError):
            size = None
        if size is not None:
            measure = str(size)
            if budget > 0:
                share = f"{size * 100 // budget}%"
        rows.append((key, label, measure, share, counter))
    return rows


def context_summary(result: dict[str, Any]) -> str:
    """`04` §4.30's totals as one line under the `/context` list.

    The bytes are the frame's own numbers, not a sum of the blocks: `used` is what the
    device reports for the whole request, and a reader comparing it against `budget`
    is asking how close the next prompt is to being refused. The message count is the
    history's own, which is not the sum of the four role counters either — a message
    with a role the classifier does not recognise counts in neither.
    """
    try:
        budget = int(result.get("budget") or 0)
        used = int(result.get("used") or 0)
        free = int(result.get("free") or 0)
        messages = int(result.get("messages") or 0)
    except (TypeError, ValueError):
        budget = used = free = messages = 0
    return (
        f"used {used} of {budget} B, {free} B free · "
        f"{messages} message{'s' if messages != 1 else ''} in the history"
    )


def wifi_scan_lines(result: dict[str, Any]) -> list[str]:
    """`04-protocol.md` §4.24's answer, one line per network, strongest first.

    An ssid is whatever the access point chose to broadcast, so it goes through
    `sanitize` like any other untrusted payload: a hostile one can carry escape
    bytes into this terminal.
    """
    networks = [n for n in result.get("networks") or [] if isinstance(n, dict)]
    if not networks:
        return ["no networks heard"]
    rows = [(sanitize(str(n.get("ssid") or "")), n.get("rssi", 0), str(n.get("auth") or "unknown")) for n in networks]
    names = max(len(ssid) for ssid, _, _ in rows)
    levels = max(len(str(rssi)) for _, rssi, _ in rows)
    lines = [f"  {ssid.ljust(names)}  {str(rssi).rjust(levels)} dBm  {auth}" for ssid, rssi, auth in rows]
    if result.get("truncated"):
        lines.append(f"  truncated: {len(networks)} networks shown, more were in range")
    return lines



def wifi_known_lines(
    status: dict[str, Any], scan: dict[str, Any] | None, networks: list[Any]
) -> list[str]:
    """`/wifi list`: every configured network, and whether the radio can hear it.

    Two frames and neither is a substitute: `config.get` says which networks exist and
    in what order they will be tried, and the scan says which are audible now. A
    configured network the scan did not return is drawn `not in range` rather than
    dropped — "configured but silent" is the answer to the question asked
    (`05-bridge.md` §7.6).

    `scan` is `None` when the scan was refused (the radio is off, or a run holds it),
    which is not the same as a scan that returned nothing: an absent scan cannot say a
    network is out of range, so every state is `unknown` instead of a guess. An empty
    dict would turn "I could not look" into "I looked and it was not there".
    """
    joined = sanitize(str((status or {}).get("ssid") or ""))
    heard: dict[str, Any] = {}
    for n in (scan or {}).get("networks") or []:
        if isinstance(n, dict):
            heard[sanitize(str(n.get("ssid") or ""))] = n.get("rssi")
    # `status` is the only frame that reports the signal of the network actually
    # associated, and a scan taken from that association often omits it.
    if joined and (status or {}).get("rssi") is not None:
        heard[joined] = (status or {}).get("rssi")

    rows: list[tuple[str, str, str, str]] = []
    for entry in networks or []:
        if not isinstance(entry, dict):
            continue
        ssid = sanitize(str(entry.get("ssid") or ""))
        priority = str(entry.get("priority", 0))
        if ssid == joined:
            state = "joined"
        elif scan is None:
            state = "unknown"
        elif ssid in heard:
            state = "in range"
        else:
            state = "not in range"
        rssi = heard.get(ssid)
        signal = f"{rssi} dBm" if rssi is not None else "-"
        rows.append((ssid, priority, state, signal))
    if not rows:
        return ["no networks configured"]
    names = max(len(r[0]) for r in rows)
    prios = max(len(r[1]) for r in rows)
    return [
        f"  {ssid.ljust(names)}  {prio.rjust(prios)}  {state:<12} {signal}"
        for ssid, prio, state, signal in rows
    ]


def provider_lines(providers: list[Any]) -> list[str]:
    """`/providers`: one row per entry of `llm.providers`, in chain order.

    The key is named, never valued: `api_key_ref` is an `nvs:` reference and the
    device has no frame that would return anything else (`05-bridge.md` §7.7).
    """
    rows = [p for p in providers or [] if isinstance(p, dict)]
    if not rows:
        return ["no providers configured"]
    ids = max(len(str(p.get("id") or "")) for p in rows)
    models = max(len(str(p.get("model") or "")) for p in rows)
    lines = []
    for index, entry in enumerate(rows):
        pid = sanitize(str(entry.get("id") or ""))
        model = sanitize(str(entry.get("model") or ""))
        ref = sanitize(str(entry.get("api_key_ref") or ""))
        base = sanitize(str(entry.get("base_url") or ""))
        lines.append(f"  {index + 1}. {pid.ljust(ids)}  {model.ljust(models)}  {base}")
        lines.append(f"     key: {ref}")
    return lines


#: `05` §7: the five states a `/todo` line distinguishes, and the mark each gets.
_TODO_MARKS = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
    "abandoned": "[-]",
    "blocked": "[!]",
}


def todo_lines(result: dict[str, Any]) -> list[str]:
    """`04-protocol.md` §4.27's `phases` tree flattened to one line per task.

    Content is model-written and a phase name is user-written, so both go through
    `sanitize`. A `blocked` task carries its `blocker` (`tools/todo.md` §Result) and
    printing it is the whole point of the state: without it the user cannot tell a
    dependency from a failure.
    """
    phases = [p for p in result.get("phases") or [] if isinstance(p, dict)]
    if not phases:
        return ["no todos"]
    tasks = [t for p in phases for t in p.get("tasks") or [] if isinstance(t, dict)]
    # A phase with no tasks is legal (`tools/todo.md` §Operations `init`), so the
    # header is what proves it exists; a taskless tree must still print its phases.
    width = max((len(sanitize(str(t.get("content") or ""))) for t in tasks), default=0)
    lines: list[str] = []
    for phase in phases:
        name = sanitize(str(phase.get("name") or ""))
        lines.append(f"{name}")
        for task in phase.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status") or "pending")
            mark = _TODO_MARKS.get(status, "[?]")
            content = sanitize(str(task.get("content") or ""))
            line = f"  {mark} {content.ljust(width)}  {status}"
            if task.get("blocker"):
                line += f"  ({sanitize(str(task['blocker']))})"
            lines.append(line)
    return lines


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


def llm_verdict(llm: dict[str, Any]) -> bool | None:
    """Whether the provider answered, or `None` when no call has decided it.

    `04` §3.9 defines `available` as "last call succeeded", so a device that has
    not called yet reports `false` with a null `last_error` and nothing is wrong.
    `last_error` is what names a call that actually failed, so the two together
    are the only honest "down": reading `available` alone sent the reader after a
    provider that was answering (`/sys` and the prompt bar both did).
    """
    if llm.get("available"):
        return True
    return False if llm.get("last_error") else None


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

    # `17` §2: every value here comes from the device, and the grid is built as
    # markup, so a provider id or an SSID containing `[`/`]` is escaped before it is
    # spliced in. Unescaped, `[/bold]` in an SSID raised `MarkupError` and took the
    # whole screen down.
    brokers = escape(
        ", ".join(f"{b.get('id')} {'connected' if b.get('connected') else 'down'}" for b in mqtt)
    ) or "none"
    geo_line = escape(
        f"{geo.get('lat')}, {geo.get('lon')} (+/-{geo.get('accuracy_m')}m) age {geo.get('age_s')}s"
        if geo
        else "no fix"
    )
    busy = escape(f"Busy ({agent.get('run_id')})" if agent.get("busy") else "Idle")

    def verdict(value: bool | None, good: str = "up", bad: str = "down") -> str:
        """`17` §2: a verdict word, coloured where colour is available."""
        if value is None:
            return f"[dim]{bad}[/dim]"
        return f"[green]{good}[/green]" if value else f"[red]{bad}[/red]"

    def shown(value: Any, word: str = "—") -> str:
        """`04` §3.9 types these nullable, so an absent one is drawn as absent.

        A device with no provider, no model and no network is a legal document, and
        `str(None)` printed `None` on four rows of a grid whose whole job is to say
        what is missing (`17` §2)."""
        if value is None or value == "":
            return word
        return escape(str(value))

    rssi = wifi.get("rssi")
    rssi_cell = f"{rssi}dBm" if isinstance(rssi, (int, float)) else "—"
    return [
        f"[b]WIFI [/b] {shown(wifi.get('state'), 'off')} "
        f"({shown(wifi.get('ssid'), 'no network')}) "
        f"| rssi {rssi_cell} | ip {shown(wifi.get('ip'))}",
        f"[b]TIME [/b] unix {shown(clock.get('unix'))} | tz {shown(clock.get('tz'))} "
        f"| synced {verdict(clock.get('synced'), 'yes', 'no')}",
        f"[b]LLM  [/b] {shown(llm.get('provider'), 'no provider')} "
        f"({shown(llm.get('model'), 'no model')}) "
        f"| available {verdict(llm_verdict(llm), 'yes', 'no')}",
        f"[b]AGENT[/b] {busy} | queue {shown(agent.get('queue_depth'))}"
        f"/{shown(agent.get('max_queue'))}",
        f"[b]MEM  [/b] heap {shown(mem.get('heap_free'))} | psram {shown(mem.get('psram_free'))}",
        f"[b]DISK [/b] userfs {shown(userfs.get('used_bytes'))}/{shown(userfs.get('total_bytes'))} "
        f"| wsfs {shown(wsfs.get('used_bytes'))}/{shown(wsfs.get('total_bytes'))} "
        f"(quota {shown(storage.get('workspace_quota_bytes'))})",
        f"[b]MQTT [/b] {brokers}",
        f"[b]GEO  [/b] {geo_line}",
        f"[b]BOOT [/b] {shown(status.get('boot_id'))} | uptime {shown(status.get('uptime_s'))}s",
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


#: `17` §2: the bar's semantic slots. Colour never carries a fact alone — the
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
        # The thinking level rides with the model it belongs to, in one segment: it
        # is a property of that model, and a narrow bar must not drop one and keep
        # the other (`05` §5.1).
        thinking = llm.get("thinking")
        segments.append(
            (f"{provider}/{model}/{thinking}" if thinking else f"{provider}/{model}",
             "muted")
        )
        # A provider that is down is a fact worth the space; a working one is not.
        # `available` alone cannot say it — see `llm_verdict`.
        if llm_verdict(llm) is False:
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
    """`17` §3: the hint line from a screen's own bindings, plus the keys it
    handles outside `BINDINGS`. One source, so a hint cannot name a dead key."""
    pairs = [(key_label(binding.key), binding.description) for binding in bindings]
    pairs.extend(extra)
    return pairs


def hint_bar(pairs: list[tuple[str, str]]) -> Text:
    """`17` §3: the one hint line every screen ends in.

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


def diag_sections(status: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """`/sys diag`: the counters half of `status`, headed per `05-bridge.md` §5.10.

    Four heads rather than one list of eight lines: the box already renders section
    headings, and a flat run of `key: value` gives a reader no seam to scan by. The
    facts and their order are unchanged — one line each — so nothing about what the
    device reports moves here.
    """
    mem = status.get("mem") or {}
    sysinfo = status.get("sys") or {}
    llm = status.get("llm") or {}
    return [
        (
            "Memory",
            [f"heap_free: {mem.get('heap_free')}", f"psram_free: {mem.get('psram_free')}"],
        ),
        (
            "Boot",
            [
                f"safe_mode: {sysinfo.get('safe_mode')}",
                f"boot_failures: {sysinfo.get('boot_failures')}",
            ],
        ),
        (
            "Tools",
            [
                f"tool_calls: {sysinfo.get('tool_calls')}",
                f"tool_failures: {sysinfo.get('tool_failures')}",
            ],
        ),
        (
            "LLM budget",
            [
                f"llm calls hour: {llm.get('calls_hour')}/{llm.get('limit_hour')}",
                f"llm calls day: {llm.get('calls_day')}/{llm.get('limit_day')}",
            ],
        ),
    ]


def reload_lines(result: dict[str, Any]) -> list[str]:
    """`/sys reload`: what reloaded, then each warning that needs an action.

    `04` §4.28's reply is `{reloaded, warnings}`. The scopes are one line — a reader
    who typed `/sys reload` wants to know it happened — and a warning gets its own,
    because `scope: message` is the only part of this reply anyone acts on.
    """
    scopes = [sanitize(str(scope)) for scope in result.get("reloaded") or []]
    warnings = [w for w in result.get("warnings") or [] if isinstance(w, dict)]
    lines = [f"reloaded: {', '.join(scopes)}" if scopes else "reloaded: nothing"]
    for warning in warnings:
        lines.append(
            f"  warning ({sanitize(str(warning.get('scope') or '?'))}): "
            f"{sanitize(str(warning.get('message') or ''))}"
        )
    if not warnings:
        lines.append("  no warnings")
    return lines


def reboot_line(result: dict[str, Any]) -> str:
    """`/sys reboot`: the grace period, in the unit the reader thinks in."""
    grace = result.get("grace_ms")
    return f"rebooting in {grace} ms" if grace is not None else "rebooting"


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

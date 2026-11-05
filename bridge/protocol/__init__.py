"""L0 protocol codec: envelope, NDJSON framing, enumerations, id allocation.

Pure by construction (`09-bridge-architecture.md` §3). No socket, no thread, no
file, no environment, no clock. A function takes bytes or a `Frame` and returns
bytes or a `Frame`, which is what lets the same package run inside the TUI, a
one-shot CLI, a pytest run, or the fake peer.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from typing import Any, FrozenSet

VERSION = 1

#: `04-protocol.md` §1 caps the *device's* buffers: it reads at most 4096 bytes per
#: line and writes at most 16384. A bridge line is therefore bounded by the device's
#: RX, and a received line by the device's TX. Naming them by direction rather than
#: by device side is what keeps the two from being swapped.
MAX_FROM_BRIDGE_BYTES = 4096
MAX_FROM_DEVICE_BYTES = 16384

#: Frozen by `04-protocol.md` §2.1. Direction is a decode concern: a `pong`
#: arriving bridge-to-device is malformed input, not a session bug.
BRIDGE_TO_DEVICE: FrozenSet[str] = frozenset(
    {"hello", "prompt", "cancel", "command", "secret.set", "ping", "ack"}
)
DEVICE_TO_BRIDGE: FrozenSet[str] = frozenset(
    {
        "hello_ack",
        "status",
        "log",
        "agent.start",
        "agent.tool",
        "agent.tool_result",
        "agent.final",
        "agent.error",
        "task.fired",
        "gpio.edge",
        "mqtt.message",
        "event",
        "command.result",
        "pong",
    }
)
ALL_TYPES = BRIDGE_TO_DEVICE | DEVICE_TO_BRIDGE

#: `04-protocol.md` §5. Stable identifiers; never renamed.
ERROR_CODES = frozenset(
    {
        "E_CONFIG",
        "E_WIFI",
        "E_LLM_UNAVAILABLE",
        "E_LLM_RATE",
        "E_TOOL",
        "E_MCP",
        "E_BUSY",
        "E_SAFE_MODE",
        "E_TIMEOUT",
        "E_PROTOCOL",
        "E_AUTH",
        "E_MQTT",
        "E_GEO",
        "E_STORAGE",
        "E_CANCELLED",
        "E_INTERNAL",
    }
)

ENVELOPE_KEYS = frozenset({"v", "type", "id", "ts", "reply_to"})
REDACTED = "***"

#: `04-protocol.md` §4. A protocol fact, not a bridge one: an unknown name is
#: `E_PROTOCOL` at the device, so the codec package owns the list and every
#: consumer (commands, test agent) imports it from here.
COMMAND_NAMES = frozenset(
    {
        "status",
        "reload",
        "reboot",
        "tasks.list",
        "tasks.run",
        "tasks.set",
        "tasks.delete",
        "config.get",
        "config.set",
        "secret.set",
        "templates.list",
        "template.get",
        "geo.locate",
        "mqtt.publish",
        "mqtt.status",
        "fs.put",
        "sandbox.list",
        "sandbox.read",
        "sandbox.delete",
        "sandbox.write",
        "sandbox.mkdir",
        "sandbox.move",
        "sys.reset_counter",
    }
)

#: Members that carry a credential out of the host (`09` §3.2).
_SECRET_MEMBERS = {"hello": ("token",), "secret.set": ("value",)}

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_counters: dict[str, itertools.count[int]] = {}


class ProtocolError(Exception):
    """A frame that violates `04-protocol.md`. Carries a frozen error code."""

    def __init__(self, message: str, code: str = "E_PROTOCOL", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Frame:
    """One enveloped message.

    `body` holds the message-specific members only; the envelope is built from
    the dataclass fields. `ts` is supplied by the caller because a codec that
    read the clock could not be compared byte for byte in a golden test.
    """

    type: str
    id: str
    ts: int
    body: dict[str, Any]
    reply_to: str | None = None


def new_id(prefix: str) -> str:
    """Next frame id for `prefix` (`b` for the bridge, `d` for the device)."""
    counter = _counters.setdefault(prefix, itertools.count(1))
    return f"{prefix}-{next(counter)}"


def reset_ids() -> None:
    """Clear the counters. Test seam; production never calls it."""
    _counters.clear()


def _dumps(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ProtocolError(f"{what} is not 1-32 chars of [A-Za-z0-9._-]")
    return value


def _check_ts(value: Any) -> int:
    # bool is an int subclass and is never a valid timestamp.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError("ts is not an integer")
    return value


def encode(frame: Frame, *, max_bytes: int = MAX_FROM_BRIDGE_BYTES) -> bytes:
    """Serialise one frame to a single NDJSON line, terminating newline included.

    `max_bytes` defaults to the device's RX cap, which is what bounds a bridge line.
    A *device* double must pass `MAX_FROM_DEVICE_BYTES` instead: it is emitting as
    the device, and a reply the device may legally send would otherwise be refused
    by the bridge's own outgoing limit.
    """
    if frame.type not in ALL_TYPES:
        raise ProtocolError(f"unknown type: {frame.type!r}")
    _check_id(frame.id, "id")
    _check_ts(frame.ts)
    obj: dict[str, Any] = {
        "v": VERSION,
        "type": frame.type,
        "id": frame.id,
        "ts": frame.ts,
    }
    if frame.reply_to is not None:
        _check_id(frame.reply_to, "reply_to")
        obj["reply_to"] = frame.reply_to
    for key, value in frame.body.items():
        if key in ENVELOPE_KEYS:
            raise ProtocolError(f"body must not redefine envelope member {key!r}")
        obj[key] = value
    line = _dumps(obj) + b"\n"
    if len(line) > max_bytes:
        raise ProtocolError(f"frame is {len(line)} bytes, over the {max_bytes} cap")
    return line


def decode_line(line: bytes, *, expect: FrozenSet[str] | None = None) -> Frame:
    """Parse one line into a `Frame`.

    `expect` restricts the accepted direction, so a caller cannot accidentally
    admit a frame it has no handler for. A type outside `ALL_TYPES` is unknown
    rather than misdirected, and `04-protocol.md` §2.1 makes that distinction.
    """
    if line.endswith(b"\n"):
        line = line[:-1]
    if line.endswith(b"\r"):
        line = line[:-1]
    if len(line) + 1 > MAX_FROM_DEVICE_BYTES:
        raise ProtocolError(
            f"line is {len(line) + 1} bytes, over the {MAX_FROM_DEVICE_BYTES} cap"
        )
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"invalid UTF-8 at byte {exc.start}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not valid JSON at offset {exc.pos}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("frame is not a JSON object")

    if obj.get("v") != VERSION:
        raise ProtocolError(f"unsupported protocol version: {obj.get('v')!r}")
    frame_type = obj.get("type")
    if not isinstance(frame_type, str):
        raise ProtocolError("type is not a string")
    if frame_type not in ALL_TYPES:
        raise ProtocolError(f"unknown type: {frame_type!r}")
    if expect is not None and frame_type not in expect:
        raise ProtocolError(
            f"type {frame_type!r} is not allowed in this direction",
            detail={"direction": "device_to_bridge" if frame_type in DEVICE_TO_BRIDGE else "bridge_to_device"},
        )
    frame_id = _check_id(obj.get("id"), "id")
    ts = _check_ts(obj.get("ts"))
    reply_to = obj.get("reply_to")
    if reply_to is not None:
        _check_id(reply_to, "reply_to")
    body = {k: v for k, v in obj.items() if k not in ENVELOPE_KEYS}
    return Frame(type=frame_type, id=frame_id, ts=ts, body=body, reply_to=reply_to)


def redact_line(line: bytes) -> bytes:
    """Replace credential-bearing members in a line about to be logged.

    Called by `link` on the way out (`09` §3.2). It never raises: a line it
    cannot parse is returned unchanged, because the logging boundary must not
    turn a debug flag into a crash. A line with nothing to redact is returned
    byte for byte, so this is safe on every frame, not only the two that matter.
    """
    text = line[:-1] if line.endswith(b"\n") else line
    try:
        obj = json.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return line
    if not isinstance(obj, dict):
        return line
    members = _SECRET_MEMBERS.get(obj.get("type"))
    if not members:
        return line
    changed = False
    for member in members:
        if member in obj:
            obj[member] = REDACTED
            changed = True
    if not changed:
        return line
    return _dumps(obj) + (b"\n" if line.endswith(b"\n") else b"")


from .sandbox import ROOT as SANDBOX_ROOT  # noqa: E402
from .sandbox import SANDBOX_CHUNK_BYTES  # noqa: E402
from .sandbox import SandboxPathError  # noqa: E402
from .sandbox import display as sandbox_display  # noqa: E402
from .sandbox import join as sandbox_join  # noqa: E402
from .sandbox import normalise as sandbox_normalise  # noqa: E402
from .sandbox import parent as sandbox_parent  # noqa: E402

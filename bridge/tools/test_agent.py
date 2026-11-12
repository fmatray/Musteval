#!/usr/bin/env python3
"""Protocol conformance checker and echo. Specified by `docs/10-test-agent.md`.

Off-tree maintainer tool, not a layer (`09-bridge-architecture.md` §2). It imports
`bridge.protocol` (L0) and nothing above it, so it can never mask a bug in `link`
or `client` and never becomes a second codec.

It is not a device simulator: no config, NVS, WiFi, scheduler or agent loop. Every
reply is the one `04-protocol.md` §3 says the request gets.

Usage:
    python bridge/tools/test_agent.py [--target usb|test] [--port PATH]
                                      [--token T] [--fail-on TYPE[,TYPE]]
                                      [--delay-ms N] [--no-hello] [--script FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pty
import sys
import time
import tty
from pathlib import Path
from typing import Any, Iterable

import base64
import binascii

from bridge.protocol import (
    COMMAND_NAMES,
    MAX_FROM_BRIDGE_BYTES,
    MAX_FROM_DEVICE_BYTES,
    SANDBOX_ROOT,
    Frame,
    ProtocolError,
    SandboxPathError,
    decode_line,
    encode,
    new_id,
    sandbox_normalise,
)

DEVICE_NAME = "test-agent"
FW_VERSION = "test-1.0.0"
BOOT_ID = "00000001"
PROTOCOL_VERSION = 1
CANNED_TOOLS = ["time.now", "sys.diagnostics", "todo"]

#: `10-test-agent.md` §6.2. Two files, one nested directory, fixed bytes.
WORKSPACE_FILES = {
    "/workspace/notes.md": "# notes\n\nplain text, no markup.\n",
    "/workspace/runs/run_7.json": '{"run":"r-7","ok":true}',
}
WORKSPACE_DIRS = {
    "/workspace/": ["runs", "notes.md"],
    "/workspace/runs/": ["run_7.json"],
}

#: `10-test-agent.md` §6.1. Constant, schema-valid against `04` §3.9.
STATUS = {
    "wifi": {"state": "off", "ssid": None, "rssi": None, "ip": None, "failovers": 0},
    "time": {"synced": True, "unix": 1730563600, "tz": "UTC"},
    "llm": {
        "provider": "test",
        "model": "test-echo",
        "available": True,
        "last_error": None,
        "calls_hour": 0,
        "calls_day": 0,
        "limit_hour": None,
        "limit_day": None,
    },
    "mcp": [],
    "agent": {"busy": False, "run_id": None, "queue_depth": 0, "max_queue": 2},
    "mqtt": [],
    "geo": None,
    "storage": {
        "userfs": {"used_bytes": 0, "total_bytes": 3932160},
        "wsfs": {"used_bytes": 0, "total_bytes": 1048576},
        "workspace_quota_bytes": 65536,
    },
    "mem": {"heap_free": 0, "psram_free": 0},
    "sys": {"safe_mode": False, "boot_failures": 0, "tool_calls": 0, "tool_failures": 0},
    "uptime_s": 0,
    "boot_id": BOOT_ID,
}

#: `10-test-agent.md` §5, check 5.
REQUIRED_MEMBERS: dict[str, tuple[str, ...]] = {
    "hello": ("client", "client_version", "protocol"),
    "prompt": ("text",),
    "cancel": (),
    "command": ("name",),
    "secret.set": ("key", "value"),
    "ping": (),
    "ack": ("ack_of",),
}


def _err(message: str, code: str = "E_PROTOCOL", detail: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return error


class TestAgent:
    """The peer. Owns validation, replies and the terminal's raw byte stream."""

    def __init__(
        self,
        master_fd: int,
        *,
        token: str | None = None,
        fail_on: Iterable[str] = (),
        delay_ms: int = 0,
        no_hello: bool = False,
        script: list[dict[str, Any]] | None = None,
        stream: Any = None,
    ) -> None:
        self.fd = master_fd
        self.token = token
        self.fail_on = set(fail_on)
        self.delay_ms = delay_ms
        self.no_hello = no_hello
        self.script = script
        self.stream = stream or sys.stderr
        self.hello_seen = False
        self.run_id: str | None = None
        # The `todo` list the device keeps in `agent` RAM (`tools/todo.md` §State),
        # so a `done` is observable after the `init` that created the task.
        self._todo: list[dict[str, Any]] = []
        self._script_index = 0
        self._buf = bytearray()
        self._closed = False
        self._rejected = False

    # -- I/O ------------------------------------------------------------

    def emit(self, frame: Frame) -> None:
        # This tool is the device double, so its lines are bounded by the device's
        # TX cap, not by the bridge's outgoing limit.
        line = encode(frame, max_bytes=MAX_FROM_DEVICE_BYTES)
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        os.write(self.fd, line)

    def reply(self, frame_type: str, body: dict[str, Any], reply_to: str | None = None) -> None:
        self.emit(Frame(type=frame_type, id=new_id("d"), ts=int(time.time()), body=body, reply_to=reply_to))

    def log(self, message: str) -> None:
        print(message, file=self.stream, flush=True)

    # -- the loop -------------------------------------------------------

    def run(self) -> None:
        while not self._closed:
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            for line in self._consume(chunk):
                if not self._rejected:
                    self.handle(line)
            # After a rejection the agent keeps the pty open until the host
            # closes it. Exiting here would discard the still-buffered event,
            # and the host would see EOF before the refusal it was sent.

    def _consume(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        for byte in chunk:
            if byte == 0x0A:
                out.append(bytes(self._buf))
                self._buf.clear()
            else:
                self._buf.append(byte)
        return out

    def handle(self, line: bytes) -> None:
        """Climb the §5 ladder, then answer per §6."""
        command_name = ""

        # 1. size
        if len(line) + 1 > MAX_FROM_BRIDGE_BYTES:
            self._protocol_error(
                "line over the RX cap", f"line is {len(line) + 1} bytes", line, command_name
            )
            return

        # 2. UTF-8, then a single JSON object
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._protocol_error("invalid UTF-8", f"invalid UTF-8 at byte {exc.start}", line, command_name)
            return
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            self._protocol_error("not valid JSON", f"parse error at offset {exc.pos}", line, command_name)
            return
        if not isinstance(raw, dict):
            self._protocol_error("not a JSON object", "frame is not an object", line, command_name)
            return
        if isinstance(raw.get("name"), str):
            command_name = raw["name"]

        # 3. envelope
        if raw.get("v") != PROTOCOL_VERSION:
            self._protocol_error("bad envelope", f"v is {raw.get('v')!r}", line, command_name)
            return
        if not isinstance(raw.get("type"), str) or not isinstance(raw.get("id"), str):
            self._protocol_error("bad envelope", "type or id is not a string", line, command_name)
            return
        if not isinstance(raw.get("ts"), int) or isinstance(raw.get("ts"), bool):
            self._protocol_error("bad envelope", "ts is not an integer", line, command_name)
            return

        try:
            frame = decode_line(line)
        except ProtocolError as exc:
            self._protocol_error(str(exc), str(exc), line, command_name, reply_to=raw.get("id"))
            return

        # 4. direction
        if frame.type not in REQUIRED_MEMBERS:
            self._protocol_error(
                "wrong direction",
                f"type {frame.type!r} is not bridge to device",
                line,
                command_name,
                reply_to=frame.id,
                detail={"direction": "bridge_to_device"},
            )
            return

        # 5. required members
        missing = [
            member
            for member in REQUIRED_MEMBERS[frame.type]
            if member not in frame.body
            or (member == "token" and not frame.body.get(member))
        ]
        if frame.type == "hello":
            # `token` is required exactly when one is configured.
            has_token = bool(frame.body.get("token"))
            if (self.token is not None) != has_token:
                missing.append("token" if self.token is not None else "unexpected token")
        if missing:
            self._protocol_error(
                "missing required member",
                f"{frame.type} requires {', '.join(missing)}",
                line,
                command_name,
                reply_to=frame.id,
            )
            return

        # 6. semantics
        if frame.type == "secret.set" and not frame.body.get("value"):
            self._protocol_error(
                "empty secret value",
                "secret.set value is empty",
                line,
                command_name,
                reply_to=frame.id,
                code="E_CONFIG",
            )
            return
        if frame.type == "command":
            if frame.body["name"] not in COMMAND_NAMES:
                self._protocol_error(
                    "unknown command name",
                    f"unknown command name: {frame.body['name']!r}",
                    line,
                    command_name,
                    reply_to=frame.id,
                )
                return
        if frame.type == "hello" and self.token is not None:
            if frame.body.get("token") != self.token:
                self.log("link.rejected: bad token")
                self.reply("event", {"kind": "link.rejected", "data": {"reason": "auth"}})
                # Keep reading rather than returning: the event must reach the
                # peer before the pty closes, or the host sees EOF first and
                # reports a timeout where `05-bridge.md` §4.1 promises a refusal.
                self._rejected = True
                return

        # 7. duplicate hello
        if frame.type == "hello":
            if self.hello_seen:
                self._protocol_error(
                    "duplicate hello",
                    "hello arrived twice since the last reset",
                    line,
                    command_name,
                    reply_to=frame.id,
                )
                return
            self.hello_seen = True

        self.answer(frame)

    def _protocol_error(
        self,
        message: str,
        detail_message: str,
        line: bytes,
        command_name: str,
        *,
        reply_to: str | None = None,
        detail: Any = None,
        code: str = "E_PROTOCOL",
    ) -> None:
        """Two failure paths, both from `04` §1.2. There is no third."""
        self.log(f"E_PROTOCOL: {message}")
        error = _err(detail_message, code, detail or {"direction": "bridge_to_device"})
        if reply_to is None:
            reply_to = self._recover_id(line)
        if reply_to is None:
            self.reply(
                "event",
                {
                    "kind": "protocol.error",
                    "data": {
                        "code": code,
                        "message": detail_message,
                        "dropped_bytes": len(line),
                    },
                },
            )
            return
        self.reply(
            "command.result",
            {
                "name": command_name,
                "ok": False,
                "error": error,
                "duration_ms": 0,
            },
            reply_to=reply_to,
        )

    @staticmethod
    def _recover_id(line: bytes) -> str | None:
        """Best-effort id from a line that did not parse, per `04` §1.2."""
        marker = b'"id"'
        index = line.find(marker)
        if index < 0:
            return None
        rest = line[index + len(marker) :].lstrip()
        if not rest.startswith(b":"):
            return None
        rest = rest[1:].lstrip()
        if not rest.startswith(b'"'):
            return None
        end = rest.find(b'"', 1)
        if end < 0:
            return None
        try:
            return rest[1:end].decode("ascii")
        except UnicodeDecodeError:
            return None

    # -- §6 replies -----------------------------------------------------

    def answer(self, frame: Frame) -> None:
        if self.script is not None and self._run_script(frame):
            return
        if frame.type in self.fail_on:
            self._protocol_error(
                f"injected failure for {frame.type}",
                f"--fail-on {frame.type}",
                encode(frame, max_bytes=MAX_FROM_DEVICE_BYTES).rstrip(b"\n"),
                frame.body.get("name", ""),
                reply_to=frame.id,
            )
            return

        if frame.type == "hello":
            if self.no_hello:
                self.log("--no-hello: suppressing hello_ack")
                return
            self.reply("hello_ack", self._hello_ack_body(), reply_to=frame.id)
            return

        if frame.type == "ping":
            self.reply("pong", {}, reply_to=frame.id)
            return

        if frame.type == "ack":
            return

        if frame.type == "command":
            self._answer_command(frame)
            return

        if frame.type == "secret.set":
            self.reply(
                "command.result",
                {
                    "name": "secret.set",
                    "ok": True,
                    "result": {
                        "key": frame.body["key"],
                        "stored": True,
                        "length": len(frame.body["value"]),
                    },
                    "duration_ms": 0,
                },
                reply_to=frame.id,
            )
            return

        if frame.type == "prompt":
            self._answer_prompt(frame)
            return

        if frame.type == "cancel":
            if self.run_id is not None:
                run_id = self.run_id
                self.run_id = None
                self.reply(
                    "agent.error",
                    {
                        "code": "E_CANCELLED",
                        "message": "run cancelled by host",
                        "run_id": run_id,
                        "retryable": False,
                    },
                    reply_to=frame.id,
                )
            else:
                self.reply(
                    "event", {"kind": "cancel.noop", "data": {"reason": "user"}}
                )
            return

    def _hello_ack_body(self) -> dict[str, Any]:
        return {
            "device": DEVICE_NAME,
            "fw_version": FW_VERSION,
            "protocol": PROTOCOL_VERSION,
            "boot_id": BOOT_ID,
            "time_synced": True,
            "limits": {"rx_max": MAX_FROM_BRIDGE_BYTES, "tx_max": MAX_FROM_DEVICE_BYTES},
            "capabilities": {
                "tools": list(CANNED_TOOLS),
                "mcp": [],
                "dangerous_tools": False,
                "streaming": False,
            },
        }

    def _sandbox_result(self, name: str, args: dict) -> dict | str:
        """Answer `sandbox.list|read` from `10` §6.2, or name the error to send."""
        path = str(args.get("path", SANDBOX_ROOT))
        if path not in WORKSPACE_DIRS and path not in WORKSPACE_FILES:
            # Normalise before refusing: `/workspace/runs/../notes.md` is legal.
            try:
                path = sandbox_normalise(path)
            except SandboxPathError:
                return "E_CONFIG"
        if name == "sandbox.list":
            directory = path if path.endswith("/") else path + "/"
            if directory not in WORKSPACE_DIRS:
                return "E_STORAGE"
            entries = []
            for entry in WORKSPACE_DIRS[directory]:
                child = directory + entry
                if child + "/" in WORKSPACE_DIRS:
                    entries.append({"name": entry, "dir": True, "bytes": 0})
                else:
                    body = WORKSPACE_FILES.get(child, "")
                    entries.append({"name": entry, "dir": False, "bytes": len(body)})
            return {"path": directory, "entries": entries}
        if path not in WORKSPACE_FILES:
            return "E_STORAGE"
        body = WORKSPACE_FILES[path]
        return {"path": path, "text": body, "bytes": len(body), "truncated": False}

    def _answer_command(self, frame: Frame) -> None:
        name = frame.body["name"]
        args = frame.body.get("args") or {}
        if name in ("sandbox.list", "sandbox.read"):
            result = self._sandbox_result(name, args)
            if isinstance(result, str):
                self.reply(
                    "command.result",
                    {"name": name, "ok": False,
                     "error": {"code": result, "message": "outside the sandbox"
                               if result == "E_CONFIG" else "no such entry"},
                     "duration_ms": 0},
                    reply_to=frame.id,
                )
                return
            self.reply(
                "command.result",
                {"name": name, "ok": True, "result": result, "duration_ms": 0},
                reply_to=frame.id,
            )
            return
        if name == "sandbox.delete":
            # `04` §4.19: a populated directory without `recursive` is refused, and
            # the error names the entries so the bridge can count them.
            path = str(args.get("path", ""))
            directory = path if path.endswith("/") else path + "/"
            entries = WORKSPACE_DIRS.get(directory)
            if entries and not args.get("recursive"):
                self.reply(
                    "command.result",
                    {"name": name, "ok": False,
                     "error": {"code": "E_CONFIG",
                               "message": "directory is not empty",
                               "detail": {"path": path, "entries": len(entries)}},
                     "duration_ms": 0},
                    reply_to=frame.id,
                )
                return
            result = {
                "path": path,
                "deleted": True,
                "entries": len(entries) if entries else 0,
            }
        elif name == "sandbox.mkdir":
            result = {"path": args.get("path"), "created": True}
        elif name == "sandbox.move":
            result = {"from": args.get("from"), "to": args.get("to"), "moved": True}
        elif name == "sys.reset_counter":
            # `04` §4.23: `previous` is what the reader can verify against `status`.
            result = {"reset": "boot_failures", "previous": 0, "safe_mode": False}
        elif name == "sandbox.write":
            # The device decodes the chunk, so the tool does too: a client that
            # sent raw text where base64 was owed would otherwise look correct.
            raw = str(args.get("data", ""))
            try:
                chunk = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                self.reply(
                    "command.result",
                    {"name": name, "ok": False,
                     "error": {"code": "E_PROTOCOL", "message": "data is not base64"},
                     "duration_ms": 0},
                    reply_to=frame.id,
                )
                return
            result = {
                "path": args.get("path"),
                "created": args.get("offset", 0) == 0,
                "bytes": len(chunk),
            }
        elif name in ("wifi.status", "wifi.connect", "wifi.disconnect", "wifi.reconnect"):
            # `10-test-agent.md` §6: the §3.9 object, and the state each verb leaves
            # behind, so a bridge assertion distinguishes them instead of reading the
            # same echo four times.
            down = name == "wifi.disconnect"
            result = {
                "state": "off" if down else "connected",
                "ssid": None if down else "HomeLab",
                "rssi": 0 if down else -47,
                "ip": None if down else "192.168.1.42",
                # A join that succeeded has failed over zero times; the counter is the
                # boot sequence's, and a verb that invented one would let a bridge test
                # pass against a device that never does.
                "failovers": 0,
            }
            self.reply(
                "command.result",
                {"name": name, "ok": True, "result": result, "duration_ms": 0},
                reply_to=frame.id,
            )
            return
        elif name == "todo.apply":
            # `10-test-agent.md` §6: a list the double actually holds, so `done` after
            # `start` is observable. An echo of `args` would let a broken bridge pass:
            # every assertion would read its own request back.
            self.reply(
                "command.result",
                {
                    "name": name,
                    "ok": True,
                    "result": self._todo_apply(args),
                    "duration_ms": 0,
                },
                reply_to=frame.id,
            )
            return
        elif name == "wifi.scan":
            # `10-test-agent.md` §6: an echo would give the dropdown `args` and no
            # `networks`, which is the failure it shows against a real device.
            result = {
                "networks": [
                    {"ssid": "HomeLab", "rssi": -47, "auth": "wpa2"},
                    {"ssid": "Cafe-FREE", "rssi": -71, "auth": "open"},
                    {"ssid": "OldRouter", "rssi": -83, "auth": "wep"},
                ],
                "truncated": False,
            }
            self.reply(
                "command.result",
                {"name": name, "ok": True, "result": result, "duration_ms": 0},
                reply_to=frame.id,
            )
            return

        else:
            result = dict(STATUS) if name == "status" else {"echo": args}
        self.reply(
            "command.result",
            {"name": name, "ok": True, "result": result, "duration_ms": 0},
            reply_to=frame.id,
        )

    def _todo_apply(self, args: dict) -> dict:
        """`tools/todo.md` §Operations, enough of it to be worth asserting against.

        The double keeps the tree so a `done` is visible in the reply after the
        `init` that created it. Bounds and the all-or-nothing rule belong to the
        firmware and are proven on target; what is proven here is that the bridge
        sends the op and renders the answer.
        """
        op = args.get("op", "view")
        target = args.get("task")
        if op == "init":
            self._todo = [{"name": args.get("phase") or "Tasks", "tasks": []}]
            for item in args.get("items") or []:
                self._todo[0]["tasks"].append({"content": item, "status": "pending"})
        elif op == "append":
            self._todo.append(
                {
                    "name": args.get("phase") or "Tasks",
                    "tasks": [
                        {"content": item, "status": "pending"}
                        for item in args.get("items") or []
                    ],
                }
            )
        elif op in ("start", "done", "drop", "rm", "block", "unblock"):
            status = {"start": "in_progress", "done": "completed", "drop": "abandoned"}.get(op)
            for phase in self._todo:
                kept = []
                for task in phase["tasks"]:
                    hit = target is None or target in (task["content"], phase["name"])
                    if hit and op == "rm":
                        continue
                    if hit and status:
                        task["status"] = status
                    if hit and op == "block" and task["status"] not in ("completed", "abandoned"):
                        # `tools/todo.md` §Operations: `block` leaves a finished task alone.
                        task["status"] = "blocked"
                        task["blocker"] = args.get("reason") or "blocked"
                    if hit and op == "unblock":
                        task["status"] = "pending"
                        task.pop("blocker", None)
                    kept.append(task)
                phase["tasks"] = kept
        return {"phases": self._todo}

    def _answer_prompt(self, frame: Frame) -> None:
        text = frame.body["text"]
        self.run_id = new_id("r")
        self.reply(
            "agent.start",
            {
                "run_id": self.run_id,
                "trigger": "manual",
                "source": "prompt",
                "provider": "test",
                "model": "test-echo",
                "input_bytes": len(text.encode("utf-8")),
                "queue_depth": 0,
            },
            reply_to=frame.id,
        )
        self.reply(
            "agent.final",
            {
                "run_id": self.run_id,
                "text": text,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "rounds": 1,
                "provider": "test",
                "model": "test-echo",
                "duration_ms": 0,
                "truncated": False,
                "todo_open": False,
            },
            reply_to=frame.id,
        )
        self.run_id = None

    # -- --script -------------------------------------------------------

    def _run_script(self, frame: Frame) -> bool:
        """Send the next scripted frame in place of the table reply."""
        if self._script_index >= len(self.script or []):
            return False
        entry = self.script[self._script_index]
        self._script_index += 1
        if "burst" in entry:
            # One inbound frame, many outbound frames. A run is exactly that: a
            # prompt is answered by `agent.start`, then `agent.tool`, then
            # `agent.tool_result`, then `agent.final`. Without a group shape the
            # per-frame consumption rule makes that unreachable, which is the gap
            # `10-test-agent.md` §7 always claimed was closed.
            for item in entry["burst"]:
                self._emit_entry(item, frame)
            return True
        self._emit_entry(entry, frame)
        return True

    def _emit_entry(self, entry: dict[str, Any], frame: Frame) -> None:
        """Emit one script entry, completing its envelope."""
        if "burst" in entry:
            # `10-test-agent.md` §7: a burst does not nest. Say so rather than
            # failing later on a missing `type`.
            raise SystemExit("a script burst may not contain another burst")
        if "wait_ms" in entry:
            time.sleep(entry["wait_ms"] / 1000)
            return
        if entry.get("type") == "raw":
            os.write(self.fd, entry["data"].encode("utf-8"))
            return
        body = {k: v for k, v in entry.items() if k not in ("type", "reply_to")}
        reply_to = frame.id if entry.get("reply_to", "auto") == "auto" else entry.get("reply_to")
        self.reply(entry["type"], body, reply_to=reply_to)


def _load_script(path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{number}: {exc}") from None
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Musteval protocol conformance checker")
    parser.add_argument("--target", choices=("usb", "test"), default="test")
    parser.add_argument("--port", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--fail-on", default="")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--no-hello", action="store_true")
    parser.add_argument("--script", default=None)
    args = parser.parse_args(argv)

    fail_on = [t.strip() for t in args.fail_on.split(",") if t.strip()]
    script = _load_script(args.script) if args.script else None

    master_fd, slave_fd = pty.openpty()
    tty.setraw(master_fd)
    slave_path = os.ttyname(slave_fd)
    # Only NDJSON crosses the pty; the path and diagnostics go to stderr.
    print(slave_path, file=sys.stdout, flush=True)
    print(f"test-agent listening on {slave_path}", file=sys.stderr, flush=True)

    agent = TestAgent(
        master_fd,
        token=args.token,
        fail_on=fail_on,
        delay_ms=args.delay_ms,
        no_hello=args.no_hello,
        script=script,
    )
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    finally:
        os.close(master_fd)
        os.close(slave_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

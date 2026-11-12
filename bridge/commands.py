"""L2 leaf: slash-command string -> `list[Frame]`.

Pure, importing `protocol` only (`09-bridge-architecture.md` §4.4). It stores
nothing, so every value it needs from cached device state arrives in
`CommandState` and a stale cache produces a frame that re-creates a task rather
than a silent no-op. `(Local)` rows are not here: they render state the bridge
already holds and send no frame, so they belong to `ui`.
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    COMMAND_NAMES,
    SANDBOX_ROOT,
    Frame,
    SandboxPathError,
    new_id,
    sandbox_normalise,
)

#: `05-bridge.md` §7 marks these `(Local)`: no frame leaves the host.
LOCAL_COMMANDS = frozenset(
    {
        "tools",
        "mcp",
        "usage",
        "context",
        "todo",
        "settings",
        "export",
        "quit",
        "logs",
        "help",
        "files",
        "onboard",
    }
)

#: A skill may be 8 KiB, so `/fs put` splits it under the 4 KiB RX cap.
FS_PUT_CHUNK_BYTES = 2048


class CommandError(Exception):
    """Malformed slash command. Reported to the user, never sent to the device."""


@dataclass
class CommandState:
    """Everything `parse` may need that the bridge learned from the device."""

    tasks: list[dict[str, Any]] = field(default_factory=list)
    last_prompt: str | None = None
    secret_value: str | None = None
    upload_bytes: bytes | None = None
    upload_path: str | None = None


def _command(name: str, args: dict[str, Any] | None = None) -> Frame:
    assert name in COMMAND_NAMES, name
    return Frame(
        type="command", id=new_id("b"), ts=0, body={"name": name, "args": args or {}}
    )


def _find_task(state: CommandState, task_id: str) -> dict[str, Any]:
    for task in state.tasks:
        if task.get("id") == task_id:
            return dict(task)
    raise CommandError(f"unknown task id: {task_id}")


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise CommandError(f"unbalanced quotes: {exc}") from None


def _require(argv: list[str], count: int, usage: str) -> None:
    if len(argv) < count:
        raise CommandError(f"usage: {usage}")


def parse(text: str, state: CommandState | None = None) -> list[Frame]:
    """Map one slash-command line to the frames it sends.

    Returns `[]` for a `(Local)` command, which the caller renders itself.
    """
    state = state or CommandState()
    text = text.strip()
    if not text.startswith("/"):
        raise CommandError("not a slash command")
    argv = _split(text[1:])
    if not argv:
        raise CommandError("empty command")
    head, rest = argv[0], argv[1:]

    if head in LOCAL_COMMANDS:
        return []

    if head == "status":
        return [_command("status")]
    if head == "reload":
        return [_command("reload")]
    if head == "reboot":
        return [_command("reboot")]
    if head == "templates":
        return [_command("templates.list")]
    if head == "template":
        _require(rest, 1, "/template <name>")
        return [_command("template.get", {"name": rest[0]})]
    if head == "mqtt":
        if not rest:
            return [_command("mqtt.status")]
        if rest[0] != "publish":
            raise CommandError("usage: /mqtt [publish <srv> <topic> <payload>]")
        if len(rest) < 4:
            raise CommandError("usage: /mqtt publish <srv> <topic> <payload>")
        return [
            _command(
                "mqtt.publish",
                {"server_id": rest[1], "topic": rest[2], "payload": " ".join(rest[3:])},
            )
        ]
    if head == "geo":
        return [_command("geo.locate")]
    if head == "wifi":
        # `05-bridge.md` §7: `scan` asks the radio, the other four ask the station.
        if rest == ["scan"]:
            return [_command("wifi.scan")]
        if rest in (["status"], ["connect"], ["disconnect"], ["reconnect"]):
            return [_command(f"wifi.{rest[0]}")]
        raise CommandError("usage: /wifi scan|status|connect|disconnect|reconnect")
    if head == "ping":
        # `05-bridge.md` §7: `device` is the link-latency ping the bridge already
        # measured; the other three make the device run an ICMP round itself.
        # Bare `/ping` used to mean `device`, which is the guess this removes.
        if rest == ["device"]:
            return [Frame(type="ping", id=new_id("b"), ts=0, body={})]
        if len(rest) == 1 and rest[0] in ("router", "llm", "ntp"):
            return [_command("net.ping", {"target": rest[0]})]
        raise CommandError("usage: /ping device|router|llm|ntp")
    if head == "cancel":
        return [Frame(type="cancel", id=new_id("b"), ts=0, body={"reason": "user"})]
    if head == "retry":
        if not state.last_prompt:
            raise CommandError("no prompt to retry")
        return [
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": state.last_prompt})
        ]
    if head == "diag":
        return [_command("status")]
    if head == "secret":
        if len(rest) < 2 or rest[0] != "set":
            raise CommandError("usage: /secret set <key>")
        value = state.secret_value
        if not value:
            raise CommandError("no secret value supplied")
        return [
            Frame(
                type="secret.set", id=new_id("b"), ts=0, body={"key": rest[1], "value": value}
            )
        ]
    if head == "config":
        if len(rest) < 2:
            raise CommandError("usage: /config get <path> | /config set <path> <value>")
        if rest[0] == "get":
            return [_command("config.get", {"path": rest[1]})]
        if rest[0] == "set":
            if len(rest) < 3:
                raise CommandError("usage: /config set <path> <value>")
            return [_command("config.set", {"path": rest[1], "value": " ".join(rest[2:])})]
        raise CommandError("usage: /config get <path> | /config set <path> <value>")
    if head == "run":
        _require(rest, 1, "/run <id>")
        return [_command("tasks.run", {"id": rest[0]})]
    if head == "fs":
        if len(rest) < 2 or rest[0] != "put":
            raise CommandError("usage: /fs put <path>")
        return _fs_put(rest[1], state)
    if head == "sandbox":
        return _sandbox(rest)
    if head == "tasks":
        return _tasks(rest, state)
    if head == "sys":
        if rest[:1] != ["reset-counter"]:
            raise CommandError("usage: /sys reset-counter")
        return [_command("sys.reset_counter")]

    raise CommandError(f"unknown command: /{head}")


def _sandbox_path(path: str) -> str:
    """Refuse a path outside the sandbox here, so it costs no round trip."""
    try:
        return sandbox_normalise(path)
    except SandboxPathError as exc:
        raise CommandError(str(exc)) from exc


def _sandbox(rest: list[str]) -> list[Frame]:
    """`/sandbox list|read|mkdir|move|delete <path> [-r]` — the Files screen's non-interactive form."""
    if not rest:
        return [_command("sandbox.list", {"path": SANDBOX_ROOT})]
    sub, args = rest[0], rest[1:]
    if sub == "list":
        return [_command("sandbox.list", {"path": _sandbox_path(args[0] if args else SANDBOX_ROOT)})]
    if sub == "read":
        _require(args, 1, "/sandbox read <path>")
        return [_command("sandbox.read", {"path": _sandbox_path(args[0])})]
    if sub in ("delete", "rm"):
        recursive = False
        positional = []
        for a in args:
            if a in ("-r", "--recursive"):
                recursive = True
            elif a.startswith("-"):
                raise CommandError(f"unknown option: {a}")
            else:
                positional.append(a)
        _require(positional, 1, "/sandbox delete [-r] <path>")
        body = {"path": _sandbox_path(positional[0])}
        if recursive:
            body["recursive"] = True
        return [_command("sandbox.delete", body)]
    if sub == "mkdir":
        _require(args, 1, "/sandbox mkdir <path>")
        return [_command("sandbox.mkdir", {"path": _sandbox_path(args[0])})]
    if sub == "move":
        _require(args, 2, "/sandbox move <from> <to>")
        return [_command("sandbox.move", {"from": _sandbox_path(args[0]), "to": _sandbox_path(args[1])})]
    raise CommandError("usage: /sandbox list|read|mkdir|move|delete <path>")


def _tasks(rest: list[str], state: CommandState) -> list[Frame]:
    if not rest:
        return [_command("tasks.list")]
    sub = rest[0]
    if sub == "delete":
        _require(rest[1:], 1, "/tasks delete <id>")
        return [_command("tasks.delete", {"id": rest[1]})]
    if sub in ("enable", "disable"):
        _require(rest[1:], 1, f"/tasks {sub} <id>")
        task = _find_task(state, rest[1])
        task["enabled"] = sub == "enable"
        return [_command("tasks.set", {"task": task})]
    if sub == "set":
        _require(rest[1:], 1, "/tasks set <id>")
        return [_command("tasks.set", {"task": _find_task(state, rest[1])})]
    if sub == "add":
        raise CommandError("a task body must be supplied (add opens a modal)")
    raise CommandError(f"unknown subcommand: /tasks {sub}")


def _fs_put(path: str, state: CommandState) -> list[Frame]:
    if state.upload_bytes is None:
        raise CommandError("no file loaded to upload")
    if not path.startswith("/"):
        raise CommandError(f"path must be absolute: {path}")
    chunks = [
        state.upload_bytes[i : i + FS_PUT_CHUNK_BYTES]
        for i in range(0, len(state.upload_bytes), FS_PUT_CHUNK_BYTES)
    ] or [b""]
    frames: list[Frame] = []
    for index, chunk in enumerate(chunks):
        args = {
            "path": path,
            "data": base64.b64encode(chunk).decode("ascii"),
            "encoding": "base64",
            "offset": index * FS_PUT_CHUNK_BYTES,
            "final": index == len(chunks) - 1,
        }
        frames.append(_command("fs.put", args))
    return frames

"""L2 leaf: slash-command string -> `list[Frame]`.

Pure, importing `protocol` only (`09-bridge-architecture.md` §4.4). It stores
nothing, so every value it needs from cached device state arrives in
`CommandState` and a stale cache produces a frame that re-creates a task rather
than a silent no-op. `(Local)` rows are not here: they render state the bridge
already holds and send no frame, so they belong to `ui`.
"""

from __future__ import annotations

import base64
import copy
import shlex
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    COMMAND_NAMES,
    SANDBOX_ROOT,
    Frame,
    SandboxPathError,
    new_id,
    userfs_import_ok,
    sandbox_normalise,
)

#: `05-bridge.md` §7 marks these `(Local)`: no frame leaves the host.
LOCAL_COMMANDS = frozenset(
    {
        "mcp",
        "usage",
        "export",
        "quit",
        "logs",
        "help",
        "files",
        "onboard",
    }
)

#: `05-bridge.md` §7 and §7.6: the verbs `/wifi` accepts, in the order the refusal
#: names them. `list` and `edit` are intercepted by the screen and only reach this
#: arm on the `--send` path, but they are still verbs of the command.
WIFI_VERBS = (
    "scan",
    "status",
    "connect",
    "disconnect",
    "reconnect",
    "list",
    "edit",
)

#: `tools/todo.md` §Operations, in the order that fence lists them. `/todo` accepts
#: exactly these and nothing else: a verb the tool has no op for has no meaning.
TODO_OPS = (
    "init",
    "start",
    "done",
    "drop",
    "block",
    "unblock",
    "rm",
    "append",
    "view",
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
    #: The bytes `/fs put` sends. Read once, chunked by `_fs_put`: the device's RX
    #: line is 4 KiB, so an 8 KiB skill is several frames.
    upload_bytes: bytes | None = None


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


def _exact(argv: list[str], count: int, usage: str) -> None:
    """A fixed-arity form: a surplus word is as wrong as a missing one."""
    if len(argv) != count:
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

    if head == "tools":
        # `05` §7.4: the same table as `/tool list`. The UI intercepts both so the
        # write verbs can read `config.get` first; this arm keeps `parse` honest for
        # the read form and for `--send /tools` on the command line.
        _exact(rest, 0, "/tools")
        return [_command("tools.list")]
    if head == "context":
        # `05` §7.5: the block sizes come from the device, because the prompt, the tool
        # array and the history are all device state. A host-side estimate would be a
        # number the reader might act on and the device would contradict.
        _exact(rest, 0, "/context")
        return [_command("context.budget")]
    if head == "compact":
        # `05` §7.5: the summary is written by the device's model, because the API key
        # is in its NVS and the conversation is its state. `keep` is the one argument
        # and it is optional.
        # `keep` is optional, so this is an arity range and not a fixed one: `_exact`
        # would make `/compact` alone a usage error.
        if len(rest) > 1:
            raise CommandError("usage: /compact [keep]")
        keep = 0
        if rest:
            try:
                keep = int(rest[0])
            except ValueError:
                raise CommandError("/compact [keep]: keep must be a number") from None
            if keep < 0 or keep > 8:
                raise CommandError("/compact [keep]: keep must be 0 to 8")
        return [_command("context.compact", {"keep": keep} if keep else {})]
    if head == "templates":
        _exact(rest, 0, "/templates")
        return [_command("templates.list")]
    if head == "template":
        _exact(rest, 1, "/template <name>")
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
        _exact(rest, 0, "/geo")
        return [_command("geo.locate")]
    if head == "wifi":
        # `05-bridge.md` §7: `scan` asks the radio, the four steer verbs ask the
        # station, and `list`/`edit` are UI commands (§7.6) that the screen
        # intercepts. This arm keeps `parse` honest for the `--send` path.
        if rest == ["scan"]:
            return [_command("wifi.scan")]
        if rest in (["status"], ["connect"], ["disconnect"], ["reconnect"]):
            return [_command(f"wifi.{rest[0]}")]
        if rest == ["list"]:
            return [_command("wifi.status")]
        if rest == ["edit"]:
            return [_command("config.get")]
        # The usage text is derived, not spelled again: a verb added above without a
        # word here would make the refusal name a set the parser does not accept.
        raise CommandError("usage: /wifi " + "|".join(WIFI_VERBS))
    if head == "providers":
        # `05` §7.7: the editor is a modal, but the read it opens with is one
        # `config.get`, so the `--send` path still produces a frame.
        _exact(rest, 0, "/providers")
        return [_command("config.get")]
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
        _exact(rest, 0, "/cancel")
        return [Frame(type="cancel", id=new_id("b"), ts=0, body={"reason": "user"})]
    if head == "clear":
        # `05` §7: the pane is wiped by `ui`, after this reply says the device forgot
        # the conversation too. `04` §4.28 refuses mid-run, and then nothing is wiped.
        _exact(rest, 0, "/clear")
        return [_command("history.clear")]
    if head == "retry":
        _exact(rest, 0, "/retry")
        if not state.last_prompt:
            raise CommandError("no prompt to retry")
        return [
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": state.last_prompt})
        ]
    if head == "secret":
        if rest[:1] == ["delete"]:
            # `04` §4.31: erasure is its own verb, never a blank write. No value is
            # read, so this form needs nothing from the prompt.
            _exact(rest[1:], 1, "/secret delete <key>")
            return [_command("secret.delete", {"key": rest[1]})]
        if not rest or rest[0] != "set":
            raise CommandError("usage: /secret set <key> | /secret delete <key>")
        _exact(rest[1:], 1, "/secret set <key>")
        value = state.secret_value
        if not value:
            raise CommandError("no secret value supplied")
        return [
            Frame(
                type="secret.set", id=new_id("b"), ts=0, body={"key": rest[1], "value": value}
            )
        ]
    if head == "config":
        if not rest or rest[0] == "edit":
            # `05` §5.7: both open the settings screen, which a pure function cannot
            # raise; by the time `parse` sees one the chat path did not intercept it.
            raise CommandError(
                "nothing to send (bare /config and /config edit open the settings screen)"
            )
        if rest[0] == "get":
            _exact(rest[1:], 1, "/config get <path>")
            return [_command("config.get", {"path": rest[1]})]
        if rest[0] == "set":
            _require(rest[1:], 2, "/config set <path> <value>")
            return [_command("config.set", {"path": rest[1], "value": " ".join(rest[2:])})]
        raise CommandError("usage: /config get <path> | /config set <path> <value>")
    if head == "run":
        _exact(rest, 1, "/run <id>")
        return [_command("tasks.run", {"id": rest[0]})]
    if head == "fs":
        if not rest or rest[0] != "put":
            raise CommandError("usage: /fs put <path>")
        _exact(rest[1:], 1, "/fs put <path>")
        return _fs_put(rest[1], state)
    if head == "sandbox":
        return _sandbox(rest)
    if head == "todo":
        return _todo(rest)
    if head == "tasks":
        return _tasks(rest, state)
    if head == "sys":
        # `05-bridge.md` §7: one head, four of its verbs are the former top-level
        # commands. `/sys status` and `/sys diag` share the `status` frame because
        # they read the same object; the UI tells them apart by the verb.
        if rest[:1] == ["reset-counter"]:
            _exact(rest[1:], 0, "/sys reset-counter")
            return [_command("sys.reset_counter")]
        if rest[:1] == ["status"]:
            _exact(rest[1:], 0, "/sys status")
            return [_command("status")]
        if rest[:1] == ["diag"]:
            _exact(rest[1:], 0, "/sys diag")
            return [_command("status")]
        if rest[:1] == ["reload"]:
            _exact(rest[1:], 0, "/sys reload")
            return [_command("reload")]
        if rest[:1] == ["reboot"]:
            _exact(rest[1:], 0, "/sys reboot")
            return [_command("reboot")]
        raise CommandError("usage: /sys status|diag|reload|reboot|reset-counter")

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
        _exact(args, 1 if args else 0, "/sandbox list [<path>]")
        return [_command("sandbox.list", {"path": _sandbox_path(args[0] if args else SANDBOX_ROOT)})]
    if sub == "read":
        _exact(args, 1, "/sandbox read <path>")
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
        _exact(positional, 1, "/sandbox delete [-r] <path>")
        body = {"path": _sandbox_path(positional[0])}
        if recursive:
            body["recursive"] = True
        return [_command("sandbox.delete", body)]
    if sub == "mkdir":
        _exact(args, 1, "/sandbox mkdir <path>")
        return [_command("sandbox.mkdir", {"path": _sandbox_path(args[0])})]
    if sub == "move":
        _exact(args, 2, "/sandbox move <from> <to>")
        return [_command("sandbox.move", {"from": _sandbox_path(args[0]), "to": _sandbox_path(args[1])})]
    raise CommandError("usage: /sandbox list|read|mkdir|move|delete <path>")


def _todo(rest: list[str]) -> list[Frame]:
    """`05-bridge.md` §7: `/todo` is the `todo` tool's op set over the wire.

    Every verb here is a `tools/todo.md` §Operations op with the same name, so the
    command cannot drift from the tool the model calls. On the verbs that take one
    target, that target is sent as both `task` and `phase`, and `todo_apply`'s own
    `task → phase` chain decides which one matched. A task name wins when one
    exists; otherwise a phase matches every task in it. Sending only `task` would
    make `/todo done auth` fail as an unknown task, and resolving it here would be
    a second opinion of what a target means.

    `append` and `init` both take a phase then their items: the device refuses a
    phase-less `init` (`tools/todo.md` §Errors) and refuses `append` the same way,
    so the command asks for the same argument the tool does. Bare `init` clears.
    The nested `list[]` `init` form stays model-only: flat `init` plus `append`
    reaches any multi-phase tree, so the command needs no second spelling for it.
    A required argument is refused here, so a malformed op costs no round trip.
    """
    if not rest:
        return [_command("todo.apply", {"op": "view"})]
    op = rest[0]
    if op not in TODO_OPS:
        raise CommandError(f"usage: /todo [{'|'.join(TODO_OPS)}] [target]")
    args: dict[str, Any] = {"op": op}
    tail = rest[1:]
    if op == "view":
        # A trailing word on a read-only op would be dropped silently, and a dropped
        # argument is how `/todo append auth` looked correct while sending nothing.
        if tail:
            raise CommandError("view takes no target: /todo [view]")
        return [_command("todo.apply", args)]
    if op == "append":
        if not tail:
            raise CommandError("append needs a phase: /todo append <phase> <item> [item...]")
        if len(tail) < 2:
            raise CommandError(
                "append needs at least one item: /todo append <phase> <item> [item...]"
            )
        args["phase"] = tail[0]
        args["items"] = tail[1:]
    elif op == "init":
        # Bare `init` clears the list. With items, the phase is required rather
        # than defaulted, so `/todo init auth "a" "b"` cannot silently land in a
        # phase the caller never named.
        if tail:
            _require(tail, 2, "/todo init <phase> <item> [item...]")
            args["phase"] = tail[0]
            args["items"] = tail[1:]
    elif op == "start":
        _require(tail, 1, "/todo start <task>")
        args["task"] = " ".join(tail)
    else:
        target = " ".join(tail)
        if target:
            args["task"] = target
            args["phase"] = target
    return [_command("todo.apply", args)]


#: `/todo edit`'s verbs. Each is a local rewrite of the tree followed by one
#: `init`, because no device op moves, renames or inserts at a position.
TODO_EDIT_OPS = (
    "add-task",
    "modify-task",
    "delete-task",
    "move-task",
    "add-phase",
    "modify-phase",
    "delete-phase",
    "move-phase",
)


def _todo_list(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The nested `init` form. `status` rides on every item, so a rewrite keeps it."""
    out = []
    for phase in phases:
        items = []
        # A phase the device renders always carries `tasks`, but a tree the caller
        # built by hand may not, and an empty list is the meaning either way.
        for task in phase.get("tasks") or []:
            item: dict[str, Any] = {
                "content": str(task.get("content") or ""),
                "status": str(task.get("status") or "pending"),
            }
            if item["status"] == "blocked" and task.get("blocker"):
                item["blocker"] = str(task["blocker"])
            items.append(item)
        out.append({"phase": str(phase.get("name") or ""), "items": items})
    return out


def _phase_at(phases: list[dict[str, Any]], name: str) -> int:
    for i, phase in enumerate(phases):
        if phase.get("name") == name:
            return i
    raise CommandError(f"unknown phase: {name or '(none)'}")


def _task_at(phases: list[dict[str, Any]], content: str) -> tuple[int, int]:
    for i, phase in enumerate(phases):
        for j, task in enumerate(phase.get("tasks") or []):
            if task.get("content") == content:
                return i, j
    raise CommandError(f"unknown task: {content or '(none)'}")


def _contents(phases: list[dict[str, Any]]) -> list[str]:
    return [
        task["content"] for phase in phases for task in phase.get("tasks") or []
    ]


def _step(where: str, op: str) -> int:
    if where == "up":
        return -1
    if where == "down":
        return 1
    raise CommandError(f"{op} needs where up|down, not {where or '(none)'}")


def _insert_where(where: str, op: str) -> int:
    if where == "before":
        return 0
    if where == "after":
        return 1
    raise CommandError(f"{op} needs where before|after, not {where or '(none)'}")


def todo_edit_tree(
    phases: list[dict[str, Any]],
    *,
    op: str,
    phase: str = "",
    task: str = "",
    value: str = "",
    where: str = "after",
) -> list[dict[str, Any]]:
    """`05-bridge.md` §7 `/todo edit`: one verb applied to a copy of the tree.

    The editor calls this once per keystroke and keeps the result as what it shows,
    so the rows on screen and the frame that ships come from the same function.
    Refusals name the argument at fault and raise, leaving the caller's tree alone.
    """
    if op not in TODO_EDIT_OPS:
        raise CommandError(f"unknown todo edit op: {op or '(none)'}")
    tree = copy.deepcopy(phases)
    value = value.strip()

    if op == "add-task":
        at = _phase_at(tree, phase)
        bias = _insert_where(where, op)
        if not value:
            raise CommandError("add-task needs a value: the new content")
        if value in _contents(tree):
            raise CommandError(f"duplicate content: {value}")
        tasks = tree[at].setdefault("tasks", [])
        pos = len(tasks)
        if task:
            pos = next((k for k, t in enumerate(tasks) if t.get("content") == task), None)
            if pos is None:
                raise CommandError(f"unknown task: {task}")
            pos += bias
        tasks.insert(pos, {"content": value, "status": "pending"})
    elif op == "add-phase":
        bias = _insert_where(where, op)
        if not value:
            raise CommandError("add-phase needs a value: the new phase name")
        if any(p.get("name") == value for p in tree):
            raise CommandError(f"duplicate phase name: {value}")
        pos = len(tree)
        if phase:
            pos = _phase_at(tree, phase) + bias
        tree.insert(pos, {"name": value, "tasks": []})
    elif op == "modify-task":
        if not task:
            raise CommandError("modify-task needs a task: the content to rewrite")
        if not value:
            raise CommandError("modify-task needs a value: the new content")
        pi, ti = _task_at(tree, task)
        if value != task and value in _contents(tree):
            raise CommandError(f"duplicate content: {value}")
        tree[pi]["tasks"][ti]["content"] = value
    elif op == "modify-phase":
        at = _phase_at(tree, phase)
        if not value:
            raise CommandError("modify-phase needs a value: the new name")
        if value != phase and any(p.get("name") == value for p in tree):
            raise CommandError(f"duplicate phase name: {value}")
        tree[at]["name"] = value
    elif op == "delete-task":
        pi, ti = _task_at(tree, task)
        del tree[pi]["tasks"][ti]
    elif op == "delete-phase":
        del tree[_phase_at(tree, phase)]
    elif op == "move-task":
        pi, ti = _task_at(tree, task)
        tasks = tree[pi].setdefault("tasks", [])
        tj = ti + _step(where, op)
        if 0 <= tj < len(tasks):
            tasks[ti], tasks[tj] = tasks[tj], tasks[ti]
    elif op == "move-phase":
        at = _phase_at(tree, phase)
        to = at + _step(where, op)
        if 0 <= to < len(tree):
            tree[at], tree[to] = tree[to], tree[at]

    return tree


def todo_edit(
    phases: list[dict[str, Any]],
    *,
    op: str,
    phase: str = "",
    task: str = "",
    value: str = "",
    where: str = "after",
) -> list[Frame]:
    """The same rewrite as one nested `init`, the only form that carries order."""
    tree = todo_edit_tree(
        phases, op=op, phase=phase, task=task, value=value, where=where
    )
    return todo_init(tree)


def todo_init(phases: list[dict[str, Any]]) -> list[Frame]:
    """One `init` that replaces the whole list with `phases`.

    The editor sends the tree it has been showing, so it needs the frame for a tree
    rather than a verb applied to one.
    """
    return [_command("todo.apply", {"op": "init", "list": _todo_list(phases)})]


def _tasks(rest: list[str], state: CommandState) -> list[Frame]:
    if not rest:
        return [_command("tasks.list")]
    sub = rest[0]
    if sub == "delete":
        _exact(rest[1:], 1, "/tasks delete <id>")
        return [_command("tasks.delete", {"id": rest[1]})]
    if sub in ("enable", "disable"):
        _exact(rest[1:], 1, f"/tasks {sub} <id>")
        task = _find_task(state, rest[1])
        task["enabled"] = sub == "enable"
        return [_command("tasks.set", {"task": task})]
    if sub == "set":
        _exact(rest[1:], 1, "/tasks set <id>")
        return [_command("tasks.set", {"task": _find_task(state, rest[1])})]
    if sub == "add":
        # The modal is the UI's (`05` §7); by the time `parse` sees `add` the chat
        # path did not intercept it, so there is no task body to send.
        raise CommandError("a task body must be supplied (add opens a modal)")
    raise CommandError(f"unknown subcommand: /tasks {sub}")


def _fs_put(path: str, state: CommandState) -> list[Frame]:
    if state.upload_bytes is None:
        raise CommandError("no file loaded to upload")
    if not path.startswith("/"):
        raise CommandError(f"path must be absolute: {path}")
    if not userfs_import_ok(path):
        # `05` §7: refused here so a doomed upload costs no round trip. The device
        # is still the authority; this is the same rule stated twice on purpose.
        raise CommandError(
            "path is outside /config.json, /tasks.json, /skills/, /prompts/, /tools/"
        )
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

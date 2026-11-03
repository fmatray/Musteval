"""The Textual App: six screens, one frame sink, slash commands routed."""

from __future__ import annotations

import time
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from ..client import LinkState, Session
from ..client.session import SessionError
from ..commands import LOCAL_COMMANDS, CommandError, CommandState, parse
from ..protocol import Frame, new_id
from .help import COMMANDS, HELP_TEXT, CommandSuggester
from .modals import ConfirmPrompt, SecretPrompt
from .render import (
    SlashHighlighter,
    clip,
    render_diag,
    render_json_block,
    render_status,
    render_tool_call,
    render_tool_result,
    sanitize,
)

class ChatScreen(Vertical):
    """`05-bridge.md` §5.1: the conversation pane, composer at the bottom."""

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", wrap=True, markup=False, highlight=False)
        yield Input(
            placeholder="Type a prompt, or / for a command",
            id="chat-input",
            suggester=CommandSuggester(),
            highlighter=SlashHighlighter(frozenset(c.name for c in COMMANDS)),
        )

    def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def write(self, text: str) -> None:
        self.query_one("#chat-log", RichLog).write(text)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.value = ""
        text = event.value.strip()
        if text:
            self.app.submit(text)  # type: ignore[attr-defined]


class StatusScreen(VerticalScroll):
    """`05-bridge.md` §5.2: the health grid, refreshed from `status`."""

    def compose(self) -> ComposeResult:
        yield Static("no status yet", id="status-body")

    def render_status(self, status: dict[str, Any]) -> None:
        self.query_one("#status-body", Static).update("\n".join(render_status(status)))


class TasksScreen(Vertical):
    """`05-bridge.md` §5.3."""

    def compose(self) -> ComposeResult:
        yield DataTable(id="task-table", cursor_type="row")
        yield Static(
            "[Enter] Run [Space] Enable [D] Delete [R] Refresh", id="task-hint"
        )

    def on_mount(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.add_columns("ID", "Description", "Trigger", "Status", "Next Fire")

    def render_tasks(self, tasks: list[dict[str, Any]]) -> None:
        table = self.query_one("#task-table", DataTable)
        table.clear()
        for task in tasks:
            trigger = (task.get("trigger") or {}).get("type", "-")
            table.add_row(
                task.get("id", "-"),
                task.get("description", "-"),
                trigger,
                "Enabled" if task.get("enabled") else "Disabled",
                str(task.get("next_fire_at", "-")),
                key=task.get("id"),
            )

    def selected_task_id(self) -> str | None:
        table = self.query_one("#task-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None


class LogsScreen(Vertical):
    """`05-bridge.md` §5.4."""

    def compose(self) -> ComposeResult:
        yield Static("Follow: ON", id="log-header")
        yield RichLog(id="log-body", wrap=True, markup=False, highlight=False, max_lines=2000)

    def write(self, line: str) -> None:
        self.query_one("#log-body", RichLog).write(line)


class MqttScreen(Vertical):
    """`05-bridge.md` §5.5: read-only broker state and traffic."""

    def compose(self) -> ComposeResult:
        yield Static("no brokers reported", id="mqtt-brokers")
        yield RichLog(id="mqtt-traffic", wrap=True, markup=False, highlight=False)

    def render_brokers(self, servers: list[dict[str, Any]]) -> None:
        if not servers:
            self.query_one("#mqtt-brokers", Static).update("no brokers reported")
            return
        lines = [
            f"{s.get('id')}  {'connected' if s.get('connected') else 'disconnected'}  "
            f"{len(s.get('subscriptions') or [])} subs"
            for s in servers
        ]
        self.query_one("#mqtt-brokers", Static).update("\n".join(lines))

    def write_traffic(self, body: dict[str, Any]) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(body.get("received_ts") or 0))
        topic = sanitize(str(body.get("topic", "")))
        payload = clip(str(body.get("payload", "")))
        self.query_one("#mqtt-traffic", RichLog).write(
            f"{stamp} {body.get('server_id')} {topic} {payload}"
        )


class HelpScreen(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-body")


class BridgeApp(App):
    """Composition root for the UI. `__main__` builds the `Session` and hands it in."""

    CSS = """
    Screen { layout: vertical; }
    #status-body, #help-body { padding: 1 2; }
    #mqtt-brokers { padding: 0 2; }
    #task-hint { padding: 0 2; }
    """

    BINDINGS = [
        Binding("f1", "show('help')", "Help"),
        Binding("f2", "show('chat')", "Chat"),
        Binding("f3", "show('status')", "Status"),
        Binding("f4", "show('tasks')", "Tasks"),
        Binding("f5", "show('logs')", "Logs"),
        Binding("f6", "show('mqtt')", "MQTT"),
        Binding("escape", "back", "Back / Cancel"),
        Binding("ctrl+r", "reconnect", "Reconnect"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self._state: dict[str, Any] = {}
        self._last_status: dict[str, Any] = {}
        self._tasks: list[dict[str, Any]] = []
        self._last_prompt: str | None = None
        self._usage_total = 0
        self._last_input_bytes = 0
        self._todo_open = False
        self._log_backlog: list[str] = []
        self._seen_lines: set[str] = set()

    # -- lifecycle ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield ChatScreen(id="chat")
            yield StatusScreen(id="status")
            yield TasksScreen(id="tasks")
            yield LogsScreen(id="logs")
            yield MqttScreen(id="mqtt")
            yield HelpScreen(id="help")
        yield Footer()

    async def on_mount(self) -> None:
        self.session.install_sink(self._on_frame)
        for name in ("status", "tasks", "logs", "mqtt", "help"):
            self.query_one(f"#{name}").display = False
        self.query_one("#chat").display = True
        # Run in a worker: mounting must not block the loop that pumps the
        # transport, or the handshake can never complete.
        self.run_worker(self._start(), exclusive=False)

    async def _start(self) -> None:
        await self.session.connect()
        if self.session.state is not LinkState.CONNECTED:
            self._chat().write(f"[{self.session.state.value}]")
            return
        ack = self.session.hello_ack
        if ack is not None:
            self._on_hello_ack(ack)
        await self._request("status")

    def action_show(self, name: str) -> None:
        for screen in ("chat", "status", "tasks", "logs", "mqtt", "help"):
            self.query_one(f"#{screen}").display = screen == name
        self.title = name.capitalize()
        if name == "status":
            self._render_status()
        if name == "tasks":
            self._render_tasks()
        if name == "logs":
            self._flush_logs()

    def action_back(self) -> None:
        """`05-bridge.md` §6: Esc closes the top modal, then leaves a screen.

        On Chat it cancels the current run, which is the one place Esc changes
        device state rather than local navigation.
        """
        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        if self.query_one("#chat").display is False:
            self.action_show("chat")
            return
        frame = Frame(type="cancel", id=new_id("b"), ts=0, body={"reason": "user"})
        self.run_worker(self.session.send(frame), exclusive=False)

    async def action_reconnect(self) -> None:
        await self.session.close()
        await self.session.connect()
        self._chat().write(f"[{self.session.state.value}]")

    # -- frame routing (`05-bridge.md` §10) -----------------------------

    def _on_frame(self, frame: Frame) -> None:
        handler = {
            "hello_ack": self._on_hello_ack,
            "status": self._on_status,
            "log": self._on_log,
            "agent.start": self._on_agent_start,
            "agent.tool": self._on_agent_tool,
            "agent.tool_result": self._on_agent_tool_result,
            "agent.final": self._on_agent_final,
            "agent.error": self._on_agent_error,
            "task.fired": self._on_task_fired,
            "gpio.edge": self._on_gpio_edge,
            "mqtt.message": self._on_mqtt_message,
            "event": self._on_event,
            "command.result": self._on_command_result,
            "pong": self._on_pong,
        }.get(frame.type)
        if handler is not None:
            handler(frame)

    def _flush_logs(self) -> None:
        log = self._logs()
        for line in self._log_backlog:
            log.write(line)
        self._log_backlog.clear()

    def _chat(self) -> ChatScreen:
        return self.query_one("#chat", ChatScreen)

    def _logs(self) -> LogsScreen:
        return self.query_one("#logs", LogsScreen)

    def _on_hello_ack(self, frame: Frame) -> None:
        self._state = frame.body
        self._chat().write(
            f"connected: {frame.body.get('device')} fw {frame.body.get('fw_version')} "
            f"boot {frame.body.get('boot_id')}"
        )

    def _on_status(self, frame: Frame) -> None:
        self._last_status = frame.body
        self._render_status()

    def _render_status(self) -> None:
        if self._last_status:
            self.query_one("#status", StatusScreen).render_status(self._last_status)

    def _render_tasks(self) -> None:
        self.query_one("#tasks", TasksScreen).render_tasks(self._tasks)

    def _on_log(self, frame: Frame) -> None:
        line = (
            f"{frame.body.get('level')} {frame.body.get('tag')}: "
            f"{sanitize(str(frame.body.get('msg')))}"
        )
        # `RichLog` discards writes while its screen is hidden, so the line is
        # buffered here and flushed when Logs is opened.
        self._log_backlog.append(line)
        if self.query_one("#logs").display:
            self._logs().write(line)

    def _on_agent_start(self, frame: Frame) -> None:
        self._last_input_bytes = frame.body.get("input_bytes", 0)
        self._chat().write(f"( ) thinking… [run {frame.body.get('run_id')}]")

    def _on_agent_tool(self, frame: Frame) -> None:
        self._chat().write(render_tool_call(frame.body))

    def _on_agent_tool_result(self, frame: Frame) -> None:
        self._chat().write(render_tool_result(frame.body))

    def _on_agent_final(self, frame: Frame) -> None:
        usage = frame.body.get("usage") or {}
        self._usage_total += usage.get("total_tokens", 0)
        self._todo_open = bool(frame.body.get("todo_open"))
        self._chat().write(str(frame.body.get("text", "")))

    def _on_agent_error(self, frame: Frame) -> None:
        self._chat().write(f"✗ {frame.body.get('code')}: {frame.body.get('message')}")

    def _on_task_fired(self, frame: Frame) -> None:
        self._chat().write(
            f"task {frame.body.get('task_id')} fired → run {frame.body.get('run_id')}"
        )

    def _on_gpio_edge(self, frame: Frame) -> None:
        self._logs().write(
            f"gpio pin {frame.body.get('pin')} {frame.body.get('edge')} → {frame.body.get('level')}"
        )

    def _on_mqtt_message(self, frame: Frame) -> None:
        self.query_one("#mqtt", MqttScreen).write_traffic(frame.body)

    def _on_event(self, frame: Frame) -> None:
        kind = frame.body.get("kind")
        if kind == "link.rejected":
            self._chat().write("✗ link rejected: bad token")
            return
        self._logs().write(f"event:{kind} {clip(str(frame.body.get('data')), 200)}")

    def _on_pong(self, frame: Frame) -> None:
        self.title = f"{self.title} · pong"

    def _on_command_result(self, frame: Frame) -> None:
        body = frame.body
        name = body.get("name")
        if not body.get("ok"):
            error = body.get("error") or {}
            self._chat().write(f"✗ {name}: {error.get('code')} {error.get('message')}")
            return
        result = body.get("result")
        if name == "status" and isinstance(result, dict):
            self._last_status = result
            self._render_status()
            return
        if name == "tasks.list" and isinstance(result, dict):
            self._tasks = result.get("tasks") or []
            self._render_tasks()
            self._chat().write(f"{len(self._tasks)} task(s)")
            return
        if name == "mqtt.status":
            servers = result if isinstance(result, list) else (result or {}).get("servers", [])
            self.query_one("#mqtt", MqttScreen).render_brokers(servers)
            self._chat().write(f"{len(servers)} broker(s)")
            return
        if name == "secret.set":
            # Never render the value; `05-bridge.md` §8.
            line = f"secret {result.get('key')} stored ({result.get('length')} bytes)"
            if line not in self._seen_lines:
                self._seen_lines.add(line)
                self._chat().write(line)
            return
        self._chat().write(render_json_block(result))

    # -- input ----------------------------------------------------------

    async def _request(self, name: str, args: dict[str, Any] | None = None) -> None:
        frame = Frame(
            type="command",
            id=new_id("b"),
            ts=0,
            body={"name": name, "args": args or {}},
        )
        try:
            reply = await self.session.request(frame)
        except SessionError as exc:
            self._chat().write(f"✗ {exc}")
            return
        if reply.type == "command.result":
            self._on_command_result(reply)

    def submit(self, text: str) -> None:
        if text.startswith("/"):
            self.run_worker(self._run_command(text), exclusive=False)
        else:
            self.run_worker(self._send_prompt(text), exclusive=False)

    async def _send_prompt(self, text: str) -> None:
        self._last_prompt = text
        self._chat().write(f"you: {text}")
        frame = Frame(type="prompt", id=new_id("b"), ts=0, body={"text": text})
        try:
            reply = await self.session.request(frame)
        except SessionError as exc:
            self._chat().write(f"✗ {exc}")
            return
        if reply.type == "agent.error":
            self._on_agent_error(reply)
        elif reply.type == "agent.start":
            self._on_agent_start(reply)
            self._chat().write("waiting for the run…")

    async def _run_command(self, text: str) -> None:
        head = text[1:].split()[0] if len(text) > 1 else ""
        if head in LOCAL_COMMANDS:
            self._run_local(head)
            return
        if head == "diag":
            # `05-bridge.md` §7: reuses the `status` frame, renders only the
            # diagnostic half, so the chat gets fresh counters rather than the
            # last screen refresh.
            await self._request("status")
            self._chat().write(
                "\n".join(render_diag(self._last_status)) if self._last_status else "no status yet"
            )
            return
        if head == "secret" and self._pending_secret is None:
            # `05-bridge.md` §8.1: masked modal, Esc cancels without sending.
            parts = text[1:].split()
            if len(parts) < 3 or parts[1] != "set":
                self._chat().write("✗ usage: /secret set <key>")
                return
            value = await self.push_screen_wait(SecretPrompt(parts[2]))
            if value is None:
                self._chat().write("secret entry cancelled")
                return
            self._pending_secret = value
        words = text[1:].split()
        if words[:2] == ["tasks", "delete"] and len(words) >= 3:
            # `05-bridge.md` §7: delete is the one irreversible command.
            if not await self.push_screen_wait(ConfirmPrompt(f"Delete task {words[2]}?")):
                self._chat().write("delete cancelled")
                return
        # Built after the modals: a secret prompt fills the value `parse` reads.
        state = CommandState(
            tasks=self._tasks,
            last_prompt=self._last_prompt,
            secret_value=self._secret_value(),
        )
        try:
            frames = parse(text, state)
        except CommandError as exc:
            self._chat().write(f"✗ {exc}")
            return
        for frame in frames:
            if frame.type == "command":
                await self._request(frame.body["name"], frame.body.get("args"))
            elif frame.type == "prompt":
                await self._send_prompt(frame.body["text"])
            elif frame.type == "secret.set":
                # Request, not send: the store reply is what confirms the write,
                # and its `result` never carries the value (`04` §4.10).
                await self._request_secret(frame)
            else:
                await self.session.send(frame)

    async def _request_secret(self, frame: Frame) -> None:
        outgoing = Frame(
            type="secret.set",
            id=frame.id,
            ts=0,
            body={"key": frame.body["key"], "value": self._secret_value()},
        )
        try:
            reply = await self.session.request(outgoing)
        except SessionError as exc:
            self._chat().write(f"✗ {exc}")
            return
        if reply.type == "command.result":
            self._on_command_result(reply)
        self._pending_secret = None

    _pending_secret: str | None = None

    def _secret_value(self) -> str:
        # `05-bridge.md` §8: masked input, never rendered. `ui` collects the value
        # and hands it over here, so the parser stays pure and the value never
        # reaches a log line.
        return self._pending_secret or ""

    def _run_local(self, head: str) -> None:
        if head == "tools":
            caps = self._state.get("capabilities") or {}
            self._chat().write("tools: " + ", ".join(caps.get("tools") or []))
            self._chat().write("mcp: " + ", ".join(caps.get("mcp") or []))
        elif head == "mcp":
            servers = self._last_status.get("mcp") or []
            self._chat().write(
                ", ".join(f"{s.get('id')}={s.get('state')}" for s in servers) or "no MCP servers"
            )
        elif head == "usage":
            self._chat().write(f"total tokens: {self._usage_total}")
        elif head == "context":
            self._chat().write(
                f"last input: {self._last_input_bytes} bytes | tokens: {self._usage_total}"
            )
        elif head == "todo":
            self._chat().write(
                "last run left the todo list open" if self._todo_open else "no open todos"
            )
        elif head == "export":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._chat().write(f"exported bridge-export-{stamp}.md")
        elif head == "quit":
            self.exit()

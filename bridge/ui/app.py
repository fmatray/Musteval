"""The Textual App: six screens, one frame sink, slash commands routed."""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio
import base64
import os

import time
from typing import Any

from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from rich.markdown import Markdown
from rich.markup import escape
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from textual.widgets import DataTable, Header, Input, RichLog, Static

from ..client import LinkState, Session
from ..client.session import SessionError
from ..commands import LOCAL_COMMANDS, CommandError, CommandState, parse
from .. import mermaid
from .. import providers as providers_mod
from .. import transfer
from ..protocol import Frame, new_id
from .help import COMMANDS, CommandSuggester, Completion, help_tabs, options
from .history import PromptHistory, history_path
from .logs import LogBuffer, LogFilter, render_log_line
from .modals import (
    ContextModal,
    Working,
    HelpModal,
    InfoModal,
    ConfirmPrompt,
    FilePrompt,
    FilterPrompt,
    ModelPicker,
    WifiCustomForm,
    WifiScanForm,
    _PickBox,
    ModelStep,
    NetworkStep,
    ProviderStep,
    SecretPrompt,
    ValuePrompt,
)
from .task_prompt import TaskPrompt
from ..protocol import (
    SANDBOX_CHUNK_BYTES,
    SANDBOX_ROOT,
    SandboxPathError,
    sandbox_join,
    sandbox_normalise,
    sandbox_parent,
)
from .config import ROWS, DEFAULT_SYSTEM_PROMPT, coerce, flatten, lookup, render_value
from .splash import SPLASH_SECONDS, pick_tip, render_splash
from . import onboard
from .onboard import provider_secret_key, wifi_secret_key
from .render import (
    LINK_WORDS,
    binding_pairs,
    UNREACHABLE,
    SlashHighlighter,
    clip,
    geo_line,
    ping_line,
    reboot_line,
    reload_lines,
    diag_sections,
    render_json_block,
    hint_bar,
    render_prompt_bar,
    render_prompt_text,
    render_status,
    render_tool_call,
    tool_table,
    render_tool_result,
    sanitize,
    template_names,
    template_text,
    provider_lines,
    todo_lines,
    wifi_known_lines,
    wifi_scan_lines,
    wifi_state_line,
)

COMPOSER_MAX_ROWS = 6
#: `05-bridge.md` §5.1: the candidate list never pushes the conversation away.
COMPLETION_ROWS = 8
#: How many lines `/export` keeps. Matches `RichLog(max_lines=2000)` on the Logs
#: screen's order of magnitude: a session long enough to exceed it is one the
#: reader has already scrolled past, and the alternative is an unbounded list.
EXPORT_MAX_LINES = 5000


def _entries_note(result: dict[str, Any] | None) -> str:
    """How many entries a delete took with it, when the device says so."""
    if not isinstance(result, dict):
        return ""
    entries = result.get("entries")
    return f" and {entries} entries" if entries else ""


class ChatLog(RichLog):
    """The conversation pane, deliberately not focusable.

    Tab traverses focusable widgets, and a keystroke landing here is silently
    lost. Scrolling survives: `PageUp` and the wheel work without focus.
    """

    can_focus = False


#: The code fence's colours, shared with the JSON blocks in the log so a fenced
#: block and a tool payload do not speak two dialects of highlight.
MARKDOWN_CODE_THEME = "ansi_dark"


class ChatScreen(Vertical):
    """`05-bridge.md` §5.1: the conversation pane, composer at the bottom."""

    # Up/Down recall submitted lines; Tab accepts the completion rather than
    # moving focus, which is what the composer's suggestion is for. Shift+Tab
    # keeps the platform meaning so the Footer stays reachable.
    BINDINGS = [
        Binding("up", "history_back", "Previous prompt", show=False),
        Binding("down", "history_forward", "Next prompt", show=False),
        Binding("tab", "accept_completion", "Complete", show=False),
        # `priority` because the focused `Input` binds Right for line editing and
        # would otherwise shadow this; the action still falls through mid-line.
        Binding("right", "cursor_right", "Accept", show=False, priority=True),
        Binding("pageup", "page_back", "Earlier", show=False),
        # Home/End are listed on `Input`, which is focused, so they are matched
        # in `on_key` rather than here: a container binding never sees them.
        # Esc closes the list first; the App's binding handles the rest.
        Binding("escape", "dismiss_completion", "Close list", show=False),
        Binding("pagedown", "page_forward", "Later", show=False),
        Binding("ctrl+j", "newline", "Newline", show=False),
        Binding("ctrl+l", "clear_log", "Clear", show=False),
        Binding("ctrl+c", "clear_input", "Clear input", show=False),
        # `Input` accepts this key itself when it is multiline; the binding is
        # what makes it reachable once the composer has focus.
        Binding("shift+enter", "newline", "Newline", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history = PromptHistory(path=history_path())
        #: Candidates currently listed, and the index the arrows sit on.
        self._candidates: list[Completion] = []
        self._total = 0
        #: Index the arrows sit on. OMP opens the list with the top row chosen.
        self._cursor = 0
        #: Prompts opened in this pane, which is what makes the first rule absent.
        self._turns = 0
        #: `#chat-log` exists once `on_mount` has run, which is after the widget is
        #: mounted but before `is_mounted` flips. `_start` runs in a worker, so its
        #: first line can arrive before either; buffering keeps that line instead
        #: of raising out of the worker or silently losing it.
        self._ready = False
        self._pending: list[tuple[Any, str]] = []
        #: The pane as text, for `/export`. `RichLog.lines` is width-cropped and
        #: trimmed to the scrollback cap, so what a narrow terminal shows is not
        #: what the file should say.
        self.transcript: list[str] = []

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat-log", wrap=True, markup=False, highlight=False)
        yield Static("", id="completion", markup=False)
        yield Static("", id="prompt-bar", markup=False)
        yield Input(
            placeholder="Type a prompt, or / for a command",
            id="chat-input",
            suggester=CommandSuggester(),
            highlighter=SlashHighlighter(frozenset(c.name for c in COMMANDS)),
        )
        yield Static("", id="chat-hint", markup=False)

    def on_key(self, event: Any) -> None:
        """Home/End jump to the list's edges, as OMP's picker does.

        They are intercepted here because the focused `Input` claims both keys
        for line editing before a container binding can run.
        """
        if not self._candidates:
            return
        if event.key == "home":
            self.action_first_candidate()
        elif event.key == "end":
            self.action_last_candidate()
        else:
            return
        event.stop()
        event.prevent_default()

    def on_input_changed(self, event: Input.Changed) -> None:
        """`05-bridge.md` §5.1: list the candidates for the token being typed.

        A hint, not a menu: `Tab` still accepts the ghost completion and Enter
        still submits whatever has been typed.
        """
        self._show_options(event.value)

    def _tool_names(self) -> "tuple[str, ...]":
        """Every tool name a reader can type after `/tool enable|disable`.

        The union of what the device advertises and what `tools.disabled` holds: a
        disabled tool is absent from `hello_ack` by design, and it is exactly the name
        `enable` must accept. Both facts live on the app, and completion runs on every
        keystroke, so neither may cost a round trip.
        """
        app = self.app
        caps = app._state.get("capabilities") or {}
        advertised = {str(name) for name in caps.get("tools") or []}
        stored = app._existing("tools.disabled")
        disabled = {n for n in stored if isinstance(n, str)} if isinstance(stored, list) else set()
        return tuple(sorted(advertised | disabled))

    def _show_options(self, value: str) -> None:
        candidates = options(value, self._tool_names())
        self._total = len(candidates)
        self._candidates = candidates[:COMPLETION_ROWS]
        # A refresh is a new list, so the arrow's choice does not carry over.
        self._cursor = 0
        self._render_options()

    def _render_options(self) -> None:
        panel = self.query_one("#completion", Static)
        if not self._candidates:
            panel.display = False
            return
        # A marker, not a colour: colour never carries meaning on its own.
        width = max(len(candidate.text) for candidate in self._candidates)
        # The help column is clipped to the panel so a row never wraps: the box is
        # sized to the line count, and a wrapped row would clip `+N more`.
        room = max((panel.size.width or 80) - width - 4, 0)
        lines: list[Text] = []
        for index, candidate in enumerate(self._candidates):
            marker = "> " if index == self._cursor else "  "
            row = Text(f"{marker}{candidate.text.ljust(width)}")
            if room > 2:
                row.append("  " + clip(candidate.help, room), style="dim")
            lines.append(row)
        hidden = self._total - len(self._candidates)
        if hidden > 0:
            lines.append(Text(f"+{hidden} more"))
        panel.update(Text("\n").join(lines))
        panel.display = True

    def _move_cursor(self, delta: int) -> None:
        """Move by `delta` rows, wrapping at the edges — OMP's list behaviour.

        Wrapping is what makes Down from the last row reach the first, so a
        short list never needs Home/End to get back to the top.
        """
        count = len(self._candidates)
        if not count:
            return
        self._cursor = (self._cursor + delta) % count
        self._render_options()

    def on_mount(self) -> None:
        panel = self.query_one("#completion", Static)
        # Derived, not a second hand-tuned constant: a `Static` has no border and
        # no vertical padding, so one row per candidate keeps `+N more` visible.
        panel.styles.max_height = COMPLETION_ROWS + 1
        panel.display = False
        self._show_hint()
        self._ready = True
        pending, self._pending = self._pending, []
        for text, kind in pending:
            self.write(text, kind)
        self.query_one("#chat-input", Input).focus()

    def _show_hint(self) -> None:
        self.query_one("#chat-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS, [("Enter", "Send")]))
        )

    def on_show(self) -> None:
        # Coming back to Chat must land in the composer, or a prompt typed after
        # switching screens goes to whichever view held focus.
        self._show_hint()
        self.query_one("#chat-input", Input).focus()

    def write(self, text: str, kind: str = "info") -> None:
        """One message into the pane, as one of `05` §5.1's seven kinds.

        The kind, not a caller's prefix, decides how the line reads: a line
        cannot arrive unclassified, and the `!`/`✗` marker is added here so the
        text a test and a screen reader read is the same text a colour terminal
        shows. Colour is the enhancement, the marker is the fact.

        `$accent` and `$error` are CSS syntax and Rich does not parse it, so the
        theme slot is resolved here; a light terminal keeps a contrast that
        holds. `user` and `assistant` are parsed as Markdown because they are
        prose; every other kind is terminal text and is never parsed, so a tag a
        device returned cannot become a heading.
        """
        # A line can arrive from a worker before the pane is laid out: `_start`
        # awaits the transport, so under load its first line lands after `compose`
        # and, in a test, after teardown. Buffered rather than dropped, because the
        # line says why the link never came up.
        if not self._ready:
            self._pending.append((text, kind))
            return
        # Teardown removes the children before the screen itself, so a reply that
        # lands mid-shutdown has no pane to reach. There is no reader left to tell.
        found = self.query("#chat-log")
        if not found:
            return
        log = found.first(RichLog)
        theme = self.app.current_theme
        if not isinstance(text, str):
            # A caller that already built a renderable (`render_json_block`) owns
            # its own presentation; wrapping it in a `Text` would crash on it.
            # `Syntax` carries the source it highlights, so the transcript gets the
            # JSON rather than a blank line the file could not explain.
            self._record(str(getattr(text, "code", text)))
            log.write(text)
            return
        # `06` T16 and `17` §2: every one of these strings can carry a device or
        # broker payload, and a raw ESC reaches the terminal as a control sequence.
        # `sanitize` is the one place that is stripped, before any path renders it.
        text = sanitize(text)
        if kind in ("user", "assistant"):
            style = theme.accent if kind == "user" else "none"
            if kind == "assistant":
                # `05` §5.6: the transcript is the source text, not the laid-out
                # diagram, so `/export` keeps the fence a reader can re-render.
                self._record(text)
                # A drawn diagram is a tree of text, so it is laid out to the pane
                # it lands in rather than to a fixed column count.
                rendered = Markdown(
                    mermaid.rewrite(text, max(20, log.size.width - 4)),
                    style=style,
                    code_theme=MARKDOWN_CODE_THEME,
                )
                log.write(rendered)
                return
            self._record(f"you: {text}")
            rendered = Markdown(text, style=style, code_theme=MARKDOWN_CODE_THEME)
            # `you: ` is its own column rather than a prefix inside the Markdown:
            # glued on, a prompt that opens with a fence or a `#` stops being a
            # fence or a heading, and the reader's own paste renders as noise.
            grid = Table.grid(padding=0)
            grid.add_column(no_wrap=True)
            grid.add_column(ratio=1)
            grid.add_row(Text("you: ", style=f"bold {theme.accent}"), rendered)
            log.write(grid)
            return
        if kind == "warn":
            text = f"! {text}"
            style = theme.warning
        elif kind == "error":
            text = f"✗ {text}"
            style = f"bold {theme.error}"
        else:  # thinking, tool, info
            style = "dim" if kind in ("thinking", "tool") else "none"
        # After the marker, so the file and the pane say the same line: `!` and `✗`
        # are the fact, not decoration (`17` §2).
        self._record(text)
        log.write(Text(text, style=style))

    def _record(self, text: str) -> None:
        """One line for `/export`, in the pane's own order.

        Bounded by the same cap as the scrollback so a long session cannot grow the
        bridge without limit; `RichLog.max_lines` is the number this tracks.
        """
        self.transcript.append(text)
        overflow = len(self.transcript) - EXPORT_MAX_LINES
        if overflow > 0:
            del self.transcript[:overflow]

    def render_bar(self, line: str, styled: Text | None = None) -> None:
        """`05` §5.1: the fact line above the composer.

        The text is held here as well: textual 8's `Static` keeps no readable
        renderable, and a test needs the line, not the widget. `styled` carries
        the same characters with the `17` §2 slots applied.
        """
        self.bar_line = line
        self.query_one("#prompt-bar", Static).update(styled if styled is not None else line)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # OMP applies the selected row and then submits the applied value, which
        # is why `/tasks a` + Enter runs `/tasks add` rather than the fragment.
        text = event.value.strip()
        if self._candidates:
            text = self._candidates[self._cursor].text
        event.input.value = ""
        self._show_options("")
        self._fit_composer()
        if text:
            self._history.add(text)
            self.app.submit(text)  # type: ignore[attr-defined]

    def action_clear_log(self) -> None:
        """`05` §6.2: clear this session's scrollback, which is local only."""
        self.query_one("#chat-log", RichLog).clear()
        # The exported file is the scrollback's own content, so `/clear` clears what
        # `/export` would write too; a transcript that survived the pane would
        # reintroduce the lines the reader asked to forget (`05` §7.5).
        self.transcript.clear()
        self._turns = 0

    def action_clear_input(self) -> None:
        """`05` §6.2: drop the composer, its candidate list and its walk cursor.

        The run is untouched: this discards what has been typed, not what is
        running, and the history entries stay readable through `Up`.
        """
        self._input().value = ""
        self._history.reset()
        self._show_options("")
        self._fit_composer()

    def open_turn(self) -> None:
        """`05` §5.1: separate this prompt from the turn before it.

        A dim full-width rule, drawn only once a turn exists to separate: the
        first prompt has nothing above it, and a rule there would be the top of
        the pane drawn twice. The rule is the scroll landmark, which a blank line
        is not — the pane has blank lines inside a message too.
        """
        if self._turns:
            self.query_one("#chat-log", RichLog).write(
                Rule(characters="─", style="dim")
            )
            self._record("─" * 40)
        self._turns += 1

    def _input(self) -> Input:
        return self.query_one("#chat-input", Input)

    def _recall(self, recalled: str) -> None:
        field = self._input()
        field.value = recalled
        field.cursor_position = len(recalled)
        self._show_options(recalled)

    def action_history_back(self) -> None:
        if self._candidates:
            self._move_cursor(-1)
            return
        self._recall(self._history.previous(self._input().value))

    def action_history_forward(self) -> None:
        if self._candidates:
            self._move_cursor(1)
            return
        self._recall(self._history.next(self._input().value))

    def action_first_candidate(self) -> None:
        if self._candidates:
            self._cursor = 0
            self._render_options()

    def action_last_candidate(self) -> None:
        if self._candidates:
            self._cursor = len(self._candidates) - 1
            self._render_options()

    def action_page_back(self) -> None:
        if self._candidates:
            self._move_cursor(-COMPLETION_ROWS)

    def action_page_forward(self) -> None:
        if self._candidates:
            self._move_cursor(COMPLETION_ROWS)

    def action_accept_completion(self) -> None:
        """Tab and Right-at-end: apply the listed choice, never move focus.

        Moving focus would send the next keystroke to the conversation log,
        where it is silently lost.
        """
        if self._candidates:
            self._apply(self._candidates[self._cursor].text)
            return
        field = self._input()
        if field._suggestion and field.cursor_at_end:
            field.action_cursor_right()

    def _apply(self, chosen: str) -> None:
        field = self._input()
        field.value = chosen
        field.cursor_position = len(chosen)
        self._show_options(chosen)

    def action_cursor_right(self) -> None:
        """OMP accepts the selection from a Right at the end of the line."""
        field = self._input()
        if self._candidates and field.cursor_at_end:
            self.action_accept_completion()
            return
        field.action_cursor_right()

    def action_dismiss_completion(self) -> None:
        """Esc closes the list and does nothing else; it falls back to Back."""
        if self._candidates:
            self._candidates = []
            self._render_options()
            return
        self.app.action_back()  # type: ignore[attr-defined]

    def action_newline(self) -> None:
        field = self._input()
        field.insert_text_at_cursor("\n")
        self._fit_composer()

    def _fit_composer(self) -> None:
        """`05` §5.1 asks for a multi-line box: grow to fit, up to a ceiling.

        `Input` renders embedded newlines but reports a height of one line, so
        the height is set here rather than by the widget.
        """
        field = self._input()
        # +2 for the tall border `Input` draws above and below its content.
        rows = min(field.value.count("\n") + 1, COMPOSER_MAX_ROWS)
        field.styles.height = rows + 2


class StatusScreen(VerticalScroll):
    """`05-bridge.md` §5.2: the health grid, refreshed from `status`."""

    BINDINGS = [Binding("ctrl+r", "refetch", "Refresh", show=False)]
    # Focusable so its own bindings fire: otherwise the hidden Chat composer
    # keeps focus and swallows the key.
    can_focus = True

    def on_show(self) -> None:
        # Focusable so its own bindings fire: the hidden Chat composer would
        # otherwise keep focus and swallow the key.
        self.focus()
        self.query_one("#status-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS))
        )

    def compose(self) -> ComposeResult:
        yield Static("no status yet: Ctrl+R asks the device", id="status-body")
        yield Static("", id="status-hint", markup=False)

    def action_refetch(self) -> None:
        self.app.run_worker(self.app._request("status"))  # type: ignore[attr-defined]

    def render_status(self, status: dict[str, Any]) -> None:
        self._status = status
        self._paint()

    def show_latency(self, rtt_ms: float) -> None:
        """`05-bridge.md` §10: `pong` refreshes the latency footer."""
        self._rtt_ms = rtt_ms
        if getattr(self, "_status", None) is not None:
            self._paint()

    def _paint(self) -> None:
        lines = render_status(self._status)
        rtt = getattr(self, "_rtt_ms", None)
        lines.append(
            f"[b]PING [/b] {rtt:.1f} ms" if rtt is not None else "[b]PING [/b] no pong yet"
        )
        self.query_one("#status-body", Static).update(Text.from_markup("\n".join(lines)))


class TasksScreen(Vertical):
    """`05-bridge.md` §5.3. Every mutation re-issues `tasks.list`."""

    # `17` §1: `Ctrl` + a letter, never a bare printable one, so a keystroke
    # meant for the table cannot fire an action on the way past.
    BINDINGS = [
        Binding("ctrl+x", "run_selected", "Run", show=False),
        Binding("ctrl+y", "toggle_enabled", "Enable/disable", show=False),
        Binding("ctrl+e", "edit_task", "Edit", show=False),
        Binding("ctrl+d", "delete_task", "Delete", show=False),
        Binding("ctrl+n", "new_task", "New", show=False),
        Binding("ctrl+r", "refetch", "Refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield DataTable(id="task-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="task-hint", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.add_columns("ID", "Description", "Trigger", "Status", "Next Fire")

    def on_show(self) -> None:
        self.query_one("#task-table", DataTable).focus()
        self.query_one("#task-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS, [("Enter", "Run")]))
        )

    def render_tasks(self, tasks: list[dict[str, Any]]) -> None:
        table = self.query_one("#task-table", DataTable)
        table.clear()
        for task in tasks:
            trigger = (task.get("trigger") or {}).get("type", "-")
            # `17` §2: the word carries the fact; the colour only speeds it up.
            state = Text("Enabled", style="green") if task.get("enabled") else Text(
                "Disabled", style="yellow"
            )
            table.add_row(
                Text(str(task.get("id", "-")), style="bold"),
                task.get("description", "-"),
                Text(trigger, style="dim"),
                state,
                Text(str(task.get("next_fire_at", "-")), style="dim"),
                key=str(task.get("id")),
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.app.run_worker(self.app.run_task(str(event.row_key.value)))  # type: ignore[attr-defined]

    def action_run_selected(self) -> None:
        self.app.run_worker(self.app.run_selected_task())  # type: ignore[attr-defined]

    def action_toggle_enabled(self) -> None:
        self.app.run_worker(self.app.toggle_task())  # type: ignore[attr-defined]

    def action_edit_task(self) -> None:
        self.app.run_worker(self.app.edit_task())  # type: ignore[attr-defined]

    def action_delete_task(self) -> None:
        self.app.run_worker(self.app.delete_task())  # type: ignore[attr-defined]

    def action_new_task(self) -> None:
        self.app.run_worker(self.app.edit_task(new=True))  # type: ignore[attr-defined]

    def action_refetch(self) -> None:
        self.app.run_worker(self.app._request("tasks.list"))  # type: ignore[attr-defined]

    def selected_task_id(self) -> str | None:
        table = self.query_one("#task-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        # `row_count` and a non-`None` cursor row are the whole precondition; a
        # `try` here only swallowed a bug the table would otherwise report.
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)


class LogsScreen(Vertical):
    """`05-bridge.md` §5.4: filter by tag and level, follow mode, clear.

    Every line is buffered whether or not it passes the filter, so widening the
    filter shows what already arrived rather than only what arrives next.
    """

    BINDINGS = [
        Binding("ctrl+f", "edit_filter", "Filter", show=False),
        Binding("ctrl+l", "clear", "Clear", show=False),
        Binding("ctrl+y", "toggle_follow", "Follow", show=False),
    ]
    can_focus = True

    def on_show(self) -> None:
        self.focus()
        self.query_one("#log-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS))
        )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.buffer = LogBuffer()
        self.follow = True

    def compose(self) -> ComposeResult:
        # `17` §2: the header shows the filter the reader typed, so no markup.
        yield Static("Follow: ON  Filter: -", id="log-header", markup=False)
        yield RichLog(id="log-body", wrap=True, markup=False, highlight=False, max_lines=2000)
        yield Static("", id="log-hint", markup=False)

    def write(self, line: str) -> None:
        self.buffer.add(line)
        if self.buffer.log_filter.matches(line):
            self._append(line)

    def _append(self, line: str) -> None:
        # Follow mode is `RichLog.auto_scroll`: off, the view stays where the
        # reader put it while lines keep arriving.
        body = self.query_one("#log-body", RichLog)
        body.auto_scroll = self.follow
        body.write(render_log_line(line))

    def refresh_view(self) -> None:
        """Redraw from the buffer; the filter is the only thing that changes it."""
        body = self.query_one("#log-body", RichLog)
        body.clear()
        for line in self.buffer.visible():
            body.write(render_log_line(line))
        self.query_one("#log-header", Static).update(self._header())

    def _header(self) -> str:
        log_filter = self.buffer.log_filter
        return (
            f"Follow: {'ON' if self.follow else 'OFF'}  "
            f"Filter: {log_filter.tag or '-'}  Level: {log_filter.min_level}"
        )

    def action_clear(self) -> None:
        self.buffer.clear()
        self.query_one("#log-body", RichLog).clear()

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        body = self.query_one("#log-body", RichLog)
        body.auto_scroll = self.follow
        if self.follow:
            body.scroll_end(animate=False)
        self.query_one("#log-header", Static).update(self._header())

    def action_edit_filter(self) -> None:
        self.app.run_worker(self.app.edit_log_filter())  # type: ignore[attr-defined]

    def apply_filter(self, tag: str, min_level: str | None = None) -> None:
        self.buffer.set_filter(
            LogFilter(tag, min_level or self.buffer.log_filter.min_level)
        )
        self.refresh_view()


BOUNDS_CLIP = 44


class SettingsScreen(Vertical):
    """`05-bridge.md` §5.7: the device config, one editable row per scalar key."""

    # `Enter` is not bound here: `DataTable` consumes it as `select_cursor`, so
    # the edit is driven by `RowSelected` instead of a shadowed ancestor binding.
    BINDINGS = [Binding("ctrl+r", "refetch", "Fetch", show=False)]

    def compose(self) -> ComposeResult:
        yield Static("no config yet: Ctrl+R fetches it from the device", id="settings-header")
        yield DataTable(id="settings-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="settings-hint", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#settings-table", DataTable)
        table.add_columns("Key", "Value", "Default", "Bounds")

    def render_rows(self, values: dict[str, Any] | None) -> None:
        """Show every row, with the device value where the config carries one."""
        table = self.query_one("#settings-table", DataTable)
        table.clear()
        self.query_one("#settings-header", Static).update(
            "device config" if values else "no config yet: Ctrl+R fetches it from the device"
        )
        for row in ROWS:
            current = lookup(values, row.path) if values else None
            table.add_row(
                row.path,
                render_value(row, current) if values else "-",
                render_value(row, row.default),
                clip(row.bound, BOUNDS_CLIP),
                key=row.path,
            )

    def on_show(self) -> None:
        # The table is the only thing here that takes keys; without this the
        # screen appears with focus left on the previous view and ignores Enter.
        self.query_one("#settings-table", DataTable).focus()
        self.query_one("#settings-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS, [("Enter", "Edit")]))
        )

    def selected_path(self) -> str | None:
        table = self.query_one("#settings-table", DataTable)
        if table.row_count == 0:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # In a worker: `edit_setting` awaits `push_screen_wait`, which Textual
        # only allows from a worker.
        event.stop()
        self.app.run_worker(self.app.edit_setting(str(event.row_key.value)))  # type: ignore[attr-defined]

    def action_refetch(self) -> None:
        self.app._config_fetched = False  # type: ignore[attr-defined]
        self.app.run_worker(self.app._request("config.get"))  # type: ignore[attr-defined]


class FilesScreen(Vertical):
    """`05-bridge.md` §5.8. Browsing the agent sandbox, `/workspace/` only."""

    BINDINGS = [
        Binding("ctrl+e", "edit_file", "Edit", show=False),
        Binding("ctrl+d", "delete_entry", "Delete", show=False),
        Binding("ctrl+n", "new_file", "New", show=False),
        Binding("ctrl+m", "move_entry", "Move", show=False),
        Binding("ctrl+k", "new_directory", "Mkdir", show=False),
        Binding("ctrl+u", "go_up", "Up", show=False),
        Binding("ctrl+r", "refetch", "Refresh", show=False),
        Binding("ctrl+o", "upload_entry", "Upload", show=False),
        Binding("ctrl+x", "download_entry", "Download", show=False),
        Binding("ctrl+f", "search", "Search", show=False),
    ]

    def compose(self) -> ComposeResult:
        # `17` §2: a path is device data, so the widget takes no markup at all.
        yield Static("/workspace/", id="files-path", markup=False)
        yield Static("", id="files-header", markup=False)
        yield DataTable(id="files-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="files-hint", markup=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.path: str = SANDBOX_ROOT
        #: The unfiltered listing, kept so clearing the filter is instant.
        self._all_entries: list[dict[str, Any]] = []
        self._needle: str = ""

    def on_mount(self) -> None:
        table = self.query_one("#files-table", DataTable)
        table.add_columns("Type", "Name", "Bytes")

    def on_show(self) -> None:
        self.query_one("#files-table", DataTable).focus()
        self.query_one("#files-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS, [("Enter", "Open")]))
        )

    def render_entries(self, path: str, entries: list[dict[str, Any]]) -> None:
        table = self.query_one("#files-table", DataTable)
        table.clear()
        if path != self.path:
            # A filter names rows in one directory; carrying it into the next would
            # hide entries the reader never asked about (`17` §5).
            self._needle = ""
        self.path = path
        self.query_one("#files-path", Static).update(path)
        self._all_entries = list(entries)
        self._render_filtered()
        self._render_header()

    def _render_header(self) -> None:
        """`17` §5: the applied filter is shown, so a hidden entry is explained."""
        count = len(self._all_entries)
        # The `..` row is navigation, not an entry, so it is not counted.
        shown = len(self._shown_names())
        needle = self._needle
        if needle:
            note = f"  filter {needle!r}: {shown} of {count}"
        else:
            note = f"  {count} entr" + ("y" if count == 1 else "ies")
        self.query_one("#files-header", Static).update(Text(note, style="dim"))

    def _shown_names(self) -> list[dict[str, Any]]:
        needle = self._needle.lower()
        return [
            entry
            for entry in getattr(self, "_all_entries", [])
            if not needle or needle in str(entry.get("name", "")).lower()
        ]

    def _render_filtered(self) -> None:
        table = self.query_one("#files-table", DataTable)
        table.clear()
        if self.path != SANDBOX_ROOT:
            table.add_row("d", "..", "-", key="..")
        for entry in self._shown_names():
            name = str(entry.get("name", "-"))
            is_directory = bool(entry.get("dir"))
            table.add_row(
                "d" if is_directory else "f",
                name,
                "-" if is_directory else str(entry.get("bytes", 0)),
                key=f"{self.path.rstrip('/')}/{name}",
            )

    def needle(self) -> str:
        return self._needle

    def set_needle(self, needle: str) -> None:
        """Narrow the listing without another `sandbox.list` (`17` §5)."""
        self._needle = needle
        self._render_filtered()
        self._render_header()

    def current_path(self) -> str:
        return self.path

    def selected_entry(self) -> tuple[str, str] | None:
        """Return `(path, kind)` for the cursor row, or `None` on an empty table."""
        table = self.query_one("#files-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        kind, name, _ = table.get_row_at(table.cursor_row)
        if str(name) == "..":
            return (sandbox_parent(self.current_path()), "dir")
        path = sandbox_join(self.current_path(), str(name))
        return (path, "dir" if str(kind) == "d" else "file")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.app.run_worker(self.app.open_entry())  # type: ignore[attr-defined]

    def action_edit_file(self) -> None:
        self.app.run_worker(self.app.edit_file())  # type: ignore[attr-defined]

    def action_delete_entry(self) -> None:
        self.app.run_worker(self.app.delete_entry())  # type: ignore[attr-defined]

    def action_new_file(self) -> None:
        self.app.run_worker(self.app.new_file())  # type: ignore[attr-defined]

    def action_move_entry(self) -> None:
        self.app.run_worker(self.app.move_entry())  # type: ignore[attr-defined]

    def action_new_directory(self) -> None:
        self.app.run_worker(self.app.new_directory())  # type: ignore[attr-defined]

    def action_go_up(self) -> None:
        self.app.run_worker(self.app.browse(sandbox_parent(self.current_path())))  # type: ignore[attr-defined]

    def action_refetch(self) -> None:
        self.app.run_worker(self.app.browse(self.current_path()))  # type: ignore[attr-defined]

    def action_upload_entry(self) -> None:
        self.app.run_worker(self.app.upload_entry())  # type: ignore[attr-defined]

    def action_download_entry(self) -> None:
        self.app.run_worker(self.app.download_entry())  # type: ignore[attr-defined]

    def action_search(self) -> None:
        self.app.run_worker(self.app.search_files())  # type: ignore[attr-defined]


class MqttScreen(Vertical):
    """`05-bridge.md` §5.5: read-only broker state and traffic."""

    BINDINGS = [
        Binding("ctrl+r", "refetch", "Refresh", show=False),
        # Arrows stay: `17` §1 bans bare *printable* letters as actions, and an
        # arrow cannot be mistaken for text.
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("down", "move(1)", "Next", show=False),
    ]
    can_focus = True

    def on_show(self) -> None:
        self.focus()
        self.query_one("#mqtt-hint", Static).update(
            hint_bar(binding_pairs(self.BINDINGS))
        )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # `17` §4: state belongs in the constructor; `compose` may run again.
        self.selected = 0
        self._brokers: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static("no brokers reported: Ctrl+R asks the device", id="mqtt-brokers")
        yield RichLog(id="mqtt-traffic", wrap=True, markup=False, highlight=False)
        yield Static("", id="mqtt-hint", markup=False)

    def action_refetch(self) -> None:
        self.app.run_worker(self.app._request("mqtt.status"))  # type: ignore[attr-defined]

    def action_move(self, delta: int) -> None:
        if not self._brokers:
            return
        self.selected = max(0, min(len(self._brokers) - 1, self.selected + delta))
        self.render_brokers(self._brokers)

    def render_brokers(self, servers: list[dict[str, Any]]) -> None:
        self._brokers = servers
        if not servers:
            self.query_one("#mqtt-brokers", Static).update(
                "no brokers reported: Ctrl+R asks the device"
            )
            return
        lines = []
        for index, broker in enumerate(servers):
            cursor = ">" if index == self.selected else " "
            # `17` §2: the word carries the state, the colour speeds it up.
            state = (
                "[green]connected[/green]"
                if broker.get("connected")
                else "[red]disconnected[/red]"
            )
            subs = len(broker.get("subscriptions") or [])
            # `17` §2: the id is a device string spliced into markup, so it is
            # escaped; unescaped, a `[` in a broker id raised `MarkupError`.
            lines.append(
                f"[b]{cursor}[/b] {escape(str(broker.get('id')))}  {state}  "
                f"[dim]{subs} subs[/dim]"
            )
        self.query_one("#mqtt-brokers", Static).update(
            Text.from_markup("\n".join(lines))
        )

    def write_traffic(self, body: dict[str, Any]) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(body.get("received_ts") or 0))
        topic = sanitize(str(body.get("topic", "")))
        payload = clip(str(body.get("payload", "")))
        self.query_one("#mqtt-traffic", RichLog).write(
            f"{stamp} {body.get('server_id')} {topic} {payload}"
        )


class SplashScreen(Vertical):
    """`05-bridge.md` §5.9: the mascot and one tip, dismissed by any key.

    A screen, not a chat banner: a banner scrolls away, and this is the first
    thing a reader sees. It is displayed by `on_mount` and hidden by the same
    display toggle the other screens use.
    """

    can_focus = True

    BINDINGS = [Binding("escape", "close_splash", "Continue", show=False)]

    def __init__(self, tip: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tip = tip
        #: A key press and the timer both arrive; only the first one acts.
        self._gone = False

    def compose(self) -> ComposeResult:
        yield Static(render_splash(self.tip), id="splash-body")

    def on_mount(self) -> None:
        # Headless runs mount Chat instead (`05` §5.9), so the timer is not
        # started at all: a timer that fires under `run_test` would move focus
        # away from the composer the pilot is driving.
        if self.app.is_headless:  # type: ignore[attr-defined]
            self._gone = True
            return
        self.focus()
        # The timer is for a reader who walked away; a key is the real path.
        self.set_timer(SPLASH_SECONDS, self.action_close_splash)

    def action_close_splash(self) -> None:
        if self._gone:
            return
        self._gone = True
        self.display = False
        self.app.query_one("#chat").display = True  # type: ignore[attr-defined]
        self.app.query_one("#chat-input").focus()  # type: ignore[attr-defined]

    def on_key(self, event: Any) -> None:
        event.stop()
        self.action_close_splash()


class BridgeApp(App):
    """Composition root for the UI. `__main__` builds the `Session` and hands it in."""

    CSS = """
    Screen { layout: vertical; }
    #status-body { padding: 1 2; }
    #completion { padding: 0 2; height: auto; color: $text-muted; }
    /* One row, never wrapped: a bar that grows with the facts would push the
       composer off a short terminal. Truncated by `clip` at the call site. */
    #prompt-bar { padding: 0 2; height: 1; color: $text-muted; }
    #mqtt-brokers { padding: 0 2; }
    /* `17` §3: the hint wraps rather than clipping, and `height: auto` is what
       lets it take the second row instead of losing the last key. */
    #task-hint, #status-hint, #log-hint, #mqtt-hint, #chat-hint,
    #settings-hint, #files-hint, #app-hint {
        padding: 0 2; height: auto;
    }
    """

    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "show('chat')", "Chat"),
        Binding("f3", "show('status')", "Status"),
        Binding("f4", "show('tasks')", "Tasks"),
        Binding("f5", "show('logs')", "Logs"),
        Binding("f6", "show('mqtt')", "MQTT"),
        Binding("f7", "show('files')", "Files"),
        Binding("escape", "back", "Back / Cancel"),
        Binding("ctrl+r", "reconnect", "Reconnect"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self._state: dict[str, Any] = {}
        self._last_status: dict[str, Any] = {}
        self._config: dict[str, Any] = {}
        #: `_config` is empty both before the first fetch and when the device
        #: reports nothing, so a flag decides whether a fetch already happened.
        self._config_fetched = False
        #: Set while a caller fetches the document for its own use, so the reply
        #: caches it without moving the reader off the screen they were on.
        self._config_quiet = False
        # A silent refresh after a mutation should not narrate its count.
        self._tasks_quiet = False
        self._tasks: list[dict[str, Any]] = []
        self._last_prompt: str | None = None
        self._usage_total = 0
        self._seen_lines: set[str] = set()
        self._reader_pinged = False
        self._files_path: str = ""
        #: Set when `/files <path>` wants a specific directory; `action_show` is
        #: the one place that fetches, so the screen never lists twice.
        self._files_target: str = ""
        #: The `phases` tree from the last `todo.apply`, kept whole because the
        #: editor rewrites the tree and a name list cannot carry status.
        self._todo_tree: list[dict[str, Any]] = []
        #: Set while `/todo edit` fetches the tree it is about to rewrite, so the
        #: fetch does not print the plan twice.
        self._todo_quiet = False

    async def _send_frames(self, frames: list[Frame]) -> None:
        """Send one plan the way every command does: one await per frame."""
        if any(frame.type == "ping" for frame in frames):
            self._reader_pinged = True
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
                # `cancel`, `ping`, `ack`: no reply to wait for, but the write can
                # still fail. Uncaught it escapes the worker as `WorkerFailed`,
                # which is a crash, not the line saying the frame never left.
                try:
                    await self.session.send(frame)
                except SessionError as exc:
                    self._chat().write(f"{exc}", kind="error")

    # -- lifecycle ------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield ChatScreen(id="chat")
            yield StatusScreen(id="status")
            yield TasksScreen(id="tasks")
            yield LogsScreen(id="logs")
            yield SettingsScreen(id="settings")
            yield MqttScreen(id="mqtt")
            yield FilesScreen(id="files")
            yield SplashScreen(pick_tip(), id="splash")
        # `17` §3: the app row is the entry point to the shortcut list, not the list.
        # It held every global key and wrapped to four rows at 60 columns, pushing the
        # composer off a short terminal; the whole list now lives in the `/help` modal
        # (`F1`), which has the room. Derived from `BINDINGS` by action, so the row
        # cannot name a key the app does not bind.
        yield Static(
            hint_bar(
                binding_pairs(
                    [b for b in self.BINDINGS if b.action in ("help", "back")]
                )
            ),
            id="app-hint",
            markup=False,
        )

    async def on_mount(self) -> None:
        self.session.install_sink(self._on_frame)
        # A link drop sends no frame: without this the bar would sit at
        # `linked` through an outage (`05` §5.1).
        self.session.install_state_hook(lambda _state: self._refresh_bar())
        for name in ("status", "tasks", "logs", "settings", "mqtt", "files"):
            self.query_one(f"#{name}").display = False
        # `05` §5.9: the splash is the first thing a reader sees. Headless runs
        # (`run_test`, `--headless`) mount Chat directly so the 140 existing
        # `BridgeApp(session)` sites keep the screen they assert on.
        splash = self.query_one("#splash")
        splash.display = not self.is_headless
        self.query_one("#chat").display = self.is_headless
        self._refresh_bar()
        # Run in a worker: mounting must not block the loop that pumps the
        # transport, or the handshake can never complete.
        self.run_worker(self._start(), exclusive=False)

    async def _start(self) -> None:
        await self.session.connect()
        if self.session.state is not LinkState.CONNECTED:
            self._chat().write(f"[{self.session.state.value}]")
            return
        self._refresh_bar()
        ack = self.session.hello_ack
        if ack is not None:
            self._on_hello_ack(ack)
        await self._request("status")

    def action_help(self) -> None:
        """`05` §5.6: `F1` opens the catalogue as a modal, not a screen."""
        self.push_screen(HelpModal(help_tabs(), binding_pairs(self.BINDINGS)))

    def action_show(self, name: str) -> None:
        self.title = name.capitalize()
        # `05` §5.7: the config screen is reached as `/config`; its widget keeps the
        # name the screen's own tests and bindings use.
        name = "settings" if name == "config" else name
        for screen in (
            "chat",
            "status",
            "tasks",
            "logs",
            "settings",
            "mqtt",
            "files",
        ):
            self.query_one(f"#{screen}").display = screen == name
        if name == "status":
            self._render_status()
        if name == "tasks":
            self._render_tasks()
        if name == "logs":
            self._logs().refresh_view()
        if name == "settings" and not self._config_fetched:
            self.run_worker(self._request("config.get"))
        if name == "files":
            # An explicit `/files <path>` always refetches; a bare `/files` only
            # fetches the first time, then shows the path the reader left it at.
            target, self._files_target = self._files_target, ""
            if target or not self._files_path:
                self.run_worker(self.browse(target or SANDBOX_ROOT))

    async def edit_setting(self, path: str) -> None:
        """Edit one config row: prompt, coerce, then send `config.set`.

        The bound is checked here so a rejected value costs no round trip, but
        the device re-validates (`03-config.md` §Full schema).
        """
        row = next((candidate for candidate in ROWS if candidate.path == path), None)
        if row is None:
            return
        stored = lookup(self._config, path)
        current = render_value(row, row.default if stored is None else stored)
        typed = await self.push_screen_wait(ValuePrompt(path, current))
        if typed is None:
            return
        try:
            value = coerce(row, typed)
        except ValueError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        await self._request("config.set", {"path": row.path, "value": value})

    # -- Tasks screen actions (`05` §5.3) --------------------------------

    def _tasks_screen(self) -> TasksScreen:
        return self.query_one("#tasks", TasksScreen)

    async def run_task(self, task_id: str) -> None:
        await self._request("tasks.run", {"id": task_id})

    async def run_selected_task(self) -> None:
        """`17` §1: `Ctrl+X` runs the row under the cursor, same path as Enter."""
        task_id = self._tasks_screen().selected_task_id()
        if task_id is None:
            self._chat().write("no task selected")
            return
        await self.run_task(task_id)

    async def toggle_task(self) -> None:
        task = self._task_by_id(self._tasks_screen().selected_task_id())
        if task is None:
            self._chat().write("no task selected")
            return
        await self._request(
            "tasks.set", {"task": {**task, "enabled": not task.get("enabled")}}
        )

    async def delete_task(self) -> None:
        task_id = self._tasks_screen().selected_task_id()
        if task_id is None:
            self._chat().write("no task selected")
            return
        # `05` §7: delete is the one irreversible command.
        if not await self.push_screen_wait(ConfirmPrompt(f"Delete task {task_id}?")):
            self._chat().write("delete cancelled")
            return
        await self._request("tasks.delete", {"id": task_id})

    async def edit_task(self, new: bool = False) -> None:
        """`E`/`N`: edit the selected task as JSON and send `tasks.set`."""
        task = None if new else self._task_by_id(self._tasks_screen().selected_task_id())
        if not new and task is None:
            self._chat().write("no task selected")
            return
        edited = await self.push_screen_wait(TaskPrompt(task))
        if edited is None:
            return
        await self._request("tasks.set", {"task": edited})

    def _task_by_id(self, task_id: str | None) -> dict[str, Any] | None:
        if task_id is None:
            return None
        return next((task for task in self._tasks if task.get("id") == task_id), None)

    async def edit_log_filter(self) -> None:
        """`/` on Logs: one line, `tag` or `tag:level`."""
        typed = await self.push_screen_wait(FilterPrompt(self._logs().buffer.log_filter))
        if typed is None:
            return
        tag, _, level = typed.partition(":")
        try:
            self._logs().apply_filter(tag.strip(), level.strip() or None)
        except ValueError as exc:
            self._chat().write(f"{exc}", kind="error")

    # -- files (`05` §5.8) ----------------------------------------------

    def _files(self) -> FilesScreen:
        return self.query_one(FilesScreen)

    async def browse(self, path: str) -> None:
        """List one sandbox directory. The device refuses a path outside it."""
        try:
            path = sandbox_normalise(path)
        except SandboxPathError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        # A listing path is always a directory, so it is shown with its `/`
        # (`sandbox_display`). `normalise` never returns one, so this appends it
        # unconditionally rather than testing a condition that is always true.
        path += "/"
        self._files_path = path
        await self._request("sandbox.list", {"path": path})

    async def search_files(self) -> None:
        """`17` §5: narrow the listing locally, seeded with what is applied."""
        screen = self._files()
        typed = await self.push_screen_wait(
            FilterPrompt(screen.needle(), "find in files", "name contains")
        )
        if typed is None:
            screen.set_needle("")
            return
        screen.set_needle(typed)

    async def open_entry(self) -> None:
        """Enter: descend into a directory, or view a file."""
        chosen = self._files().selected_entry()
        if chosen is None:
            return
        path, kind = chosen
        if kind == "dir":
            await self.browse(path)
            return
        await self.view_file(path)

    async def view_file(self, path: str) -> None:
        body = await self._request("sandbox.read", {"path": path})
        if not body:
            return
        text = str(body.get("text", ""))
        if body.get("truncated"):
            # Editing a partial file would save the cut over the original.
            self._chat().write(f"{path} is above the read cap; not opened", kind="warn")
            return
        edited = await self.push_screen_wait(FilePrompt(path, text))
        if edited is None or edited == text:
            return
        # A dismissed modal writes nothing: only Ctrl+S inside it commits.
        await self.save_file(path, edited)

    async def edit_file(self) -> None:
        chosen = self._files().selected_entry()
        if chosen is None or chosen[1] != "file":
            return
        await self.view_file(chosen[0])

    async def delete_entry(self) -> None:
        """`05` §5.8: delete a file, or a directory after naming what it holds.

        A directory is never removed on one keypress. The device refuses a
        populated one unless `recursive` is set, and the confirmation reports the
        child count so "delete `runs`" and "delete `runs` and the 47 files under
        it" cannot look the same at the moment the key is pressed.
        """
        chosen = self._files().selected_entry()
        if chosen is None:
            return
        path, kind = chosen
        if kind == "file":
            if not await self.push_screen_wait(ConfirmPrompt(f"Delete {path}?")):
                return
            await self._request("sandbox.delete", {"path": path})
            return
        entries = await self._request("sandbox.list", {"path": path + "/"})
        count = len((entries or {}).get("entries") or [])
        question = (
            f"Delete {path} and the {count} entries under it?"
            if count
            else f"Delete the empty directory {path}?"
        )
        if not await self.push_screen_wait(ConfirmPrompt(question)):
            return
        # `recursive` only when there is something to recurse into: an empty
        # directory is the same frame either way, and saying less is safer.
        args: dict[str, Any] = {"path": path}
        if count:
            args["recursive"] = True
        await self._request("sandbox.delete", args)

    async def move_entry(self) -> None:
        """`05` §5.8: rename or relocate the cursor row.

        Both a file and a directory move, because `sandbox.move` is one `rename`
        on the device. The default in the prompt is the current name, so a plain
        rename is Enter and an edit, not a retyped absolute path.
        """
        chosen = self._files().selected_entry()
        if chosen is None:
            return
        path, _kind = chosen
        if path.rstrip("/") == SANDBOX_ROOT.rstrip("/"):
            self._chat().write("/workspace cannot be moved", kind="warn")
            return
        name = await self.push_screen_wait(
            ValuePrompt(f"Move {path} to", path)
        )
        if not name or name == path:
            return
        try:
            target = sandbox_normalise(name)
        except SandboxPathError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        await self._request("sandbox.move", {"from": path, "to": target})
        await self.browse(self._files_path)

    async def new_directory(self) -> None:
        name = await self.push_screen_wait(ValuePrompt("New directory name", ""))
        if not name:
            return
        try:
            path = sandbox_join(self._files_path, name)
        except SandboxPathError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        await self._request("sandbox.mkdir", {"path": path})
        await self.browse(self._files_path)

    async def download_entry(self) -> None:
        """`05` §5.8.1: copy the selected file or tree to the host.

        A directory is collected completely before the first host write, so a
        refusal leaves no half tree behind: a reader who sees `notes.md` copied and
        `run_7.json` missing cannot tell that from an empty directory.
        """
        chosen = self._files().selected_entry()
        if chosen is None:
            return
        path, kind = chosen
        if path.rstrip("/") == SANDBOX_ROOT.rstrip("/"):
            self._chat().write("/workspace itself is not downloadable", kind="warn")
            return
        default = os.path.join(os.getcwd(), os.path.basename(path.rstrip("/")))
        target = await self.push_screen_wait(ValuePrompt("Download to", default))
        if not target:
            return
        send = self._request
        try:
            if kind == "file":
                body = await send("sandbox.read", {"path": path})
                if body is None:
                    return
                if body.get("truncated"):
                    self._chat().write(
                        f"{path} is {body.get('bytes')} bytes, above the "
                        f"{transfer.READ_CAP}-byte read cap; a copy would be a prefix",
                        kind="warn",
                    )
                    return
                nodes = [transfer.FileNode(path, str(body.get("text", "")))]
            else:
                nodes = await transfer.collect(send, path)
        except transfer.TransferError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        try:
            if kind == "file":
                written = [transfer.write_file(target, nodes[0].text)]
            else:
                written = transfer.write_tree(target, nodes)
        except OSError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        self._chat().write(
            f"downloaded {len(written)} file" + ("" if len(written) == 1 else "s")
            + f" to {target}"
        )

    async def upload_entry(self) -> None:
        """`05` §5.8.1: copy a host file or tree into the directory browsed."""
        source = await self.push_screen_wait(ValuePrompt("Upload from", ""))
        if not source:
            return
        source = os.path.expanduser(source)
        try:
            written = await transfer.upload(self._request, source, self._files_path)
        except transfer.TransferError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        except OSError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        self._chat().write(
            f"uploaded {len(written)} file" + ("" if len(written) == 1 else "s")
            + f" to {self._files_path}"
        )
        await self.browse(self._files_path)

    async def new_file(self) -> None:
        name = await self.push_screen_wait(ValuePrompt("New file name", ""))
        if not name:
            return
        try:
            path = sandbox_join(self._files_path, name)
        except SandboxPathError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        await self.save_file(path, "")
        await self.view_file(path)

    async def _stage(self, command: str, path: str, text: str) -> bool:
        """Send `text` as `command` chunks under `SANDBOX_CHUNK_BYTES`, `True` on a
        clean commit.

        `data` carries at most `SANDBOX_CHUNK_BYTES` raw bytes per frame, so a file
        above that is several calls rather than one that overruns the cap and comes
        back `E_PROTOCOL`. Both writers take the same four arguments, so one loop
        serves `sandbox.write` (a workspace file) and `fs.put` (a userfs file, which
        `04` §4.16 caps at 2048 raw bytes per chunk for the 4 KiB RX line).
        """
        raw = text.encode("utf-8")
        chunks = [
            raw[i : i + SANDBOX_CHUNK_BYTES]
            for i in range(0, len(raw), SANDBOX_CHUNK_BYTES)
        ] or [b""]
        for index, chunk in enumerate(chunks):
            ok = await self._request(
                command,
                {
                    "path": path,
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "encoding": "base64",
                    "offset": index * SANDBOX_CHUNK_BYTES,
                    "final": index == len(chunks) - 1,
                },
            )
            if ok is None:
                # A refused chunk makes the device clear its staging file
                # (`write_chunk_canonical` in `store.c`), so the remaining chunks have
                # no staged length to match and would each fail too.
                return False
        return True

    async def save_file(self, path: str, text: str) -> None:
        """Stage the whole buffer in `04` §4.20 chunks, committing on the last."""
        await self._stage("sandbox.write", path, text)

    async def _edit_system_prompt(self) -> None:
        """`05` §7.6: edit the persona file the device reads at the start of a run.

        The path comes from `agent.system_prompt` rather than a constant: the config
        key is what `build_system_prompt` reads, so a reader who moved the persona
        would otherwise edit a file no run looks at.
        """
        config = await self._request("config.get", {"path": "agent.system_prompt"})
        # The default comes from the settings table rather than a second constant:
        # `agent.system_prompt` is absent from a stock `config.json` on some builds,
        # and one owner for the fact is the rule (`03-config.md` §prompts).
        path = str((config or {}).get("value") or DEFAULT_SYSTEM_PROMPT)
        body = await self._request("template.get", {"name": os.path.basename(path)})
        if body is None:
            # `template.get` refuses an oversize file (`store_read_file` is
            # all-or-nothing at 4096 bytes) and `_request` has said why.
            return
        text = str(body.get("text", ""))
        edited = await self.push_screen_wait(FilePrompt(path, text))
        if edited is None or edited == text:
            return
        if not edited.strip():
            # An empty persona is legal on the device but never what the reader
            # meant, and `build_system_prompt` would run the model without one.
            self._chat().write("empty prompt discarded; nothing written", kind="warn")
            return
        if await self._stage("fs.put", path, edited):
            self._chat().write(
                f"system prompt written: {len(edited.encode('utf-8'))} bytes at {path};"
                " the next prompt uses it"
            )

    def action_back(self) -> None:
        """`05-bridge.md` §6: Esc closes the top modal, then leaves a screen.

        On Chat it cancels the current run, which is the one place Esc changes
        device state rather than local navigation.
        """
        # No screen-stack branch: every screen below is a child of the app toggled
        # by `display`, and the only pushed screens are `ModalScreen`s, which
        # suppress app bindings — so this binding never fires with a modal on top.
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
            "agent.ask": self._on_agent_ask,
            "agent.thinking": self._on_agent_thinking,
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

    def _chat(self) -> ChatScreen:
        return self.query_one("#chat", ChatScreen)

    def _logs(self) -> LogsScreen:
        return self.query_one("#logs", LogsScreen)

    def _refresh_bar(self) -> None:
        """Redraw the prompt bar from state already in hand (`05` §5.1).

        Called on every fact that changes, never on a keystroke, so the bar
        costs no frames.
        """
        # The bar lives on the chat screen, which is not queryable until the app's
        # children are mounted. A link-state change that lands before that must not
        # take the whole app down over a bar nobody can see yet — so this one check
        # covers both "not mounted yet" and "gone".
        chat = self.query("#chat")
        if not chat:
            return
        width = max(self.size.width - 4, 20)
        bar = render_prompt_bar(
            self.session.state.value, self._state, self._last_status,
            self._usage_total, width=width,
        )
        styled = render_prompt_text(
            self.session.state.value, self._state, self._last_status,
            self._usage_total, width=width,
        )
        # One row: the bar drops whole trailing segments rather than wrapping, so
        # a narrow terminal loses the rightmost facts, never the composer.
        self._chat().render_bar(bar, styled)

    def on_resize(self) -> None:
        # The bar truncates to the width in force when it was drawn, so a narrow
        # window would keep a tail that no longer fits.
        self._refresh_bar()

    def _on_hello_ack(self, frame: Frame) -> None:
        self._state = frame.body
        self._refresh_bar()
        self._chat().write(
            f"connected: {frame.body.get('device')} fw {frame.body.get('fw_version')} "
            f"boot {frame.body.get('boot_id')}"
        )

    def _on_status(self, frame: Frame) -> None:
        self._last_status = frame.body
        self._render_status()
        self._refresh_bar()

    def _render_status(self) -> None:
        if self._last_status:
            self.query_one("#status", StatusScreen).render_status(self._last_status)

    def _render_tasks(self) -> None:
        self.query_one("#tasks", TasksScreen).render_tasks(self._tasks)

    def _on_log(self, frame: Frame) -> None:
        # Bracketed so `LogBuffer` can recover the level for filtering and colour;
        # `05` §5.4's mockup prints it the same way.
        line = (
            f"[{frame.body.get('level')}] {frame.body.get('tag')}: "
            f"{sanitize(str(frame.body.get('msg')))}"
        )
        # The logs screen buffers whether or not it is on screen.
        self._logs().write(line)

    def _on_agent_start(self, frame: Frame) -> None:
        self._chat().write(f"thinking… [run {frame.body.get('run_id')}]", kind="thinking")

    def _on_agent_tool(self, frame: Frame) -> None:
        self._chat().write(render_tool_call(frame.body), kind="tool")

    def _on_agent_tool_result(self, frame: Frame) -> None:
        # A failed tool result is an error line for the same reason a failed command
        # is: it is the only place the reader learns why the agent stopped.
        self._chat().write(
            render_tool_result(frame.body),
            kind="tool" if frame.body.get("ok") else "error",
        )

    def _on_agent_ask(self, frame: Frame) -> None:
        # `05` §10.1: the one device frame that expects an answer, so it opens a modal
        # rather than writing a log line. The log line is still written first — a
        # modal with no trace leaves a transcript in which the run appears to pause
        # for no reason, and a reader who answers in a hurry cannot tell why.
        question = str(frame.body.get("question", ""))
        self._chat().write(f"? {question}", kind="info")
        self.run_worker(self._answer_ask(frame.body))

    async def _answer_ask(self, body: dict[str, Any]) -> None:
        question = str(body.get("question", ""))
        options = [str(o) for o in (body.get("options") or [])]
        if not options:
            answer = await self.push_screen_wait(ValuePrompt(question, ""))
        else:
            # The free-text row is always there: the question that needed a fifth
            # answer is the case `ask` exists for (`docs/tools/ask.md` §ask).
            rows = [(option, "answer with this") for option in options]
            rows.append(("type an answer…", "write your own"))
            choice = await self.push_screen_wait(_PickBox(rows, title=question))
            answer = choice
            if choice == "type an answer…":
                answer = await self.push_screen_wait(ValuePrompt(question, ""))
        # `None` is the modal's dismissal; `ValuePrompt` also answers `None` for an
        # emptied field, so a cancelled reply and an empty answer are one case here.
        # The field exists because the device cannot infer a dismissal from an empty
        # answer it chose itself (`04` §3.23).
        cancelled = answer is None
        if not cancelled:
            self._chat().write(f"↳ {answer}", kind="info")
        await self.session.send(
            Frame(
                type="ask.reply",
                id=new_id("b"),
                ts=0,
                body={
                    "ask_id": str(body.get("ask_id", "")),
                    "answer": "" if cancelled else str(answer),
                    "cancelled": cancelled,
                },
            )
        )

    def _on_agent_thinking(self, frame: Frame) -> None:
        # `04` §3.21: the model's working, never its answer, so it is a dim line above
        # the reply rather than part of it. A truncated trace says so, for the same
        # reason a truncated answer does: the reader must not take a cut for a whole.
        text = str(frame.body.get("text", ""))
        if frame.body.get("truncated"):
            text += f" … [cut at {frame.body.get('full_bytes', 0)} bytes]"
        self._chat().write(text, kind="thinking")

    def _on_agent_final(self, frame: Frame) -> None:
        usage = frame.body.get("usage") or {}
        self._usage_total += usage.get("total_tokens", 0)
        self._refresh_bar()
        self._chat().write(str(frame.body.get("text", "")), kind="assistant")
        if frame.body.get("todo_open"):
            # `05` §7: the list is device-resident, so a run that stopped with open
            # todos says so here — otherwise the plan dies with the run and the user
            # only finds out by asking.
            self._chat().write("todos left open: /todo", kind="hint")
        if frame.body.get("truncated"):
            # `05-bridge.md` §5: a reply cut at the provider's token cap stops
            # mid-sentence. Rendered as-is it reads as a finished answer.
            full = frame.body.get("full_bytes")
            detail = (
                f"{len(str(frame.body.get('text', '')))} of {full} bytes"
                if full
                else "the provider stopped at its output token limit"
            )
            self._chat().write(f"answer truncated: {detail}", kind="warn")

    def _on_agent_error(self, frame: Frame) -> None:
        code = frame.body.get("code")
        message = frame.body.get("message")
        if code == "E_CANCELLED":
            # `04` §3.4: cancellation is not a failure, so it is not coloured as one —
            # `E_CANCELLED` is the run reporting that the reader's Esc landed. The
            # `message` already is the sentence (`run cancelled`), so no prefix.
            self._chat().write(message or "run cancelled")
            return
        self._chat().write(f"{code}: {message}", kind="error")

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
            self._chat().write("link rejected: bad token", kind="error")
            return
        if kind == "cancel.noop":
            # `04` §3.4: `/cancel` with nothing in flight is otherwise answered only by
            # silence — the live-run case emits `agent.error`/`E_CANCELLED`, which
            # `_on_agent_error` prints. The device's `reason` is the verdict itself
            # (`nothing to cancel`, `no run with that id in flight`), so the frame is
            # printed as it arrives rather than dressed in a second prefix.
            data = frame.body.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            self._chat().write(reason or "nothing to cancel")
            return
        self._logs().write(f"event:{kind} {clip(str(frame.body.get('data')), 200)}")

    def _on_pong(self, frame: Frame) -> None:
        """`05-bridge.md` §10: `pong` updates the latency the Status screen shows.

        It used to append "· pong" to the window title, which grew one segment per
        heartbeat (every 10 s, forever) and told the reader nothing: the number the
        doc promises lives in `Session.last_rtt_ms`, measured where the round trip
        is observable. No status frame means no grid to refresh yet, and
        `on_show`/`_on_status` fill it on the next fetch.
        """
        rtt = self.session.last_rtt_ms
        if rtt is None:
            return
        try:
            screen = self.query_one("#status", StatusScreen)
        except NoMatches:
            return
        screen.show_latency(rtt)
        # `05-bridge.md` §10 calls `/ping` the latency check, so a reader who typed
        # it must see the number without knowing to open the Status screen. The
        # heartbeat stays silent: a line every 10 s would bury the conversation.
        if self._reader_pinged:
            self._reader_pinged = False
            self._chat().write(f"pong: {rtt:.1f} ms")

    def _on_command_result(self, frame: Frame) -> None:
        body = frame.body
        name = body.get("name")
        if not body.get("ok"):
            # `04` §3.20 requires `name` and, on a refusal, `error{code,message}`.
            # Without them the line read `None: None None`, which names nothing and
            # hides that the device broke the contract.
            error = body.get("error")
            if isinstance(name, str) and isinstance(error, dict) and error.get("code"):
                self._chat().write(
                    f"{name}: {error.get('code')} {error.get('message')}", kind="error"
                )
            else:
                self._chat().write("the device sent a malformed reply", kind="error")
            return
        result = body.get("result")
        if name == "status" and isinstance(result, dict):
            self._last_status = result
            self._render_status()
            self._refresh_bar()
            return
        if name == "tasks.list" and isinstance(result, dict):
            self._tasks = result.get("tasks") or []
            self._render_tasks()
            if not self._tasks_quiet:
                self._chat().write(f"{len(self._tasks)} task(s)")
            self._tasks_quiet = False
            return
        if name in ("tasks.set", "tasks.delete"):
            # `05` §5.3: a mutation re-issues `tasks.list`, so no row goes stale.
            self._chat().write(f"{name} {result.get('id') or result.get('task_id')}")
            self._tasks_quiet = True
            self.run_worker(self._request("tasks.list"))
            return
        if name == "history.clear" and isinstance(result, dict):
            # `05` §7: the device forgot the conversation first, so the pane and the
            # counters that describe it can follow. Wiping on a refusal would leave
            # the reader with an empty pane and a device that still remembers.
            self._chat().action_clear_log()
            self._usage_total = 0
            self._last_prompt = None
            self._chat().write(f"cleared {result.get('messages', 0)} message(s)")
            self._refresh_bar()
            return
        if name == "todo.apply" and isinstance(result, dict):
            self._todo_tree = [
                phase for phase in result.get("phases") or [] if isinstance(phase, dict)
            ]
            if not self._todo_quiet:
                for line in todo_lines(result):
                    self._chat().write(line)
            self._todo_quiet = False
            return
        if name == "reload" and isinstance(result, dict):
            # `05` §7: a reload's warnings are the part a reader acts on, and the
            # JSON block buried them. `/sys reload` and `/sys reboot` share this
            # renderer, so the two verbs cannot drift apart.
            for line in reload_lines(result):
                self._chat().write(line)
            return
        if name == "reboot" and isinstance(result, dict):
            self._chat().write(reboot_line(result))
            return
        if name == "tools.list" and isinstance(result, dict):
            # `04` §4.29: the one table both `/tools` and `/tool list` print. It lives
            # here rather than in `_tool` because the bare `/tools` and the `list` verb
            # are two typed paths to one frame, and a second renderer would let the
            # two surfaces disagree. `--send` is not a third: `_headless` prints the
            # reply body raw and never builds a `BridgeApp`.
            for line in tool_table(result):
                self._chat().write(line)
            return
        if name == "context.budget" and isinstance(result, dict):
            # `05` §7.5: the blocks of the next request on the left, the selected
            # block's text on the right. A modal because it is a glance at a budget
            # rather than a place to work — the same rule as the `/sys` box.
            # `context_blocks` owns the row layout so the list and the counters
            # cannot disagree about the order or the labels.
            self.push_screen(ContextModal(result))
            return
        if name == "templates.list" and isinstance(result, dict):
            for line in template_names(result):
                self._chat().write(line)
            return
        if name == "template.get" and isinstance(result, dict):
            self._chat().write(template_text(result))
            return
        if name == "geo.locate" and isinstance(result, dict):
            # `05` §10: a fix updates the Status screen GEO row and reports in chat.
            # Without this branch the reply fell through to the raw JSON block, so
            # `/geo` looked like a debug dump and the GEO row kept reading `no fix`
            # from the last `status`.
            self._last_status = {**self._last_status, "geo": result}
            self._render_status()
            self._chat().write(geo_line(result))
            return
        if name == "mqtt.status":
            servers = result if isinstance(result, list) else (result or {}).get("servers", [])
            self.query_one("#mqtt", MqttScreen).render_brokers(servers)
            self._chat().write(f"{len(servers)} broker(s)")
            return
        if name == "config.get" and isinstance(result, dict):
            # Two reply shapes share this name (`04` §4.8): a whole document comes
            # back under `config`, a path-scoped read under `path`/`value`. Treating
            # the second as the first flattened one value into the Settings grid and
            # showed the reader nothing, so `/config get <path>` looked dead.
            if "path" in result:
                self._chat().write(
                    render_json_block(
                        {"path": result.get("path"), "value": result.get("value")}
                    )
                )
                return
            raw = result.get("config", result)
            self._config = flatten(raw)
            self._config_fetched = True
            self.query_one("#settings", SettingsScreen).render_rows(self._config)
            if not self._config_quiet:
                self.action_show("settings")
            return
        if name == "config.set":
            self._chat().write(
                f"{result.get('path')} applied"
                + (", requires reboot" if result.get("requires_reboot") else "")
            )
            self.run_worker(self._request("config.get"))
            # The bar names the configured model and thinking level, so a *live*
            # write to `llm.providers` makes it stale until the next unsolicited
            # `status` — up to 30 s of showing the model the reader just replaced
            # (`05` §5.1). A write the device reports as not applied changed nothing
            # the bar reads, so it costs no round trip.
            if result.get("applied"):
                self.run_worker(self._request("status"))
            return
        if name == "sandbox.list" and isinstance(result, dict):
            self.query_one("#files", FilesScreen).render_entries(
                str(result.get("path", SANDBOX_ROOT)), result.get("entries") or []
            )
            return
        if name == "sandbox.read":
            # Consumed by the editor modal; echoing the body here would print
            # the whole file into the chat log.
            return
        if name in ("sandbox.write", "sandbox.delete"):
            self._chat().write(
                f"{name} {result.get('path')} "
                + ("created" if result.get("created") else "ok")
                + _entries_note(result)
            )
            self.run_worker(self.browse(self._files_path))
            return
        if name in ("sandbox.mkdir", "sandbox.move"):
            self._chat().write(
                f"{name} {result.get('path') or result.get('to')} "
                + ("created" if result.get("created") else "ok")
            )
            self.run_worker(self.browse(self._files_path))
            return
        if name.startswith("wifi.") and name != "wifi.scan" and isinstance(result, dict):
            # `04` §4.26: one line, worded from the §3.9 object so a reading from a
            # command and a reading from `status` cannot disagree.
            self._chat().write(wifi_state_line(result))
            return
        if name == "wifi.scan" and isinstance(result, dict):
            for line in wifi_scan_lines(result):
                self._chat().write(line)
            return
        if name == "fs.put" and isinstance(result, dict):
            # `04` §4.16: the reply is the whole confirmation of an upload, so it is
            # said in words. `requires_reboot` is the one field a reader must act on.
            line = f"{result.get('path')}: {result.get('bytes')} bytes, "
            line += "created" if result.get("created") else "replaced"
            if result.get("requires_reboot"):
                line += " (reboot to apply)"
            self._chat().write(line)
            return
        if name == "tasks.run" and isinstance(result, dict):
            # `04` §4 catalog: the run id is what `/cancel` and the status screen name.
            self._chat().write(f"started run {result.get('run_id')}")
            return
        if name == "net.ping" and isinstance(result, dict):
            # `04` §4.25: one line. 100 % loss is a result the device measured, not
            # an error frame, so it is shown as the answer it is — but coloured as
            # the failure the reader is looking for.
            loss = result.get("loss_pct", 0)
            kind = "error" if loss == 100 else "warn" if loss else None
            self._chat().write(ping_line(result), kind=kind)
            return
        if name == "sys.reset_counter":
            # `04` §4.23: the previous value is the proof the reply names.
            self._chat().write(f"boot failures: {result.get('previous')} -> 0")
            if result.get("safe_mode"):
                self._chat().write("safe mode lifts on the next boot")
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

    async def _fetch_config(self) -> None:
        """Read the whole document into `_config` without showing the Settings grid.

        The whole-document reply navigates on purpose: `/config` and a `Ctrl+R`
        refresh are the reader asking for that grid. A command that needs the
        document as an input — `/thinking`, `/onboard` — is not, and letting the
        reply navigate wrote its answer into a pane it had just hidden.
        """
        self._config_quiet = True
        try:
            await self._request("config.get")
        finally:
            self._config_quiet = False

    async def _request(
        self, name: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Send one command and return its `result`, or `None` on any failure."""
        frame = Frame(
            type="command",
            id=new_id("b"),
            ts=0,
            body={"name": name, "args": args or {}},
        )
        try:
            reply = await self.session.request(frame)
        except SessionError as exc:
            self._chat().write(f"{exc}", kind="error")
            return None
        except asyncio.TimeoutError:
            self._chat().write("the device did not answer in time", kind="error")
            return None
        if reply.type != "command.result":
            return None
        self._on_command_result(reply)
        return reply.body.get("result") if reply.body.get("ok") else None

    def submit(self, text: str) -> None:
        if text.startswith("/"):
            self.run_worker(self._run_command(text), exclusive=False)
        else:
            self.run_worker(self._send_prompt(text), exclusive=False)

    async def _send_prompt(self, text: str) -> None:
        self._last_prompt = text
        self._chat().open_turn()
        self._chat().write(text, kind="user")
        # `05-bridge.md` §5.1: a prompt with no link is answered immediately. The
        # ack timeout is 6x `connect_timeout`, so waiting would leave the reader
        # staring at their own line for half a minute with nothing to act on.
        # `flaky` still carries traffic, so only the states that cannot answer are
        # refused; a prompt there may succeed or time out, and both are reported.
        state = self.session.state
        if state.value in UNREACHABLE:
            self._chat().write(
                f"{LINK_WORDS[state.value]}; the prompt was not sent", kind="error"
            )
            return
        frame = Frame(type="prompt", id=new_id("b"), ts=0, body={"text": text})
        try:
            reply = await self.session.request(frame)
        except SessionError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        except asyncio.TimeoutError:
            self._chat().write("the run did not answer in time", kind="error")
            return
        if reply.type == "agent.error":
            self._on_agent_error(reply)
        elif reply.type == "agent.start":
            self._on_agent_start(reply)
            self._chat().write("waiting for the run…", kind="thinking")

    async def _run_command(self, text: str) -> None:
        head = text[1:].split()[0] if len(text) > 1 else ""
        words = text[1:].split()
        if words[:2] == ["todo", "edit"]:
            # `05` §7: `edit` is a UI verb with no device op, so it never reaches
            # `parse`; the editor rewrites the tree and sends one `init`.
            await self._edit_todo()
            return

        # `05-bridge.md` §7: the bare form opens its screen; a sub-command such as
        # `/mqtt publish` answers in chat, so it lands there rather than moving
        # the reader to a screen that will not show the answer.
        if head in ("tasks", "mqtt"):
            self.action_show(head if len(words) == 1 else "chat")
        if head == "config" and words[1:2] in ([], ["edit"]):
            # `05` §5.7: `/config` and `/config edit` open the settings screen; `get`
            # and `set` stay device frames. A surplus word is named rather than
            # dropped, so `/config edit wifi` is not read as a narrowed screen.
            if len(words) > 2:
                self._chat().write("usage: /config edit", kind="error")
                return
            self.action_show("config")
            return
        if head == "model":
            await self._model(words[1:])
            return

        if head == "files":
            # `/files` opens at the sandbox root; `/files /workspace/runs` opens there.
            target = words[1] if len(words) > 1 else SANDBOX_ROOT
            try:
                target = sandbox_normalise(target)
            except SandboxPathError as exc:
                self._chat().write(f"{exc}", kind="error")
                return
            self._files_target = target
            self.action_show("files")
            return
        if head == "onboard":
            await self.onboard()
            return
        if head in ("tool", "tools"):
            # `05` §7.4: `list` reads, the other two are a read-modify-write of one
            # config array. `parse` never sees either head because the array is an
            # input the device only hands over whole (`config.get`). `/tools` renders
            # the same table as `/tool list`, which is the point of the frame. This
            # arm precedes `LOCAL_COMMANDS`, which still lists `tools` so `parse`
            # leaves the line alone.
            await self._tool(["list"] if head == "tools" else words[1:])
            return
        if head in LOCAL_COMMANDS:
            self._run_local(head)
            return
        if head == "thinking":
            await self._thinking(words[1:])
            return
        if head == "compact":
            await self._compact(words[1:])
            return
        if head == "sys" and words[1:2] in (["reload"], ["reboot"]):
            # `05` §7: both answer in chat as a sentence, which
            # `_on_command_result` renders from the frame.
            await self._request(words[1])
            return
        if head == "sys" and words[1:2] == ["prompt"]:
            # `05` §7.6: the persona lives on the device at `agent.system_prompt`
            # and the editor is the only writer, because `fs.put` takes bytes and
            # no other command carries a file's text.
            await self._edit_system_prompt()
            return
        if head == "sys" and words[1:2] in (["diag"], ["status"]):
            # `05-bridge.md` §5.10: both verbs answer from one fresh `status` frame, in
            # a modal, so a reader comparing the two does not reopen it. `status`
            # opens on the general half, `diag` on the counters.
            if await self._request("status") is None:
                # `_request` already said why. Rendering the cached frame here would
                # show last refresh's counters as this command's answer.
                return
            self._sys_info(diag_first=words[1] == "diag")
            return
        if head == "wifi" and words[1:2] == ["list"]:
            # `05` §7.7: configured networks joined with a live scan.
            await self._wifi_list()
            return
        if head == "wifi" and words[1:2] == ["edit"]:
            # `05` §7.7: add, re-key or forget, all over `wifi.networks`.
            await self._wifi_edit()
            return
        if head == "providers":
            # `05` §7.8: add, log in or disconnect, over `llm.providers`.
            await self._providers()
            return
        if head == "secret" and self._pending_secret is None:
            # `05-bridge.md` §8.1: masked modal, Esc cancels without sending.
            parts = text[1:].split()
            if len(parts) < 3 or parts[1] != "set":
                self._chat().write("usage: /secret set <key>", kind="error")
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
        # `05-bridge.md` §7 and §5.3: `add` and `set` both open the task modal, so
        # neither reaches `parse`. `parse` refuses `add` because a modal cannot be
        # raised from a pure function, and `set <id>` would otherwise resend the
        # cached task untouched — a round trip that changes nothing.
        if words[:2] == ["tasks", "add"]:
            if len(words) > 2:
                self._chat().write("usage: /tasks add", kind="error")
                return
            edited = await self.push_screen_wait(TaskPrompt(None))
            if edited is None:
                self._chat().write("task edit cancelled")
                return
            await self._request("tasks.set", {"task": edited})
            return
        if words[:2] == ["tasks", "set"] and len(words) >= 3:
            if len(words) > 3:
                self._chat().write("usage: /tasks set <id>", kind="error")
                return
            cached = self._task_by_id(words[2])
            if cached is None:
                self._chat().write(f"no task {words[2]} in the last tasks.list", kind="error")
                return
            edited = await self.push_screen_wait(TaskPrompt(cached))
            if edited is None:
                self._chat().write("task edit cancelled")
                return
            await self._request("tasks.set", {"task": edited})
            return
        # `05-bridge.md` §7: `fs.put` takes its bytes over the wire, so the one
        # command that carries a file asks for it here; nothing else loads one.
        upload_bytes: bytes | None = None
        if words[:2] == ["fs", "put"]:
            source = await self.push_screen_wait(ValuePrompt("Upload from", ""))
            if not source:
                return
            path = os.path.expanduser(source)
            try:
                with open(path, "rb") as handle:
                    upload_bytes = handle.read()
            except OSError as exc:
                self._chat().write(f"{path}: {exc.strerror or exc}", kind="error")
                return
        # Built after the modals: a secret prompt fills the value `parse` reads.
        state = CommandState(
            tasks=self._tasks,
            last_prompt=self._last_prompt,
            secret_value=self._secret_value(),
            upload_bytes=upload_bytes,
        )
        try:
            frames = parse(text, state)
        except CommandError as exc:
            # The value is consumed here or by `_request_secret`, never kept: a value
            # left in hand makes the next `/secret set` skip its modal and file this
            # one under another key.
            self._pending_secret = None
            self._chat().write(f"{exc}", kind="error")
            return
        await self._send_frames(frames)

    async def _edit_todo(self) -> None:
        """Open the tree editor on the device's current list.

        The editor sends one `init` that replaces the whole list, so it has to
        start from what the device holds right now: a tree cached from an earlier
        `/todo` would silently drop a task the run added since.
        """
        self._todo_quiet = True
        try:
            result = await self._request("todo.apply", {"op": "view"})
        finally:
            self._todo_quiet = False
        if result is None:
            return
        from .modals import TodoEditor

        opened = self._todo_signature(self._todo_tree)
        frames = await self.push_screen_wait(TodoEditor(self._todo_tree))
        if not frames:
            return
        # The frame replaces the whole list, so a task the run added while the
        # editor was open would be dropped by an edit made without seeing it.
        self._todo_quiet = True
        try:
            current = await self._request("todo.apply", {"op": "view"})
        finally:
            self._todo_quiet = False
        if current is not None and self._todo_signature(self._todo_tree) != opened:
            self._chat().write(
                "the todo list changed while you were editing; nothing was sent — "
                "run /todo edit again",
                kind="error",
            )
            return
        await self._send_frames(frames)

    @staticmethod
    def _todo_signature(tree: list[dict[str, Any]]) -> list[tuple]:
        """The tree in the form a rewrite must preserve: name, status and blocker.

        Compares what a reader saw against what the device holds, so an edit made
        against a stale tree is refused instead of overwriting someone's work.
        """
        return [
            (
                str(phase.get("name") or ""),
                [
                    (
                        str(task.get("content") or ""),
                        str(task.get("status") or "pending"),
                        str(task.get("blocker") or ""),
                    )
                    for task in phase.get("tasks") or []
                    if isinstance(task, dict)
                ],
            )
            for phase in tree
            if isinstance(phase, dict)
        ]

    async def _request_secret(self, frame: Frame) -> None:
        # Taken and cleared before the await: the write can block on a reply that
        # never comes, and a value still in hand makes the next `/secret set` skip
        # its own modal and file this one under another key.
        value = self._pending_secret or ""
        self._pending_secret = None
        outgoing = Frame(
            type="secret.set",
            id=frame.id,
            ts=0,
            body={"key": frame.body["key"], "value": value},
        )
        try:
            reply = await self.session.request(outgoing)
        except SessionError as exc:
            self._chat().write(f"{exc}", kind="error")
            return
        except asyncio.TimeoutError:
            self._chat().write("the device did not answer in time", kind="error")
            return
        if reply.type == "command.result":
            self._on_command_result(reply)

    _pending_secret: str | None = None
    #: Set when the reader types `/ping`, so its pong gets a chat line and the
    #: heartbeat's does not (`05-bridge.md` §10).
    _reader_pinged: bool = False

    def _secret_value(self) -> str:
        # `05-bridge.md` §8: masked input, never rendered. `ui` collects the value
        # and hands it over here, so the parser stays pure and the value never
        # reaches a log line.
        return self._pending_secret or ""

    async def onboard(self) -> None:
        """`05-bridge.md` §7 `/onboard`: three steps, each tested before the next.

        The order is the dependency order, and each step's test is the unlock for the
        one after it: `reload` decides whether the station joins, the station decides
        whether a provider can be reached, and the provider's list decides which model
        names are real. Writing all three and testing at the end would tell the reader
        after the fact, which is the shape that let a configured device sit with a
        blank provider and a model list nobody could load.

        Refuses at once on a link that cannot answer, naming the bar's word — the
        same rule `_send_prompt` follows.
        """
        state = self.session.state
        if state.value in UNREACHABLE:
            self._chat().write(
                f"{LINK_WORDS[state.value]}; /onboard was not sent", kind="error"
            )
            return
        if not self._config_fetched:
            # The form pre-fills from the device's config, so the config has to have
            # arrived. It is only fetched when the settings screen is opened, so a
            # fresh process opened this form on an empty document and every field
            # looked unconfigured — including the provider, which suppressed the model
            # fetch. The fetch is quiet so the wizard stays a modal over the screen
            # the reader was on, rather than landing them on the settings grid.
            async with self._working(
                "reading the device's config", "one config.get, to pre-fill the form"
            ):
                await self._fetch_config()

        answers = await self._onboard_network()
        if answers is None:
            return
        provider_answers = await self._onboard_provider()
        if provider_answers is None:
            return
        answers.base_url = provider_answers.base_url
        answers.api_key = provider_answers.api_key
        answers.provider = provider_answers.provider
        answers.token_param = provider_answers.token_param
        if await self._onboard_models(answers) is None:
            return

    async def _onboard_network(self) -> "onboard.Answers | None":
        """Step 1: write the network, reload, and report what `status` says.

        The test is the reload's consequence, not the write's `ok`: `config.set`
        answering ok means the document changed. Whether the station joined is a
        different fact and the one the next step depends on.
        """
        scan, notes = await self._onboard_scan()
        for note in notes:
            self._chat().write(note)
        step = NetworkStep(dict(self._config), scan)
        answers = await self.push_screen_wait(step)
        if answers is None:
            self._chat().write("onboard: cancelled at the network step")
            return None

        # The network and the zone are one step because they are one test: a single
        # `reload` applies both, and one `status` reply answers both — `wifi.state`
        # for the join and `time.tz` for the zone. Two reloads would each block up to
        # one `connect_timeout` per configured network to learn the same two facts.
        if answers.skip_network:
            # Nothing was written, so there is nothing to reload and nothing to
            # report: a pass that printed a verdict would be claiming a test that
            # never ran.
            self._chat().write("onboard: network and zone kept as stored")
            return answers
        plan = onboard.writes_network(answers) + onboard.writes_time(answers)
        for line in onboard.narration(answers, dict(self._config), "network"):
            self._chat().write(f"onboard: {line}")
        if not plan:
            self._chat().write(onboard.verdict_network(None))
            return answers
        if not await self._apply(plan):
            return None
        if not await self._reload():
            return None
        status = await self._read_status()
        self._chat().write(onboard.verdict_network(status))
        self._chat().write(onboard.verdict_time(status, answers.timezone))
        return answers

    async def _onboard_provider(self) -> "onboard.Answers | None":
        """Step 2: take the provider and key, then fetch the list with them.

        The fetch runs here rather than after the write, because the list is the
        proof the key works. A provider that refuses it is named as such instead of
        surfacing later as a 401 on the first prompt.
        """
        entries, notes = providers_mod.catalog()
        for note in notes:
            self._chat().write(note)
        answers = await self.push_screen_wait(
            ProviderStep(dict(self._config), entries)
        )
        if answers is None:
            self._chat().write("onboard: cancelled at the provider step")
            return None
        if answers.skip_provider:
            # A pass proves nothing, so it claims nothing: the stored provider is
            # carried forward for the model step to complete, and the fetch is
            # skipped because there is no key to fetch with.
            self._chat().write("onboard: provider kept as stored")
            answers.model_list = []
            return answers

        models = await self._fetch_models(answers)
        self._chat().write(onboard.verdict_provider(models, answers))
        answers.model_list = models
        return answers

    async def _onboard_models(self, answers: "onboard.Answers") -> object | None:
        """Step 3: the model, offered from the list step 2 fetched

        The name is checked against that list before the write, because a typo the
        device would answer with a 404 on the first prompt is exactly the failure this
        step exists to catch.
        """
        models = list(getattr(answers, "model_list", []) or [])
        stored = self._existing("llm.providers")
        first = stored[0] if isinstance(stored, list) and stored else {}
        stored_model = str(first.get("model") or "") if isinstance(first, dict) else ""
        # The level is seeded from the stored entry, not from `none`: re-running the
        # wizard must not be a silent downgrade for a reader who set one (`17` §6).
        stored_extra = first.get("extra_body") if isinstance(first, dict) else None
        # No catalog note loop here: `_onboard_provider` already wrote them, and this
        # step always follows it, so repeating them would print the same complaint
        # twice for one malformed file.
        entries = providers_mod.catalog()[0]
        entry = providers_mod.find(answers.provider) if answers.provider else None
        step = ModelStep(
            models=models,
            provider=answers.provider or "",
            model=(answers.model or stored_model or (models[0] if models else "")),
            thinking=entry.level_of(stored_extra) if entry is not None else "none",
            entries=entries,
            efforts=answers.model_efforts,
        )
        picked = await self.push_screen_wait(step)
        if picked is None:
            self._chat().write("onboard: cancelled at the model step")
            return None
        answers.skip_model = bool(picked.get("skip_model"))
        answers.model = picked["model"]
        answers.extra_body = self._extra_body(answers.provider, picked["thinking"])
        if answers.skip_model:
            self._chat().write("onboard: model kept as stored")
            return answers
        self._chat().write(onboard.verdict_model(models, answers))
        if not answers.model:
            return None
        # The provider write lands here, not in step 2: `03-config.md` makes `model`
        # required, so a provider written before the model exists is a document the
        # device refuses.
        plan = onboard.writes_provider(answers)
        for line in onboard.narration(answers, dict(self._config), "provider"):
            self._chat().write(f"onboard: {line}")
        if not await self._apply(plan):
            return None
        return answers

    def _existing(self, path: str) -> object | None:
        """One config value from the flat document `app.py` holds."""
        if path in self._config:
            return self._config[path]
        value: object = self._config
        for segment in path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(segment)
        return value

    def _extra_body(self, provider: str, level: str) -> "dict[str, object]":
        """The `extra_body` for a level, empty where the provider rejects the member."""
        entry = providers_mod.find(provider) if provider else None
        if entry is None or not entry.thinking_levels:
            return {}
        return dict(entry.extra_body(level))

    async def _send_body(
        self, kind: str, body: dict[str, Any], timeout: float | None = None
    ) -> "tuple[bool, str]":
        """One frame of a step, with the device's own refusal code when it refuses.

        `_request` folds a rejection into `None`, which is the right shape for a
        screen that only shows what succeeded. A step has to name the code — a
        refused `config.set` and an unanswered one are different problems — so this
        reads `ok` and `error.code` off the reply itself.
        """
        if kind == "secret":
            outgoing = Frame(
                type="secret.set", id=new_id("b"), ts=0,
                body={"key": body["key"], "value": body["value"]},
            )
        elif kind == "config":
            path = next(iter(body))
            outgoing = Frame(
                type="command", id=new_id("b"), ts=0,
                body={"name": "config.set",
                      "args": {"path": path, "value": body[path]}},
            )
        else:
            outgoing = Frame(
                type="command", id=new_id("b"), ts=0,
                body={"name": body["name"], "args": body.get("args") or {}},
            )
        frame = outgoing
        try:
            reply = await self.session.request(frame, timeout=timeout)
        except SessionError as exc:
            return False, str(exc)
        except asyncio.TimeoutError:
            return False, "the device did not answer in time"
        if reply.type != "command.result":
            return False, "unexpected reply"
        if reply.body.get("ok"):
            return True, ""
        error = reply.body.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        return False, str(code or "no code")

    @asynccontextmanager
    async def _working(self, action: str, detail: str):
        """Show what the device is being asked, for as long as it takes to answer.

        A step's proof is a round trip on the device's schedule, so without a line
        saying so a wizard that is working is indistinguishable from one that has
        hung. Popped in a `finally` because a raise out of the body would otherwise
        leave the spinner on top of the next step forever.
        """
        self.push_screen(Working(action, detail))
        # Let the screen mount before the blocking await, or the reader sees nothing
        # until it is already gone.
        await asyncio.sleep(0)
        try:
            yield
        finally:
            if isinstance(self.screen, Working):
                self.pop_screen()

    async def _apply(
        self, plan: "list[tuple[str, dict[str, object]]]", what: str = "onboard"
    ) -> bool:
        """Send a step's frames in order, stopping at the first refusal.

        `what` names the command that asked, because two do — `/onboard` and
        `/providers` — and a reader who typed one must not be told the other refused.
        """
        for kind, body in plan:
            async with self._working(
                "writing the config", f"{kind}: {next(iter(body.values()))!r}"
            ):
                ok, why = await self._send_body(kind, body)
            if not ok:
                self._chat().write(f"{what}: refused ({why})", kind="warn")
                return False
        return True

    async def _reload(self) -> bool:
        """`reload` and report where the device says it got to.

        The reply is the device's own account of the boot sequence it just reran, so a
        step that depends on the network being up can read it rather than guess.
        """
        async with self._working(
            "reloading the device",
            "re-running the boot sequence: wifi, time, then the provider",
        ):
            ok, why = await self._send_body(
                "command", {"name": "reload"}, timeout=self._reload_budget()
            )
        self._chat().write(
            "onboard: reload: " + ("applied" if ok else f"refused ({why})")
        )
        return ok

    def _reload_budget(self) -> float:
        """How long `reload` may take before it is called unanswered.

        `reload` reruns the boot sequence inside the command handler, so its reply
        waits on `net_connect` — up to one `wifi.connect_timeout_ms` per configured
        network — and then on `clock_sync`. The session's default budget
        (`ack_timeout * 6`) is sized for a command that answers at once, which is
        why step 1 timed out on a device that was working.
        """
        networks = self._existing("wifi.networks")
        count = len(networks) if isinstance(networks, list) else 0
        per_ms = self._existing("wifi.connect_timeout_ms")
        per = float(per_ms) if isinstance(per_ms, (int, float)) and per_ms else 15000.0
        # One connect per network, plus a clock sync and the flash re-read. Bounded
        # so a config naming eight networks and a two-minute timeout does not turn a
        # wedged device into an eight-minute wait with no way out but Ctrl+C.
        return min(120.0, 15.0 + (count * per / 1000.0))

    async def _read_status(self) -> "dict[str, object]":
        """The `status` blocks, for a step that has to observe a consequence."""
        status = await self._request("status")
        return status if isinstance(status, dict) else {}

    async def _fetch_models(self, answers: "onboard.Answers") -> list[str]:
        """One provider's model names, fetched off the UI thread.

        `05` §7.1: the fetch runs in a worker thread and a failure is one chat line,
        never a broken write. `providers.models` holds the parsing and the exception
        handling; this supplies the socket the L2 module may not open. Takes the
        step's answers so a provider with no `models_url` is a stated fact rather than
        a fetch that silently returns nothing. Also records the efforts each name
        publishes, which is what keeps the next step's picker honest per model.
        """
        entry = providers_mod.find(answers.provider) if answers.provider else None
        if entry is None or not entry.models_url:
            return []
        try:
            async with self._working(
                "asking the provider for its models",
                f"{entry.label}: one GET with the key you typed",
            ):
                catalog = await asyncio.to_thread(
                    providers_mod.models_with_efforts, entry, answers.api_key, _http_fetch
                )
        except Exception as err:  # noqa: BLE001 - a failed fetch is a chat line
            self._chat().write(f"model list: {err}", kind="error")
            return []
        answers.model_efforts = dict(catalog)
        return [name for name, _ in catalog]

    async def _onboard_scan(self) -> tuple[list[dict], list[str]]:
        """The `wifi.scan` result and the lines worth showing about it.

        The wizard has a text field, so a refused scan still leaves the reader a way
        forward and the note says so.
        """
        networks, refused, truncated = await self._wifi_scan_quiet()
        if refused:
            return [], [f"{refused}; type the ssid"]
        return networks, [truncated] if truncated else []

    async def _quiet(
        self, name: str, args: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Send one command and return `(result, note)`, bypassing the renderer.

        `_request` hands every reply to `_on_command_result`, which is right for a
        command the reader typed and wrong for one this code asked on their behalf:
        `context.budget` opens the `/context` modal, so a budget read for a count would
        open a box nobody requested — under the confirm that is about to be pushed.

        `note` is why there is no result, phrased for chat; the caller decides whether
        to say it. A caller that shows the same facts in a pane of its own (the scan
        modal) leaves it alone, and one that has no pane writes it.
        """
        try:
            reply = await self.session.request(
                Frame(
                    type="command",
                    id=new_id("b"),
                    ts=0,
                    body={"name": name, "args": args or {}},
                )
            )
        except (SessionError, asyncio.TimeoutError):
            return None, "the device did not answer in time"
        if reply.type != "command.result":
            return None, "the device sent no result"
        if not reply.body.get("ok"):
            error = reply.body.get("error") or {}
            return None, f"refused ({error.get('code') or 'no answer'})"
        return reply.body.get("result"), None

    async def _compact(self, rest: list[str]) -> None:
        """`05` §7.5: replace the conversation with a model-written summary.

        Four steps, and the confirm between the second and the third is the one that
        matters: the device discards the messages it summarises, so the transcript has
        to be exported *before* the frame is sent. The modal names what will go —
        messages and bytes — rather than asking "are you sure", because the count is
        the only thing that makes the answer decidable.
        """
        # One owner for the arity and the bounds: `parse` already refuses a `keep` it
        # cannot honour, and a second copy here is a second place for the ceiling to
        # drift from `04` §4.33.
        try:
            args = parse(
                "/compact" + (f" {' '.join(rest)}" if rest else "")
            )[0].body["args"]
        except CommandError as exc:
            self._chat().write(f"{exc}", kind="error")
            return

        budget, note = await self._quiet("context.budget")
        if not isinstance(budget, dict):
            # A compaction on numbers it could not read would be a summary of a guess.
            self._chat().write(f"compact: {note}", kind="error")
            return
        keep = int(args.get("keep") or 0)
        messages = int(budget.get("messages") or 0)
        used = int(budget.get("used") or 0)
        if messages <= keep:
            # `05` §7.5's wording, which a reader sees before spending a request.
            self._chat().write("compact: the conversation is empty")
            return

        answer = await self.push_screen_wait(
            ConfirmPrompt(
                f"Compact {messages} messages ({used} B) into a summary, keeping the "
                f"newest {keep}?"
            )
        )
        if not answer:
            self._chat().write("compact cancelled")
            return

        result, note = await self._quiet("context.compact", args)
        if not isinstance(result, dict):
            # `05` §7.5: `E_BUSY` names the thing to do first, because "busy" alone
            # leaves a reader with no next step.
            if note == "refused (E_BUSY)":
                self._chat().write(
                    "compact: a run is in flight; cancel it first, then compact",
                    kind="error",
                )
            else:
                # `05` §7.5: a refusal leaves the history untouched, and saying so is
                # what stops a reader assuming the conversation is already shorter.
                self._chat().write(
                    f"compact: {note}; the conversation is unchanged", kind="error"
                )
            return
        before = int(result.get("before_bytes") or 0)
        after = int(result.get("after_bytes") or 0)
        kept = int(result.get("kept") or 0)
        summarised = int(result.get("summarised") or 0)
        self._chat().write(
            f"summarised {summarised} message{'s' if summarised != 1 else ''}, "
            f"kept {kept}, {before} B -> {after} B"
        )
        # `05` §7.5: the summary is now the only copy, and saying so is what keeps the
        # command trustworthy — a reader who had not exported the transcript has just
        # lost it.
        self._chat().write(
            "the summary is now the only copy of the conversation; "
            "/export writes the chat log to a file"
        )
        self._chat().write(result.get("summary") or "(the device sent no summary text)")


    async def _thinking(self, rest: list[str]) -> None:
        """`05` §7.2: a typed level writes; no level offers one to choose.

        The level is validated before any frame is sent: the device accepts any flat
        `extra_body` member and turns every call into a 400, so a refused word has to
        be refused here, where it can be named. The set comes from the model the entry
        names, not from the provider row — a row is a statement about every model of
        that provider and cannot say that one of them takes `max` (`05` §7.1).
        """
        # Both forms read the stored entry: the write is a read-modify-write, and the
        # picker needs the entry to know what to offer. Without this the line said "no
        # provider is not in this bridge's catalog" against an empty `_config`, which
        # is a report on a document nobody asked the device for.
        if not self._config_fetched:
            await self._fetch_config()
        chain = self._existing("llm.providers")
        levels = await self._model_levels(chain)
        if rest:
            await self._thinking_set(rest[0], chain, levels)
            return
        # No argument is the picker, not a report: a level the reader can only read
        # is a level they copy into a second command by hand. The modal is offered
        # only where there is something to offer — a provider with no levels, or one
        # this bridge cannot describe, has no rows to show, so the sentence that
        # names why is the whole answer and a box would be a dead end.
        offered = onboard.thinking_levels(chain, levels)
        if not offered:
            self._chat().write(onboard.thinking_line("", chain, levels))
            return
        entry_now, _ = onboard.provider_entry(chain)
        choice = await self.push_screen_wait(
            _PickBox(
                [(lvl, "set this level") for lvl in offered],
                title=f"thinking — {(entry_now or {}).get('id', '')}",
            )
        )
        if choice is None:
            self._chat().write("thinking: cancelled")
            return
        await self._thinking_set(choice, chain, levels)

    async def _thinking_set(
        self, level: str, chain: object, levels: "tuple[str, ...] | None"
    ) -> None:
        """Write `level` and report the device's answer. `05` §7.2.

        Shared by the typed form and the picker so a level reaches `extra_body` by
        one path: the picker choosing a row and the reader typing the same word must
        not differ in what is validated or what is sent.
        """
        plan, why = onboard.thinking_writes(level, chain, levels)
        if plan is None:
            self._chat().write(why, kind="error")
            return
        async with self._working(
            "writing the thinking level", f"config.set llm.providers ({level})"
        ):
            ok, refused = await self._send_body(plan[0][0], plan[0][1])
        if not ok:
            self._chat().write(f"thinking: refused ({refused})", kind="error")
            return
        # The device's own answer, not the request's: `config.set` reports `applied`
        # only when the running chain re-read the document (`04` §4.9). The refresh is
        # quiet: a whole-document reply navigates, and `/thinking` reading the
        # document as an input must not move the reader onto the settings grid.
        await self._fetch_config()
        self._chat().write(f"thinking: {level}")

    async def _tool(self, rest: list[str]) -> None:
        """`05` §7.4: read, or read-modify-write, `tools.disabled`.

        One array feeds both the capability list and dispatch, so a tool turned off is
        absent from the next run's request rather than shown to the model and then
        refused. `config.set` replaces a value, so the whole array is written.

        `list` renders `tools.list` (`04` §4.29) rather than `hello_ack`'s
        capabilities: the catalogue carries the descriptions and the tools the model
        cannot call, and `/tools` shares this code path so the two surfaces cannot
        disagree again.
        """
        verb = rest[0] if rest else ""
        if verb not in ("list", "enable", "disable") or len(rest) > 2 or (
            verb != "list" and len(rest) != 2
        ):
            self._chat().write("usage: /tool list|<enable|disable> <name>", kind="error")
            return
        if not self._config_fetched:
            await self._fetch_config()
        stored = self._existing("tools.disabled")
        disabled = [n for n in stored if isinstance(n, str)] if isinstance(stored, list) else []

        if verb == "list":
            # `_on_command_result` renders the table, so this is the whole read form:
            # doing it here as well would print it twice.
            await self._request("tools.list")
            return

        name = rest[1]
        if verb == "disable":
            if name in disabled:
                self._chat().write(f"tool disable: {name} is already disabled", kind="error")
                return
            wanted = disabled + [name]
        else:
            if name not in disabled:
                self._chat().write(f"tool enable: {name} is not disabled", kind="error")
                return
            wanted = [entry for entry in disabled if entry != name]

        async with self._working(f"{verb} {name}", "config.set tools.disabled"):
            ok, refused = await self._send_body("config", {"tools.disabled": wanted})
        if not ok:
            self._chat().write(f"tool {verb}: refused ({refused})", kind="error")
            return
        # The write is the whole effect: the next run rebuilds its tool list from this
        # array. `applied` is read only to say whether the device took it, and this
        # path has no screen to repaint.
        await self._fetch_config()
        # The whole array is the state a reader has to reason about, so it is what the
        # reply shows; `disabled: none` is the case they are usually aiming for.
        self._chat().write(
            f"tool {verb} {name}: disabled = {', '.join(wanted) if wanted else 'none'}"
        )

    async def _wifi_list(self) -> None:
        """`05` §7.7: every configured network, and whether the radio can hear it.

        Two frames, both reads. The document says which networks exist and in what
        order they are tried; the scan says which are audible now. Neither is a
        substitute, and a configured network the scan did not return is drawn rather
        than dropped.
        """
        if not self._config_fetched:
            await self._fetch_config()
        stored = self._existing("wifi.networks")
        networks = [n for n in stored if isinstance(n, dict)] if isinstance(stored, list) else []
        if not networks:
            self._chat().write("wifi list: no networks configured")
            return
        # Asked for, not read off `_last_status`: the cached frame is whatever arrived
        # last, and a row claiming `joined` from a status the reader moved past is the
        # same defect `_sys_info` avoids by refreshing before it opens.
        status = await self._request("wifi.status")
        if status is None:
            # `_request` already reported why; a second line would only paraphrase it.
            return
        scan = await self._request("wifi.scan")
        # A refused scan is passed on as `None`, not as an empty one: the rows then say
        # `unknown` instead of `not in range`, which would be a claim about the radio
        # the device never made. `_request` has already named the refusal.
        lines = wifi_known_lines(status, scan, networks)
        for line in lines:
            self._chat().write(line)

    async def _wifi_edit(self) -> None:
        """`05` §7.7: pick a known network to rekey or forget, or add one.

        Two questions, both in a modal: which network, then what to do with it. The
        row per action that this used to draw made the list grow by two every time a
        network was added, and a reader with eight networks scrolled past sixteen rows
        to find the one they meant.

        Every gesture writes the whole `wifi.networks` array, because `config.set`
        replaces a value and `04` §4.9 will not create an array index. A passphrase is
        written before the array that names it, so the reference is never briefly
        unresolved (`03-config.md` rule 9).
        """
        if not self._config_fetched:
            await self._fetch_config()
        stored = self._existing("wifi.networks")
        networks = [n for n in stored if isinstance(n, dict)] if isinstance(stored, list) else []

        # `add` is a row of its own even when the array is empty: it is the gesture
        # that makes the list non-empty, and the alternative — no rows at all — is a
        # modal that cannot be acted on.
        # An ssid may be anything, including this row's own label, so the label is
        # grown until it names no configured network: a collision would make one of
        # the two rows unreachable whichever way the answer is read.
        add_label = "add a network"
        while any(str(n.get("ssid") or "") == add_label for n in networks):
            add_label += " (new)"
        rows: list[tuple[str, str]] = [(add_label, "from the scan, or by hand")]
        for entry in networks:
            ssid = str(entry.get("ssid") or "")
            rows.append((ssid, f"priority {entry.get('priority', 0)} — change or forget"))
        choice = await self.push_screen_wait(_PickBox(rows, title="Wi-Fi networks"))
        if choice is None:
            self._chat().write("wifi edit cancelled")
            return
        if choice != add_label:
            await self._wifi_edit_one(choice, networks)
            return

        rows = [
            ("from the scan", "pick a network the radio can hear"),
            ("by hand", "type an ssid the scan cannot see"),
        ]
        choice = await self.push_screen_wait(
            _PickBox(rows, title="Add a network")
        )
        if choice is None:
            self._chat().write("wifi add cancelled")
            return
        if choice == "by hand":
            answer = await self.push_screen_wait(WifiCustomForm("Add a network"))
            if answer is None:
                self._chat().write("wifi add cancelled")
                return
            await self._wifi_add(answer, networks)
            return
        await self._wifi_add_scanned(networks)

    async def on_context_modal_request_block(
        self, event: "ContextModal.RequestBlock"
    ) -> None:
        """`05` §7.5: fetch one block's text for the modal's right pane.

        The request goes through `parse` for the same reason `_wifi_scan_quiet` does:
        the app cannot name a command the parser refuses. The reply is deliberately
        not routed through `_on_command_result`, which would open a second modal on
        top of the one already asking.
        """
        block = event.block
        try:
            async with self._working("reading the block", "context.block"):
                reply = await self.session.request(
                    Frame(
                        type="command",
                        id=new_id("b"),
                        ts=0,
                        body={"name": "context.block", "args": {"block": block}},
                    )
                )
        except (SessionError, asyncio.TimeoutError):
            self._chat().write("the block could not be read", kind="error")
            return
        if reply.type != "command.result" or not reply.body.get("ok"):
            error = reply.body.get("error") or {}
            self._chat().write(
                f"block refused ({error.get('code') or 'no answer'})", kind="error"
            )
            return
        result = reply.body.get("result") or {}
        screen = self.screen
        if not isinstance(screen, ContextModal):
            # The reader closed the box while the frame was in flight; there is no
            # pane to fill and writing the text to chat would answer a question they
            # have already dismissed.
            return
        screen.show_block(
            block,
            str(result.get("text") or ""),
            bool(result.get("truncated")),
        )

    async def _wifi_scan_quiet(
        self,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """One `wifi.scan`, its rows going nowhere, and the two things to say.

        `_request` hands its reply to `_on_command_result`, which prints the scan as
        chat lines — right for `/wifi scan`, wrong for a modal that is already showing
        the same list. The frame comes from `parse` for the same reason the wizard
        does it: the app cannot name a command the parser refuses.

        Returns `(networks, refused, truncated)`. The two notes are separate because a
        scan can succeed *and* be partial, and a caller that treated the truncation as
        a refusal would throw away the rows it did return.
        """
        try:
            async with self._working(
                "scanning for networks",
                "wifi.scan; a few seconds, longer in a busy band",
            ):
                result, note = await self._quiet("wifi.scan")
        except (SessionError, asyncio.TimeoutError):
            return [], "scan unavailable", None
        if not isinstance(result, dict):
            # `05` §7.2's notes are pinned by `test_ui_app_errors.py`, and the code the
            # device named is the part a reader acts on.
            if note and note.startswith("refused ("):
                return [], f"scan {note}", None
            return [], "scan unavailable", None
        networks = [n for n in result.get("networks") or [] if isinstance(n, dict)]
        truncated = None
        if result.get("truncated"):
            # A truncated list is a partial one: saying nothing would read as "these
            # are all the networks in range", which the device did not claim.
            truncated = f"{len(networks)} networks shown; the list was truncated"
        return networks, None, truncated

    async def _wifi_add_scanned(self, networks: list[dict[str, Any]]) -> None:
        """`add from the scan`: the scan's own list, rescannable, with no chat output.

        The scan's rows are the only place its answer is shown. Printing them and then
        asking which one would make the modal a caption for a list already scrolled
        past, and `Ctrl+R` would have to redraw a pane it does not own.
        """
        known = {str(n.get("ssid") or "") for n in networks}
        note = ""
        while True:
            heard_all, refused, truncated = await self._wifi_scan_quiet()
            if refused:
                # Not the same as a scan that heard nothing: the radio is off, so
                # there is no list to add from, and "nothing new" would blame the
                # access points for a station that cannot hear any.
                heard: list[dict[str, Any]] = []
                note = f"{refused}; check the radio, then Ctrl+R"
            else:
                heard = [
                    n for n in heard_all
                    if str(n.get("ssid") or "") not in known
                ]
                # Cleared, not overwritten: a refresh that works after one that did
                # not must stop showing the first one's reason. A truncation note
                # outlives the emptiness check, because a short list explains it.
                note = truncated or (
                    "" if heard else "heard nothing new; Ctrl+R scans again"
                )
            answer = await self.push_screen_wait(
                WifiScanForm("Add a network", scan=heard, note=note)
            )
            if answer is None:
                self._chat().write("wifi add cancelled")
                return
            if answer.get("refresh"):
                continue
            await self._wifi_add(answer, networks)
            return

    async def _wifi_add(
        self, answer: dict[str, Any], networks: list[dict[str, Any]]
    ) -> None:
        """Write one network: its secret first, then the array that names it."""
        ssid = str(answer["ssid"])
        password = str(answer.get("password") or "")
        if any(str(n.get("ssid") or "") == ssid for n in networks):
            self._chat().write(
                f"wifi add: {ssid} is already configured (change its passphrase instead)",
                kind="error",
            )
            return
        entry: dict[str, Any] = {"ssid": ssid, "priority": len(networks)}
        if password:
            key = wifi_secret_key(ssid)
            ok, refused = await self._send_body(
                "secret", {"key": key, "value": password}
            )
            if not ok:
                self._chat().write(f"wifi add: secret refused ({refused})", kind="error")
                return
            entry["password_ref"] = f"nvs:{key}"
        async with self._working(f"add {ssid}", "config.set wifi.networks"):
            ok, refused = await self._send_body(
                "config", {"wifi.networks": networks + [entry]}
            )
        if not ok:
            self._chat().write(f"wifi add: refused ({refused})", kind="error")
            return
        await self._fetch_config()
        self._chat().write(f"wifi add {ssid}: {len(networks) + 1} networks configured")

    async def _wifi_edit_one(
        self, ssid: str, networks: list[dict[str, Any]]
    ) -> None:
        """The second question: what to do with the chosen network."""
        rows = [
            ("change the passphrase", "replace the stored nvs secret"),
            ("forget it", "remove it from the list"),
        ]
        choice = await self.push_screen_wait(
            _PickBox(rows, title=ssid)
        )
        if choice is None:
            self._chat().write("wifi edit cancelled")
            return

        if choice == "change the passphrase":
            answer = await self.push_screen_wait(
                WifiCustomForm(f"New passphrase for {ssid}", ssid=ssid)
            )
            if answer is None:
                self._chat().write("wifi passphrase: cancelled")
                return
            password = str(answer.get("password") or "")
            if not password:
                # An empty field is a cancel, not a request to blank the credential.
                self._chat().write("wifi passphrase: nothing entered, nothing written")
                return
            # The row was built from `networks` and `_PickBox` answers only a label it
            # was handed, so the entry exists. The ref is used when there is one,
            # because a network `/onboard` seeded names `wifi_psk` and deriving a new
            # key would leave the array pointing at a secret the passphrase never
            # reaches.
            entry = next(n for n in networks if str(n.get("ssid") or "") == ssid)
            ref = str(entry.get("password_ref") or "")
            key = ref[4:] if ref.startswith("nvs:") else wifi_secret_key(ssid)
            async with self._working(f"passphrase for {ssid}", "secret.set"):
                ok, refused = await self._send_body(
                    "secret", {"key": key, "value": password}
                )
            if not ok:
                self._chat().write(f"wifi passphrase: refused ({refused})", kind="error")
                return
            self._chat().write(f"wifi passphrase for {ssid}: stored as nvs:{key}")
            return

        if len(networks) <= 1:
            # `wifi.networks` is 1..8: the empty list is a document the device
            # refuses, so this answers here rather than sending a doomed write.
            self._chat().write(
                "wifi forget: the last network cannot be removed "
                "(wifi.networks holds at least one)"
            )
            return
        kept = [n for n in networks if str(n.get("ssid") or "") != ssid]
        if not await self.push_screen_wait(ConfirmPrompt(f"Forget {ssid}?")):
            self._chat().write("wifi forget cancelled")
            return
        async with self._working(f"forget {ssid}", "config.set wifi.networks"):
            ok, refused = await self._send_body("config", {"wifi.networks": kept})
        if not ok:
            self._chat().write(f"wifi forget: refused ({refused})", kind="error")
            return
        await self._fetch_config()
        # The secret is deliberately not erased: another network may name it, and
        # `secret.delete` refuses a key a live `*_ref` still names (`04` §4.31).
        self._chat().write(
            f"wifi forget {ssid}: {len(kept)} networks configured (its secret is kept)"
        )

    async def _providers(self) -> None:
        """`05` §7.8: add, log in to, or remove an `llm.providers` entry.

        Every gesture writes the whole array, because `config.set` replaces a value and
        `04` §4.9 will not create an array index. `add` runs `ProviderStep` and
        `ModelStep`, the same two steps `/onboard` uses, so the model offered is one
        the key actually unlocked.
        """
        if not self._config_fetched:
            await self._fetch_config()
        stored = self._existing("llm.providers")
        entries = [p for p in stored if isinstance(p, dict)] if isinstance(stored, list) else []

        rows: list[tuple[str, str]] = [
            ("add a provider", "catalog, url, key, then a model from its list")
        ]
        for entry in entries:
            pid = str(entry.get("id") or "")
            rows.append((f"log in to {pid}", "replace its stored api key"))
            rows.append((f"disconnect {pid}", "remove it and erase its key"))
        choice = await self.push_screen_wait(
            _PickBox(rows, header=provider_lines(entries))
        )
        if choice is None:
            self._chat().write("providers cancelled")
            return

        if choice == "add a provider":
            catalog_entries, notes = providers_mod.catalog()
            for note in notes:
                self._chat().write(note)
            answers = await self.push_screen_wait(
                ProviderStep(dict(self._config), catalog_entries)
            )
            if answers is None:
                self._chat().write("provider add cancelled")
                return
            if (answers.provider or "custom") in {
                str(e.get("id") or "") for e in entries
            }:
                self._chat().write(
                    f"provider add: {answers.provider} already exists (use log in)",
                    kind="error",
                )
                return
            if not answers.base_url or not answers.api_key:
                self._chat().write(
                    "provider add: a base url and an api key are both required"
                )
                return
            models = await self._fetch_models(answers)
            if not models:
                self._chat().write(
                    f"provider add: {answers.provider or 'that provider'} published no "
                    "model list with that key, so nothing was written"
                )
                return
            picked = await self.push_screen_wait(
                ModelStep(
                    models,
                    answers.provider,
                    "",
                    "none",
                    catalog_entries,
                    {},
                )
            )
            if picked is None:
                self._chat().write("provider add cancelled")
                return
            answers.model = picked.model
            answers.extra_body = dict(picked.extra_body)
            # One key per provider, so removing one row cannot blank another's
            # (`05` §7.8). The id is the unique member the schema already enforces.
            secret_key = provider_secret_key(answers.provider or "custom")
            plan = [
                ("secret", {"key": secret_key, "value": answers.api_key}),
                (
                    "config",
                    {
                        "llm.providers": entries
                        + [
                            {
                                "id": answers.provider or "custom",
                                "base_url": answers.base_url,
                                "model": answers.model,
                                "api_key_ref": f"nvs:{secret_key}",
                            }
                        ]
                    },
                ),
            ]
            for line in onboard.narration(answers, dict(self._config), "provider"):
                self._chat().write(f"provider add: {line}")
            if not await self._apply(plan, "provider add"):
                return
            await self._fetch_config()
            self._chat().write(
                f"provider add {answers.provider or 'custom'}: "
                f"{len(entries) + 1} providers configured"
            )
            return

        pid = (
            choice.split(" to ", 1)[1]
            if choice.startswith("log in")
            else choice.split(" ", 1)[1]
        )
        # `_PickBox` answers a label it was handed, and every label here was built from
        # `entries`, so the row exists: a miss would need the modal to invent a string.
        entry = next(e for e in entries if str(e.get("id") or "") == pid)
        ref = str(entry.get("api_key_ref") or "")
        secret_key = ref[4:] if ref.startswith("nvs:") else provider_secret_key(pid)

        if choice.startswith("log in"):
            key = await self.push_screen_wait(SecretPrompt(secret_key))
            if not key:
                self._chat().write("provider login: cancelled, nothing written")
                return
            async with self._working(f"log in to {pid}", f"secret.set {secret_key}"):
                ok, refused = await self._send_body(
                    "secret", {"key": secret_key, "value": key}
                )
            if not ok:
                self._chat().write(f"provider login: refused ({refused})", kind="error")
                return
            self._chat().write(f"provider login {pid}: key replaced (nvs:{secret_key})")
            return

        if len(entries) <= 1:
            # `llm.providers` is 1..4: an empty array is a document the device
            # refuses, so this answers here rather than sending a doomed write.
            self._chat().write(
                "provider disconnect: the last provider cannot be removed "
                "(llm.providers holds at least one)"
            )
            return
        if not await self.push_screen_wait(
            ConfirmPrompt(f"Disconnect {pid} and erase nvs:{secret_key}?")
        ):
            self._chat().write("provider disconnect cancelled")
            return
        kept = [e for e in entries if str(e.get("id") or "") != pid]
        async with self._working(f"disconnect {pid}", "config.set llm.providers"):
            ok, refused = await self._send_body("config", {"llm.providers": kept})
        if not ok:
            self._chat().write(f"provider disconnect: refused ({refused})", kind="error")
            return
        # The array first, then the key: `secret.delete` refuses a key a live `*_ref`
        # still names (`04` §4.31), and another entry may name this one.
        async with self._working(f"erase {secret_key}", f"secret.delete {secret_key}"):
            ok, refused = await self._send_body(
                "delete", {"name": "secret.delete", "args": {"key": secret_key}}
            )
        await self._fetch_config()
        if not ok:
            self._chat().write(
                f"provider disconnect {pid}: removed; its key was kept ({refused})"
            )
            return
        self._chat().write(
            f"provider disconnect {pid}: {len(kept)} providers configured, "
            f"nvs:{secret_key} erased"
        )

    def _sys_info(self, *, diag_first: bool) -> None:
        """`05` §5.10: the status halves in one scrollable box, the asked-for one first.

        Both come from one `status` frame, which is why this is a modal and not two
        screens: the general half is `render_status` and the counters are
        `diag_sections`, and the reader usually wants the second while looking at the
        first.
        """
        general = ("", render_status(self._last_status))
        # `05` §5.10: the counters arrive as four headed sections, because the box
        # already draws headings and eight unlabelled lines give a reader no seam.
        diag = diag_sections(self._last_status)
        sections = diag + [general] if diag_first else [general] + diag
        self.push_screen(InfoModal("sys", sections))

    async def _model_levels(
        self, chain: object
    ) -> "tuple[str, ...] | None":
        """The levels the stored model takes, or `None` when nothing knows.

        `None` means "ask the row", which is what both callers do when this answers
        nothing. The row is the intersection over the provider's models, so it is a
        safe answer when no per-model surface exists (`05` §7.1).

        Only the per-model endpoint is consulted, deliberately: OpenRouter carries each
        model's `supported_efforts` inline in its catalog, but that list is 466 names,
        and re-fetching it to answer `/thinking` would cost more than the row's
        narrower answer is worth. The wizard's step 3 already has that list and reads
        it there.
        """
        entry_now, _ = onboard.provider_entry(chain)
        if entry_now is None:
            return None
        provider_id = str(entry_now.get("id") or "")
        model = str(entry_now.get("model") or "")
        known = providers_mod.find(provider_id) if provider_id else None
        if known is None or not known.show_url or not model:
            return None
        async with self._working(
            "asking the provider which levels this model takes",
            f"{model}: one POST, no API key (it stays on the device)",
        ):
            return await asyncio.to_thread(
                providers_mod.levels_for, known, model, "", _http_fetch
            )

    async def _model(self, rest: list[str]) -> None:
        """`05` §7.3: pick from the provider's published list, or name one directly.

        The write is a read-modify-write of `llm.providers`, so every other member
        of the entry survives. The list is fetched without a key — `06` A1 keeps it
        in NVS and no frame reads it back — so a provider that guards `/models`
        yields no list and the typed name is the route.
        """
        if not self._config_fetched:
            await self._fetch_config()
        chain = self._existing("llm.providers")
        entry, why = onboard.provider_entry(chain)
        if entry is None:
            self._chat().write(f"model: {why}", kind="error")
            return
        provider_id = str(entry.get("id") or "")
        stored = str(entry.get("model") or "")
        if rest:
            chosen: str | None = rest[0]
        else:
            known = providers_mod.find(provider_id) if provider_id else None
            label = _provider_label(known, provider_id, entry.get("base_url"))
            names = await self._model_list(entry, provider_id, label, known)
            if not names:
                self._chat().write(
                    f"{label}: no model list (it is fetched without the key, which "
                    "stays on the device); type the model name",
                    kind="warn",
                )
            chosen = await self.push_screen_wait(
                ModelPicker(provider_id, label, names, stored)
            )
            if chosen is None:
                self._chat().write("model: cancelled")
                return
        plan, why = onboard.model_writes(chosen, chain)
        if plan is None:
            self._chat().write(f"model: {why}", kind="error")
            return
        async with self._working("writing the model", f"config.set ({chosen})"):
            ok, refused = await self._send_body(plan[0][0], plan[0][1])
        if not ok:
            self._chat().write(f"model: refused ({refused})", kind="error")
            return
        # Quiet, for the same reason `/thinking`'s refresh is: the reader asked to
        # set a model, not to be moved onto the settings grid (`05` §7.3).
        await self._fetch_config()
        self._chat().write(f"model: {chosen}")

    async def _model_list(
        self,
        entry: dict[str, Any],
        provider_id: str,
        label: str,
        known: "providers_mod.Provider | None",
    ) -> list[str]:
        """The names the provider publishes, or `[]`.

        The URL comes from the entry's own `base_url`, not from the catalog row: a
        device can store `https://ollama.com/v1` while the catalog id `ollama`
        points at `127.0.0.1`, and a `custom` entry has no row at all
        (`05` §7.3). The catalog URL is the fallback when no `base_url` is stored.
        """
        base_url = str(entry.get("base_url") or "").rstrip("/")
        url = f"{base_url}/models" if base_url else (
            known.models_url if known is not None else ""
        )
        if not url:
            return []
        probe = providers_mod.Provider(
            id=provider_id, label=label, base_url=base_url, models_url=url
        )
        async with self._working(
            "asking the provider for its models",
            f"{label}: one GET, no API key (it stays on the device)",
        ):
            # No `try`: `models_with_efforts` answers `[]` for every failure — a 401,
            # a 403 and a refused connection alike — so the empty list is the signal
            # and a wrapper here would be a branch that cannot run.
            catalog = await asyncio.to_thread(
                providers_mod.models_with_efforts, probe, "", _http_fetch
            )
        return [name for name, _ in catalog]

    def _run_local(self, head: str) -> None:
        if head == "logs":
            self.action_show("logs")
        elif head == "help":
            self.action_help()
        elif head == "mcp":
            servers = self._last_status.get("mcp") or []
            self._chat().write(
                ", ".join(f"{s.get('id')}={s.get('state')}" for s in servers) or "no MCP servers"
            )
        elif head == "usage":
            self._chat().write(f"total tokens: {self._usage_total}")
        elif head == "export":
            self._export()
        elif head == "quit":
            self.exit()

    def _export(self) -> None:
        """`05` §7: write the chat log to `bridge-export-<ts>.md` in the cwd.

        The file, not the widget, is the transcript: `RichLog` stores width-cropped
        `Strip`s trimmed to the scrollback cap, so reading the pane back would write
        text cut at the terminal's width. The failure is reported and the pane is
        left alone — a reader who cannot write the cwd still has their scrollback.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = f"bridge-export-{stamp}.md"
        # No code fence around the body: an assistant reply can itself contain one,
        # and a transcript wrapped in a fence it also closes is not a transcript.
        lines = [f"# musteval transcript {stamp}", "", *self._chat().transcript, ""]
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
        except OSError as exc:
            self._chat().write(f"could not write {path}: {exc.strerror or exc}", kind="error")
            return
        self._chat().write(f"exported {path}")


def _provider_label(
    known: "providers_mod.Provider | None", provider_id: str, base_url: object
) -> str:
    """How `/model` names the provider it is about to ask.

    The catalog label describes the catalog row, not the entry: a device can carry
    `id: ollama` with `base_url: https://ollama.com/v1`, and calling that "Ollama
    (local)" points the reader at a server that is not there (`05` §7.3). When the
    stored base URL is not the row's own, the host is the honest name.
    """
    stored = str(base_url or "").rstrip("/")
    if known is not None and (not stored or stored == known.base_url.rstrip("/")):
        return known.label
    host = stored.split("://", 1)[-1].split("/", 1)[0]
    return host or (known.label if known is not None else provider_id or "provider")


def _http_fetch(url: str, key: str, body: dict[str, object] | None = None) -> bytes:
    """`GET url`, or `POST url` when `body` is given, with a bearer token.

    The one socket `/onboard` and `/model` open. It lives in `ui` rather than in
    `providers.py` because `09` §2 keeps L2 pure, and it is a plain `urllib` call
    because the project ships no HTTP client and a handful of fetches do not justify
    a dependency.

    The body form exists for one endpoint: Ollama's `POST /api/show` names the model
    in the request body, and it is the only surface that says which levels a *chosen*
    model takes (`05` §7.1).

    A non-http(s) scheme and a non-200 both raise. `models_with_efforts` and
    `levels_for` turn either into an empty answer, so the `/model` picker still opens
    with its field as the whole route.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"refusing a non-http scheme: {parts.scheme or '(none)'}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    # The rule cannot see the scheme guard above, which closes exactly the `file://`
    # risk it names, so this call is safe.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - scheme pinned above
        return response.read()
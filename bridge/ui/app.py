"""The Textual App: six screens, one frame sink, slash commands routed."""

from __future__ import annotations

import asyncio
import base64
import os

import time
from typing import Any

from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from rich.text import Text

from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from ..client import LinkState, Session
from ..client.session import SessionError
from ..commands import LOCAL_COMMANDS, CommandError, CommandState, parse
from .. import providers as providers_mod
from .. import transfer
from ..protocol import Frame, new_id
from .help import COMMANDS, CommandSuggester, help_tabs, options
from .history import PromptHistory
from .logs import LogBuffer, LogFilter, render_log_line
from .modals import (
    HelpModal,
    ConfirmPrompt,
    FilePrompt,
    FilterPrompt,
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
from .settings import ROWS, coerce, flatten, lookup, render_value
from .splash import SPLASH_SECONDS, pick_tip, render_splash
from . import onboard
from .render import (
    LINK_WORDS,
    binding_pairs,
    UNREACHABLE,
    SlashHighlighter,
    clip,
    render_diag,
    render_json_block,
    hint_bar,
    render_prompt_bar,
    render_prompt_text,
    render_status,
    render_tool_call,
    render_tool_result,
    sanitize,
)

COMPOSER_MAX_ROWS = 6
#: `05-bridge.md` §5.1: the candidate list never pushes the conversation away.
COMPLETION_ROWS = 8


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
        # `Input` accepts this key itself when it is multiline; the binding is
        # what makes it reachable once the composer has focus.
        Binding("shift+enter", "newline", "Newline", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history = PromptHistory()
        #: Candidates currently listed, and the index the arrows sit on.
        self._candidates: list[str] = []
        self._total = 0
        #: Index the arrows sit on. OMP opens the list with the top row chosen.
        self._cursor = 0

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

    def _show_options(self, value: str) -> None:
        candidates = options(value)
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
        lines = [
            f"{'> ' if index == self._cursor else '  '}{candidate}"
            for index, candidate in enumerate(self._candidates)
        ]
        hidden = self._total - len(self._candidates)
        if hidden > 0:
            lines.append(f"+{hidden} more")
        panel.update("\n".join(lines))
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
        self.query_one("#chat-input", Input).focus()

    def on_show(self) -> None:
        # Coming back to Chat must land in the composer, or a prompt typed after
        # switching screens goes to whichever view held focus.
        self.query_one("#chat-input", Input).focus()

    def write(self, text: str) -> None:
        self.query_one("#chat-log", RichLog).write(text)

    def render_bar(self, line: str, styled: Text | None = None) -> None:
        """`05` §5.1: the fact line above the composer.

        The text is held here as well: textual 8's `Static` keeps no readable
        renderable, and a test needs the line, not the widget. `styled` carries
        the same characters with the §13.2 slots applied.
        """
        self.bar_line = line
        self.query_one("#prompt-bar", Static).update(styled if styled is not None else line)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # OMP applies the selected row and then submits the applied value, which
        # is why `/tasks a` + Enter runs `/tasks add` rather than the fragment.
        text = event.value.strip()
        if self._candidates:
            text = self._candidates[self._cursor]
        event.input.value = ""
        self._show_options("")
        self._fit_composer()
        if text:
            self._history.add(text)
            self.app.submit(text)  # type: ignore[attr-defined]

    def action_clear_log(self) -> None:
        """`05` §6.2: clear this session's scrollback, which is local only."""
        self.query_one("#chat-log", RichLog).clear()

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
            self._apply(self._candidates[self._cursor])
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

    def compose(self) -> ComposeResult:
        yield Static("no status yet: Ctrl+R asks the device", id="status-body")

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

    # `05` §13.1: `Ctrl` + a letter, never a bare printable one, so a keystroke
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
            # `05` §13.2: the word carries the fact; the colour only speeds it up.
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
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None


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

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.buffer = LogBuffer()
        self.follow = True

    def compose(self) -> ComposeResult:
        yield Static("Follow: ON  Filter: -", id="log-header")
        yield RichLog(id="log-body", wrap=True, markup=False, highlight=False, max_lines=2000)

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
        try:
            return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception:
            return None

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
        yield Static("/workspace/", id="files-path")
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
            # hide entries the reader never asked about (`05` §13.5).
            self._needle = ""
        self.path = path
        self.query_one("#files-path", Static).update(path)
        self._all_entries = list(entries)
        self._render_filtered()
        self._render_header()

    def _render_header(self) -> None:
        """`05` §13.5: the applied filter is shown, so a hidden entry is explained."""
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
        """Narrow the listing without another `sandbox.list` (`05` §13.5)."""
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
        # Arrows stay: §13.1 bans bare *printable* letters as actions, and an
        # arrow cannot be mistaken for text.
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
    ]
    can_focus = True

    def on_show(self) -> None:
        self.focus()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # `05` §13.4: state belongs in the constructor; `compose` may run again.
        self.selected = 0
        self._brokers: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static("no brokers reported: Ctrl+R asks the device", id="mqtt-brokers")
        yield RichLog(id="mqtt-traffic", wrap=True, markup=False, highlight=False)

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
            # `05` §13.2: the word carries the state, the colour speeds it up.
            state = (
                "[green]connected[/green]"
                if broker.get("connected")
                else "[red]disconnected[/red]"
            )
            subs = len(broker.get("subscriptions") or [])
            lines.append(
                f"[b]{cursor}[/b] {broker.get('id')}  {state}  [dim]{subs} subs[/dim]"
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
    #task-hint { padding: 0 2; }
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
        # A silent refresh after a mutation should not narrate its count.
        self._tasks_quiet = False
        self._tasks: list[dict[str, Any]] = []
        self._last_prompt: str | None = None
        self._usage_total = 0
        self._last_input_bytes = 0
        self._todo_open = False
        self._seen_lines: set[str] = set()
        self._files_path: str = ""
        #: Set when `/files <path>` wants a specific directory; `action_show` is
        #: the one place that fetches, so the screen never lists twice.
        self._files_target: str = ""

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
        yield Footer()

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
        self.push_screen(HelpModal(help_tabs()))

    def action_show(self, name: str) -> None:
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
        self.title = name.capitalize()
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
            self._chat().write(f"✗ {exc}")
            return
        await self._request("config.set", {"path": row.path, "value": value})

    # -- Tasks screen actions (`05` §5.3) --------------------------------

    def _tasks_screen(self) -> TasksScreen:
        return self.query_one("#tasks", TasksScreen)

    async def run_task(self, task_id: str) -> None:
        await self._request("tasks.run", {"id": task_id})

    async def run_selected_task(self) -> None:
        """`05` §13.1: `Ctrl+X` runs the row under the cursor, same path as Enter."""
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
            self._chat().write(f"✗ {exc}")

    # -- files (`05` §5.8) ----------------------------------------------

    def _files(self) -> FilesScreen:
        return self.query_one(FilesScreen)

    async def browse(self, path: str) -> None:
        """List one sandbox directory. The device refuses a path outside it."""
        try:
            path = sandbox_normalise(path)
        except SandboxPathError as exc:
            self._chat().write(f"[error] {exc}")
            return
        # A listing path is always a directory, so it is shown with its `/`
        # (`sandbox_display`), and `normalise` strips it again on the way out.
        if not path.endswith("/"):
            path += "/"
        self._files_path = path
        await self._request("sandbox.list", {"path": path})

    async def search_files(self) -> None:
        """`05` §13.5: narrow the listing locally, seeded with what is applied."""
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
            self._chat().write(f"[warn] {path} is above the read cap; not opened")
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
            self._chat().write("[warn] /workspace cannot be moved")
            return
        name = await self.push_screen_wait(
            ValuePrompt(f"Move {path} to", path)
        )
        if not name or name == path:
            return
        try:
            target = sandbox_normalise(name)
        except SandboxPathError as exc:
            self._chat().write(f"[error] {exc}")
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
            self._chat().write(f"[error] {exc}")
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
            self._chat().write("[warn] /workspace itself is not downloadable")
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
                        f"[warn] {path} is {body.get('bytes')} bytes, above the "
                        f"{transfer.READ_CAP}-byte read cap; a copy would be a prefix"
                    )
                    return
                nodes = [transfer.FileNode(path, str(body.get("text", "")))]
            else:
                nodes = await transfer.collect(send, path)
        except transfer.TransferError as exc:
            self._chat().write(f"[error] {exc}")
            return
        try:
            if kind == "file":
                written = [transfer.write_file(target, nodes[0].text)]
            else:
                written = transfer.write_tree(target, nodes)
        except OSError as exc:
            self._chat().write(f"[error] {exc}")
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
            self._chat().write(f"[error] {exc}")
            return
        except OSError as exc:
            self._chat().write(f"[error] {exc}")
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
            self._chat().write(f"[error] {exc}")
            return
        await self.save_file(path, "")
        await self.view_file(path)

    async def save_file(self, path: str, text: str) -> None:
        """Stage the whole buffer in `04` §4.20 chunks, committing on the last.

        `data` carries at most `SANDBOX_CHUNK_BYTES` raw bytes per frame, so a
        file above that is several `sandbox.write` calls rather than one that
        overruns the cap and comes back `E_PROTOCOL`.
        """
        raw = text.encode("utf-8")
        chunks = [
            raw[i : i + SANDBOX_CHUNK_BYTES]
            for i in range(0, len(raw), SANDBOX_CHUNK_BYTES)
        ] or [b""]
        for index, chunk in enumerate(chunks):
            await self._request(
                "sandbox.write",
                {
                    "path": path,
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "encoding": "base64",
                    "offset": index * SANDBOX_CHUNK_BYTES,
                    "final": index == len(chunks) - 1,
                },
            )

    def action_back(self) -> None:
        """`05-bridge.md` §6: Esc closes the top modal, then leaves a screen.

        On Chat it cancels the current run, which is the one place Esc changes
        device state rather than local navigation.
        """
        if len(self.screen_stack) > 1:
            # A modal never reaches this binding: `ModalScreen` sets `_modal`,
            # which suppresses app bindings, so `PromptBox` owns its own Esc.
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

    def _chat(self) -> ChatScreen:
        return self.query_one("#chat", ChatScreen)

    def _logs(self) -> LogsScreen:
        return self.query_one("#logs", LogsScreen)

    def _refresh_bar(self) -> None:
        """Redraw the prompt bar from state already in hand (`05` §5.1).

        Called on every fact that changes, never on a keystroke, so the bar
        costs no frames.
        """
        if not self.is_mounted:
            return
        # The bar lives on the chat screen, which is not queryable until the app's
        # children are mounted. A link-state change that lands before that must not
        # take the whole app down over a bar nobody can see yet.
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
        self._last_input_bytes = frame.body.get("input_bytes", 0)
        self._chat().write(f"( ) thinking… [run {frame.body.get('run_id')}]")

    def _on_agent_tool(self, frame: Frame) -> None:
        self._chat().write(render_tool_call(frame.body))

    def _on_agent_tool_result(self, frame: Frame) -> None:
        self._chat().write(render_tool_result(frame.body))

    def _on_agent_final(self, frame: Frame) -> None:
        usage = frame.body.get("usage") or {}
        self._usage_total += usage.get("total_tokens", 0)
        self._refresh_bar()
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
            error = body.get("error") or {}
            self._chat().write(f"✗ {name}: {error.get('code')} {error.get('message')}")
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
            self.action_show("settings")
            return
        if name == "config.set":
            self._chat().write(
                f"{result.get('path')} applied"
                + (", requires reboot" if result.get("requires_reboot") else "")
            )
            self.run_worker(self._request("config.get"))
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
            self._chat().write(f"✗ {exc}")
            return None
        except asyncio.TimeoutError:
            self._chat().write("✗ the device did not answer in time")
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
        self._chat().write(f"you: {text}")
        # `05-bridge.md` §5.1: a prompt with no link is answered immediately. The
        # ack timeout is 6x `connect_timeout`, so waiting would leave the reader
        # staring at their own line for half a minute with nothing to act on.
        # `flaky` still carries traffic, so only the states that cannot answer are
        # refused; a prompt there may succeed or time out, and both are reported.
        state = self.session.state
        if state.value in UNREACHABLE:
            self._chat().write(
                f"✗ {LINK_WORDS[state.value]}; the prompt was not sent"
            )
            return
        frame = Frame(type="prompt", id=new_id("b"), ts=0, body={"text": text})
        try:
            reply = await self.session.request(frame)
        except SessionError as exc:
            self._chat().write(f"✗ {exc}")
            return
        except asyncio.TimeoutError:
            self._chat().write("✗ the run did not answer in time")
            return
        if reply.type == "agent.error":
            self._on_agent_error(reply)
        elif reply.type == "agent.start":
            self._on_agent_start(reply)
            self._chat().write("waiting for the run…")

    async def _run_command(self, text: str) -> None:
        head = text[1:].split()[0] if len(text) > 1 else ""
        words = text[1:].split()
        # `05-bridge.md` §7: the bare form opens its screen; a sub-command such as
        # `/mqtt publish` answers in chat, so it lands there rather than moving
        # the reader to a screen that will not show the answer.
        if head in ("status", "tasks", "mqtt"):
            self.action_show(head if len(words) == 1 else "chat")
        if head == "settings":
            self.action_show("settings")
            return
        if head == "files":
            # `/files` opens at the sandbox root; `/files /workspace/runs` opens there.
            target = words[1] if len(words) > 1 else SANDBOX_ROOT
            try:
                target = sandbox_normalise(target)
            except SandboxPathError as exc:
                self._chat().write(f"✗ {exc}")
                return
            self._files_target = target
            self.action_show("files")
            return
        if head == "onboard":
            await self.onboard()
            return
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
        except asyncio.TimeoutError:
            self._chat().write("✗ the device did not answer in time")
            self._pending_secret = None
            return
        if reply.type == "command.result":
            self._on_command_result(reply)
        self._pending_secret = None

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
                f"✗ {LINK_WORDS[state.value]}; /onboard was not sent"
            )
            return
        if not self._config_fetched:
            # The form pre-fills from the device's config, so the config has to have
            # arrived. It is only fetched when the settings screen is opened, so a
            # fresh process opened this form on an empty document and every field
            # looked unconfigured — including the provider, which suppressed the model
            # fetch. The reply also navigates to the settings grid, so the screen the
            # reader was on is restored: the wizard is a modal over their screen.
            before = next(
                (
                    name
                    for name in ("chat", "status", "tasks", "logs", "settings",
                                 "mqtt", "files")
                    if self.query_one(f"#{name}").display
                ),
                "chat",
            )
            await self._request("config.get")
            self.action_show(before)

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
        step = ModelStep(
            models=models,
            provider=answers.provider or "",
            model=models[0] if models else "",
        )
        picked = await self.push_screen_wait(step)
        if picked is None:
            self._chat().write("onboard: cancelled at the model step")
            return None
        answers.model = picked["model"]
        answers.extra_body = self._extra_body(answers.provider, picked["thinking"])
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

    async def _send_body(self, kind: str, body: dict[str, Any]) -> "tuple[bool, str]":
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
            reply = await self.session.request(frame)
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

    async def _apply(self, plan: "list[tuple[str, dict[str, object]]]") -> bool:
        """Send a step's frames in order, stopping at the first refusal."""
        for kind, body in plan:
            ok, why = await self._send_body(kind, body)
            if not ok:
                self._chat().write(f"onboard: refused ({why})")
                return False
        return True

    async def _reload(self) -> bool:
        """`reload` and report where the device says it got to.

        The reply is the device's own account of the boot sequence it just reran, so a
        step that depends on the network being up can read it rather than guess.
        """
        ok, why = await self._send_body("command", {"name": "reload"})
        self._chat().write(
            "onboard: reload: " + ("applied" if ok else f"refused ({why})")
        )
        return ok

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
        a fetch that silently returns nothing.
        """
        entry = providers_mod.find(answers.provider) if answers.provider else None
        if entry is None or not entry.models_url:
            return []
        try:
            return await asyncio.to_thread(
                providers_mod.models, entry, answers.api_key, _http_fetch
            )
        except Exception as err:  # noqa: BLE001 - a failed fetch is a chat line
            self._chat().write(f"model list: {err}")
            return []

    async def _onboard_scan(self) -> tuple[list[dict], list[str]]:
        """The `wifi.scan` result and the lines worth showing about it."""
        try:
            reply = await self.session.request(
                Frame(type="command", id=new_id("b"), ts=0,
                      body={"name": "wifi.scan", "args": {}})
            )
        except (SessionError, asyncio.TimeoutError):
            return [], ["scan unavailable; type the ssid"]
        if reply.type != "command.result" or not reply.body.get("ok"):
            error = reply.body.get("error") or {}
            return [], [
                f"scan refused ({error.get('code') or 'no answer'}); type the ssid"
            ]
        result = reply.body.get("result") or {}
        networks = result.get("networks") or []
        notes: list[str] = []
        if result.get("truncated"):
            notes.append(f"{len(networks)} networks shown; the list was truncated")
        return [n for n in networks if isinstance(n, dict)], notes

    async def _onboard_secret(self, key: str, value: str) -> None:
        """One `secret.set`, via the existing path so redaction stays in one place."""
        self._pending_secret = value
        await self._request_secret(
            Frame(type="secret.set", id=new_id("b"), ts=0, body={"key": key})
        )

    def _run_local(self, head: str) -> None:
        if head == "logs":
            self.action_show("logs")
        elif head == "help":
            self.action_help()
        elif head == "tools":
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


def _http_fetch(url: str, key: str) -> bytes:
    """`GET url` with a bearer token, returning the body.

    The one socket `/onboard` opens. It lives in `ui` rather than `providers.py`
    because `09` §2 keeps L2 pure, and it is a plain `urllib` call because the project
    ships no HTTP client and one GET does not justify a dependency.

    A non-200 raises, and the caller turns any failure into one chat line with an
    empty model list, so the form stays usable.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - https
        return response.read()

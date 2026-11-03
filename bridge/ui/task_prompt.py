"""`05-bridge.md` §5.3 `E` and `N`: edit a task dict as JSON before `tasks.set`.

Parsing errors stay in the modal: a bad dict should be fixable, not retyped.
"""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, Static

from .modals import _CSS, PromptBox
from .render import sanitize


class TaskPrompt(PromptBox[dict | None]):
    """JSON editor for one task; Enter sends, Esc and an empty field cancel."""

    BINDINGS = [Binding("enter", "submit", "Send", show=False)]
    CSS = _CSS
    AUTO_FOCUS = "#task-input"

    def __init__(self, task: dict | None = None) -> None:
        super().__init__()
        self._initial_json_dict = task or {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static("task", classes="prompt-title")
            # Canonical JSON per `AGENTS.md`: sorted keys, so the edit starts stable.
            yield Input(value=json.dumps(self._initial_json_dict, sort_keys=True), id="task-input")
            yield Static("", id="task-error", markup=False)
            yield Static("Enter: send | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        raw = self.query_one("#task-input", Input).value.strip()
        if not raw:
            self.dismiss(None)
            return
        try:
            task = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._fail(f"invalid JSON: {exc.msg}")
            return
        if not isinstance(task, dict):
            self._fail(f"a task must be a JSON object, not {type(task).__name__}")
            return
        self.dismiss(task)

    def _fail(self, message: str) -> None:
        # Sanitized because the field can hold a broker payload echoed back.
        self.query_one("#task-error", Static).update(sanitize(message))

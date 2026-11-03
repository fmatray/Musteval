"""Escapable modal prompts for secret entry and destructive confirmation."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_CSS = """
Screen {
    align: center middle;
}

.prompt-box {
    width: 60;
    height: auto;
    border: thick $accent;
    padding: 1 2;
}

.prompt-title {
    text-style: bold;
}

.prompt-hint {
    color: $text-muted;
}
"""


class SecretPrompt(ModalScreen[str | None]):
    """Masked one-value prompt for `05-bridge.md` §8 masked input."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Store", show=False),
    ]

    CSS = _CSS

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(f"secret: {self.key}", classes="prompt-title")
            yield Input(password=True, id="secret-input")
            yield Static("Enter: store | Esc: cancel", classes="prompt-hint")

    def on_mount(self) -> None:
        self.query_one("#secret-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#secret-input", Input).value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmPrompt(ModalScreen[bool]):
    """Yes/no gate for `05-bridge.md` §7 `/tasks delete` confirmation."""

    BINDINGS = [
        Binding("y", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    CSS = _CSS

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(self.question, classes="prompt-title")
            yield Static("y: confirm | n: cancel", classes="prompt-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

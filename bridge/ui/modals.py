"""Escapable modal prompts: secrets, confirmation, file editing and help."""

from __future__ import annotations

from typing import Any, TypeVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TabbedContent, TabPane, TextArea
from textual.widgets.text_area import Selection

from . import onboard

_T = TypeVar("_T")


def _point_to_offset(text: str, row: int, column: int) -> int:
    """Inverse of `_offset_to_point`: where a `(row, column)` pair starts."""
    lines = text.split("\n")
    return sum(len(line) + 1 for line in lines[:row]) + column


def _offset_to_point(text: str, offset: int) -> tuple[int, int]:
    """Line and column of a character offset. Pure, and the only place the
    buffer's newline arithmetic lives."""
    row = text.count("\n", 0, offset)
    column = offset - (text.rfind("\n", 0, offset) + 1)
    return row, column

# `Screen` as a selector does NOT match a `ModalScreen` in Textual 8.2: the
# subclass has its own DEFAULT_CSS (`layout: vertical`), and a type selector on the
# base class loses to it. Every modal was therefore pinned to the top-left corner
# despite the rule below reading correctly. `ModalScreen` matches the subclass, and
# `*` covers the concrete class in case a future Textual changes that.
_CSS = """
ModalScreen, * {
    align: center middle;
}

/* `05` §13.4: width follows the content, bounded by the terminal, so a one-line
   prompt is not stretched and the editor is not pinned narrower than its text. */
.prompt-box {
    width: 60;
    max-width: 100%;
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


class PromptBox(ModalScreen[_T]):
    """A modal that can always be left with Esc.

    `ModalScreen` sets `_modal`, which suppresses app-level bindings, so a modal
    without its own Esc binding traps the user and leaves the `push_screen_wait`
    worker running forever. Inheriting this class is what makes that impossible:
    a subclass's `BINDINGS` extend it rather than replace the guarantee.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = _CSS

    def action_cancel(self) -> None:
        self.dismiss(None)


class SecretPrompt(PromptBox[str | None]):
    """Masked one-value prompt for `05-bridge.md` §8 masked input."""

    BINDINGS = [Binding("enter", "submit", "Store", show=False)]
    CSS = _CSS

    #: Textual focuses this after `_compose` has mounted the children, using a
    #: non-raising `query`. Focusing from `on_mount` instead races the mount and
    #: raises `NoMatches` when the Input is not queryable yet.
    AUTO_FOCUS = "#secret-input"

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(f"secret: {self.key}", classes="prompt-title")
            yield Input(password=True, id="secret-input")
            yield Static("Enter: store | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#secret-input", Input).value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmPrompt(PromptBox[bool]):
    """Yes/no gate for `05-bridge.md` §7 `/tasks delete` confirmation."""

    BINDINGS = [
        Binding("y", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel", show=False),
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


class FilterPrompt(PromptBox[str | None]):
    """One narrowing line: a log filter, or a file search (`05` §5.4, §13.5).

    The two screens want the same widget with a different seed and title, so the
    seed is either a `LogFilter` (read for `tag`/`min_level`) or a plain string.
    """

    BINDINGS = [Binding("enter", "submit", "Apply", show=False)]
    CSS = _CSS
    AUTO_FOCUS = "#filter-input"

    def __init__(
        self,
        current: object = None,
        title: str = "log filter",
        placeholder: str = "tag[:level]",
    ) -> None:
        super().__init__()
        self.current = current
        self.title = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        if isinstance(self.current, str):
            seeded = self.current
        else:
            tag = getattr(self.current, "tag", "") or ""
            level = getattr(self.current, "min_level", "debug")
            seeded = f"{tag}:{level}" if level != "debug" else tag
        with Vertical(classes="prompt-box"):
            yield Static(self.title, classes="prompt-title")
            yield Input(value=seeded, placeholder=self.placeholder, id="filter-input")
            yield Static("Enter: apply | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#filter-input", Input).value.strip() or None)


class ValuePrompt(PromptBox[str | None]):
    """One line of text under a title: a `05` §5.7 config value, or a host path
    for the file transfers of §5.8.1. The title is the question, not a key."""

    BINDINGS = [Binding("enter", "submit", "Apply", show=False)]
    CSS = _CSS
    AUTO_FOCUS = "#value-input"

    def __init__(self, path: str, current: str) -> None:
        super().__init__()
        self.path = path
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box"):
            yield Static(self.path, classes="prompt-title")
            yield Input(value=self.current, id="value-input")
            yield Static("Enter: apply | Esc: cancel", classes="prompt-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # An emptied field is a cancel, not a request to blank the setting.
        self.dismiss(self.query_one("#value-input", Input).value.strip() or None)

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#value-input", Input).value.strip() or None)


class OnboardPrompt(PromptBox["onboard.Answers | None"]):
    """`05` §7 and §13.6: the whole first-run form, grouped by domain.

    One dialog per domain, not one per value: a reader sees everything `wifi.*`
    will become before committing any of it, can revise an earlier answer after
    reading a later one, and knows how much is left. `Ctrl+S` writes the whole
    form; `Esc` writes nothing.
    """

    BINDINGS = [Binding("ctrl+s", "submit", "Write", show=False)]
    CSS = (
        _CSS
        + """
.prompt-box.onboard-box {
    width: 88;
    max-width: 100%;
    max-height: 90%;
}
.onboard-section {
    text-style: bold;
    color: $accent;
    margin-top: 1;
}
.onboard-label {
    color: $text-muted;
}
"""
    )
    AUTO_FOCUS = "#onboard-ssid"

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        """The fields, pre-filled from whatever the device already reports.

        Pre-filling is what makes the form revisable: the previous answer is
        already there, so the reader edits instead of retyping blind.
        """
        existing = self.current or {}
        networks = (existing.get("wifi") or {}).get("networks") or []
        first = networks[0] if networks else {}
        providers = (existing.get("llm") or {}).get("providers") or []
        provider = providers[0] if providers else {}

        with Vertical(classes="prompt-box onboard-box"):
            yield Static("First run", classes="prompt-title")
            with VerticalScroll(id="onboard-body"):
                yield Static("Wi-Fi  → wifi.networks[0]", classes="onboard-section")
                yield Static("ssid", classes="onboard-label")
                yield Input(str(first.get("ssid") or ""), id="onboard-ssid")
                yield Static(
                    "password (the nvs secret wifi_psk; blank leaves it alone)",
                    classes="onboard-label",
                )
                yield Input(password=True, id="onboard-password")

                yield Static("LLM  → llm.providers[0]", classes="onboard-section")
                yield Static("base url", classes="onboard-label")
                yield Input(
                    str(provider.get("base_url") or "https://api.openai.com/v1"),
                    id="onboard-base-url",
                )
                yield Static(
                    "api key (the nvs secret llm_api_key; blank leaves it alone)",
                    classes="onboard-label",
                )
                yield Input(password=True, id="onboard-api-key")
                yield Static("model", classes="onboard-label")
                yield Input(str(provider.get("model") or ""), id="onboard-model")
            yield Static(
                "Ctrl+S: write | Esc: cancel | blank fields are left alone",
                classes="prompt-hint",
            )

    def answers(self) -> onboard.Answers:
        def value(widget_id: str) -> str:
            return self.query_one(widget_id, Input).value.strip()

        return onboard.Answers(
            ssid=value("#onboard-ssid"),
            password=value("#onboard-password"),
            base_url=value("#onboard-base-url"),
            api_key=value("#onboard-api-key"),
            model=value("#onboard-model"),
        )

    def action_submit(self) -> None:
        self.dismiss(self.answers())


class FilePrompt(PromptBox[str | None]):
    """Text editor for one sandbox file (`05` §5.8).

    `Ctrl+S` commits the whole buffer and `Esc` discards it, so a modal left open
    by mistake cannot save by itself.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("ctrl+f", "find", "Find", show=False),
        Binding("f3", "find_next", "Next", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y", "redo", "Redo", show=False),
    ]
    CSS = (
        _CSS
        + """
.prompt-box.file-box {
    width: 90%;
    max-width: 160;
    height: 80%;
}
#file-find { display: none; height: 3; }
#file-find.active { display: block; }
"""
    )
    AUTO_FOCUS = "#file-body"

    HINT = (
        "Ctrl+S: save | Ctrl+F: find/replace | F3: next | "
        "Ctrl+Z/Ctrl+Y: undo/redo | Esc: discard"
    )

    def __init__(self, path: str, text: str) -> None:
        super().__init__()
        self.path = path
        self.text = text
        #: The find bar is a two-step prompt: `finding` asks for the needle,
        #: `replacing` asks what to put there. A dict avoids a second modal and
        #: keeps the caret in the buffer while the query is typed.
        self._query = ""
        self._finding = False
        self._replacing = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="prompt-box file-box"):
            yield Static(self.path, classes="prompt-title")
            area = TextArea(self.text, id="file-body")
            area.show_line_numbers = False
            yield area
            yield Input(placeholder="find...", id="file-find")
            yield Static(self.HINT, classes="prompt-hint")

    # -- find and replace -------------------------------------------------
    #
    # `TextArea.replace` takes an insertion string, not a callable, so a global
    # replace builds the joined result and applies it as one edit. That also
    # makes the whole cascade a single undo step, which is what a reader
    # expects from "replace all".

    def _area(self) -> TextArea:
        return self.query_one("#file-body", TextArea)

    def _bar(self) -> Input:
        return self.query_one("#file-find", Input)

    def action_find(self) -> None:
        self._finding = True
        self._replacing = False
        bar = self._bar()
        bar.placeholder = "find..."
        bar.add_class("active")
        bar.focus()

    def action_find_next(self) -> None:
        area = self._area()
        if not self._query:
            self.action_find()
            return
        text = area.text
        row, column = area.selection.end
        start = _point_to_offset(text, row, column)
        index = text.find(self._query, start)
        if index < 0:
            # Wrap: a search that stops at the end of the buffer reads as
            # "not found" even when the match is above the caret.
            index = text.find(self._query)
        if index < 0:
            return
        row, column = _offset_to_point(text, index)
        end_row, end_column = _offset_to_point(text, index + len(self._query))
        area.move_cursor((row, column))
        area.selection = Selection((row, column), (end_row, end_column))

    def action_undo(self) -> None:
        self._area().undo()

    def action_redo(self) -> None:
        self._area().redo()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "file-find":
            return
        event.stop()
        if self._replacing:
            needle, replacement = self._query, event.value
            if needle:
                self._replace_all(needle, replacement)
            self._close_find()
            return
        self._query = event.value
        self._finding = False
        self._replacing = True
        self._bar().value = ""
        self._bar().placeholder = f"replace {self._query!r} with..."
        if self._query:
            self.action_find_next()

    def on_key(self, event: Any) -> None:
        if event.key == "escape" and self._bar().has_class("active"):
            event.stop()
            self._close_find()

    def _close_find(self) -> None:
        self._finding = False
        self._replacing = False
        bar = self._bar()
        bar.remove_class("active")
        bar.value = ""
        self._area().focus()

    def _replace_all(self, needle: str, replacement: str) -> None:
        area = self._area()
        text = area.text
        if needle not in text:
            return
        end_row = text.count("\n")
        end_column = len(text.rsplit("\n", 1)[-1])
        area.replace(
            text.replace(needle, replacement), (0, 0), (end_row, end_column)
        )

    def action_save(self) -> None:
        # The body is the return value; `Esc` reaching `PromptBox`'s cancel
        # yields `None`, so a discarded edit is distinguishable from saving.
        self.dismiss(self.query_one("#file-body", TextArea).text)


class HelpModal(ModalScreen[None]):
    """`05` §5.6: the command catalogue, one tab per group.

    A modal rather than a screen because help is consulted *while* composing: a
    screen swap loses the half-typed prompt, and `F1` is pressed mid-sentence far
    more often than it is pressed to browse.
    """

    CSS = _CSS + """
.help-box {
    width: 90%;
    height: 80%;
    border: thick $accent;
    padding: 0 1;
}
#help-tabs { height: 1fr; }
"""
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(self, categories: dict[str, str]) -> None:
        super().__init__()
        self.categories = categories

    def compose(self) -> ComposeResult:
        with Vertical(classes="help-box"):
            yield Static("Commands", classes="prompt-title")
            with TabbedContent(id="help-tabs"):
                for name, text in self.categories.items():
                    with TabPane(name, id=f"help-tab-{name.lower()}"):
                        yield Static(text, classes="help-body")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        # The first tab is the device catalogue, which is what a reader pressing
        # F1 is usually after.
        first = next(iter(self.categories))
        self.query_one("#help-tabs", TabbedContent).active = f"help-tab-{first.lower()}"

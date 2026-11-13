"""Test-only helpers that must live under `bridge/ui/`.

`09-bridge.md` §6 confines `textual` to `bridge/ui/`, and its law applies to
`bridge/tests/` as well, so a pilot that drives a modal borrows this host rather
than importing `textual` itself.
"""

from __future__ import annotations

from typing import Any

from textual.app import App

from .modals import PromptBox


class ModalHost(App[Any]):
    """A bare app that pushes one modal from a worker and records its result.

    `push_screen_wait` only works from a worker, which is the reason the real
    app wraps every modal call in one.
    """

    def __init__(self, modal: PromptBox[Any]) -> None:
        super().__init__()
        self.modal = modal
        #: Empty until the modal dismisses; `modal_result` is the outcome.
        self.dismissed: dict[str, Any] = {}

    def on_mount(self) -> None:
        async def wait() -> None:
            self.dismissed["value"] = await self.push_screen_wait(self.modal)

        self.run_worker(wait())


class ModalCanvas(App[Any]):
    """A blank 100x40-ish screen a test pushes one modal onto to measure it.

    Separate from `ModalHost` because centring is about geometry, not the
    returned value, and a plain push is enough.
    """


class WidgetHost(App[Any]):
    """A bare app showing one widget, for a case that needs a mount and nothing else.

    A test that wants to prove what a screen does before and after it is mounted
    cannot import `textual` to write the host itself: `09` §6 confines the import
    to `bridge/ui/`, tests included.
    """

    def __init__(self, widget: Any) -> None:
        super().__init__()
        self.widget = widget

    def compose(self) -> Any:
        yield self.widget


def chat_lines(app: Any) -> list[str]:
    """The chat pane as text, one string per rendered line.

    A line written as a Rich renderable — Markdown, a `Table.grid`, a `Syntax`
    block — is stored as a `Strip`, whose `str()` is its repr. A test that read
    `line.text` would compare against `Strip([Segment(...)])` and pass on a
    substring of the repr, which is not what the reader sees.
    """
    lines: list[str] = []
    for line in app.query_one("#chat-log").lines:
        text = getattr(line, "text", None)
        if isinstance(text, str):
            lines.append(text)
        else:
            lines.append("".join(segment.text for segment in line.segments))
    return lines


def option_rows(widget: Any) -> list[str]:
    """Every row of an `OptionList` as plain text, so a test reads the screen.

    `textual` stays under `bridge/ui/` (`09` §6), so the selector type lives here
    rather than in the test that asks the question.
    """
    from textual.widgets import OptionList

    tree = widget.query_one("#todo-tree", OptionList)
    return [
        str(tree.get_option_at_index(index).prompt)
        for index in range(tree.option_count)
    ]


def chat_input(app: Any) -> Any:
    """The `#chat-input` widget, typed, without a test importing `textual`."""
    from textual.widgets import Input

    return app.query_one("#chat-input", Input)

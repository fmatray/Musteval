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

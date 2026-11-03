from __future__ import annotations

"""`05-bridge.md` §5.3 `E`/`N`: the task JSON modal, driven through a pilot.

No `textual` import here: `09` §6 reserves it for `bridge/ui/`, so the app class
is taken from the package the modal already loaded.
"""


import contextlib
import json

import pytest

from bridge.ui.task_prompt import TaskPrompt
from bridge.ui.testing import ModalHost


class _TaskApp(ModalHost):
    """Bare app that pushes the modal from a worker, as the real app does."""

    def __init__(self, task: dict | None = None) -> None:
        super().__init__(TaskPrompt(task))


@contextlib.asynccontextmanager
async def _open(task: dict | None = None):
    app = _TaskApp(task)
    async with app.run_test() as pilot:
        for _ in range(50):
            if app.screen is app.modal:
                break
            await pilot.pause(0.02)
        assert app.screen is app.modal
        yield pilot, app


def _error_text(modal: TaskPrompt) -> str:
    # Textual Static content property returns the original string.
    return str(modal.query_one("#task-error").content).strip()


async def test_the_field_is_prefilled_with_sorted_key_json():
    # Keys out of order to verify sorted pre-fill.
    task = {"name": "b", "args": {"z": 1, "a": 2}, "id": "t1"}
    async with _open(task) as (pilot, app):
        value = app.modal.query_one("#task-input").value
        assert value == json.dumps(task, sort_keys=True)
        assert list(json.loads(value)) == ["args", "id", "name"]


async def test_enter_on_valid_json_dismisses_with_the_dict():
    async with _open() as (pilot, app):
        app.modal.query_one("#task-input").value = '{"id": "t1", "name": "n"}'
        await pilot.press("enter")
        for _ in range(50):
            if "value" in app.dismissed:
                break
            await pilot.pause(0.02)
        assert app.dismissed.get("value") == {"id": "t1", "name": "n"}


async def test_malformed_json_is_refused_in_place():
    async with _open() as (pilot, app):
        app.modal.query_one("#task-input").value = "{oops}"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert "value" not in app.dismissed
        assert app.screen is app.modal
        assert _error_text(app.modal)


async def test_a_json_list_is_refused_as_a_task():
    async with _open() as (pilot, app):
        app.modal.query_one("#task-input").value = '[1, 2, 3]'
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert "value" not in app.dismissed
        assert app.screen is app.modal
        assert "must be a JSON object" in _error_text(app.modal)


@pytest.mark.parametrize("mode", ["empty", "esc"])
async def test_cancels_dismiss_with_none(mode):
    async with _open() as (pilot, app):
        if mode == "empty":
            app.modal.query_one("#task-input").value = ""
            await pilot.press("enter")
        else:
            await pilot.press("escape")

        for _ in range(50):
            if "value" in app.dismissed:
                break
            await pilot.pause(0.02)
        assert app.dismissed.get("value") is None

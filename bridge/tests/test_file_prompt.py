"""`05-bridge.md` §5.8: the file editor modal — search, replace, undo, redo.

No `textual` import here: `09` §6 reserves it for `bridge/ui/`, so the host comes
from `bridge.ui.testing`.
"""

from __future__ import annotations

import contextlib

from bridge.ui.modals import FilePrompt, _offset_to_point, _point_to_offset
from bridge.ui.testing import ModalHost


class _FileApp(ModalHost):
    def __init__(self, path: str = "/workspace/a.md", text: str = "") -> None:
        super().__init__(FilePrompt(path, text))


@contextlib.asynccontextmanager
async def _open(text: str, path: str = "/workspace/a.md"):
    app = _FileApp(path, text)
    async with app.run_test(size=(100, 40)) as pilot:
        for _ in range(50):
            if app.screen is app.modal:
                break
            await pilot.pause(0.02)
        assert app.screen is app.modal
        yield pilot, app


async def _type(pilot, text: str) -> None:
    for character in text:
        await pilot.press(character)


async def test_the_buffer_starts_from_the_read_text():
    async with _open("one\ntwo\n") as (_pilot, app):
        assert app.modal.query_one("#file-body").text == "one\ntwo\n"


async def test_ctrl_s_returns_the_edited_buffer():
    async with _open("old") as (pilot, app):
        area = app.modal.query_one("#file-body")
        area.text = "new"
        await pilot.press("ctrl+s")
        for _ in range(50):
            if "value" in app.dismissed:
                break
            await pilot.pause(0.02)
        assert app.dismissed.get("value") == "new"


async def test_escape_returns_none_so_a_discarded_edit_is_recognisable():
    async with _open("keep") as (pilot, app):
        app.modal.query_one("#file-body").text = "thrown away"
        await pilot.press("escape")
        for _ in range(50):
            if "value" in app.dismissed:
                break
            await pilot.pause(0.02)
        assert app.dismissed.get("value") is None


async def test_find_selects_the_first_match():
    """`Ctrl+F` then `Enter` puts the caret on the needle, not merely near it."""
    async with _open("alpha beta alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "alpha")
        await pilot.press("enter")
        await pilot.pause(0.1)
        selection = app.modal.query_one("#file-body").selection
        assert selection.start == (0, 0)
        assert selection.end == (0, 5)


async def test_f3_walks_to_the_next_match_and_wraps_at_the_end():
    """A search that stops at the buffer's end reads as "not found"."""
    async with _open("alpha beta alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "alpha")
        await pilot.press("enter")
        await pilot.pause(0.1)
        await pilot.press("f3")
        await pilot.pause(0.1)
        area = app.modal.query_one("#file-body")
        assert area.selection.start == (0, 11)
        await pilot.press("f3")  # nothing after (0, 11), so wrap to the first
        await pilot.pause(0.1)
        assert area.selection.start == (0, 0)


async def test_replacing_rewrites_every_occurrence():
    async with _open("alpha beta alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "alpha")
        await pilot.press("enter")
        await pilot.pause(0.1)
        await _type(pilot, "gamma")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.modal.query_one("#file-body").text == "gamma beta gamma\n"


async def test_replace_all_undoes_as_one_step():
    """The whole cascade is a single edit, which is what "replace all" means."""
    async with _open("alpha beta alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "alpha")
        await pilot.press("enter")
        await pilot.pause(0.1)
        await _type(pilot, "gamma")
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("ctrl+z")
        await pilot.pause(0.1)
        assert app.modal.query_one("#file-body").text == "alpha beta alpha\n"


async def test_redo_reapplies_the_replaced_text():
    async with _open("alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "alpha")
        await pilot.press("enter")
        await pilot.pause(0.1)
        await _type(pilot, "gamma")
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("ctrl+z")
        await pilot.pause(0.1)
        await pilot.press("ctrl+y")
        await pilot.pause(0.1)
        assert app.modal.query_one("#file-body").text == "gamma\n"


async def test_a_needle_that_is_absent_changes_nothing():
    async with _open("alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "zzz")
        await pilot.press("enter")
        await pilot.pause(0.1)
        await _type(pilot, "gamma")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.modal.query_one("#file-body").text == "alpha\n"


async def test_escape_in_the_find_bar_closes_it_and_keeps_the_buffer():
    """Esc must back out of the search, not discard the file being edited."""
    async with _open("alpha\n") as (pilot, app):
        await pilot.press("ctrl+f")
        await _type(pilot, "alp")
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert "value" not in app.dismissed
        assert not app.modal.query_one("#file-find").has_class("active")
        assert app.modal.query_one("#file-body").text == "alpha\n"


def test_the_offset_helpers_are_inverses():
    for text, offset in (("", 0), ("abc", 2), ("a\nbc", 3), ("a\n\nb", 2)):
        row, column = _offset_to_point(text, offset)
        assert _point_to_offset(text, row, column) == offset

"""`05` §6.2: `/todo edit`'s editor, driven through Textual's pilot.

The modal is the only place a tree is edited row by row, so every assertion here is
about the tree the modal would send, not about which handler ran.
"""

from __future__ import annotations

from bridge.ui.modals import TodoEditor
from bridge.ui.testing import ModalHost, option_rows


class _Host(ModalHost):
    """A bare app, so the modal is exercised without the whole bridge."""

    def __init__(self) -> None:
        super().__init__(TodoEditor(TREE))


TREE = [
    {"name": "design", "tasks": [
        {"content": "sketch", "status": "completed"},
        {"content": "wire jwt", "status": "blocked", "blocker": "needs key"},
    ]},
    {"name": "ship", "tasks": [
        {"content": "tag", "status": "in_progress"},
    ]},
]


def _rows(editor: TodoEditor) -> list[str]:
    """Every rendered row as plain text, so a test reads what the reader sees."""
    return option_rows(editor)


async def _open(pilot) -> TodoEditor:
    editor = pilot.app.screen
    assert isinstance(editor, TodoEditor), "the editor did not open"
    await pilot.pause(0.05)
    return editor


async def test_the_editor_lists_every_phase_and_task():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        assert _rows(await _open(pilot)) == [
            "design",
            "  [x] sketch",
            "  [!] wire jwt",
            "ship",
            "  [>] tag",
        ]


async def test_modify_rewrites_a_tasks_content_and_keeps_its_status():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        # Second row is `sketch`; `Ctrl+N` moves to the task below it first.
        editor.query_one("#todo-tree").highlighted = 1
        await pilot.press("ctrl+e")
        assert editor.query_one("#todo-value").value == "sketch"
        editor.query_one("#todo-value").value = "sketch it"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert editor.phases[0]["tasks"][0] == {
            "content": "sketch it", "status": "completed",
        }


async def test_modify_renames_a_phase():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 0
        await pilot.press("ctrl+e")
        editor.query_one("#todo-value").value = "discovery"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert [p["name"] for p in editor.phases] == ["discovery", "ship"]


async def test_add_after_a_phase_creates_an_empty_phase():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 0
        await pilot.press("ctrl+n")
        editor.query_one("#todo-value").value = "review"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert [(p["name"], p["tasks"]) for p in editor.phases] == [
            ("design", TREE[0]["tasks"]), ("review", []), ("ship", TREE[1]["tasks"]),
        ]


async def test_add_before_a_task_inserts_beside_it():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 2
        await pilot.press("ctrl+b")
        editor.query_one("#todo-value").value = "mint"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert [t["content"] for t in editor.phases[0]["tasks"]] == [
            "sketch", "mint", "wire jwt",
        ]


async def test_a_phase_drop_needs_two_presses_and_names_the_loss():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 0
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert "2 task(s)" in str(editor.query_one("#todo-status").render())
        assert [p["name"] for p in editor.phases] == ["design", "ship"]

        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert [p["name"] for p in editor.phases] == ["ship"]


async def test_any_other_key_disarms_a_drop():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 0
        await pilot.press("ctrl+d")
        await pilot.press("down")
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert [p["name"] for p in editor.phases] == ["design", "ship"]


async def test_a_task_drop_needs_two_presses():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 2
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert len(editor.phases[0]["tasks"]) == 2
        await pilot.press("ctrl+d")
        await pilot.pause(0.05)
        assert [t["content"] for t in editor.phases[0]["tasks"]] == ["sketch"]


async def test_move_up_swaps_a_task_with_its_sibling():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 2
        await pilot.press("ctrl+up")
        await pilot.pause(0.05)
        assert [t["content"] for t in editor.phases[0]["tasks"]] == [
            "wire jwt", "sketch",
        ]
        # The cursor followed the task, so a second press moves the same one.
        assert editor._selected() == ("task", "design", "wire jwt")


async def test_move_down_at_the_end_is_a_no_op():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 3
        await pilot.press("ctrl+down")
        await pilot.pause(0.05)
        assert editor.phases == TREE


async def test_move_a_phase():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 3
        await pilot.press("ctrl+up")
        await pilot.pause(0.05)
        assert [p["name"] for p in editor.phases] == ["ship", "design"]


async def test_escape_clears_a_pending_verb_before_leaving():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 1
        await pilot.press("ctrl+e")
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert isinstance(pilot.app.screen, TodoEditor)
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] is None


async def test_sending_an_untouched_tree_sends_nothing():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        await pilot.press("ctrl+s")
        await pilot.pause(0.05)
        # The modal stays open: nothing to send is a refusal, not a dismissal.
        assert pilot.app.dismissed == {}
        assert "unchanged" in str(editor.query_one("#todo-status").render())


async def test_sending_one_change_ships_the_whole_tree_with_its_state():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 1
        await pilot.press("ctrl+down")
        await pilot.pause(0.05)
        await pilot.press("ctrl+s")
        await pilot.pause(0.05)
        frames = pilot.app.dismissed["value"]
        assert len(frames) == 1
        args = frames[0].body["args"]
        assert args["op"] == "init"
        assert args["list"] == [
            {"phase": "design", "items": [
                {"content": "wire jwt", "status": "blocked", "blocker": "needs key"},
                {"content": "sketch", "status": "completed"},
            ]},
            {"phase": "ship", "items": [
                {"content": "tag", "status": "in_progress"},
            ]},
        ]


async def test_a_refused_edit_names_the_problem_and_keeps_the_tree():
    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        editor.query_one("#todo-tree").highlighted = 0
        await pilot.press("ctrl+e")
        editor.query_one("#todo-value").value = "ship"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert "duplicate phase name" in str(editor.query_one("#todo-status").render())
        assert [p["name"] for p in editor.phases] == ["design", "ship"]


async def test_the_modal_fits_a_small_terminal_with_its_input_and_legend():
    """`17` §4: the tree scrolls, so a long plan must not push the controls off."""
    async with _Host().run_test(size=(80, 16)) as pilot:
        await pilot.pause(0.1)
        editor = await _open(pilot)
        # Region counts padding and border; `size` is the content the modal needs.
        box = editor.query_one(".prompt-box")
        assert box.size.height <= 12
        screen = editor.screen.region
        assert box.region.bottom <= screen.bottom
        for selector in ("#todo-value", "#todo-hint"):
            assert editor.query_one(selector).region.bottom <= box.region.bottom

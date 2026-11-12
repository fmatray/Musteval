"""`05-bridge.md` §7: `/todo edit` is a host modal and a pure plan.

The planner rewrites the tree locally and sends it as one nested `init` frame.
"""

from __future__ import annotations

import copy

import pytest

from bridge.commands import CommandError, todo_edit


def _phases() -> list[dict]:
    return [
        {"name": "design", "tasks": [
            {"content": "sketch", "status": "completed"},
            {"content": "wire jwt", "status": "blocked", "blocker": "needs key"},
        ]},
        {"name": "ship", "tasks": [
            {"content": "tag", "status": "in_progress"},
        ]},
    ]


def _tree(frames: list) -> list[dict]:
    (frame,) = frames
    assert frame.body["name"] == "todo.apply"
    args = frame.body["args"]
    assert args["op"] == "init"
    return args["list"]


def test_a_reorder_keeps_every_status_and_blocker():
    tree = _tree(todo_edit(_phases(), op="move-task", task="sketch", where="down"))
    assert tree[0]["items"] == [
        {"content": "wire jwt", "status": "blocked", "blocker": "needs key"},
        {"content": "sketch", "status": "completed"},
    ]


def test_move_task_past_the_start_is_a_no_op():
    tree = _tree(todo_edit(_phases(), op="move-task", task="sketch", where="up"))
    assert [i["content"] for i in tree[0]["items"]] == ["sketch", "wire jwt"]


def test_move_task_past_the_end_is_a_no_op():
    tree = _tree(todo_edit(_phases(), op="move-task", task="tag", where="down"))
    assert [i["content"] for i in tree[1]["items"]] == ["tag"]


def test_move_task_down_swaps_inside_its_phase():
    tree = _tree(todo_edit(_phases(), op="move-task", task="sketch", where="down"))
    assert [i["content"] for i in tree[0]["items"]] == ["wire jwt", "sketch"]


def test_move_phase_swaps_the_phases():
    tree = _tree(todo_edit(_phases(), op="move-phase", phase="ship", where="up"))
    assert [p["phase"] for p in tree] == ["ship", "design"]
    assert [i["content"] for i in tree[1]["items"]] == ["sketch", "wire jwt"]


def test_move_phase_past_either_end_is_a_no_op():
    for where in ("up", "down"):
        phase = "design" if where == "up" else "ship"
        tree = _tree(todo_edit(_phases(), op="move-phase", phase=phase, where=where))
        assert [p["phase"] for p in tree] == ["design", "ship"]


def test_add_task_appends_pending_to_the_named_phase():
    tree = _tree(todo_edit(_phases(), op="add-task", phase="design", value="mint"))
    assert tree[0]["items"][-1] == {"content": "mint", "status": "pending"}


def test_add_task_before_and_after_a_task():
    before = _tree(todo_edit(
        _phases(), op="add-task", phase="design", task="wire jwt",
        value="mint", where="before",
    ))
    assert [i["content"] for i in before[0]["items"]] == ["sketch", "mint", "wire jwt"]
    after = _tree(todo_edit(
        _phases(), op="add-task", phase="design", task="wire jwt",
        value="mint", where="after",
    ))
    assert [i["content"] for i in after[0]["items"]] == ["sketch", "wire jwt", "mint"]


def test_add_phase_appends_an_empty_phase():
    tree = _tree(todo_edit(_phases(), op="add-phase", value="ship later"))
    assert tree[-1] == {"phase": "ship later", "items": []}


def test_add_phase_before_and_after_a_phase():
    before = _tree(todo_edit(
        _phases(), op="add-phase", phase="design", value="setup", where="before",
    ))
    assert [p["phase"] for p in before] == ["setup", "design", "ship"]
    after = _tree(todo_edit(
        _phases(), op="add-phase", phase="design", value="setup", where="after",
    ))
    assert [p["phase"] for p in after] == ["design", "setup", "ship"]


def test_modify_task_keeps_status_and_blocker():
    tree = _tree(todo_edit(_phases(), op="modify-task", task="wire jwt", value="wire oauth"))
    assert tree[0]["items"][1] == {
        "content": "wire oauth", "status": "blocked", "blocker": "needs key",
    }


def test_modify_phase_keeps_the_tasks():
    tree = _tree(todo_edit(_phases(), op="modify-phase", phase="design", value="blueprint"))
    assert tree[0]["phase"] == "blueprint"
    assert [i["content"] for i in tree[0]["items"]] == ["sketch", "wire jwt"]


def test_delete_task_removes_only_it():
    tree = _tree(todo_edit(_phases(), op="delete-task", task="sketch"))
    assert [i["content"] for i in tree[0]["items"]] == ["wire jwt"]


def test_delete_phase_drops_its_tasks():
    tree = _tree(todo_edit(_phases(), op="delete-phase", phase="design"))
    assert [p["phase"] for p in tree] == ["ship"]


def test_deleting_the_last_phase_clears_the_list():
    tree = _tree(todo_edit(
        [{"name": "solo", "tasks": [{"content": "one", "status": "pending"}]}],
        op="delete-phase", phase="solo",
    ))
    assert tree == []


@pytest.mark.parametrize("kwargs, match", [
    ({"op": "renumber"}, "unknown todo edit op: renumber"),
    ({"op": ""}, r"unknown todo edit op: \(none\)"),
    ({"op": "add-task", "phase": "nope", "value": "x"}, "unknown phase: nope"),
    ({"op": "add-task", "value": "x"}, r"unknown phase: \(none\)"),
    ({"op": "add-task", "phase": "design"}, "add-task needs a value"),
    ({"op": "add-task", "phase": "design", "value": "sketch"}, "duplicate content: sketch"),
    ({"op": "add-task", "phase": "design", "task": "ghost", "value": "x"}, "unknown task: ghost"),
    ({"op": "add-task", "phase": "design", "value": "x", "where": "beside"}, "before|after"),
    ({"op": "add-phase", "value": ""}, "add-phase needs a value"),
    ({"op": "add-phase", "value": "design"}, "duplicate phase name: design"),
    ({"op": "add-phase", "value": "x", "phase": "ghost"}, "unknown phase: ghost"),
    ({"op": "modify-task", "value": "x"}, "modify-task needs a task"),
    ({"op": "modify-task", "task": "sketch"}, "modify-task needs a value"),
    ({"op": "modify-task", "task": "ghost", "value": "x"}, "unknown task: ghost"),
    ({"op": "modify-task", "task": "sketch", "value": "tag"}, "duplicate content: tag"),
    ({"op": "modify-phase", "phase": "design", "value": "ship"}, "duplicate phase name: ship"),
    ({"op": "modify-phase", "value": "x"}, r"unknown phase: \(none\)"),
    ({"op": "delete-task", "task": "ghost"}, "unknown task: ghost"),
    ({"op": "delete-phase", "phase": "ghost"}, "unknown phase: ghost"),
    ({"op": "move-task", "task": "sketch", "where": "left"}, "up|down"),
    ({"op": "move-phase", "phase": "design", "where": "left"}, "up|down"),
])
def test_planner_refusals(kwargs, match):
    with pytest.raises(CommandError, match=match):
        todo_edit(_phases(), **kwargs)


def test_the_callers_tree_is_never_mutated():
    phases = _phases()
    snapshot = copy.deepcopy(phases)
    todo_edit(phases, op="delete-phase", phase="design")
    todo_edit(phases, op="modify-task", task="sketch", value="redraw")
    todo_edit(phases, op="move-phase", phase="ship", where="up")
    assert phases == snapshot

"""The arms of the onboarding and file modals that only a real document reaches.

`05` §7.1 and §5.8: each of these is a case where the modal must *not* offer or
*not* apply something — a level the model cannot take, a find with no query, a
stored name the provider did not publish. Each is a refusal, so each is a test.
"""

from __future__ import annotations

from bridge import providers as providers_mod
from bridge.ui import modals
from bridge.ui.modals import (
    FilePrompt,
    ModelStep,
    ModelPicker,
    _StepBox,
    ValuePrompt,
    _thinking_levels,
)
from bridge.ui.testing import ModalHost, option_rows

ENTRIES = list(providers_mod.BUILTIN)


def _host(screen) -> ModalHost:
    return ModalHost(screen)


def test_a_stored_model_the_provider_did_not_publish_is_still_offered():
    """`05` §7.1: `gemma4:cloud` against the cloud list is the state of the first
    device this ran on. Without the stored name the field would say one model and
    the highlight another, and clearing the field would move the reader onto a model
    they never chose."""
    assert _StepBox._options(["a", "b"], "stored") == [
        ("stored", "stored"), ("a", "a"), ("b", "b"),
    ]


def test_a_published_stored_model_is_not_duplicated():
    assert _StepBox._options(["a", "b"], "b") == [("a", "a"), ("b", "b")]
    assert _StepBox._options([], "") == []


def test_a_provider_with_no_row_offers_no_levels():
    """`05` §7.1: a provider with no catalog row has no `extra_body` shape, so no
    level is expressible and the step must not draw a picker."""
    assert _thinking_levels(ENTRIES, "no-such-provider") == []


def test_a_published_model_effort_set_wins_over_the_providers():
    """`05` §7.1: levels are a property of the model. Offering `xhigh` to a model
    that does not publish it earns a 400 on the first prompt."""
    row = providers_mod.find("ollama")
    assert row is not None
    efforts = {"small": ("low",), "big": ("low", "high")}
    assert _thinking_levels(ENTRIES, "ollama", model="small", efforts=efforts) == ["low"]
    assert _thinking_levels(ENTRIES, "ollama", model="unknown", efforts=efforts) == list(
        row.thinking_levels
    )


async def test_an_empty_value_prompt_answer_is_a_cancel():
    """`05` §5.8: an emptied field is a cancel, not a request to blank the setting —
    a blanked `base_url` is a device that cannot answer."""
    async with _host(ValuePrompt("llm.providers[0].model", "old")).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.05)
        screen = pilot.app.screen
        assert isinstance(screen, ValuePrompt)
        screen.query_one("#value-input").value = "   "
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] is None


async def test_a_secret_prompt_returns_its_text_and_never_the_redacted_field():
    async with _host(modals.SecretPrompt("wifi_psk")).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.05)
        pilot.app.screen.query_one("#secret-input").value = "hunter2"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] == "hunter2"


async def test_the_editor_find_key_opens_the_bar_when_there_is_no_query():
    """`05` §5.8: Ctrl+F with nothing typed yet is "start a find", not "search for
    the empty string", which would match at offset 0 every time."""
    async with _host(FilePrompt("/workspace/a.txt", "alpha beta alpha")).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, FilePrompt)
        editor.action_find_next()
        await pilot.pause(0.05)
        assert editor.query_one("#file-find").placeholder == "find..."
        assert editor.query_one("#file-find").has_class("active")


async def test_a_find_that_misses_wraps_to_the_top():
    """`05` §5.8: a search that stops at the end of the buffer reads as "not found"
    even when the match is above the caret."""
    async with _host(FilePrompt("/workspace/a.txt", "alpha beta alpha")).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, FilePrompt)
        editor._query = "alpha"
        area = editor._area()
        area.move_cursor((0, 16))  # past the last `alpha`
        editor.action_find_next()
        await pilot.pause(0.05)
        assert area.selection.start == (0, 0), "it wrapped to the first match"


async def test_a_query_that_is_nowhere_leaves_the_caret_alone():
    """`05` §5.8: a miss must not move the cursor, or the next keystroke lands
    somewhere the reader did not choose."""
    async with _host(FilePrompt("/workspace/a.txt", "alpha beta")).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, FilePrompt)
        editor._query = "zulu"
        area = editor._area()
        area.move_cursor((0, 5))
        editor.action_find_next()
        await pilot.pause(0.05)
        assert area.selection.start == (0, 5)


async def test_undo_and_redo_walk_the_editors_own_history():
    """`05` §5.8: Ctrl+Z on the editor, not on the terminal's scrollback."""
    async with _host(FilePrompt("/workspace/a.txt", "one")).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, FilePrompt)
        area = editor._area()
        area.focus()
        area.move_cursor((0, 3))
        area.insert(" two")
        await pilot.pause(0.05)
        assert area.text == "one two"
        editor.action_undo()
        await pilot.pause(0.05)
        assert area.text == "one"
        editor.action_redo()
        await pilot.pause(0.05)
        assert area.text == "one two"


async def test_the_model_step_narrows_its_levels_to_the_typed_model():
    """`05` §7.1: a name in the list is a choice whichever field carried it, so the
    levels have to follow the field as it changes."""
    row = providers_mod.find("ollama")
    assert row is not None
    step = ModelStep(
        models=["small", "big"],
        provider="ollama",
        model="small",
        thinking="low",
        efforts={"big": ("low", "high"), "small": ("low",)},
    )
    async with _host(step).run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.05)
        screen = pilot.app.screen
        thinking = screen.query_one("#step-thinking")
        assert [value for _, value in thinking._options] == ["low"]

        field = screen.query_one("#step-model")
        field.value = "big"
        await pilot.pause(0.1)
        assert [value for _, value in thinking._options] == ["low", "high"]


async def test_the_model_step_keeps_the_current_level_when_it_survives():
    """`05` §7.1: narrowing must not silently reset a level the new model accepts."""
    row = providers_mod.find("ollama")
    assert row is not None
    step = ModelStep(
        models=["small", "big"],
        provider="ollama",
        model="small",
        thinking="low",
        efforts={"big": ("low", "high"), "small": ("low",)},
    )
    async with _host(step).run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.05)
        screen = pilot.app.screen
        screen.query_one("#step-model").value = "big"
        await pilot.pause(0.1)
        assert screen.query_one("#step-thinking").value == "low"


async def test_a_row_with_no_tasks_counts_zero():
    """`05` §6.2: the drop hint states what is lost; a phase with no tasks must say
    zero rather than reuse the previous phase's count."""
    tree = [
        {"name": "empty", "tasks": []},
        {"name": "busy", "tasks": [{"content": "a", "status": "pending"}]},
    ]
    async with _host(modals.TodoEditor(tree)).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, modals.TodoEditor)
        assert editor._count("empty") == 0
        assert editor._count("busy") == 1
        assert editor._count("absent") == 0


async def test_a_send_with_a_pending_edit_is_refused():
    """`05` §6.2: a half-typed verb is not in the tree yet, so sending would drop it
    without saying so."""
    tree = [{"name": "design", "tasks": []}]
    async with _host(modals.TodoEditor(tree)).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, modals.TodoEditor)
        editor.query_one("#todo-tree").highlighted = 0
        editor.action_modify()
        editor.action_send()
        await pilot.pause(0.05)
        assert "finish the pending edit first" in str(
            editor.query_one("#todo-status").render()
        )
        assert isinstance(pilot.app.screen, modals.TodoEditor)


async def test_escape_after_a_verb_clears_the_field_before_it_closes():
    """`05` §6.2: the first Esc drops the verb and keeps the editor open, so a
    mistyped command does not cost the whole edit."""
    tree = [{"name": "design", "tasks": [{"content": "a", "status": "pending"}]}]
    async with _host(modals.TodoEditor(tree)).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.05)
        editor = pilot.app.screen
        assert isinstance(editor, modals.TodoEditor)
        editor.query_one("#todo-tree").highlighted = 1
        editor.action_modify()
        editor.query_one("#todo-value").value = "half typed"
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert isinstance(pilot.app.screen, modals.TodoEditor)
        assert editor.query_one("#todo-value").value == ""


# -- the plain prompts ---------------------------------------------------


async def test_confirm_prompt_answers_yes_and_no():
    """`05` §7: the delete gate has one key per answer, and both must return a real
    bool — a `None` from Esc is a third state the caller would read as "no"."""
    for key, expected in (("y", True), ("n", False)):
        async with _host(modals.ConfirmPrompt("Delete x?")).run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.05)
            screen = pilot.app.screen
            assert screen.query_one(".prompt-title").content == "Delete x?"
            await pilot.press(key)
            await pilot.pause(0.05)
            assert pilot.app.dismissed["value"] is expected


async def test_a_secret_prompt_cancels_to_none():
    """`05` §8: Esc is a cancel, not a blank secret — writing `""` would replace a
    stored key with nothing."""
    async with _host(modals.SecretPrompt("llm_api_key")).run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.05)
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] is None


async def test_a_filter_prompt_seeds_from_either_shape_of_current_value():
    """`05` §5.4: the prompt is opened from two screens, one holding a string and one
    a `LogFilter`, and both must open with what is applied rather than empty."""
    from bridge.ui.logs import LogFilter

    cases = [
        ("api", "api"),
        (LogFilter("api", "warn"), "api:warn"),
        (LogFilter("api"), "api"),
        (None, ""),
    ]
    for current, seed in cases:
        async with _host(modals.FilterPrompt(current)).run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.05)
            value = pilot.app.screen.query_one("#filter-input").value
            assert value == seed, current


async def test_a_filter_prompt_returns_the_trimmed_tag():
    """`05` §5.4: the submitted text is the filter, and a blank one is a cancel."""
    for typed, expected in (("  api  ", "api"), ("   ", None)):
        async with _host(modals.FilterPrompt(None)).run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.05)
            screen = pilot.app.screen
            screen.query_one("#filter-input").value = typed
            screen.action_submit()
            await pilot.pause(0.05)
            assert pilot.app.dismissed["value"] == expected


# -- the todo editor with no row under the cursor ------------------------


async def test_every_verb_on_an_empty_tree_says_which_gesture_is_missing():
    """`05` §6.2: a verb with no row under the cursor must refuse by name — a
    `modify` that guessed a phase would rewrite the wrong one."""
    from bridge.ui.modals import TodoEditor

    async with _host(TodoEditor([])).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = pilot.app.screen
        assert editor._selected() is None
        for verb in ("action_modify", "action_drop", "action_move_up",
                     "action_move_down"):
            getattr(editor, verb)()
            await pilot.pause(0.02)
            assert "select a phase or a task first" in str(
                editor.query_one("#todo-status").content
            ), verb
        # `Ctrl+N` on an empty tree has no anchor, so it adds a phase.
        assert editor._add_op() == "add-phase"


async def test_an_enter_with_no_verb_typed_does_nothing():
    """`05` §6.2: Enter in the value field with no verb armed must not rewrite the
    tree — the field is used for the next verb only."""
    from bridge.ui.modals import TodoEditor

    async with _host(TodoEditor([{"name": "design", "tasks": []}])).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.1)
        editor = pilot.app.screen
        before = list(editor.phases)
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert editor.phases == before
        assert isinstance(pilot.app.screen, TodoEditor)


async def test_escape_after_a_double_press_disarms_the_drop_and_stays_open():
    """`05` §6.2: Esc unwinds the armed drop first, so a reader who pressed `Ctrl+D`
    once can back out without losing the editor."""
    from bridge.ui.modals import TodoEditor

    async with _host(TodoEditor([{"name": "design", "tasks": []}])).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.1)
        editor = pilot.app.screen
        editor.query_one("#todo-tree").highlighted = 0
        editor.action_drop()
        await pilot.pause(0.05)
        assert editor._armed == "design"
        editor.action_cancel()
        await pilot.pause(0.05)
        assert editor._armed is None
        assert isinstance(pilot.app.screen, TodoEditor)


# -- provider and network step answers -----------------------------------


async def test_choosing_a_provider_from_the_row_fills_the_base_url():
    """`05` §7: the catalog row knows its own URL, so the reader does not retype it —
    and a typed URL still wins over the row's."""
    step = modals.ProviderStep({}, ENTRIES)
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        row = next(p for p in ENTRIES if p.base_url)
        screen.query_one("#step-provider").value = row.id
        await pilot.pause(0.05)
        assert screen.query_one("#step-base-url").value == row.base_url


async def test_a_provider_answer_falls_back_to_the_rows_url_only_when_blank():
    """`05` §7: a blank field means "the row's own URL"; a typed one is what the
    reader chose, even where the catalog disagrees."""
    step = modals.ProviderStep({}, ENTRIES)
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        row = next(p for p in ENTRIES if p.base_url)
        screen.query_one("#step-provider").value = row.id
        screen.query_one("#step-base-url").value = ""
        await pilot.pause(0.05)
        assert screen.answer().base_url == row.base_url
        screen.query_one("#step-base-url").value = "https://example.test/v1"
        assert screen.answer().base_url == "https://example.test/v1"


async def test_a_provider_pass_note_names_which_half_is_missing():
    """`05` §7: three stored states, three lines — "nothing stored" on a device with
    a URL but no model is a lie the reader has to disprove by hand."""
    cases = [
        ({"id": "ollama", "base_url": "https://x/v1", "model": "m"}, "ollama stored"),
        ({"id": "ollama", "base_url": "https://x/v1"}, "no stored model"),
        ({}, "nothing stored"),
    ]
    for stored, expected in cases:
        step = modals.ProviderStep({"llm.providers": [stored]}, ENTRIES)
        async with _host(step).run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.05)
            assert expected in pilot.app.screen.pass_note(), stored


async def test_a_help_modal_lists_every_category_and_closes_on_escape():
    """`05` §5.6: `F1` is the whole catalogue, grouped. A category that renders with
    no heading is a section the reader cannot find."""
    from bridge.ui.help import help_tabs
    from bridge.ui.modals import HelpModal

    modal = HelpModal(help_tabs())
    async with _host(modal).run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        shown = screen.categories
        assert shown, "the catalogue is not empty"
        assert all(name and text for name, text in shown.items())
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert pilot.app.dismissed is not None


async def test_a_model_picker_offers_the_stored_name_once_and_returns_it():
    """`05` §7.3: the picker's list is the provider's, plus the stored name when the
    fetch did not return it, and the list tab commits the row it highlights."""
    picker = ModelPicker("ollama", "Ollama", ["a", "b"], "b")
    async with _host(picker).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        rows = screen.query_one("#step-model-list")
        values = [str(option.id) for option in rows._options]
        assert values.count("b") == 1, "the stored name is not duplicated"
        assert "a" in values
        rows.highlighted = values.index("a")
        screen.action_submit()
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] == "a"


async def test_a_model_picker_cancel_returns_none():
    """`05` §7.3: Esc is a cancel. A picker that returned the stored name would
    rewrite the config it was asked to leave alone."""
    picker = ModelPicker("ollama", "Ollama", ["a"], "a")
    async with _host(picker).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] is None


async def test_a_model_step_with_no_levels_answers_none():
    """`05` §7.1: a provider with no level picker must answer `none`, not a level it
    never showed — the stored `extra_body` is written from this answer."""
    step = ModelStep(
        models=["m"], provider="no-such-provider", model="m", thinking="", efforts={}
    )
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        assert not screen.query("#step-thinking")
        assert screen.answer()["thinking"] == "none"
        assert screen.answer()["model"] == "m"


async def test_a_provider_step_ignores_a_change_that_is_not_its_own_select():
    """`05` §7: the step has two selects. A change on any other one must not fill the
    URL field with a provider's, which would overwrite what the reader typed."""
    step = modals.ProviderStep({}, ENTRIES)
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        screen.query_one("#step-base-url").value = "https://typed.test/v1"
        other = screen.query_one("#step-provider")
        screen.on_select_changed(
            type("E", (), {"select": type("S", (), {"id": "something-else"})(),
                           "value": ENTRIES[0].id})()
        )
        assert screen.query_one("#step-base-url").value == "https://typed.test/v1"
        assert other is not None


async def test_a_provider_answer_with_no_row_keeps_the_typed_url():
    """`05` §7: a provider outside the catalog has no row to borrow a URL from and no
    id to inherit, so the answer is the reader's own entry named `custom` — not the
    first catalog row's id, which would point the device at somebody else's host."""
    step = modals.ProviderStep({}, ENTRIES)
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        screen.query_one("#step-base-url").value = ""
        answered = screen.answer()
        assert answered.provider == "custom"
        assert not answered.base_url


async def test_a_model_step_that_narrows_to_no_levels_leaves_the_picker_alone():
    """`05` §7.1: a model publishing no effort set expresses no level, so the picker
    keeps the level already shown instead of being cleared to nothing."""
    efforts = {"small": ("low",)}
    step = ModelStep(
        models=["small", "other"], provider="ollama", model="small", thinking="low",
        efforts=efforts,
    )
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        before = screen.query_one("#step-thinking").value
        screen._narrow("other")
        await pilot.pause(0.05)
        assert screen.query_one("#step-thinking").value == before


async def test_a_find_submit_on_another_input_is_left_to_that_input():
    """`05` §5.8: the editor has more than one input when the find bar is open, and
    only the find bar's own Enter starts a replace."""
    async with _host(FilePrompt("/workspace/a.md", "a a a")).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        screen.action_find()
        await pilot.pause(0.05)
        screen.query_one("#file-find").value = "a"
        other = screen.query_one("#file-body")
        screen.on_input_submitted(
            type("E", (), {"input": type("I", (), {"id": "file-body"})(),
                           "value": "zzz", "stop": lambda *_: None})()
        )
        assert screen.query_one("#file-body").text == "a a a"
        assert other is not None


# -- the same verb reached by the binding and by the field ----------------


async def test_a_value_prompt_applies_from_either_the_key_or_the_enter_submit():
    """`05` §5.7: `Enter` is bound on the screen and also fires `Input.Submitted`.
    Both routes must answer the same value, or one of the two keypaths silently
    drops the edit."""
    step = ValuePrompt("llm.temperature", "0.7")
    async with _host(step).run_test(size=(100, 30)) as pilot:
        screen = pilot.app.screen
        screen.query_one("#value-input").value = " 0.9 "
        screen.action_submit()
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] == "0.9"

    step = ValuePrompt("llm.temperature", "0.7")
    async with _host(step).run_test(size=(100, 30)) as pilot:
        screen = pilot.app.screen
        screen.query_one("#value-input").value = "   "
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] is None


async def test_a_step_box_continues_on_ctrl_s_and_carries_its_answer():
    """`05` §7.1: `Ctrl+S` is the documented way forward, so the binding must reach
    the same `answer()` the `Enter` path does."""
    step = modals.ProviderStep({}, ENTRIES)
    async with _host(step).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        row = next(p for p in ENTRIES if p.base_url)
        screen.query_one("#step-provider").value = row.id
        await pilot.press("ctrl+s")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"].base_url == row.base_url


async def test_a_picker_takes_the_name_from_enter_on_the_typing_tab():
    """`05` §7.3: `/model` is one question per tab. `Enter` in the typing tab must
    use what is typed and stop the event, or `Input.Submitted` would bubble past the
    modal and the picker would answer from the list tab's highlight instead."""
    picker = ModelPicker("ollama", "Ollama", ["a", "b"], "a")
    async with _host(picker).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        screen.action_cycle_tab()
        await pilot.pause(0.05)
        field = screen.query_one("#step-model")
        field.value = "typed-model"
        await pilot.pause(0.05)
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert pilot.app.dismissed["value"] == "typed-model"


async def test_the_picker_opens_on_the_list_tab_with_the_cursor_movable():
    """`05` §7.3: the defect this rewrite fixes is a list that could not be used.
    Opening on the list tab with focus inside it is what makes the first arrow key
    move the highlight rather than the tab strip."""
    picker = ModelPicker("ollama", "Ollama", ["a", "b"], "a")
    async with _host(picker).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        assert screen._active() == "list"
        assert type(screen.focused).__name__ == "OptionList"
        await pilot.press("down")
        await pilot.pause(0.05)
        assert screen.query_one("#step-model-list").highlighted == 1


async def test_tab_switches_which_answer_enter_takes():
    """`05` §7.3: the tabs are not decoration. `Tab` moves to the typing tab and
    `Enter` there answers from the field; `Tab` again returns to the list."""
    picker = ModelPicker("ollama", "Ollama", ["a", "b"], "a")
    async with _host(picker).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert screen._active() == "type"
        assert screen.focused is screen.query_one("#step-model")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert screen._active() == "list"
        assert type(screen.focused).__name__ == "OptionList"


async def test_an_enter_in_the_todo_value_field_before_a_verb_does_nothing():
    """`05` §6.2: the value field is empty until a verb arms it. Enter there must
    return, not read a `_pending` that is not set."""
    from bridge.ui.modals import TodoEditor

    async with _host(TodoEditor([{"name": "design", "tasks": []}])).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.1)
        editor = pilot.app.screen
        field = editor.query_one("#todo-value")
        field.focus()
        assert editor._pending is None
        before = list(editor.phases)
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert editor.phases == before
        assert editor._pending is None


async def test_ctrl_n_on_an_empty_tree_adds_the_first_phase():
    """`05` §7: the editor opens on a device whose list is empty, and the first
    phase must be addable from there. Refusing every verb with "select a phase or a
    task first" leaves a tree with nothing to select and no way to seed one."""
    from bridge.ui.modals import TodoEditor

    async with _host(TodoEditor([])).run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        editor = pilot.app.screen
        assert editor.query_one("#todo-tree").option_count == 0
        await pilot.press("ctrl+n")
        await pilot.pause(0.05)
        field = editor.query_one("#todo-value")
        assert field.value == ""
        field.value = "Build"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert editor.phases == [{"name": "Build", "tasks": []}]


async def test_a_find_then_a_second_enter_replaces_every_occurrence():
    """`05` §5.8.1: the file editor's find bar takes two Enters — the needle, then
    the replacement. One Enter must not replace with the needle itself, and the
    second must rewrite every occurrence, not only the one under the cursor."""
    from bridge.ui.modals import FilePrompt

    async with _host(FilePrompt("/workspace/a.md", "a a a\nb a")).run_test(
        size=(100, 30)
    ) as pilot:
        await pilot.pause(0.1)
        screen = pilot.app.screen
        screen.action_find()
        await pilot.pause(0.05)
        bar = screen.query_one("#file-find")
        bar.focus()
        bar.value = "a"
        await pilot.press("enter")
        await pilot.pause(0.05)
        # The first Enter armed the replace; nothing is rewritten yet.
        assert screen.query_one("#file-body").text == "a a a\nb a"
        bar = screen.query_one("#file-find")
        bar.value = "z"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert screen.query_one("#file-body").text == "z z z\nb z"
        # And the bar closed, so the next Enter saves rather than replacing again.
        assert not bar.has_class("active")


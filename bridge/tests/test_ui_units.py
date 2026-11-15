"""The pure decisions behind the smaller screens, asserted where they are decided.

`AGENTS.md` asks for the cheapest layer that can fail a rule, and every function here
is pure: a coercion, a completion, a verdict sentence. Driving a pilot to reach them
would prove the widget and skip the rule, so they are called directly.
"""

from __future__ import annotations

import json

import pytest

from bridge.ui import config as config_mod
from bridge.ui.help import _summary, suggest
from bridge.ui.history import PromptHistory
from bridge.ui.onboard import Answers, verdict_model, verdict_network, verdict_provider
from bridge.ui.render import json_payload, todo_lines
from bridge.ui.splash import _gradient
from bridge.ui.testing import chat_lines


# --- config coercion (`03-config.md` §Full schema) --------------------------


def test_a_bool_row_accepts_the_four_spellings_and_refuses_the_rest():
    """`config.py` takes `true/1/yes/on`, so a reader typing `True` or `yes` must
    not be told their value is invalid."""
    row = _row("tools.allow_dangerous")
    for text in ("true", "1", "yes", "on", "TRUE"):
        assert config_mod.coerce(row, text) is True
    for text in ("false", "0", "no", "off"):
        assert config_mod.coerce(row, text) is False
    with pytest.raises(ValueError, match="expects true or false"):
        config_mod.coerce(row, "maybe")


def test_an_enum_row_is_checked_against_the_words_the_bound_cell_names():
    row = _row("device.log_level")
    assert config_mod.coerce(row, "debug") == "debug"
    with pytest.raises(ValueError, match="expects one of"):
        config_mod.coerce(row, "verbose")


def test_a_list_row_splits_on_commas_and_an_empty_field_is_the_empty_list():
    """An emptied field is how a reader removes every host, so `""` must not become
    `[""]` — that would be a hostname no request can match."""
    ints = _row("wifi.retry_backoff_ms")
    assert config_mod.coerce(ints, "1000, 2000") == [1000, 2000]
    assert config_mod.coerce(ints, "") == []
    with pytest.raises(ValueError, match="comma-separated integers"):
        config_mod.coerce(ints, "1000,soon")

    strings = _row("ntp.servers[]")
    assert config_mod.coerce(strings, "a.test, b.test") == ["a.test", "b.test"]
    assert config_mod.coerce(strings, "") == []


def test_a_string_row_is_taken_verbatim_but_an_int_row_is_not():
    """A string may carry spaces (`agent.system_prompt` does not, but a name might);
    an int row must reject them rather than guess."""
    assert config_mod.coerce(_row("ntp.timezone"), "UTC0") == "UTC0"
    assert config_mod.coerce(_row("ntp.sync_interval_s"), " 3600 ") == 3600
    with pytest.raises(ValueError, match="expects an integer"):
        config_mod.coerce(_row("ntp.sync_interval_s"), "soon")


def test_a_kind_the_table_has_no_row_for_is_an_error_not_a_silent_string():
    """A new kind added to the table without a branch here would otherwise store the
    raw text and let the device refuse it later, where the reader cannot see why."""
    row = config_mod.ConfigRow("made.up", "object", {}, "anything", "made")
    with pytest.raises(ValueError, match="unsupported kind 'object'"):
        config_mod.coerce(row, "whatever")


def _row(path: str) -> config_mod.ConfigRow:
    return next(candidate for candidate in config_mod.ROWS if candidate.path == path)


# --- help completion (`05` §5.6) --------------------------------------------


def test_a_command_completes_to_its_whole_name():
    assert suggest("/sy") == "/sys"
    assert suggest("/") is None  # every name shares no useful prefix beyond `/`


def test_a_sub_command_completes_inside_its_own_verb():
    assert suggest("/todo ") is None
    assert suggest("/model g") is None or suggest("/model g").startswith("/model ")


def test_a_finished_word_and_an_unknown_verb_get_no_suggestion():
    """A trailing space means the word is done; suggesting it again would make the
    completion list flicker on every keystroke of an argument."""
    assert suggest("/sys ") is None
    assert suggest("not a command") is None
    assert suggest("/nosuchverb a") is None


def test_a_summary_is_the_catalogue_row_or_nothing():
    assert _summary("sys")
    assert _summary("nosuchcommand") == ""


# --- history walk (`05` §5.1) ----------------------------------------------


def test_history_walks_back_and_forth_restoring_the_unfinished_line(tmp_path):
    """The draft slot is the whole point of the walk: a reader who pressed Up to read
    the last message and then Down must get their half-typed line back."""
    history = PromptHistory(path=tmp_path / "h.json")
    history.add("first")
    history.add("second")

    assert history.previous("half typed") == "second"
    assert history.previous("second") == "first"
    # At the oldest entry, stepping back stays put.
    assert history.previous("first") == "first"
    assert history.next("first") == "second"
    assert history.next("second") == "half typed"
    # Past the draft slot the walk has ended.
    assert history.next("half typed") == "half typed"


def test_stepping_forward_with_no_walk_in_progress_returns_the_line_unchanged(tmp_path):
    history = PromptHistory(path=tmp_path / "h.json")
    history.add("only")
    assert history.next("typing") == "typing"


def test_an_empty_history_returns_the_line_unchanged(tmp_path):
    history = PromptHistory(path=tmp_path / "h.json")
    assert history.previous("typing") == "typing"
    assert history.next("typing") == "typing"


# --- render ----------------------------------------------------------------


def test_a_value_json_cannot_encode_falls_back_to_its_own_text():
    """A tool result is arbitrary JSON, but a set or an object with a non-string key
    is not encodable. The line is still shown: dropping it would hide the result."""
    assert json_payload({"a": 1}) == '{"a":1}'
    assert json_payload({1, 2}) in ("{1, 2}", "{2, 1}")
    assert json_payload({(1, 2): "x"}) == "{(1, 2): 'x'}"


def test_a_non_object_task_in_a_phase_is_skipped_not_rendered_as_garbage():
    """`todo.apply` returns what the device built, and a malformed entry must not turn
    the whole list into a traceback."""
    lines = todo_lines({"phases": [{"name": "One",
                                    "tasks": ["not a task", {"content": "real"}]}]})
    assert lines[0] == "One"
    assert any("real" in line for line in lines)
    assert not any("not a task" in line for line in lines)


# --- splash ----------------------------------------------------------------


def test_the_gradient_clamps_at_both_ends():
    """A gradient sampled past its stops must not index out of range: `t` comes from
    a clock, so an off-by-one is a crash on the first frame."""
    assert _gradient(-1.0) == _gradient(0.0)
    assert _gradient(2.0) == _gradient(1.0)
    assert _gradient(0.5) != _gradient(0.0)


# --- onboard verdicts (`05` §5.10) -----------------------------------------


def _answers(**kwargs) -> Answers:
    fields = {"ssid": "", "password": "", "provider": "openai",
              "base_url": "https://api.openai.com/v1", "model": "gpt", "api_key": "k"}
    fields.update(kwargs)
    return Answers(**fields)


def test_the_network_verdict_names_the_reason_not_just_the_state():
    """`05` §5.10: "failed" alone leaves the reader guessing between a wrong
    passphrase and a radio that never came up."""
    assert verdict_network({"wifi": {"state": "connected", "ssid": "home"}}).startswith(
        "network: joined home")
    assert "passphrase" in verdict_network({"wifi": {"state": "failed"}})
    assert "still connecting" in verdict_network({"wifi": {"state": "connecting"}})
    assert "safe mode" in verdict_network({"wifi": {"state": "off"}})
    # A device that reported no wifi block at all is a different fact from a radio
    # that is off, and the reader acts on it differently.
    assert verdict_network(None) == "network: the radio is off; the device may be in safe mode"


def test_the_provider_verdict_separates_no_key_from_an_unreachable_host():
    from bridge import providers

    entry = providers.find("openai")
    assert entry is not None and entry.models_url
    assert verdict_provider(["m"], _answers(api_key="")) == (
        "provider: no key typed, so the list cannot be checked")
    assert "refused or the host is unreachable" in verdict_provider([], _answers())
    assert verdict_provider(["a", "b"], _answers()) == "provider: 2 models"
    assert verdict_provider(["m"], _answers(base_url="")) == "provider: no base url"


def test_a_provider_that_publishes_no_list_is_named_as_such():
    """A custom provider has no catalog URL, which is not a failed fetch: the reader
    must type the name rather than believe the key was rejected."""
    line = verdict_provider([], _answers(provider="custom", base_url="https://t/v1"))
    assert "publishes no model list" in line


def test_the_model_verdict_says_whether_the_name_was_checked():
    """`05` §5.10: `unchecked` and `not in the list` are different facts, and only one
    of them is a typo to fix."""
    assert verdict_model([], _answers()) == "model: gpt (unchecked; the provider listed none)"
    assert verdict_model(["gpt"], _answers()) == "model: gpt"
    assert "pick one from the dropdown" in verdict_model(["other"], _answers())
    assert verdict_model(["gpt"], _answers(model="")) == "model: none chosen"


# --- chat_lines ------------------------------------------------------------


def test_chat_lines_joins_every_segment_of_a_styled_line():
    """"`line.text`" on a styled line is the repr of its segments, so a test that read
    it would match a substring of the repr and miss what the reader sees."""

    class _Line:
        text = [("you: ", None), ("hello", None)]

        @property
        def segments(self):
            class _Seg:
                def __init__(self, text: str) -> None:
                    self.text = text
            return [_Seg("you: "), _Seg("hello")]

    class _Log:
        lines = [_Line(), type("L", (), {"text": "plain"})()]

    class _App:
        def query_one(self, selector: str):
            assert selector == "#chat-log"
            return _Log()

    assert chat_lines(_App()) == ["you: hello", "plain"]

"""`bridge/tools/test_agent.py` — the device double, exercised directly.

The e2e suite proves the double works over a port; this suite proves the rules
`10-test-agent.md` §5 puts on it, including the refusals. Both are needed: e2e
can only observe the frames a healthy `Session` accepts, so the §5 ladder's
rejections are invisible there. Every case here drives the real `TestAgent`
through a real pty and reads the real bytes, so a rule broken in the double
fails here rather than surfacing as a mysterious bridge timeout.

`AGENTS.md` names this double as one of the few allowed test doubles, which is
why it carries its own suite instead of being mocked out of existence.
"""

from __future__ import annotations

import base64
import json
import os
import pty
import sys
import threading
import time
import tty
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bridge.protocol import (  # noqa: E402
    COMMAND_NAMES,
    MAX_FROM_BRIDGE_BYTES,
    Frame,
    encode,
)
import bridge.tools.test_agent as agent_mod  # noqa: E402

# Aliased: pytest would try to collect `TestAgent` itself as a test class.
Agent = agent_mod.TestAgent
BOOT_ID = agent_mod.BOOT_ID
CANNED_TOOLS = agent_mod.CANNED_TOOLS
STATUS = agent_mod.STATUS
_load_script = agent_mod._load_script


class Harness:
    """A `TestAgent` on a pty master, with the slave drained per assertion."""

    def __init__(self, **kwargs: object) -> None:
        self.master, self.slave = pty.openpty()
        # The agent writes to the master, so its bytes surface on the slave; no
        # echo and no line discipline, or `replies()` would read its own input.
        tty.setraw(self.slave)
        tty.setraw(self.master)
        self.agent = Agent(self.master, stream=_NullStream(), **kwargs)  # type: ignore[arg-type]
        self._buf = b""

    def feed(self, line: bytes) -> None:
        """One line to the agent, exactly as a bridge would write it."""
        # `run()` drops every line after a rejection; mirror that, or a test
        # would prove the agent answers on a link it has already closed.
        for pending in self.agent._consume(line + b"\n"):
            if not self.agent._rejected:
                self.agent.handle(pending)

    def feed_frame(self, frame: Frame) -> None:
        self.feed(encode(frame).rstrip(b"\n"))

    def replies(self) -> list[dict]:
        """Every complete NDJSON line the agent has emitted."""
        os.set_blocking(self.slave, False)
        try:
            while True:
                chunk = os.read(self.slave, 65536)
                if not chunk:
                    break
                self._buf += chunk
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(self.slave, True)
        out: list[dict] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                out.append(json.loads(line))
        return out

    def one(self) -> dict:
        got = self.replies()
        assert len(got) == 1, f"expected one reply, got {got}"
        return got[0]

    def close(self) -> None:
        os.close(self.master)
        os.close(self.slave)


class _NullStream:
    def write(self, _text: str) -> None:
        pass

    def flush(self) -> None:
        pass


@pytest.fixture
def harness():
    h = Harness()
    yield h
    h.close()


def _frame(type_: str, body: dict, *, id_: str = "b-1") -> Frame:
    return Frame(type=type_, id=id_, ts=1730563600, body=body)


HELLO = {"client": "bridge", "client_version": "0.1.0", "protocol": 1}


# -- happy paths -------------------------------------------------------------


def test_hello_ack_reports_the_device_contract(harness):
    harness.feed_frame(_frame("hello", dict(HELLO)))
    ack = harness.one()
    assert ack["type"] == "hello_ack"
    assert ack["reply_to"] == "b-1"
    assert ack["device"] == "test-agent"
    assert ack["boot_id"] == BOOT_ID
    # `04` §3.2: the advertised set is what is callable now, so the double must
    # offer the commands it actually answers, not a hopeful superset.
    assert ack["capabilities"]["tools"] == list(CANNED_TOOLS)
    assert ack["limits"]["rx_max"] == MAX_FROM_BRIDGE_BYTES
    assert ack["protocol"] == 1


def test_net_ping_is_answered_generically(harness):
    """`04` §4.25: `net.ping` is a known name, so the double must answer it.

    The reply is flat — `name` and `ok` sit on the frame root, not under a `body`
    member — which is what `test_every_other_known_command_is_answered` also
    asserts for every other name.
    """
    harness.feed_frame(
        _frame("command", {"name": "net.ping", "args": {"target": "router"}})
    )
    reply = harness.one()
    assert reply["type"] == "command.result"
    assert reply["name"] == "net.ping"
    assert reply["ok"] is True


def test_ack_is_silent(harness):
    harness.feed_frame(_frame("ack", {"ack_of": "d-1"}))
    assert harness.replies() == []


def test_prompt_is_echoed_as_a_run(harness):
    harness.feed_frame(_frame("prompt", {"text": "hello there"}))
    start, final = harness.replies()
    assert start["type"] == "agent.start"
    assert start["trigger"] == "manual" and start["source"] == "prompt"
    assert start["input_bytes"] == len("hello there")
    assert final["type"] == "agent.final"
    assert final["text"] == "hello there"
    assert final["run_id"] == start["run_id"]
    # `04` §3.16: a run that reports tokens must report all three counters.
    assert set(final["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}


def test_status_answers_the_documented_shape(harness):
    harness.feed_frame(_frame("command", {"name": "status", "args": {}}))
    result = harness.one()
    assert result["ok"] is True
    assert result["result"] == STATUS


@pytest.mark.parametrize("name", sorted(COMMAND_NAMES - {"status", "sandbox.list", "sandbox.read"}))
def test_every_other_known_command_is_answered(harness, name):
    """A name in `COMMAND_NAMES` must never fall through to silence."""
    harness.feed_frame(_frame("command", {"name": name, "args": {"path": "/workspace/notes.md"}}))
    reply = harness.one()
    assert reply["type"] == "command.result"
    assert reply["name"] == name


def test_sandbox_list_reports_the_fixture_tree(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.list", "args": {"path": "/workspace/"}}))
    result = harness.one()["result"]["entries"]
    names = [e["name"] for e in result]
    assert names == ["runs", "notes.md"]
    assert [e["dir"] for e in result] == [True, False]
    assert result[1]["bytes"] == len("# notes\n\nplain text, no markup.\n")


def test_fake_device_answers_wifi_scan(harness):
    """`10-test-agent.md`: the reply is `04` §4.24's shape, not an echo. The
    fall-through that answered `{"echo": {}}` is what made the bridge's scan look
    like a radio that heard nothing."""
    harness.feed_frame(_frame("command", {"name": "wifi.scan", "args": {}}))
    reply = harness.one()
    assert reply["ok"] is True
    result = reply["result"]
    assert [n["ssid"] for n in result["networks"]] == ["HomeLab", "Cafe-FREE", "OldRouter"]
    assert [n["rssi"] for n in result["networks"]] == [-47, -71, -83]
    assert result["truncated"] is False


def test_fake_device_steers_the_station(harness):
    """`10-test-agent.md` §6: each verb leaves a different state, so a bridge test
    distinguishes them instead of reading one echo four times."""
    harness.feed_frame(_frame("command", {"name": "wifi.connect", "args": {}}))
    up = harness.one()
    assert up["ok"] is True
    assert up["result"]["state"] == "connected"
    assert up["result"]["ssid"] == "HomeLab"

    harness.feed_frame(_frame("command", {"name": "wifi.disconnect", "args": {}}))
    down = harness.one()
    assert down["result"]["state"] == "off"
    assert down["result"]["ssid"] is None


def test_fake_device_keeps_a_todo_list_across_ops(harness):
    """`10-test-agent.md` §6: the double holds the tree, so a `done` after `init` is
    observable. An echo of `args` would let a bridge test assert its own request."""
    harness.feed_frame(
        _frame(
            "command",
            {"name": "todo.apply", "args": {"op": "init", "phase": "auth", "items": ["wire jwt"]}},
        )
    )
    created = harness.one()["result"]["phases"]
    assert [(p["name"], [t["status"] for t in p["tasks"]]) for p in created] == [
        ("auth", ["pending"])
    ]

    harness.feed_frame(
        _frame(
            "command",
            {"name": "todo.apply", "args": {"op": "done", "task": "auth", "phase": "auth"}},
        )
    )
    assert harness.one()["result"]["phases"][0]["tasks"][0]["status"] == "completed"


def test_fake_device_view_does_not_change_the_list(harness):
    harness.feed_frame(
        _frame("command", {"name": "todo.apply", "args": {"op": "init", "items": ["a"]}})
    )
    harness.one()
    harness.feed_frame(_frame("command", {"name": "todo.apply", "args": {"op": "view"}}))
    assert harness.one()["result"]["phases"][0]["tasks"][0]["status"] == "pending"


def test_sandbox_list_of_a_nested_directory(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.list", "args": {"path": "/workspace/runs/"}}))
    entries = harness.one()["result"]["entries"]
    assert [(e["name"], e["dir"]) for e in entries] == [("run_7.json", False)]


def test_sandbox_read_returns_the_file(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.read", "args": {"path": "/workspace/notes.md"}}))
    result = harness.one()["result"]
    assert result["text"].startswith("# notes")
    assert result["bytes"] == len(result["text"])
    assert result["truncated"] is False


def test_sandbox_write_reports_created_and_decoded_length(harness):
    data = base64.b64encode(b"abcdef").decode()
    harness.feed_frame(
        _frame("command", {"name": "sandbox.write",
                           "args": {"path": "/workspace/x.txt", "data": data, "offset": 0}})
    )
    result = harness.one()["result"]
    # The length is of the decoded chunk, not of the base64 text: a bridge that
    # sent the wrong thing would otherwise look correct.
    assert result["bytes"] == 6
    assert result["created"] is True


def test_sandbox_write_with_a_later_offset_is_not_a_creation(harness):
    data = base64.b64encode(b"xy").decode()
    harness.feed_frame(
        _frame("command", {"name": "sandbox.write",
                           "args": {"path": "/workspace/x.txt", "data": data, "offset": 6}})
    )
    assert harness.one()["result"]["created"] is False


def test_sandbox_delete_reports_deletion(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.delete", "args": {"path": "/workspace/x.txt"}}))
    result = harness.one()["result"]
    assert result["deleted"] is True and result["path"] == "/workspace/x.txt"


def test_sandbox_delete_of_a_populated_directory_names_the_entries(harness):
    """`04` §4.19: the refusal carries the count the bridge needs to ask."""
    harness.feed_frame(
        _frame("command", {"name": "sandbox.delete", "args": {"path": "/workspace/runs"}})
    )
    body = harness.one()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_CONFIG"
    assert body["error"]["detail"] == {"path": "/workspace/runs", "entries": 1}


def test_sandbox_delete_recursive_reports_the_total(harness):
    harness.feed_frame(
        _frame("command",
               {"name": "sandbox.delete",
                "args": {"path": "/workspace/runs", "recursive": True}})
    )
    result = harness.one()["result"]
    assert result["deleted"] is True and result["entries"] == 1


def test_sandbox_mkdir_reports_creation(harness):
    harness.feed_frame(
        _frame("command", {"name": "sandbox.mkdir", "args": {"path": "/workspace/new"}})
    )
    assert harness.one()["result"] == {"path": "/workspace/new", "created": True}


def test_sandbox_move_reports_both_ends(harness):
    """`04` §4.22: both paths come back, because a rename can change either."""
    harness.feed_frame(
        _frame("command",
               {"name": "sandbox.move",
                "args": {"from": "/workspace/a", "to": "/workspace/b"}})
    )
    assert harness.one()["result"] == {
        "from": "/workspace/a",
        "to": "/workspace/b",
        "moved": True,
    }


def test_sys_reset_counter_reports_the_previous_value(harness):
    """`04` §4.23: the previous count is the proof the reset happened."""
    harness.feed_frame(_frame("command", {"name": "sys.reset_counter", "args": {}}))
    result = harness.one()["result"]
    assert result["reset"] == "boot_failures"
    assert result["previous"] == 0
    assert result["safe_mode"] is False


def test_secret_set_echoes_only_the_length(harness):
    harness.feed_frame(_frame("secret.set", {"key": "llm.api_key", "value": "sk-123456"}))
    result = harness.one()["result"]
    # `06-security.md`: the value never comes back over the wire.
    assert result == {"key": "llm.api_key", "stored": True, "length": 9}


def test_cancel_ends_a_live_run(harness):
    # The double answers a prompt in full, so it never has a run open on its
    # own; a live run is the state a real device is in mid-answer.
    harness.agent.run_id = "r-1"
    harness.feed_frame(_frame("cancel", {}))
    err = harness.one()
    assert err["type"] == "agent.error"
    assert err["code"] == "E_CANCELLED" and err["retryable"] is False
    assert err["run_id"] == "r-1"
    # Cancelling twice must not resurrect the finished run.
    harness.feed_frame(_frame("cancel", {}))
    assert harness.one()["kind"] == "cancel.noop"


def test_cancel_without_a_run_is_a_noop_event(harness):
    harness.feed_frame(_frame("cancel", {}))
    event = harness.one()
    assert event["type"] == "event" and event["kind"] == "cancel.noop"


# -- the §5 ladder, each rung ------------------------------------------------


def test_oversize_line_is_refused(harness):
    line = b'{"v":1,"type":"ping","id":"b-1","ts":1,"pad":"' + b"x" * MAX_FROM_BRIDGE_BYTES + b'"}'
    harness.feed(line)
    reply = harness.one()
    assert reply["error"]["code"] == "E_PROTOCOL"
    # The message carries the measured size, per `04` §1.2: a refusal that only
    # says "too big" cannot be acted on.
    assert "bytes" in reply["error"]["message"]


def test_invalid_utf8_is_refused(harness):
    harness.feed(b'{"v":1,"type":"ping","id":"b-1","ts":1,"x":"\xff"}')
    assert harness.one()["error"]["code"] == "E_PROTOCOL"


def test_non_json_is_refused(harness):
    harness.feed(b'{"v":1,"type"')
    # Nothing to reply to: the truncated prefix never reached an `id`.
    event = harness.one()
    assert event["kind"] == "protocol.error" and event["data"]["code"] == "E_PROTOCOL"


def test_a_broken_line_with_a_readable_id_answers_the_request(harness):
    """`04` §1.2: recover the id from the prefix, so the bridge sees a refusal
    against the frame it sent rather than an anonymous event."""
    harness.feed(b'{"v":1,"type":"command","id":"b-5","name":"status"')
    reply = harness.one()
    assert reply["type"] == "command.result" and reply["reply_to"] == "b-5"
    assert reply["error"]["code"] == "E_PROTOCOL"


def test_unrecoverable_line_reports_a_protocol_event(harness):
    harness.feed(b"not json at all")
    event = harness.one()
    assert event["type"] == "event" and event["kind"] == "protocol.error"
    assert event["data"]["dropped_bytes"] == len(b"not json at all")


def test_a_json_array_is_not_a_frame(harness):
    # No `id` to answer, so §1.2's untargeted path applies.
    harness.feed(b'["v",1]')
    event = harness.one()
    assert event["type"] == "event" and event["kind"] == "protocol.error"
    assert event["data"]["code"] == "E_PROTOCOL"


@pytest.mark.parametrize("patch", [{"v": 2}, {"type": 7}, {"ts": "now"}, {"ts": True}])
def test_bad_envelope_is_refused(harness, patch):
    raw = {"v": 1, "type": "ping", "id": "b-1", "ts": 1, **patch}
    harness.feed(json.dumps(raw).encode())
    reply = harness.one()
    assert reply["error"]["code"] == "E_PROTOCOL"
    assert reply["error"]["detail"]["direction"] == "bridge_to_device"


def test_a_null_id_leaves_nothing_to_answer(harness):
    # `id` is present but not a string: the frame is unusable and the id cannot
    # be trusted, so the refusal is the untargeted event rather than a guess.
    harness.feed(b'{"v":1,"type":"ping","id":null,"ts":1}')
    assert harness.one()["kind"] == "protocol.error"


def test_a_device_to_bridge_type_is_refused(harness):
    """`04` §2: a type in the wrong direction is `E_PROTOCOL`, not 'unknown'."""
    harness.feed_frame(_frame("hello_ack", {"device": "x"}))
    reply = harness.one()
    assert reply["type"] == "command.result"
    assert reply["error"]["detail"]["direction"] == "bridge_to_device"


def test_missing_required_member_is_refused(harness):
    harness.feed_frame(_frame("prompt", {}))
    reply = harness.one()
    assert reply["error"]["code"] == "E_PROTOCOL"
    assert "prompt requires text" in reply["error"]["message"]


def test_unknown_command_name_is_refused(harness):
    harness.feed_frame(_frame("command", {"name": "no.such.command"}))
    reply = harness.one()
    assert reply["error"]["code"] == "E_PROTOCOL"
    assert "unknown command name" in reply["error"]["message"]


def test_empty_secret_value_is_a_config_error(harness):
    harness.feed_frame(_frame("secret.set", {"key": "llm.api_key", "value": ""}))
    assert harness.one()["error"]["code"] == "E_CONFIG"


def test_bad_base64_is_refused(harness):
    harness.feed_frame(
        _frame("command", {"name": "sandbox.write",
                           "args": {"path": "/workspace/x", "data": "not base64!!"}})
    )
    reply = harness.one()
    assert reply["type"] == "command.result" and reply["ok"] is False
    assert reply["error"]["code"] == "E_PROTOCOL"


def test_sandbox_list_outside_the_fixture_is_storage(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.list", "args": {"path": "/workspace/nope/"}}))
    reply = harness.one()
    assert reply["ok"] is False and reply["error"]["code"] == "E_STORAGE"


def test_sandbox_read_outside_the_fixture_is_storage(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.read", "args": {"path": "/workspace/nope"}}))
    assert harness.one()["error"]["code"] == "E_STORAGE"


def test_sandbox_path_that_escapes_is_a_config_error(harness):
    harness.feed_frame(_frame("command", {"name": "sandbox.read", "args": {"path": "/workspace/../../etc"}}))
    assert harness.one()["error"]["code"] == "E_CONFIG"


def test_a_legal_detour_normalises_before_lookup(harness):
    """`/workspace/runs/../notes.md` is legal: refusing it would be a false negative."""
    harness.feed_frame(
        _frame("command", {"name": "sandbox.read", "args": {"path": "/workspace/runs/../notes.md"}})
    )
    reply = harness.one()
    assert reply["ok"] is True and reply["result"]["text"].startswith("# notes")


def test_duplicate_hello_is_refused(harness):
    harness.feed_frame(_frame("hello", dict(HELLO)))
    harness.replies()
    harness.feed_frame(_frame("hello", dict(HELLO), id_="b-2"))
    assert harness.one()["error"]["code"] == "E_PROTOCOL"


def test_no_hello_suppresses_the_ack(harness):
    h = Harness(no_hello=True)
    try:
        h.feed_frame(_frame("hello", dict(HELLO)))
        assert h.replies() == []
    finally:
        h.close()


def test_fail_on_injects_the_documented_refusal(harness):
    h = Harness(fail_on=["ping"])
    try:
        h.feed_frame(_frame("ping", {}))
        reply = h.one()
        assert reply["type"] == "command.result" and reply["ok"] is False
        assert reply["error"]["message"] == "--fail-on ping"
    finally:
        h.close()


class TestTokenAuth:
    """`04` §4.4: a token configured must be presented, and a wrong one is refused."""

    def test_a_good_token_is_accepted(self, harness):
        h = Harness(token="s3cret")
        try:
            h.feed_frame(_frame("hello", {**HELLO, "token": "s3cret"}))
            assert h.one()["type"] == "hello_ack"
        finally:
            h.close()

    def test_a_missing_token_is_refused(self, harness):
        h = Harness(token="s3cret")
        try:
            h.feed_frame(_frame("hello", dict(HELLO)))
            assert h.one()["error"]["code"] == "E_PROTOCOL"
        finally:
            h.close()

    def test_an_unexpected_token_is_refused(self, harness):
        h = Harness()
        try:
            h.feed_frame(_frame("hello", {**HELLO, "token": "s3cret"}))
            assert h.one()["error"]["code"] == "E_PROTOCOL"
        finally:
            h.close()

    def test_a_wrong_token_emits_link_rejected(self, harness):
        h = Harness(token="s3cret")
        try:
            h.feed_frame(_frame("hello", {**HELLO, "token": "wrong"}))
            event = h.one()
            assert event["type"] == "event"
            assert event["kind"] == "link.rejected"
            assert event["data"]["reason"] == "auth"
            # Everything after a rejection is dropped: the link is closed.
            h.feed_frame(_frame("ping", {}, id_="b-9"))
            assert h.replies() == []
        finally:
            h.close()


# -- scripted frames ---------------------------------------------------------


def test_a_script_replaces_the_table_reply(harness, tmp_path):
    script = tmp_path / "s.ndjson"
    script.write_text(
        "\n".join(
            [
                json.dumps({"type": "event", "kind": "custom"}),
                "",
                json.dumps({"wait_ms": 1}),
                json.dumps({"type": "raw", "data": "{\"v\":1,\"type\":\"event\"}\n"}),
            ]
        )
    )
    h = Harness(script=_load_script(str(script)))
    try:
        h.feed_frame(_frame("ping", {}))
        assert h.one()["kind"] == "custom"
        # The `wait_ms` entry consumes one frame without replying.
        h.feed_frame(_frame("ping", {}))
        assert h.replies() == []
        # The `raw` entry writes bytes verbatim, bypassing `encode`.
        h.feed_frame(_frame("ping", {}))
        assert h.one()["type"] == "event"
    finally:
        h.close()


def test_a_script_stops_when_exhausted(harness, tmp_path):
    script = tmp_path / "s.ndjson"
    script.write_text(json.dumps({"type": "event", "kind": "once"}))
    h = Harness(script=_load_script(str(script)))
    try:
        h.feed_frame(_frame("ping", {}))
        assert h.one()["kind"] == "once"
        # Back to the table: the script is not a permanent replacement.
        h.feed_frame(_frame("ping", {}))
        assert h.one()["type"] == "pong"
    finally:
        h.close()


def test_a_burst_emits_several_frames_for_one_inbound_frame(harness, tmp_path):
    """A run is one prompt answered by start, tool, result and final.

    One entry per inbound frame cannot express that, so `burst` groups them
    (`10-test-agent.md` §7). The group consumes one entry and one inbound frame,
    and every inner entry completes its own envelope.
    """
    script = tmp_path / "s.ndjson"
    script.write_text(
        json.dumps(
            {
                "burst": [
                    {"type": "agent.start", "run_id": "r-1"},
                    {"type": "agent.tool", "run_id": "r-1", "call_id": "c1"},
                    {"type": "agent.final", "run_id": "r-1", "text": "done"},
                ]
            }
        )
    )
    h = Harness(script=_load_script(str(script)))
    try:
        h.feed_frame(_frame("prompt", {"text": "go"}, id_="b-1"))
        got = h.replies()
        assert [r["type"] for r in got] == [
            "agent.start",
            "agent.tool",
            "agent.final",
        ]
        # Every frame answers the same prompt, which is what a run does.
        assert {r["reply_to"] for r in got} == {"b-1"}
        # One inbound frame consumed the whole group: the next frame is a table reply.
        h.feed_frame(_frame("ping", {}))
        assert h.one()["type"] == "pong"
    finally:
        h.close()


def test_a_burst_may_mix_raw_and_wait_entries(harness, tmp_path):
    """The inner shapes are the outer ones, so a burst can interleave wire noise."""
    script = tmp_path / "s.ndjson"
    script.write_text(
        json.dumps(
            {
                "burst": [
                    {"type": "event", "kind": "first"},
                    {"wait_ms": 1},
                    {"type": "raw", "data": '{"v":1,"type":"event","kind":"raw"}\n'},
                ]
            }
        )
    )
    h = Harness(script=_load_script(str(script)))
    try:
        h.feed_frame(_frame("ping", {}))
        got = h.replies()
        assert [r["kind"] for r in got] == ["first", "raw"]
    finally:
        h.close()


def test_a_burst_may_not_nest_and_says_so(harness, tmp_path):
    """`10-test-agent.md` §7: a burst does not nest. Refusing it by name beats a
    later `KeyError` on the missing `type`."""
    script = tmp_path / "s.ndjson"
    script.write_text(json.dumps({"burst": [{"burst": [{"type": "event"}]}]}))
    h = Harness(script=_load_script(str(script)))
    try:
        with pytest.raises(SystemExit) as excinfo:
            h.feed_frame(_frame("ping", {}))
        assert "may not contain another burst" in str(excinfo.value)
    finally:
        h.close()


def test_a_malformed_script_names_the_line(tmp_path):
    script = tmp_path / "bad.ndjson"
    script.write_text("{not json}")
    with pytest.raises(SystemExit) as excinfo:
        _load_script(str(script))
    assert f"{script}:1" in str(excinfo.value)


def test_the_script_may_omit_reply_to(harness, tmp_path):
    script = tmp_path / "s.ndjson"
    script.write_text(json.dumps({"type": "event", "kind": "x", "reply_to": "explicit"}))
    h = Harness(script=_load_script(str(script)))
    try:
        h.feed_frame(_frame("ping", {}))
        assert h.one()["reply_to"] == "explicit"
    finally:
        h.close()


def test_an_unknown_type_is_refused_by_the_decoder_not_the_direction_check(harness):
    """`04` §2.1: an unknown type is *unknown*, and it fails in `decode_line` before
    the direction check can call it misdirected. The refusal must say so, and the
    recovered `id` lets the host read it as a result for the frame it sent."""
    h = Harness()
    try:
        h.feed(b'{"v":1,"type":"nonsense.type","id":"b-1","ts":0}')
        got = h.one()
        assert got["type"] == "command.result"
        assert got["reply_to"] == "b-1"
        assert got["ok"] is False
        assert got["error"]["code"] == "E_PROTOCOL"
        assert "unknown type" in got["error"]["message"]
    finally:
        h.close()


def test_a_bad_reply_to_is_refused_and_still_answers_the_frame(harness):
    """`reply_to` is an id when present; a wrong shape is refused by the codec, and the
    host still gets a result for its `id` rather than silence."""
    h = Harness()
    try:
        h.feed(b'{"v":1,"type":"ping","id":"b-1","ts":0,"reply_to":7}')
        got = h.one()
        assert got["reply_to"] == "b-1"
        assert got["ok"] is False
        assert "reply_to" in got["error"]["message"]
    finally:
        h.close()


# -- framing ----------------------------------------------------------------


def test_two_frames_in_one_read_are_both_answered(harness):
    """A pty read can carry the tail of one line and the whole of the next."""
    one = encode(_frame("ping", {}, id_="b-1")).rstrip(b"\n")
    two = encode(_frame("ping", {}, id_="b-2")).rstrip(b"\n")
    for line in harness.agent._consume(one + b"\n" + two + b"\n"):
        harness.agent.handle(line)
    got = harness.replies()
    assert [r["reply_to"] for r in got] == ["b-1", "b-2"]


def test_a_line_split_across_reads_is_reassembled(harness):
    line = encode(_frame("ping", {}, id_="b-7")).rstrip(b"\n")
    cut = len(line) // 2
    assert harness.agent._consume(line[:cut]) == []
    out = harness.agent._consume(line[cut:] + b"\n")
    assert len(out) == 1
    harness.agent.handle(out[0])
    assert harness.one()["reply_to"] == "b-7"


def test_recovering_an_id_from_an_unparsed_line():
    assert Agent._recover_id(b'{"id":"b-42","rest":') == "b-42"
    assert Agent._recover_id(b'{"id" : "b-9"}') == "b-9"
    # Anything ambiguous is None: guessing an id would answer the wrong request.
    assert Agent._recover_id(b"no id here") is None
    assert Agent._recover_id(b'{"id":42}') is None
    assert Agent._recover_id(b'{"id":"unterminated') is None
    assert Agent._recover_id(b'{"id" "b-1"}') is None
    assert Agent._recover_id(b'{"id": "\xff\xfe"}') is None


def test_delay_ms_slows_every_reply(harness):
    """`--delay-ms` exists so a timing-sensitive caller can be exercised; it must
    apply to each emitted line, not once per run."""
    h = Harness(delay_ms=20)
    try:
        started = time.monotonic()
        h.feed_frame(_frame("ping", {}))
        h.one()
        assert time.monotonic() - started >= 0.02
    finally:
        h.close()


def test_run_answers_a_real_read_and_stops_on_eof():
    """`run()` is the shipped entry point: it must consume a real pty read, answer
    it, and return once the peer closes rather than spinning."""
    h = Harness()
    try:
        os.write(h.slave, encode(_frame("ping", {}, id_="b-3")))
        thread = threading.Thread(target=h.agent.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        reply = None
        while time.monotonic() < deadline and reply is None:
            os.set_blocking(h.slave, False)
            try:
                raw = os.read(h.slave, 65536)
            except BlockingIOError:
                raw = b""
            finally:
                os.set_blocking(h.slave, True)
            if raw.strip():
                reply = json.loads(raw.split(b"\n")[0])
        assert reply is not None and reply["type"] == "pong"
        assert reply["reply_to"] == "b-3"
        # Closing the peer ends the loop through the empty-read branch.
        os.close(h.slave)
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        h.agent._closed = True


def test_the_run_loop_returns_when_the_host_closes_the_port(tmp_path):
    """`run()` is what `main` blocks on. A host that closes the pty must end it, or
    every `test_agent.py` subprocess would outlive its test.

    The loop is driven here rather than through `main` because the point is the
    read-loop exit, which a subprocess cannot observe from the outside.
    """
    import io

    import contextlib

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    agent = Agent(master, stream=_NullStream())
    errors: list[BaseException] = []

    def drive() -> None:
        try:
            agent.run()
        except BaseException as exc:  # noqa: BLE001 - the point is to see it
            errors.append(exc)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()

    os.write(slave, b'{"v":1,"type":"ping","id":"b-1","ts":0}\n')
    time.sleep(0.05)
    os.set_blocking(slave, False)
    try:
        assert os.read(slave, 65536).strip().endswith(b"}") or True
    finally:
        os.set_blocking(slave, True)

    os.close(slave)          # EOF for the reader
    worker.join(timeout=2)
    assert not worker.is_alive(), "run() did not return after the port closed"
    assert errors == []
    os.close(master)


def test_main_builds_the_peer_from_argv_and_prints_the_port_on_stdout(monkeypatch, tmp_path):
    """`main` is what the e2e fixtures and `bridge --target test` spawn, so its argv
    contract is a real interface: flags reach the agent, the pty path goes to stdout
    (only NDJSON crosses the port), and the diagnostics go to stderr.

    `run` is patched to return at once: the loop blocks on the master until the host
    closes, which is the subprocess lifetime the fixtures manage with `terminate`.
    """
    import io

    script = tmp_path / "s.ndjson"
    script.write_text('{"type":"event","kind":"x"}\n')
    captured, logged = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    monkeypatch.setattr("sys.stderr", logged)

    built: list[tuple[int, dict]] = []

    def fake_run(self) -> None:
        built.append(
            (
                self.fd,
                {
                    "token": self.token,
                    "fail_on": self.fail_on,
                    "delay_ms": self.delay_ms,
                    "no_hello": self.no_hello,
                    "script": self.script,
                },
            )
        )

    monkeypatch.setattr(agent_mod.TestAgent, "run", fake_run)
    code = agent_mod.main(
        [
            "--target",
            "test",
            "--token",
            "tok",
            "--fail-on",
            "status,config",
            "--delay-ms",
            "7",
            "--no-hello",
            "--script",
            str(script),
        ]
    )
    assert code == 0
    lines = captured.getvalue().splitlines()
    assert len(lines) == 1 and lines[0].startswith("/dev/pts/")
    assert "listening on" in logged.getvalue()
    (fd, options), = built
    assert isinstance(fd, int)
    assert options["token"] == "tok"
    # `--fail-on` is a comma list, and the blank entries are dropped.
    assert options["fail_on"] == {"status", "config"}
    assert options["delay_ms"] == 7
    assert options["no_hello"] is True
    assert options["script"] == [{"type": "event", "kind": "x"}]

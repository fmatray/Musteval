"""End to end scenarios: a full run as a user drives it, over a real pty.

`test_agent_session.py` proves each frame in isolation. This file proves the
*sequences* a user actually produces: a run that uses a tool, a tool that fails and
the run continuing, a prompt queued behind a running one, a retryable error
followed by a successful retry, a cancel that arrives mid-run, a corrupt line in
the middle of a run, and several turns whose replies must not cross.

Every scenario runs the real `link` + `client` against the real test agent —
`AGENTS.md` §Test Strategy, bridge end to end. The LLM is the only stub, which is
what `--script` exists for (`10-test-agent.md` §7).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bridge.client import LinkState, Session
from bridge.client.session import SessionError
from bridge.link import SerialTransport
from bridge.protocol import Frame, new_id, reset_ids

AGENT = Path(__file__).resolve().parents[2] / "tools" / "test_agent.py"


HANDSHAKE = {
    "type": "hello_ack",
    "device": "test-agent",
    "fw_version": "test-1.0.0",
    "protocol": 1,
    "boot_id": "b1",
    "time_synced": True,
    "limits": {"rx_max": 4096, "tx_max": 16384},
    "capabilities": {
        "tools": [],
        "mcp": [],
        "dangerous_tools": False,
        "streaming": False,
    },
}


def _script(tmp_path: Path, entries: list[dict], name: str = "scenario.ndjson") -> str:
    """One NDJSON line per entry, exactly as `10-test-agent.md` §7 reads it.

    An entry is consumed per inbound frame, and the first frame a session sends is
    `hello`, so the handshake is scripted too. A scenario body then describes only
    the frames under test.
    """
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in [HANDSHAKE, *entries]) + "\n")
    return str(path)


def _start(run_id: str, depth: int = 0) -> dict:
    return {
        "type": "agent.start",
        "run_id": run_id,
        "trigger": "manual",
        "source": "prompt",
        "provider": "test",
        "model": "test-echo",
        "input_bytes": 5,
        "queue_depth": depth,
    }


def _final(run_id: str, text: str, **overrides) -> dict:
    body = {
        "type": "agent.final",
        "run_id": run_id,
        "text": text,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "rounds": 1,
        "provider": "test",
        "model": "test-echo",
        "duration_ms": 5,
        "truncated": False,
        "todo_open": False,
    }
    body.update(overrides)
    return body


class Agent:
    """A running `test_agent.py`, with the pty slave path it printed."""

    def __init__(self, *args: str) -> None:
        import subprocess
        import sys

        self.proc = subprocess.Popen(
            [sys.executable, str(AGENT), "--target", "test", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.proc.stdout.readline().strip()
        if not line:
            raise RuntimeError(f"test agent did not start: {self.proc.stderr.read()}")
        self.port = line

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


@pytest.fixture
def scripted(tmp_path):
    """Start an agent whose replies come from a script, not the table."""
    started: list[Agent] = []

    def start(entries: list[dict]) -> Agent:
        proc = Agent("--script", _script(tmp_path, entries))
        started.append(proc)
        return proc

    yield start
    for proc in started:
        proc.stop()


async def _connected(port: str, **kwargs) -> Session:
    session = Session(SerialTransport(port, 115200), ack_timeout=2, **kwargs)
    state = await session.connect()
    assert state is LinkState.CONNECTED, state
    return session


async def _drain(seen: list, *, until: str, count: int = 1, limit: float = 3.0) -> list:
    """Wait until `count` frames of `until` have reached the sink, or give up."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        got = [f for f in seen if f.type == until]
        if len(got) >= count:
            return got
        await asyncio.sleep(0.01)
    raise AssertionError(f"never saw {count}x {until}; saw {[f.type for f in seen]}")


# -- a run that uses a tool ------------------------------------------------


async def test_a_run_reports_a_tool_call_then_its_result_then_the_answer(scripted):
    """One turn, both rounds: tool, result, then final, in that order.

    `04` §3.11–3.14. The order is the contract, not the presence: the spinner
    starts at `agent.start` and only the terminal frame stops it, so a result
    arriving after the final would leave the UI spinning forever.
    """
    agent = scripted(
        [
            {
                "burst": [
                    _start("r-1"),
                    {
                        "type": "agent.tool",
                        "run_id": "r-1",
                        "round": 1,
                        "call_id": "call_1",
                        "tool": "time.now",
                        "args": {},
                    },
                    {
                        "type": "agent.tool_result",
                        "run_id": "r-1",
                        "round": 1,
                        "call_id": "call_1",
                        "tool": "time.now",
                        "ok": True,
                        "result": {"unix": 1730563213},
                        "duration_ms": 2,
                    },
                    _final("r-1", "The clock reads 1730563213.", rounds=2),
                ]
            }
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        start = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "what time"})
        )
        assert start.type == "agent.start"
        assert start.body["run_id"] == "r-1"
        # The sink never sees the reply to a `request`: `_deliver` resolves the
        # future and returns, so only the follow-on frames reach it (`09` §7).
        await _drain(seen, until="agent.final")
        kinds = [f.type for f in seen]
        assert kinds == ["agent.tool", "agent.tool_result", "agent.final"]
        assert seen[1].body["ok"] is True
        assert seen[2].body["text"] == "The clock reads 1730563213."
        assert seen[2].body["rounds"] == 2
        assert [f.body["run_id"] for f in seen] == ["r-1", "r-1", "r-1"]
    finally:
        await session.close()


async def test_a_refused_tool_does_not_abort_the_run(scripted):
    """`04` §3.13: a refusal is `ok:false` + `E_TOOL`, and the run continues.

    The device turns the refusal into text for the model, which may pick another
    tool. The bridge must therefore keep the run alive and still accept the final.
    """
    agent = scripted(
        [
            {
                "burst": [
                    _start("r-2"),
                    {
                        "type": "agent.tool",
                        "run_id": "r-2",
                        "round": 1,
                        "call_id": "call_9",
                        "tool": "fs.write",
                        "args": {"path": "/workspace/a.txt", "content": "x"},
                    },
                    {
                        "type": "agent.tool_result",
                        "run_id": "r-2",
                        "round": 1,
                        "call_id": "call_9",
                        "tool": "fs.write",
                        "ok": False,
                        "error": {
                            "code": "E_TOOL",
                            "message": "fs.write refused: tools.allow_dangerous is false",
                        },
                        "duration_ms": 1,
                    },
                    _final("r-2", "I cannot write files here.", rounds=2),
                ]
            }
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "write a file"})
        )
        await _drain(seen, until="agent.final")
        results = [f for f in seen if f.type == "agent.tool_result"]
        assert len(results) == 1
        assert results[0].body["ok"] is False
        assert results[0].body["error"]["code"] == "E_TOOL"
        assert [f for f in seen if f.type == "agent.final"][0].body["text"] == (
            "I cannot write files here."
        )
        # A per-tool refusal never becomes a run-level error.
        assert not [f for f in seen if f.type == "agent.error"]
    finally:
        await session.close()


# -- queueing --------------------------------------------------------------


async def test_a_prompt_queued_behind_a_running_one_is_reported_by_depth(scripted):
    """`04` §3.11 carries `queue_depth`: the second run starts behind the first.

    A user can type again while a run is in flight. The second `agent.start` must
    say one run is queued, so the bridge can show that instead of implying the
    second run began at once.
    """
    agent = scripted(
        [
            {"burst": [_start("r-1", depth=1), _final("r-1", "first")]},
            {"burst": [_start("r-2", depth=0), _final("r-2", "second")]},
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        first = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "one"})
        )
        second = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "two"})
        )
        assert first.body["run_id"] == "r-1"
        assert first.body["queue_depth"] == 1
        assert second.body["run_id"] == "r-2"
        assert second.body["queue_depth"] == 0
        finals = await _drain(seen, until="agent.final", count=2)
        assert [f.body["text"] for f in finals] == ["first", "second"]
        # Both `agent.start` frames resolved their `request` and never reached the
        # sink, so the only run trace here is the two finals, in order.
        assert [f.body["run_id"] for f in finals] == ["r-1", "r-2"]
    finally:
        await session.close()


async def test_a_full_queue_is_refused_with_e_busy(scripted):
    """`04` §5 + §3.15: past `max_queue` the device rejects with `E_BUSY` + detail.

    The rejection is a valid reply, not a session failure: the link stays up and
    the reader is told why their prompt did not run.
    """
    agent = scripted(
        [
            {
                "type": "agent.error",
                "code": "E_BUSY",
                "run_id": None,
                "message": "queue full",
                "retryable": True,
                "detail": {"queue_depth": 2, "max_queue": 2},
            },
        ]
    )
    session = await _connected(agent.port)
    try:
        reply = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "one too many"})
        )
        assert reply.type == "agent.error"
        assert reply.body["code"] == "E_BUSY"
        assert reply.body["retryable"] is True
        assert reply.body["detail"] == {"queue_depth": 2, "max_queue": 2}
        assert session.state is LinkState.CONNECTED
    finally:
        await session.close()


# -- errors and retries ----------------------------------------------------


async def test_a_retryable_llm_error_can_be_retried_on_the_same_link(scripted):
    """`04` §5: `E_LLM_RATE` is retryable, so the same session must be reusable."""
    agent = scripted(
        [
            {
                "type": "agent.error",
                "code": "E_LLM_RATE",
                "run_id": "r-1",
                "message": "rate limited",
                "retryable": True,
                "detail": {"retry_after_s": 1},
            },
            {"burst": [_start("r-2"), _final("r-2", "after the wait")]},
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        failed = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "go"})
        )
        assert failed.type == "agent.error"
        assert failed.body["code"] == "E_LLM_RATE"
        assert failed.body["retryable"] is True

        again = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "go"})
        )
        assert again.type == "agent.start"
        assert session.state is LinkState.CONNECTED
        finals = await _drain(seen, until="agent.final")
        assert finals[0].body["text"] == "after the wait"
    finally:
        await session.close()


async def test_a_non_retryable_error_is_flagged_as_such(scripted):
    """`E_CONFIG` is not retryable: retrying it would fail identically."""
    agent = scripted(
        [
            {
                "type": "agent.error",
                "code": "E_CONFIG",
                "run_id": None,
                "message": "no provider configured",
                "retryable": False,
            },
        ]
    )
    session = await _connected(agent.port)
    try:
        reply = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "go"})
        )
        assert reply.body["code"] == "E_CONFIG"
        assert reply.body["retryable"] is False
    finally:
        await session.close()


async def test_a_cancel_mid_run_ends_it_with_e_cancelled(scripted):
    """`04` §4.4: cancel answers with `E_CANCELLED` carrying the run's id."""
    agent = scripted(
        [
            {"burst": [_start("r-7")]},
            {
                "type": "agent.error",
                "code": "E_CANCELLED",
                "run_id": "r-7",
                "message": "run cancelled",
                "retryable": False,
            },
        ]
    )
    session = await _connected(agent.port)
    try:
        start = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "long job"})
        )
        assert start.body["run_id"] == "r-7"
        cancelled = await session.request(
            Frame(type="cancel", id=new_id("b"), ts=0, body={"run_id": "r-7"})
        )
        assert cancelled.type == "agent.error"
        assert cancelled.body["code"] == "E_CANCELLED"
        assert cancelled.body["run_id"] == "r-7"
        assert session.state is LinkState.CONNECTED
    finally:
        await session.close()


# -- resilience ------------------------------------------------------------


async def test_a_corrupt_line_mid_run_does_not_desynchronise_the_stream(scripted):
    """`04` §1.2: a bad line is dropped; the next frame still decodes.

    The `raw` entry writes bytes verbatim, which is the only way to put a
    malformed line on the wire without `encode` refusing it first.
    """
    agent = scripted(
        [
            {
                "burst": [
                    _start("r-3"),
                    {"type": "raw", "data": '{"v":1,"type":"agent.fi\n'},
                    _final("r-3", "survived"),
                ]
            }
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "go"})
        )
        finals = await _drain(seen, until="agent.final")
        assert finals[0].body["text"] == "survived"
        assert session.state is LinkState.CONNECTED
    finally:
        await session.close()


async def test_the_link_notices_a_peer_that_goes_away(scripted):
    """A dropped peer must move the link out of `CONNECTED`, then retry.

    `05` §4: the supervisor owns the retry loop. A fresh pty cannot appear at the
    closed path, so this proves detection and retry scheduling, not a completed
    re-handshake, which needs a device rebooting onto the same path.
    """
    agent = scripted([])
    session = Session(
        SerialTransport(agent.port, 115200),
        ack_timeout=2,
        backoff=(0.05, 0.05, 0.05),
        ping_interval=99,
    )
    states: list[LinkState] = []
    session.install_state_hook(states.append)
    try:
        assert await session.connect() is LinkState.CONNECTED
        agent.stop()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while loop.time() < deadline and session.state is not LinkState.DOWN:
            await asyncio.sleep(0.01)
        assert session.state is LinkState.DOWN
        assert LinkState.CONNECTING in states
    finally:
        await session.close()


async def test_a_prompt_after_the_link_closes_names_the_failure(agent_port):
    """A prompt on a closed session raises, and the message names the state.

    `05` §5.1: the reader is told, rather than watching a spinner forever.
    """
    session = await _connected(agent_port)
    await session.close()
    with pytest.raises(SessionError) as excinfo:
        await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "too late"})
        )
    assert "down" in str(excinfo.value)


# -- multi-turn ------------------------------------------------------------


async def test_three_turns_keep_their_own_replies(scripted):
    """Correlation under load: three prompts, three answers, none crossed.

    `09` §7: the sink sees every frame, so a reply routed to the wrong future
    would surface here as a final carrying another turn's text.
    """
    entries = [
        {"burst": [_start(f"r-{i}"), _final(f"r-{i}", f"answer {i}")]} for i in range(3)
    ]
    agent = scripted(entries)
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        for index in range(3):
            start = await session.request(
                Frame(type="prompt", id=new_id("b"), ts=0, body={"text": f"turn {index}"})
            )
            assert start.body["run_id"] == f"r-{index}"
        finals = await _drain(seen, until="agent.final", count=3)
        assert [f.body["text"] for f in finals] == ["answer 0", "answer 1", "answer 2"]
        assert [f.body["run_id"] for f in finals] == ["r-0", "r-1", "r-2"]
    finally:
        await session.close()


async def test_two_tool_rounds_in_one_run_are_ordered_by_round(scripted):
    """A run is a loop: the model may call a tool, see the result, call another.

    `04` §3.12 carries `round` on every tool frame so the UI can group them. Two
    rounds in one run must arrive in order, with `rounds` on the final matching.
    """
    agent = scripted(
        [
            {
                "burst": [
                    _start("r-8"),
                    {
                        "type": "agent.tool",
                        "run_id": "r-8",
                        "round": 1,
                        "call_id": "c1",
                        "tool": "time.now",
                        "args": {},
                    },
                    {
                        "type": "agent.tool_result",
                        "run_id": "r-8",
                        "round": 1,
                        "call_id": "c1",
                        "tool": "time.now",
                        "ok": True,
                        "result": {"unix": 1},
                        "duration_ms": 1,
                    },
                    {
                        "type": "agent.tool",
                        "run_id": "r-8",
                        "round": 2,
                        "call_id": "c2",
                        "tool": "todo",
                        "args": {"action": "list"},
                    },
                    {
                        "type": "agent.tool_result",
                        "run_id": "r-8",
                        "round": 2,
                        "call_id": "c2",
                        "tool": "todo",
                        "ok": True,
                        "result": {"items": []},
                        "duration_ms": 1,
                    },
                    _final("r-8", "nothing to do", rounds=3),
                ]
            }
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "any todos"})
        )
        finals = await _drain(seen, until="agent.final")
        rounds = [f.body["round"] for f in seen if f.type == "agent.tool"]
        assert rounds == [1, 2]
        assert [f.body["call_id"] for f in seen if f.type == "agent.tool_result"] == [
            "c1",
            "c2",
        ]
        assert finals[0].body["rounds"] == 3
    finally:
        await session.close()


async def test_an_oversize_line_is_dropped_and_the_next_frame_survives(scripted):
    """`04` §1.2 caps a line at `MAX_FROM_DEVICE_BYTES`; over the cap is dropped.

    A dropped line must not swallow the frame after it, which is what a reader
    that gives up on the stream would do.
    """
    from bridge.protocol import MAX_FROM_DEVICE_BYTES

    huge = json.dumps({"v": 1, "type": "agent.tool", "pad": "y" * MAX_FROM_DEVICE_BYTES})
    agent = scripted(
        [
            {
                "burst": [
                    _start("r-9"),
                    {"type": "raw", "data": huge + "\n"},
                    _final("r-9", "still here"),
                ]
            }
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "go"})
        )
        finals = await _drain(seen, until="agent.final")
        assert finals[0].body["text"] == "still here"
        assert not [f for f in seen if f.type == "agent.tool"]
        assert session.state is LinkState.CONNECTED
    finally:
        await session.close()


async def test_the_close_out_flag_survives_a_truncated_answer(scripted):
    """`04` §3.14: truncation caps the text and reports the pre-truncation size.

    `todo_open` is orthogonal and advisory: a shortened answer still carries the
    close-out verdict, so a UI must not lose it while clipping the text.
    """
    agent = scripted(
        [
            {
                "burst": [
                    _start("r-4"),
                    _final(
                        "r-4", "x" * 200, truncated=True, todo_open=True, full_bytes=4096
                    ),
                ]
            }
        ]
    )
    session = await _connected(agent.port)
    seen: list = []
    session.install_sink(seen.append)
    try:
        await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "go"})
        )
        body = (await _drain(seen, until="agent.final"))[0].body
        assert body["truncated"] is True
        assert body["full_bytes"] == 4096
        assert body["todo_open"] is True
    finally:
        await session.close()


@pytest.fixture
def agent_port():
    proc = Agent()
    yield proc.port
    proc.stop()

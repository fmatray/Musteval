"""End to end through the real CLI: `__main__.main` against a pty-hosted agent.

`--send` is the headless path, so this exercises argv parsing, the `--target`
guard, `link`, `client` and `commands` without a terminal. Every case here either
takes `--send` or is refused before the UI starts; a bare invocation would open
the Textual app and never return.
"""

from __future__ import annotations

import json

from argparse import Namespace

import pytest

from bridge.__main__ import _headless, main
from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode, reset_ids

from .test_agent_session import Agent


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


@pytest.fixture
def agent():
    proc = Agent()
    yield proc
    proc.stop()


def test_send_status_prints_the_status_object(agent, capsys):
    code = main(["--target", "test", "--port", agent.port, "--send", "/status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "'boot_id': '00000001'" in out
    assert "'streaming'" not in out


def test_send_prompt_prints_the_agent_final(agent, capsys):
    code = main(["--target", "test", "--port", agent.port, "--send", "hello there"])
    out = capsys.readouterr().out
    assert code == 0
    assert "echo" in out.lower()


def test_test_target_requires_a_port(capsys):
    assert main(["--target", "test"]) == 2
    assert "requires --port" in capsys.readouterr().err


def test_usb_target_refuses_a_pty(agent, capsys):
    assert main(["--port", agent.port]) == 2
    assert "pseudo-terminal" in capsys.readouterr().err


def test_wrong_token_is_refused_before_any_command(capsys):
    strict = Agent("--token", "sekrit")
    try:
        code = main(["--target", "test", "--port", strict.port,
                     "--token", "wrong", "--send", "/status"])
    finally:
        strict.stop()
    captured = capsys.readouterr()
    assert code == 1
    assert "not connected" in captured.err
    assert "sekrit" not in captured.out and "sekrit" not in captured.err


def test_json_switch_writes_redacted_frames(agent, tmp_path):
    log = tmp_path / "frames.ndjson"
    code = main(
        [
            "--target", "test", "--port", agent.port,
            "--json", "--log-file", str(log),
            "--send", "/status",
        ]
    )
    assert code == 0
    text = log.read_text()
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert lines and all("v" in frame and "type" in frame for frame in lines)


async def test_send_prompt_that_is_never_answered_exits_one(capsys):
    """A linked peer that goes quiet on the prompt fails as a line, not a traceback.

    Built on `FakeTransport` because the point is the timeout, not the wire: the
    agent double would have to stall the reply without stalling `hello`.
    """
    ack = {
        "device": "test-agent", "fw_version": "test-1.0.0", "protocol": 1,
        "boot_id": "b1", "time_synced": True,
        "limits": {"rx_max": 4096, "tx_max": 16384},
        "capabilities": {"tools": [], "mcp": [], "dangerous_tools": False,
                         "streaming": False},
    }

    def answers_hello_only(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "prompt":
            return []
        if frame["type"] == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0,
                                 body=ack, reply_to=frame["id"]))]
        return [encode(Frame(type="pong", id="d-2", ts=0, body={},
                             reply_to=frame["id"]))]

    session = Session(FakeTransport(on_write=answers_hello_only),
                      ack_timeout=0.05, ping_interval=99)
    await session.connect()
    args = Namespace(send=["hello there"], target="test")
    try:
        code = await _headless(session, args)
    finally:
        await session.close()
    err = capsys.readouterr().err
    assert code == 1
    assert "no reply before the timeout" in err

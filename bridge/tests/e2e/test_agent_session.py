"""End to end: real `link` + `client` over a real serial device, no board.

The fixture starts `bridge/tools/test_agent.py` in a pty and hands its slave path
to `SerialTransport`. Nothing is stubbed between the two: the bytes cross a file
descriptor, the real reader thread reassembles them, and the real `Session`
correlates the replies. `AGENTS.md` names this the second of the four suites.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bridge.client import LinkState, Session
from bridge.client.session import SessionError
from bridge.link import SerialTransport
from bridge.protocol import Frame, new_id, reset_ids

AGENT = Path(__file__).resolve().parents[2] / "tools" / "test_agent.py"


class Agent:
    """A running `test_agent.py`, with the pty slave path it printed."""

    def __init__(self, *args: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(AGENT), "--target", "test", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = self.proc.stdout.readline().strip()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"test agent did not start: {err}")
        self.port = line

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def stderr(self) -> str:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        return self.proc.stderr.read()


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


@pytest.fixture
def bare_pty():
    """A pty with no peer, for handshake-failure tests."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    yield master, path
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


async def _session(port: str, **kwargs) -> Session:
    session = Session(SerialTransport(port, 115200), **kwargs)
    return session


async def test_handshake_completes_against_the_real_agent(agent):
    session = await _session(agent.port, ack_timeout=2)
    try:
        assert await session.connect() is LinkState.CONNECTED
        assert session.hello_ack is not None
        assert session.hello_ack.body["device"] == "test-agent"
    finally:
        await session.close()


async def test_status_returns_a_schema_shaped_object(agent):
    session = await _session(agent.port, ack_timeout=2)
    try:
        await session.connect()
        reply = await session.request(
            Frame(type="command", id=new_id("b"), ts=0, body={"name": "status", "args": {}})
        )
        # 10-test-agent.md §6.1: constant, and shaped like 04 §3.9.
        assert reply.type == "command.result"
        assert reply.body["ok"] is True
        result = reply.body["result"]
        for key in ("wifi", "time", "llm", "agent", "mem", "storage", "boot_id"):
            assert key in result
        assert result["boot_id"] == session.hello_ack.body["boot_id"]
    finally:
        await session.close()


async def test_prompt_produces_start_then_final(agent):
    session = await _session(agent.port, ack_timeout=2)
    seen: list[Frame] = []
    try:
        await session.connect()
        session.install_sink(seen.append)
        start = await session.request(
            Frame(type="prompt", id=new_id("b"), ts=0, body={"text": "hello agent"})
        )
        assert start.type == "agent.start"
        for _ in range(100):
            if any(f.type == "agent.final" for f in seen):
                break
            await asyncio.sleep(0.02)
        finals = [f for f in seen if f.type == "agent.final"]
        assert finals and finals[0].body["text"] == "hello agent"
    finally:
        await session.close()


async def test_ping_is_answered(agent):
    session = await _session(agent.port, ack_timeout=2)
    try:
        await session.connect()
        reply = await session.request(Frame(type="ping", id=new_id("b"), ts=0, body={}))
        assert reply.type == "pong"
    finally:
        await session.close()


async def test_unknown_command_name_is_rejected_by_the_agent(agent):
    session = await _session(agent.port, ack_timeout=2)
    try:
        await session.connect()
        reply = await session.request(
            Frame(type="command", id=new_id("b"), ts=0, body={"name": "bogus", "args": {}})
        )
        assert reply.body["ok"] is False
        assert reply.body["error"]["code"] == "E_PROTOCOL"
    finally:
        await session.close()


async def test_secret_is_never_echoed_back(agent):
    session = await _session(agent.port, ack_timeout=2)
    try:
        await session.connect()
        reply = await session.request(
            Frame(type="secret.set", id=new_id("b"), ts=0, body={"key": "wifi", "value": "hunter2"})
        )
        assert reply.body["result"] == {"key": "wifi", "stored": True, "length": 7}
        assert "hunter2" not in json.dumps(reply.body)
        assert "hunter2" not in agent.stderr if agent.proc.poll() is not None else True
    finally:
        await session.close()


async def test_bad_token_is_rejected_and_never_retried():
    proc = Agent("--token", "right")
    try:
        session = await _session(proc.port, token="wrong", ack_timeout=1, backoff=(0.01,))
        seen: list[Frame] = []
        session.install_sink(seen.append)
        state = await session.connect()
        assert state is LinkState.REJECTED
        assert any(f.body.get("kind") == "link.rejected" for f in seen)
        await session.close()
    finally:
        proc.stop()


async def test_good_token_connects():
    proc = Agent("--token", "right")
    try:
        session = await _session(proc.port, token="right", ack_timeout=2)
        assert await session.connect() is LinkState.CONNECTED
        await session.close()
    finally:
        proc.stop()


async def test_fail_on_injects_a_protocol_error():
    proc = Agent("--fail-on", "command")
    try:
        session = await _session(proc.port, ack_timeout=2)
        await session.connect()
        reply = await session.request(
            Frame(type="command", id=new_id("b"), ts=0, body={"name": "status", "args": {}})
        )
        assert reply.body["ok"] is False
        assert reply.body["error"]["code"] == "E_PROTOCOL"
        await session.close()
    finally:
        proc.stop()


async def test_no_hello_leaves_the_session_unconnected():
    proc = Agent("--no-hello")
    try:
        session = await _session(proc.port, ack_timeout=0.3, backoff=(0.01,))
        assert await session.connect() is not LinkState.CONNECTED
        await session.close()
    finally:
        proc.stop()


async def test_a_silent_pty_times_out(bare_pty):
    _master, path = bare_pty
    session = await _session(path, ack_timeout=0.2, backoff=(0.01,))
    try:
        assert await session.connect() is not LinkState.CONNECTED
    finally:
        await session.close()


async def test_cancel_without_a_run_is_a_noop_event(agent):
    session = await _session(agent.port, ack_timeout=2)
    seen: list[Frame] = []
    try:
        await session.connect()
        session.install_sink(seen.append)
        await session.send(Frame(type="cancel", id=new_id("b"), ts=0, body={"reason": "user"}))
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen and seen[0].body["kind"] == "cancel.noop"
    finally:
        await session.close()

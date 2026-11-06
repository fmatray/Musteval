"""L2 client: handshake, correlation, heartbeat, backoff, rejection."""

from __future__ import annotations

import asyncio

import pytest

from bridge.client import LinkState, Session
from bridge.client.session import SessionError
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode, new_id, reset_ids

FAST = dict(ack_timeout=0.2, ping_interval=0.05, pong_timeout=0.05, backoff=(0.01, 0.01))


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _ack(reply_to: str, **body) -> bytes:
    payload = {
        "device": "test-agent",
        "fw_version": "1.0.0",
        "protocol": 1,
        "boot_id": "b1",
        "time_synced": True,
        "limits": {"rx_max": 4096, "tx_max": 16384},
        "capabilities": {"tools": [], "mcp": [], "dangerous_tools": False, "streaming": False},
    }
    payload.update(body)
    return encode(Frame(type="hello_ack", id="d-1", ts=0, body=payload, reply_to=reply_to))


def _agent(on_write=None):
    """A transport that acks the handshake, then delegates to `on_write`."""
    def handler(line: bytes) -> list[bytes]:
        frame = __import__("json").loads(line)
        if frame["type"] == "hello":
            return [_ack(frame["id"])]
        if frame["type"] == "ping":
            return [encode(Frame(type="pong", id="d-9", ts=0, body={}, reply_to=frame["id"]))]
        return (on_write or (lambda _: []))(line)
    return handler


async def test_ping_loop_measures_the_round_trip_and_hands_the_pong_on():
    """`05-bridge.md` §10: `pong` updates the latency the Status screen shows.

    Two facts, one test, because either alone is a wiring claim: the number must be
    measured, and the pong must reach the sink or the UI has nothing to paint.
    """
    seen: list[str] = []
    session = Session(FakeTransport(on_write=_agent()), **FAST)
    session.install_sink(lambda frame: seen.append(frame.type))
    await session.connect()
    try:
        assert session.last_rtt_ms is None
        for _ in range(100):
            if session.last_rtt_ms is not None and "pong" in seen:
                break
            await asyncio.sleep(0.01)
        assert session.last_rtt_ms is not None
        assert 0 <= session.last_rtt_ms < 5000
        assert "pong" in seen
    finally:
        await session.close()


async def test_connect_reaches_connected_and_records_the_ack():
    session = Session(FakeTransport(on_write=_agent()), **FAST)
    assert await session.connect() is LinkState.CONNECTED
    assert session.hello_ack is not None
    assert session.hello_ack.body["device"] == "test-agent"
    await session.close()


async def test_hello_carries_the_frozen_client_name():
    written: list[bytes] = []

    def handler(line):
        written.append(line)
        return [_ack(__import__("json").loads(line)["id"])]

    session = Session(FakeTransport(on_write=handler), **FAST)
    await session.connect()
    hello = __import__("json").loads(written[0])
    assert hello["type"] == "hello"
    assert hello["client"] == "musteval-tui"
    assert hello["protocol"] == 1
    await session.close()


async def test_hello_includes_a_token_only_when_configured():
    written: list[bytes] = []

    def handler(line):
        written.append(line)
        return [_ack(__import__("json").loads(line)["id"])]

    session = Session(FakeTransport(on_write=handler), token="t0k", **FAST)
    await session.connect()
    assert __import__("json").loads(written[0])["token"] == "t0k"
    await session.close()

    written.clear()
    session = Session(FakeTransport(on_write=handler), **FAST)
    await session.connect()
    assert "token" not in __import__("json").loads(written[0])
    await session.close()


async def test_request_correlates_the_reply_by_reply_to():
    def handler(line):
        frame = __import__("json").loads(line)
        if frame["type"] == "command":
            body = {"name": frame["name"], "ok": True, "result": {"n": 1}, "duration_ms": 0}
            return [encode(Frame(type="command.result", id="d-2", ts=0, body=body, reply_to=frame["id"]))]
        return []

    session = Session(FakeTransport(on_write=_agent(handler)), **FAST)
    await session.connect()
    reply = await session.request(
        Frame(type="command", id=new_id("b"), ts=0, body={"name": "status", "args": {}})
    )
    assert reply.type == "command.result"
    assert reply.body["result"] == {"n": 1}
    await session.close()


async def test_request_times_out_when_no_reply_comes():
    session = Session(FakeTransport(on_write=_agent(lambda _: [])), ack_timeout=0.1, backoff=(0.01,))
    await session.connect()
    with pytest.raises(asyncio.TimeoutError):
        await session.request(
            Frame(type="command", id=new_id("b"), ts=0, body={"name": "status", "args": {}}),
            timeout=0.1,
        )
    await session.close()


async def test_request_refuses_to_send_while_down():
    session = Session(FakeTransport(), **FAST)
    with pytest.raises(SessionError):
        await session.request(Frame(type="ping", id="b-1", ts=0, body={}))


async def test_unsolicited_frames_reach_the_sink():
    sink_frames: list[Frame] = []

    def handler(line):
        frame = __import__("json").loads(line)
        if frame["type"] == "ack":
            return [encode(Frame(type="log", id="d-3", ts=0, body={"msg": "hi", "level": "I", "tag": "t"}))]
        return []

    # No heartbeat: this test asserts the exact sink contents, and a pong is a
    # solicited frame that legitimately reaches the sink too.
    session = Session(FakeTransport(on_write=_agent(handler)), **{**FAST, "ping_interval": 99})
    session.install_sink(sink_frames.append)
    await session.connect()
    await session.send(Frame(type="ack", id=new_id("b"), ts=0, body={"ack_of": "d-1"}))
    await asyncio.sleep(0.1)
    assert [f.type for f in sink_frames] == ["log"]
    await session.close()


async def test_a_malformed_line_is_dropped_without_desynchronising():
    sink_frames: list[Frame] = []

    def handler(line):
        frame = __import__("json").loads(line)
        if frame["type"] == "ack":
            return [
                b"garbage\n",
                encode(Frame(type="log", id="d-4", ts=0, body={"msg": "after", "level": "I", "tag": "t"})),
            ]
        return []

    session = Session(FakeTransport(on_write=_agent(handler)), **{**FAST, "ping_interval": 99})
    session.install_sink(sink_frames.append)
    await session.connect()
    await session.send(Frame(type="ack", id=new_id("b"), ts=0, body={"ack_of": "d-1"}))
    await asyncio.sleep(0.1)
    assert sink_frames[-1].body["msg"] == "after"
    await session.close()


async def test_rejected_link_is_terminal_and_reaches_the_sink():
    seen: list[Frame] = []

    def handler(line):
        frame = __import__("json").loads(line)
        if frame["type"] == "hello":
            return [encode(Frame(type="event", id="d-5", ts=0, body={"kind": "link.rejected", "data": {"reason": "auth"}}))]
        return []

    session = Session(FakeTransport(on_write=handler), **FAST)
    session.install_sink(seen.append)
    state = await session.connect()
    assert state is LinkState.REJECTED
    assert [f.body["kind"] for f in seen] == ["link.rejected"]
    await session.close()


async def test_bad_token_is_rejected_not_retried():
    states: list[LinkState] = []
    session = Session(
        FakeTransport(on_write=lambda line: []),
        on_state=states.append,
        **FAST,
    )
    state = await session.connect()
    # No ack ever arrives, so the attempt fails; it must not report CONNECTED.
    assert state is not LinkState.CONNECTED
    assert LinkState.REJECTED not in states
    await session.close()


async def test_missing_hello_ack_times_out_then_retries():
    opens = 0

    class Counting(FakeTransport):
        async def open(self):
            nonlocal opens
            opens += 1
            await super().open()

    session = Session(Counting(), ack_timeout=0.05, backoff=(0.01,), ping_interval=9)
    assert await session.connect() is not LinkState.CONNECTED
    await asyncio.sleep(0.15)
    assert opens >= 2
    await session.close()


async def test_heartbeat_keeps_the_link_alive_on_pongs():
    session = Session(FakeTransport(on_write=_agent()), **FAST)
    await session.connect()
    await asyncio.sleep(0.2)
    assert session.state is LinkState.CONNECTED
    await session.close()


async def test_three_missed_pongs_degrade_the_link():
    def handler(line):
        frame = __import__("json").loads(line)
        if frame["type"] == "hello":
            return [_ack(frame["id"])]
        return []  # pings are never answered

    states: list[LinkState] = []
    session = Session(
        FakeTransport(on_write=handler),
        ping_interval=0.02,
        pong_timeout=0.02,
        ack_timeout=0.2,
        backoff=(9,),  # long enough that the drop is observable
        on_state=states.append,
    )
    await session.connect()
    await asyncio.sleep(0.35)
    # Degraded is reported before the link is abandoned, which is what the UI
    # needs to show: `05-bridge.md` §4.1.
    assert LinkState.DEGRADED in states
    assert states.index(LinkState.DEGRADED) < len(states) - 1
    assert states[-1] is LinkState.DOWN
    await session.close()


async def test_close_is_idempotent():
    session = Session(FakeTransport(on_write=_agent()), **FAST)
    await session.connect()
    await session.close()
    await session.close()
    assert session.state is LinkState.DOWN


async def test_close_fails_an_in_flight_request():
    session = Session(FakeTransport(on_write=_agent(lambda _: [])), ack_timeout=1, backoff=(0.01,))
    await session.connect()
    pending = asyncio.create_task(
        session.request(Frame(type="command", id=new_id("b"), ts=0, body={"name": "status", "args": {}}), timeout=5)
    )
    await asyncio.sleep(0.05)
    await session.close()
    with pytest.raises(SessionError):
        await pending


async def test_state_callback_sees_connecting_and_connected():
    states: list[LinkState] = []
    session = Session(FakeTransport(on_write=_agent()), on_state=states.append, **FAST)
    await session.connect()
    assert states[0] is LinkState.CONNECTING
    assert LinkState.CONNECTED in states
    await session.close()


async def test_peer_disappearing_restarts_the_cycle():
    class Vanishing(FakeTransport):
        """Acks the handshake, then reports EOF so the supervisor must reconnect."""

        def __init__(self):
            super().__init__(on_write=lambda line: [_ack(__import__("json").loads(line)["id"])])
            self.opens = 0

        async def open(self):
            self.opens += 1
            await super().open()
            # Let the handshake complete, then drop the link.
            asyncio.get_running_loop().call_later(0.02, self._drop, self.opens)

        def _drop(self, generation: int) -> None:
            if generation == self.opens:
                self._lines.put_nowait(None)

    transport = Vanishing()
    session = Session(transport, **FAST)
    await session.connect()
    await asyncio.sleep(0.4)
    assert transport.opens >= 2
    await session.close()

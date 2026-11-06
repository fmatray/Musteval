"""L2: request/reply correlation, heartbeat, reconnect backoff, link state.

Framework-agnostic by construction: it imports `protocol` and `link` and nothing
else (`09-bridge-architecture.md` §2). It moves frames, it does not author them,
which is why it never imports `commands`.
"""

from __future__ import annotations

import asyncio
import enum
import time
from typing import Any, Callable, Sequence

from ..link import Transport
from ..link.transport import TransportError
from ..protocol import (
    BRIDGE_TO_DEVICE,
    DEVICE_TO_BRIDGE,
    Frame,
    ProtocolError,
    decode_line,
    encode,
    new_id,
)

#: Types whose arrival answers a pending request (`04-protocol.md` §3).
_REPLY_TYPES = {"hello_ack", "pong", "command.result", "agent.start", "agent.error"}


class LinkState(enum.Enum):
    DOWN = "down"
    CONNECTING = "connecting"
    WAIT_ACK = "wait_ack"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class SessionError(Exception):
    """The session could not carry out the call."""


class Session:
    """One link to a device.

    Timings are constructor arguments so a test controls them by sequence rather
    than by sleeping for the production values (`09` §5.1 rejects a `Clock` port
    for exactly this reason).
    """

    def __init__(
        self,
        transport: Transport,
        *,
        client_version: str = "1.0.0",
        token: str | None = None,
        ack_timeout: float = 5.0,
        ping_interval: float = 10.0,
        pong_timeout: float = 5.0,
        backoff: Sequence[float] = (1, 2, 4, 8, 15),
        on_state: Callable[[LinkState], None] | None = None,
    ) -> None:
        self._transport = transport
        self._client_version = client_version
        self._token = token
        self._ack_timeout = ack_timeout
        self._ping_interval = ping_interval
        self._pong_timeout = pong_timeout
        self._backoff = tuple(backoff)
        self._on_state = on_state

        self._state = LinkState.DOWN
        self._sink: Callable[[Frame], None] | None = None
        self._pending: dict[str, asyncio.Future[Frame]] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._stopping = False
        self._missed_pongs = 0
        #: Last measured `ping`→`pong` round trip in milliseconds, or `None` before
        #: the first answer. `05-bridge.md` §10 says `pong` updates the latency the
        #: Status screen shows, and §7 maps `/ping` to "tests link latency", so the
        #: number has to be measured somewhere: it is measured here, where the
        #: request and its reply are both in hand.
        self.last_rtt_ms: float | None = None
        self._hello_ack: Frame | None = None
        self._first_attempt: asyncio.Event = asyncio.Event()

    # -- public surface -------------------------------------------------

    @property
    def state(self) -> LinkState:
        return self._state

    @property
    def hello_ack(self) -> Frame | None:
        """The negotiated handshake, once `CONNECTED`. Read-only."""
        return self._hello_ack

    def install_sink(self, cb: Callable[[Frame], None]) -> None:
        """Install the frame sink. One consumer: `ui` (`09` §5)."""
        self._sink = cb

    def install_state_hook(self, cb: Callable[[LinkState], None]) -> None:
        """Install the state listener. One consumer: `ui` (`09` §5).

        Separate from the constructor's `on_state`: `__main__` builds the
        `Session` before the app exists, so that argument cannot reach it.
        """
        self._on_state = cb

    async def connect(self) -> LinkState:
        """Start the session and await the outcome of the first attempt."""
        if self._supervisor is not None:
            return self._state
        self._stopping = False
        self._first_attempt.clear()
        self._supervisor = asyncio.create_task(self._supervise())
        await self._first_attempt.wait()
        return self._state

    async def close(self) -> None:
        """Stop the session and release the port. Idempotent."""
        self._stopping = True
        for task in (self._heartbeat, self._reader):
            if task is not None:
                task.cancel()
        for task in (self._heartbeat, self._reader):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._heartbeat = self._reader = None
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor = None
        await self._transport.close()
        self._fail_pending(SessionError("session closed"))
        self._hello_ack = None
        self._set_state(LinkState.DOWN)

    async def request(self, frame: Frame, *, timeout: float | None = None) -> Frame:
        """Send `frame` and return the frame that answers it.

        A protocol-level rejection (e.g. `agent.error` with `E_BUSY`) is a valid
        reply and is returned rather than raised: the caller decides how to show
        it, and the session stays `CONNECTED` (`09` §7).
        """
        if self._state in (LinkState.DOWN, LinkState.REJECTED):
            raise SessionError(f"cannot send while {self._state.value}")
        frame_id = frame.id or new_id("b")
        outgoing = Frame(
            type=frame.type,
            id=frame_id,
            ts=frame.ts,
            body=frame.body,
            reply_to=frame.reply_to,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Frame] = loop.create_future()
        self._pending[frame_id] = future
        try:
            await self._send(outgoing)
            return await asyncio.wait_for(future, timeout=timeout or self._ack_timeout * 6)
        finally:
            self._pending.pop(frame_id, None)

    async def send(self, frame: Frame) -> None:
        """Fire-and-forget: `ack`, `cancel` and anything with no dedicated reply."""
        await self._send(frame)

    # -- internals ------------------------------------------------------

    async def _send(self, frame: Frame) -> None:
        try:
            await self._transport.write_line(encode(frame))
        except TransportError as exc:
            # `TransportError` already names the operation ("write failed: ..."),
            # so the message is passed through rather than prefixed again.
            raise SessionError(str(exc)) from exc
        except ProtocolError as exc:
            raise SessionError(f"encode failed: {exc}") from exc

    def _set_state(self, state: LinkState) -> None:
        if state is self._state:
            return
        self._state = state
        if self._on_state is not None:
            self._on_state(state)

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _deliver(self, frame: Frame) -> None:
        if frame.reply_to is not None and frame.type in _REPLY_TYPES:
            future = self._pending.get(frame.reply_to)
            if future is not None and not future.done():
                future.set_result(frame)
                return
        if self._sink is not None:
            self._sink(frame)

    async def _supervise(self) -> None:
        attempt = 0
        first = True
        while not self._stopping:
            ok = await self._attempt()
            if first:
                # `connect()` awaits this. It is set when the first attempt has
                # an outcome, not when the link later drops: on a healthy link
                # the drop ends the session, so setting it afterwards would make
                # `connect()` block for the whole connection.
                first = False
                self._first_attempt.set()
            if self._stopping or self._state is LinkState.REJECTED:
                return
            if ok:
                attempt = 0
                await self._run_until_lost()
                if self._stopping or self._state is LinkState.REJECTED:
                    return
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            self._set_state(LinkState.DOWN)
            await asyncio.sleep(delay)
        self._first_attempt.set()

    async def _attempt(self) -> bool:
        self._set_state(LinkState.CONNECTING)
        try:
            await self._transport.open()
        except (TransportError, OSError):
            return False
        # The reader must be running before the handshake: it is the only thing
        # that resolves the `hello_ack` future, so starting it after the wait
        # would deadlock every attempt into a timeout.
        self._reader = asyncio.create_task(self._read_loop())
        self._set_state(LinkState.WAIT_ACK)
        body: dict[str, Any] = {
            "client": "musteval-tui",
            "client_version": self._client_version,
            "protocol": 1,
        }
        if self._token:
            body["token"] = self._token
        hello = Frame(type="hello", id=new_id("b"), ts=0, body=body)
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[hello.id] = future
        try:
            await self._send(hello)
            reply = await asyncio.wait_for(future, timeout=self._ack_timeout)
        except (asyncio.TimeoutError, SessionError):
            self._pending.pop(hello.id, None)
            await self._stop_reader()
            await self._transport.close()
            return False
        finally:
            self._pending.pop(hello.id, None)
        if self._state is LinkState.REJECTED:
            await self._transport.close()
            return False
        if reply.type != "hello_ack":
            await self._stop_reader()
            await self._transport.close()
            return False
        self._hello_ack = reply
        self._missed_pongs = 0
        self._set_state(LinkState.CONNECTED)
        return True

    async def _stop_reader(self) -> None:
        reader, self._reader = self._reader, None
        if reader is None:
            return
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass

    async def _read_loop(self) -> None:
        while not self._stopping:
            try:
                line = await self._transport.read_line()
            except TransportError:
                line = None
            if line is None:
                return
            try:
                frame = decode_line(line, expect=DEVICE_TO_BRIDGE)
            except ProtocolError:
                # `04` §1.2: a bad line is dropped, the stream is not desynced.
                continue
            if frame.type == "event" and frame.body.get("kind") == "link.rejected":
                self._set_state(LinkState.REJECTED)
                # Terminal: fail the in-flight hello rather than letting it burn
                # the ack timeout, and never schedule a retry (`05-bridge.md` §4.1).
                self._fail_pending(SessionError("link rejected"))
                if self._sink is not None:
                    self._sink(frame)
                return
            if frame.type == "pong":
                self._missed_pongs = 0
                if self._state is LinkState.DEGRADED:
                    self._set_state(LinkState.CONNECTED)
            self._deliver(frame)

    async def _run_until_lost(self) -> None:
        self._heartbeat = asyncio.create_task(self._ping_loop())
        assert self._reader is not None
        done, pending = await asyncio.wait(
            {self._reader, self._heartbeat}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._heartbeat = self._reader = None
        if self._state is LinkState.REJECTED:
            return
        await self._transport.close()

    async def _ping_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._ping_interval)
            ping_id = new_id("b")
            future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
            self._pending[ping_id] = future
            started = time.monotonic()
            try:
                await self._send(Frame(type="ping", id=ping_id, ts=0, body={}))
                await asyncio.wait_for(future, timeout=self._pong_timeout)
                self.last_rtt_ms = (time.monotonic() - started) * 1000
            except (asyncio.TimeoutError, SessionError):
                self._missed_pongs += 1
                if self._missed_pongs >= 3:
                    self._set_state(LinkState.DEGRADED)
                if self._missed_pongs >= 4:
                    # The fourth miss starts backoff rather than a fourth ping.
                    return
            finally:
                self._pending.pop(ping_id, None)

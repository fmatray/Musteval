"""Scripted transport for tests: inbound lines from a list, outbound recorded."""

from __future__ import annotations

import asyncio
from typing import Callable, Iterable

from .transport import LineAssembler, Transport, TransportError
from ..protocol import MAX_FROM_DEVICE_BYTES


class FakeTransport(Transport):
    """No hardware, no pty.

    Scripted chunks go through the same `LineAssembler` the real transport uses,
    so a test can hand it partial reads (`b'{"v":1'` then `b'}\\n'`) and the line
    reassembly is exercised rather than bypassed.

    `on_write` is called with each outbound line and may return chunks to enqueue
    as replies, which is how a scripted session drives itself.
    """

    def __init__(
        self,
        inbound: Iterable[bytes] | None = None,
        *,
        log_sink: Callable[[bytes], None] | None = None,
        on_write: Callable[[bytes], Iterable[bytes] | None] | None = None,
    ) -> None:
        super().__init__(log_sink)
        self._scripted = list(inbound or [])
        self._lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.written: list[bytes] = []
        self._on_write = on_write
        self._opened = False
        self._inbound = LineAssembler(MAX_FROM_DEVICE_BYTES)

    async def open(self) -> None:
        self._opened = True
        for chunk in self._scripted:
            for line in self._inbound.feed(chunk):
                self._lines.put_nowait(line)

    async def close(self) -> None:
        self._opened = False
        self._lines.put_nowait(None)

    async def read_line(self) -> bytes | None:
        """Block until a line arrives, or `None` once the transport is closed.

        A quiet peer is not a closed peer: returning `None` on an idle timeout
        would report EOF and force a reconnect every time the device went quiet.
        """
        if not self._opened:
            return None
        item = await self._lines.get()
        if item is not None:
            self._log(b"<- ", item)
        return item

    async def write_line(self, line: bytes) -> None:
        if not self._opened:
            raise TransportError("transport is not open")
        if not line.endswith(b"\n"):
            line += b"\n"
        self._log(b"-> ", line)
        self.written.append(line)
        if self._on_write is not None:
            for reply in self._on_write(line) or ():
                for framed in self._inbound.feed(reply):
                    self._lines.put_nowait(framed)

    def feed(self, *chunks: bytes) -> None:
        """Enqueue inbound bytes after construction, through the real assembler.

        A chunk may end mid-line; the assembler holds it until the newline
        arrives, exactly as the serial reader would.
        """
        for chunk in chunks:
            for line in self._inbound.feed(chunk):
                self._lines.put_nowait(line)

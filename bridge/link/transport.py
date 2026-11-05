"""The `Transport` port and the line reassembly both transports share."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..protocol import MAX_FROM_DEVICE_BYTES, redact_line

LogSink = Callable[[bytes], None]


class LineAssembler:
    """Turns arbitrary byte chunks into complete lines.

    `04-protocol.md` §1.1 rules 2-4: a trailing `\\r` is stripped, an oversize
    line is discarded up to and including its terminating newline so one bad line
    can never desynchronise the stream, and invalid UTF-8 is handled by the codec
    rather than here.
    """

    def __init__(self, max_bytes: int = MAX_FROM_DEVICE_BYTES) -> None:
        self._buf = bytearray()
        self._discarding = False
        self._max = max_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        for byte in chunk:
            if byte == 0x0A:  # \n
                if self._discarding:
                    self._discarding = False
                    self._buf.clear()
                    continue
                line = bytes(self._buf)
                self._buf.clear()
                out.append(line[:-1] if line.endswith(b"\r") else line)
                continue
            if self._discarding:
                continue
            self._buf.append(byte)
            if len(self._buf) + 1 > self._max:
                self._discarding = True
                self._buf.clear()
        return out


class Transport(ABC):
    """One open byte stream. `read_line` yields complete frames without the newline."""

    def __init__(self, log_sink: LogSink | None = None) -> None:
        #: Set by `__main__` when `--json` is passed. `None` means frames are
        #: never written anywhere.
        self._log_sink = log_sink

    def _log(self, direction: bytes, line: bytes) -> None:
        # One NDJSON object per line: no direction prefix, or `--json` output
        # would not parse (`09-bridge-architecture.md` §4.2).
        if self._log_sink is not None:
            self._log_sink(redact_line(line) + b"\n")

    @abstractmethod
    async def open(self) -> None:
        """Acquire the port. Raises `TransportError` when it cannot be opened."""

    @abstractmethod
    async def close(self) -> None:
        """Release the port. Idempotent."""

    @abstractmethod
    async def read_line(self) -> bytes | None:
        """Next complete line without its newline, or `None` once the peer is gone."""

    @abstractmethod
    async def write_line(self, line: bytes) -> None:
        """Write one complete line. `line` may or may not carry its newline."""


class TransportError(Exception):
    """The port could not be opened, or failed while open."""

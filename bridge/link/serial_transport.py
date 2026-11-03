"""pyserial transport: blocking reads on a dedicated thread, lines into asyncio."""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Callable

import serial
from serial.tools import list_ports

from .transport import LineAssembler, Transport, TransportError
from ..protocol import MAX_RX_BYTES


def find_port() -> str | None:
    """First USB CDC device by VID, falling back to a single ttyACM/ttyUSB.

    Lives here because `pyserial` is confined to `link` (`09-bridge-architecture.md`
    §7): `__main__` must not import it to pick a default port.
    """
    candidates = [
        p.device
        for p in list_ports.comports()
        if p.vid == 0x303A or p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB"))
    ]
    return candidates[0] if candidates else None


def _is_pty(path: str) -> bool:
    """True when `path` is a pseudo-terminal slave."""
    return path.startswith("/dev/pts/")


def _is_usb(path: str) -> bool:
    """True when `path` is a USB CDC device."""
    if path.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
        return True
    return any(p.device == path and p.vid is not None for p in list_ports.comports())


def check_target(target: str, path: str) -> None:
    """Refuse a wrong-peer session before a byte is written (`10-test-agent.md` §3).

    `usb` must not open a pseudo-terminal; `test` must not open real firmware.
    """
    if target == "usb" and _is_pty(path):
        raise TransportError(f"{path} is a pseudo-terminal; use --target test")
    if target == "test" and _is_usb(path):
        raise TransportError(f"{path} is a USB device; use --target usb")


class SerialTransport(Transport):
    """pyserial in one reader thread, complete lines handed to an `asyncio.Queue`."""

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        *,
        log_sink: Callable[[bytes], None] | None = None,
        read_timeout: float = 0.1,
    ) -> None:
        super().__init__(log_sink)
        self._port = port
        self._baud = baud
        self._read_timeout = read_timeout
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._closed = threading.Event()
        self._error: str | None = None
        #: Thread-safe hand-off between the reader thread and the event loop.
        self._pending: queue.Queue[bytes | None] = queue.Queue()

    @property
    def port(self) -> str:
        return self._port

    async def open(self) -> None:
        try:
            self._serial = serial.Serial(
                self._port, self._baud, timeout=self._read_timeout, write_timeout=5
            )
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"cannot open {self._port}: {exc}") from exc
        self._queue = asyncio.Queue()
        self._closed.clear()
        self._thread = threading.Thread(target=self._reader, name="bridge-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assembler = LineAssembler(MAX_RX_BYTES)
        assert self._serial is not None
        while not self._closed.is_set():
            try:
                chunk = self._serial.read(256)
            except (serial.SerialException, OSError) as exc:
                self._error = str(exc)
                self._pending.put(None)
                return
            if not chunk:
                continue
            for line in assembler.feed(chunk):
                self._pending.put(line)
        self._pending.put(None)

    async def read_line(self) -> bytes | None:
        if self._queue is None:
            return None
        while True:
            try:
                item = self._pending.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            if item is None:
                return None
            self._log(b"<- ", item)
            return item

    async def write_line(self, line: bytes) -> None:
        if self._serial is None:
            raise TransportError("port is not open")
        if not line.endswith(b"\n"):
            line += b"\n"
        self._log(b"-> ", line)
        try:
            self._serial.write(line)
            self._serial.flush()
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"write failed: {exc}") from exc

    async def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except (serial.SerialException, OSError):
                pass
            self._serial = None
        self._queue = None


__all__ = ["SerialTransport", "check_target", "find_port"]

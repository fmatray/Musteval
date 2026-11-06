"""pyserial transport: blocking reads on a dedicated thread, lines into asyncio."""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Callable

import serial
from serial.tools import list_ports

from .transport import LineAssembler, Transport, TransportError
from ..protocol import MAX_FROM_DEVICE_BYTES


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
        #: Serialises the port handle against `close`: writes and the close itself
        #: both leave the loop, so an in-flight write could otherwise reach a port
        #: that `close` has just released (`EBADF`).
        self._io_lock = asyncio.Lock()

    @property
    def port(self) -> str:
        return self._port

    async def open(self) -> None:
        # DTR and RTS are left exactly as `open` sets them, on purpose. On the S3's
        # native USB-JTAG that pair drives the reset strap, and any *transition* on
        # it resets the chip: leaving the port alone reconnects to a running device,
        # while dropping the lines after opening reboots it on every connection
        # (measured: `boot_id` changed per connect, `uptime_s` never left 0, the boot
        # counter climbed one per connect; untouched, the same `boot_id` held with
        # `uptime_s` climbing 13→17→21). A deliberate reset is `idf.py monitor`'s job,
        # never a side effect of the bridge connecting.
        try:
            port = serial.Serial(
                self._port, self._baud, timeout=self._read_timeout, write_timeout=5
            )
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"cannot open {self._port}: {exc}") from exc
        self._serial = port
        self._queue = asyncio.Queue()
        self._closed.clear()
        self._thread = threading.Thread(target=self._reader, name="bridge-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assembler = LineAssembler(MAX_FROM_DEVICE_BYTES)
        assert self._serial is not None
        while not self._closed.is_set():
            try:
                # Read what is already buffered, not a fixed 256: `read(n)` blocks
                # until `n` bytes arrive or the timeout expires, so any reply shorter
                # than 256 bytes waited the full 100 ms and `ping`/`pong` measured
                # ~99 ms against `07-roadmap.md` M1's 10 ms budget.
                chunk = self._serial.read(self._serial.in_waiting or 1)
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
                # Poll at 1 ms, not 5: this is the tail of every round trip, and
                # `07-roadmap.md` M1 budgets 10 ms for a whole `ping`/`pong`.
                await asyncio.sleep(0.001)
                continue
            if item is None:
                return None
            self._log(b"<- ", item)
            return item

    async def write_line(self, line: bytes) -> None:
        if not line.endswith(b"\n"):
            line += b"\n"
        self._log(b"-> ", line)
        # pyserial's write blocks until the kernel accepts the bytes, and a
        # firmware that never reads turns that into the full `write_timeout`.
        # On the event loop that freezes the whole TUI, so it runs in a thread.
        # The lock is held across the hand-off so `close` cannot release the port
        # mid-write, and the handle is re-checked inside it for the same reason.
        async with self._io_lock:
            port = self._serial
            if port is None:
                raise TransportError("port is not open")
            try:
                await asyncio.to_thread(self._write_now, port, line)
            except (serial.SerialException, OSError) as exc:
                raise TransportError(f"write failed: {exc}") from exc

    @staticmethod
    def _write_now(port: serial.Serial, line: bytes) -> None:
        port.write(line)
        port.flush()

    async def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            # The reader may be inside a blocking read, so the join waits on it.
            await asyncio.to_thread(self._thread.join, 1.0)
            self._thread = None
        # Closing a CDC port whose peer stopped reading costs ~30 s inside
        # pyserial, measured on the S3's native USB-JTAG. It must not run on the
        # event loop, or every retry cycle freezes the UI.
        async with self._io_lock:
            port, self._serial = self._serial, None
            if port is not None:
                try:
                    await asyncio.to_thread(port.close)
                except (serial.SerialException, OSError):
                    pass
        self._queue = None


__all__ = ["SerialTransport", "check_target", "find_port"]

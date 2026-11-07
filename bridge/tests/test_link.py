"""L1 link: line reassembly and the two transports."""

from __future__ import annotations

import asyncio
import errno
import os
import time

import pytest

from bridge.link import FakeTransport, SerialTransport, Transport
from bridge.link.transport import LineAssembler, TransportError


# -- LineAssembler ------------------------------------------------------


def test_assembler_returns_only_complete_lines():
    asm = LineAssembler()
    assert asm.feed(b'{"a":1') == []
    assert asm.feed(b'}\n{"b":2}\n') == [b'{"a":1}', b'{"b":2}']


def test_assembler_strips_a_trailing_carriage_return():
    assert LineAssembler().feed(b"line\r\n") == [b"line"]


def test_assembler_keeps_an_interior_carriage_return():
    assert LineAssembler().feed(b"a\rb\n") == [b"a\rb"]


def test_assembler_discards_an_oversize_line_and_resynchronises():
    asm = LineAssembler(max_bytes=8)
    # Over-long line is dropped up to its newline; the next line survives intact.
    out = asm.feed(b"aaaaaaaaaaaaaaaaaaaa\nok\n")
    assert out == [b"ok"]


def test_assembler_never_emits_a_partial_line_on_oversize_boundary():
    asm = LineAssembler(max_bytes=4)
    assert asm.feed(b"abc") == []  # exactly at the cap: still buffered
    assert asm.feed(b"d") == []    # one byte over: discarding begins
    assert asm.feed(b"\n") == []


def test_assembler_handles_one_byte_at_a_time():
    asm = LineAssembler()
    assert [asm.feed(bytes([b])) for b in b"ab\n"] == [[], [], [b"ab"]]


def test_assembler_emits_empty_lines():
    # An empty line is a malformed frame the codec rejects, not a framing error.
    assert LineAssembler().feed(b"\n") == [b""]


# -- FakeTransport ------------------------------------------------------


async def test_fake_transport_reassembles_partial_scripted_reads():
    transport = FakeTransport([b'{"v":1,', b'"type":"ping","id":"b-1","ts":0}\n'])
    await transport.open()
    assert await transport.read_line() == b'{"v":1,"type":"ping","id":"b-1","ts":0}'
    await transport.close()


async def test_fake_transport_records_writes_and_appends_the_newline():
    transport = FakeTransport()
    await transport.open()
    await transport.write_line(b'{"x":1}')
    assert transport.written == [b'{"x":1}\n']
    await transport.close()


async def test_fake_transport_drives_replies_from_on_write():
    transport = FakeTransport(on_write=lambda line: [b'{"reply":1}\n'])
    await transport.open()
    await transport.write_line(b"ask")
    assert await transport.read_line() == b'{"reply":1}'
    await transport.close()


async def test_fake_transport_read_returns_none_after_close():
    transport = FakeTransport()
    await transport.open()
    await transport.close()
    assert await transport.read_line() is None


async def test_fake_transport_refuses_writes_before_open():
    with pytest.raises(TransportError):
        await FakeTransport().write_line(b"x")


async def test_fake_transport_feed_adds_lines_later():
    transport = FakeTransport()
    await transport.open()
    transport.feed(b"late\n")
    assert await transport.read_line() == b"late"
    await transport.close()


# -- the port -----------------------------------------------------------


def test_transports_are_transports():
    assert issubclass(FakeTransport, Transport)
    assert issubclass(SerialTransport, Transport)


def _logged(transport) -> list[bytes]:
    seen: list[bytes] = []
    transport._log_sink = seen.append
    return seen


async def test_write_logging_redacts_a_token():
    transport = FakeTransport()
    seen = _logged(transport)
    await transport.open()
    await transport.write_line(b'{"v":1,"type":"hello","id":"b-1","ts":0,"token":"x"}')
    assert b'"x"' not in seen[0]
    assert b"***" in seen[0]
    await transport.close()


async def test_logged_lines_are_parseable_ndjson():
    """`--json` output must be one JSON object per line, nothing else (`09` §4.2)."""
    import json

    transport = FakeTransport()
    seen = _logged(transport)
    await transport.open()
    await transport.write_line(b'{"v":1,"type":"hello","id":"b-1","ts":0}')
    await transport.close()
    assert json.loads(seen[0]) == {"v": 1, "type": "hello", "id": "b-1", "ts": 0}


async def test_feed_reassembles_a_line_split_across_chunks():
    """`feed` must not lose bytes when a caller hands it a partial line."""
    transport = FakeTransport()
    await transport.open()
    transport.feed(b'{"v":1', b',"type":"pong"}', b"\n")
    assert await transport.read_line() == b'{"v":1,"type":"pong"}'
    await transport.close()


async def test_on_write_replies_are_reassembled_too():
    transport = FakeTransport(on_write=lambda line: [b"partial", b"-reply\n"])
    await transport.open()
    await transport.write_line(b"x")
    assert await transport.read_line() == b"partial-reply"
    await transport.close()


async def test_read_logging_is_silent_without_a_sink():
    transport = FakeTransport([b"line\n"])
    await transport.open()
    await transport.read_line()  # no sink installed, must not raise
    await transport.close()


# -- SerialTransport error paths ---------------------------------------


async def test_serial_transport_reports_a_missing_port():
    transport = SerialTransport("/dev/definitely-not-a-port")
    with pytest.raises(TransportError):
        await transport.open()


async def test_serial_transport_reads_nothing_when_closed():
    assert await SerialTransport("/dev/null").read_line() is None


async def test_serial_transport_carries_a_line_across_a_real_fd():
    """The reader thread reassembles a line written by the other end."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    transport = SerialTransport(path, read_timeout=0.05)
    try:
        await transport.open()
        os.write(master, b'{"v":1,')
        os.write(master, b'"type":"pong"}\n')
        line = await asyncio.wait_for(transport.read_line(), 2)
        assert line == b'{"v":1,"type":"pong"}'
        await transport.write_line(b"to-device")
        assert os.read(master, 64) == b"to-device\n"
    finally:
        await transport.close()
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass


async def test_serial_transport_delivers_a_short_line_without_waiting_for_a_full_buffer():
    """A reply shorter than the read size must not wait for the read timeout.

    `07-roadmap.md` M1 budgets 10 ms for a whole `ping`/`pong`. With
    `read(256)` the reader blocked until 256 bytes arrived or the 100 ms timeout
    expired, so every short frame cost the timeout and the budget was missed by an
    order of magnitude. Measured here against a real fd with a 50 ms timeout: a
    correct reader returns in a few ms, the old one in >=50.
    """
    master, slave = os.openpty()
    transport = SerialTransport(os.ttyname(slave), read_timeout=0.05)
    try:
        await transport.open()
        os.write(master, b'{"v":1,"type":"pong"}\n')
        started = time.monotonic()
        line = await asyncio.wait_for(transport.read_line(), 2)
        elapsed_ms = (time.monotonic() - started) * 1000
        assert line == b'{"v":1,"type":"pong"}'
        assert elapsed_ms < 20, f"short line took {elapsed_ms:.1f} ms: read is waiting for a full buffer"
    finally:
        await transport.close()
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass


async def test_serial_transport_opens_a_pty_without_raising():
    """A pty has no modem lines, and opening one must not ask for them."""
    master, slave = os.openpty()
    transport = SerialTransport(os.ttyname(slave), read_timeout=0.05)
    try:
        await transport.open()
        os.write(master, b'{"v":1,"type":"pong"}\n')
        assert await asyncio.wait_for(transport.read_line(), 2) == b'{"v":1,"type":"pong"}'
    finally:
        await transport.close()
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass


async def test_serial_transport_does_not_block_the_loop_on_a_stalled_write(monkeypatch):
    """A firmware that never reads must not freeze the UI for `write_timeout`.

    pyserial's write blocks until the kernel takes the bytes. On the event loop
    that stalls every widget, which is exactly what made the bridge look hung.
    """

    class StalledPort:
        # Real `Serial` always exposes this and `read` sizes its call from it; without
        # it the reader thread dies on AttributeError before reaching `read`.
        in_waiting = 0

        def __init__(self, *args, **kwargs):
            pass

        def write(self, data):
            time.sleep(0.5)  # stands in for a full TX buffer

        def flush(self):
            pass

        def read(self, size):
            raise OSError(errno.EIO, "gone")

        def close(self):
            pass

    monkeypatch.setattr("bridge.link.serial_transport.serial.Serial", StalledPort)
    transport = SerialTransport("/dev/irrelevant", read_timeout=0.05)
    await transport.open()
    # The loop must keep turning while the write is stuck in the thread.
    ticks = 0
    task = asyncio.create_task(transport.write_line(b"x"))
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.01)
        if ticks > 200:
            break
    assert task.done(), "write_line blocked the event loop"
    assert ticks >= 10, f"loop starved: only {ticks} ticks during a 0.5s write"
    await task
    await transport.close()


async def test_serial_transport_write_and_close_never_meet_on_a_freed_port(monkeypatch):
    """Observed live: `EBADF` when a retry cycle closed under an in-flight frame.

    Whether the queued write wins the lock or arrives after the close is not the
    contract; that it never reaches a released handle is.
    """
    state = {"closed": False, "bad_fd": 0}

    class SlowPort:
        in_waiting = 0  # see `StalledPort`

        def __init__(self, *args, **kwargs):
            pass

        def write(self, data):
            time.sleep(0.02)
            if state["closed"]:
                state["bad_fd"] += 1
                raise OSError(errno.EBADF, "Bad file descriptor")

        def flush(self):
            pass

        def read(self, size):
            raise OSError(errno.EIO, "gone")

        def close(self):
            state["closed"] = True

    monkeypatch.setattr("bridge.link.serial_transport.serial.Serial", SlowPort)
    transport = SerialTransport("/dev/irrelevant", read_timeout=0.05)
    await transport.open()
    writers = [asyncio.create_task(transport.write_line(b"x")) for _ in range(5)]
    await asyncio.sleep(0)
    await transport.close()
    results = await asyncio.gather(*writers, return_exceptions=True)
    assert state["bad_fd"] == 0
    for result in results:
        if isinstance(result, TransportError):
            assert "not open" in str(result), result


async def test_serial_transport_never_touches_the_reset_strap(monkeypatch):
    """DTR and RTS are the S3 reset strap: any transition resets the chip.

    Driving either line, before or after `open`, pulsed a reset on every connection
    and the board never stayed up. Leaving the pair alone reconnects to the running
    device, so the transport must not set them at all.
    """

    events = []

    class RecordingPort:
        in_waiting = 0  # see `StalledPort`

        def __init__(self, *args, **kwargs):
            events.append("construct")

        def _line(self, value):
            events.append(("line", value))

        dtr = property(lambda self: True, lambda self, value: self._line(value))
        rts = property(lambda self: True, lambda self, value: self._line(value))

        def write(self, data):
            pass

        def flush(self):
            pass

        def read(self, size):
            return b""


        def close(self):
            events.append("close")

    monkeypatch.setattr("bridge.link.serial_transport.serial.Serial", RecordingPort)
    transport = SerialTransport("/dev/irrelevant", read_timeout=0.05)
    await transport.open()
    await transport.close()

    assert [e for e in events if isinstance(e, tuple)] == []


async def test_serial_transport_reports_a_write_on_a_closed_port():
    transport = SerialTransport("/dev/definitely-not-a-port")
    with pytest.raises(TransportError, match="not open"):
        await transport.write_line(b"x")


async def test_serial_transport_survives_the_peer_closing():
    """A vanished peer is reported as `None`, not an exception out of the thread."""
    master, slave = os.openpty()
    transport = SerialTransport(os.ttyname(slave), read_timeout=0.02)
    await transport.open()
    os.close(slave)
    # Closing the master is what makes the slave read fail, as an unplug does.
    os.close(master)
    assert await asyncio.wait_for(transport.read_line(), 2) is None
    await transport.close()


# -- port autodetection ------------------------------------------------


class _Port:
    def __init__(self, device: str, vid: int | None) -> None:
        self.device = device
        self.vid = vid


def _patch_comports(monkeypatch, ports) -> None:
    # Patching `link`'s own binding keeps pyserial out of the test (`09` §6).
    from bridge.link import serial_transport

    monkeypatch.setattr(serial_transport.list_ports, "comports", lambda: ports)


def test_find_port_skips_a_device_that_is_neither(monkeypatch):
    """A motherboard UART with no VID is not a candidate; the USB one is."""
    from bridge.link import find_port

    _patch_comports(
        monkeypatch,
        [_Port("/dev/ttyS0", None), _Port("/dev/ttyUSB9", 0x1A86)],
    )
    assert find_port() == "/dev/ttyUSB9"


def test_find_port_returns_the_first_candidate(monkeypatch):
    from bridge.link import find_port

    _patch_comports(
        monkeypatch,
        [_Port("/dev/ttyACM3", 0x303A), _Port("/dev/ttyACM4", 0x303A)],
    )
    assert find_port() == "/dev/ttyACM3"


def test_find_port_accepts_a_ttyacm_without_a_vid(monkeypatch):
    from bridge.link import find_port

    _patch_comports(monkeypatch, [_Port("/dev/ttyACM7", None)])
    assert find_port() == "/dev/ttyACM7"


def test_find_port_returns_none_when_nothing_matches(monkeypatch):
    from bridge.link import find_port

    _patch_comports(monkeypatch, [_Port("/dev/ttyS0", None)])
    assert find_port() is None


# -- the --target guard (`10-test-agent.md` §3) -------------------------


def test_usb_target_refuses_a_pseudo_terminal():
    from bridge.link import check_target
    from bridge.link.transport import TransportError

    with pytest.raises(TransportError, match="pseudo-terminal"):
        check_target("usb", "/dev/pts/3")


def test_test_target_refuses_a_usb_device():
    from bridge.link import check_target
    from bridge.link.transport import TransportError

    with pytest.raises(TransportError, match="USB device"):
        check_target("test", "/dev/ttyACM0")


def test_matching_targets_pass():
    from bridge.link import check_target

    check_target("usb", "/dev/ttyACM0")
    check_target("test", "/dev/pts/3")

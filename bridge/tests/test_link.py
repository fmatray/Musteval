"""L1 link: line reassembly and the two transports."""

from __future__ import annotations

import asyncio
import os

import pytest

from bridge.link import FakeTransport, SerialTransport, Transport
from bridge.link.transport import LineAssembler, TransportError
from bridge.protocol import MAX_RX_BYTES


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
    import serial.tools.list_ports as list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: ports)


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

"""Protocol L0: framing, envelope, direction, redaction. 100% line and branch."""

from __future__ import annotations

import json

import pytest

from bridge.protocol import (
    ALL_TYPES,
    BRIDGE_TO_DEVICE,
    COMMAND_NAMES,
    DEVICE_TO_BRIDGE,
    ERROR_CODES,
    MAX_FROM_BRIDGE_BYTES,
    MAX_FROM_DEVICE_BYTES,
    Frame,
    ProtocolError,
    decode_line,
    encode,
    new_id,
    redact_line,
    reset_ids,
)


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def _frame(**body) -> Frame:
    return Frame(type="ping", id="b-1", ts=0, body=body or {})


# -- the frozen catalog -------------------------------------------------


def test_catalog_matches_the_frozen_split():
    # 04 §2.1. A type moving side is a protocol change, not a refactor.
    assert BRIDGE_TO_DEVICE == {
        "hello",
        "prompt",
        "cancel",
        "command",
        "secret.set",
        "ping",
        "ack",
    }
    assert len(DEVICE_TO_BRIDGE) == 14
    assert not BRIDGE_TO_DEVICE & DEVICE_TO_BRIDGE
    assert len(ALL_TYPES) == 21


def test_error_codes_are_the_frozen_sixteen():
    assert len(ERROR_CODES) == 16
    assert all(code.startswith("E_") for code in ERROR_CODES)


def test_command_names_are_the_frozen_twenty_three():
    """16 frozen names plus the seven `04` §4 records as this release's amendment."""
    assert len(COMMAND_NAMES) == 23
    assert {"sandbox.list", "sandbox.read", "sandbox.delete", "sandbox.write", "sandbox.mkdir", "sandbox.move", "sys.reset_counter"} <= COMMAND_NAMES


# -- encode -------------------------------------------------------------


def test_encode_writes_one_terminated_line():
    line = encode(_frame())
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert json.loads(line) == {"v": 1, "type": "ping", "id": "b-1", "ts": 0}


def test_encode_includes_reply_to_only_when_set():
    assert b"reply_to" not in encode(_frame())
    assert b"reply_to" in encode(Frame(type="pong", id="d-1", ts=1, body={}, reply_to="b-1"))


@pytest.mark.parametrize("bad", ["", "a" * 33, "has space", "semi;colon", 5, None])
def test_encode_rejects_a_malformed_id(bad):
    with pytest.raises(ProtocolError):
        encode(Frame(type="ping", id=bad, ts=0, body={}))


@pytest.mark.parametrize("bad", ["0", 1.5, True, None])
def test_encode_rejects_a_malformed_ts(bad):
    with pytest.raises(ProtocolError):
        encode(Frame(type="ping", id="b-1", ts=bad, body={}))


def test_encode_rejects_unknown_type():
    with pytest.raises(ProtocolError):
        encode(Frame(type="bogus", id="b-1", ts=0, body={}))


def test_encode_refuses_a_body_that_redefines_the_envelope():
    with pytest.raises(ProtocolError):
        encode(_frame(v=2))


def test_encode_refuses_an_oversize_frame():
    with pytest.raises(ProtocolError):
        encode(Frame(type="log", id="b-1", ts=0, body={"msg": "x" * MAX_FROM_BRIDGE_BYTES}))


def test_encode_keeps_unicode_readable():
    assert "é".encode() in encode(Frame(type="log", id="b-1", ts=0, body={"msg": "é"}))


def test_the_two_directions_have_different_caps():
    """`04-protocol.md` §1: the device reads 4096 and writes 16384, so the bridge's
    outgoing and incoming limits are not interchangeable.

    Swapping them was a real bug: a `sandbox.read` reply of ~8 KiB fits the device's
    16384-byte TX window but was discarded by a 4096-byte receive cap, and the command
    hung until its timeout with no error shown.
    """
    assert MAX_FROM_BRIDGE_BYTES == 4096
    assert MAX_FROM_DEVICE_BYTES == 16384

    # A line the device is allowed to send is accepted, exactly at the cap minus its
    # terminating newline...
    head = b'{"v":1,"type":"log","id":"d-1","ts":0,"msg":"'
    tail = b'"}'
    at_limit = head + b"x" * (MAX_FROM_DEVICE_BYTES - 1 - len(head) - len(tail)) + tail
    assert decode_line(at_limit).type == "log"
    # ...and one byte more is refused, so the cap is enforced rather than decorative.
    over = head + b"x" * (MAX_FROM_DEVICE_BYTES - len(head) - len(tail) + 1) + tail
    assert len(over) + 1 > MAX_FROM_DEVICE_BYTES
    with pytest.raises(ProtocolError):
        decode_line(over)


# -- decode -------------------------------------------------------------


def test_decode_roundtrips():
    frame = Frame(type="pong", id="d-1", ts=7, body={"x": 1}, reply_to="b-1")
    assert decode_line(encode(frame)) == frame


def test_decode_accepts_crlf_and_no_newline():
    assert decode_line(b'{"v":1,"type":"ping","id":"b-1","ts":0}\r\n').type == "ping"
    assert decode_line(b'{"v":1,"type":"ping","id":"b-1","ts":0}').type == "ping"


def test_decode_separates_body_from_envelope():
    frame = decode_line(b'{"v":1,"type":"log","id":"d-1","ts":0,"msg":"hi"}')
    assert frame.body == {"msg": "hi"}


@pytest.mark.parametrize(
    "line",
    [
        b"",                                    # empty
        b"   ",                                 # whitespace only
        b"not json",                            # not JSON
        b"[1,2,3]",                             # not an object
        b'{"type":"ping","id":"b-1","ts":0}',   # missing v
        b'{"v":2,"type":"ping","id":"b-1","ts":0}',  # wrong v
        b'{"v":1,"id":"b-1","ts":0}',           # missing type
        b'{"v":1,"type":"nope","id":"b-1","ts":0}',  # unknown type
        b'{"v":1,"type":"ping","id":"b-1"}',    # missing ts
        b'{"v":1,"type":"ping","id":"","ts":0}',  # empty id
        b'{"v":1,"type":"ping","id":"b 1","ts":0}',  # bad id
        b'{"v":1,"type":"ping","id":"b-1","ts":1.5}',
        b'{"v":1,"type":"ping","id":"b-1","ts":true}',
        b'{"v":1,"type":"ping","id":"b-1","ts":0,"reply_to":"a b"}',
    ],
)
def test_decode_rejects_malformed_frames(line):
    with pytest.raises(ProtocolError):
        decode_line(line)


def test_decode_reports_the_failure_position():
    with pytest.raises(ProtocolError) as excinfo:
        decode_line(b'{"v":1,}')
    assert "offset" in str(excinfo.value)


def test_decode_rejects_invalid_utf8():
    with pytest.raises(ProtocolError):
        decode_line(b'{"v":1,"type":"log","id":"d-1","ts":0,"msg":"\xff"}')


def test_decode_rejects_an_oversize_line():
    line = b'{"v":1,"type":"log","id":"d-1","ts":0,"msg":"' + b"x" * MAX_FROM_DEVICE_BYTES + b'"}'
    with pytest.raises(ProtocolError):
        decode_line(line)


def test_decode_enforces_direction():
    # A device->bridge type offered as a device frame is fine...
    assert decode_line(b'{"v":1,"type":"pong","id":"d-1","ts":0}', expect=DEVICE_TO_BRIDGE)
    # ...but rejected when the caller expects the other direction.
    with pytest.raises(ProtocolError) as excinfo:
        decode_line(b'{"v":1,"type":"pong","id":"d-1","ts":0}', expect=BRIDGE_TO_DEVICE)
    assert excinfo.value.detail["direction"] == "device_to_bridge"


def test_decode_without_expect_accepts_both_directions():
    assert decode_line(b'{"v":1,"type":"hello","id":"b-1","ts":0}').type == "hello"
    assert decode_line(b'{"v":1,"type":"hello_ack","id":"d-1","ts":0}').type == "hello_ack"


def test_decode_rejects_a_deeply_nested_document_without_crashing():
    # `04` §1.2 fuzz rule: never a crash, never a hang.
    nested = b'{"v":1,"type":"log","id":"d-1","ts":0,"msg":' + b"[" * 5000 + b"]" * 5000
    with pytest.raises(ProtocolError):
        decode_line(nested[: MAX_FROM_DEVICE_BYTES - 1])


# -- ids ----------------------------------------------------------------


def test_new_id_counts_per_prefix():
    assert new_id("b") == "b-1"
    assert new_id("b") == "b-2"
    assert new_id("d") == "d-1"


# -- redaction ----------------------------------------------------------


def test_redact_removes_the_hello_token():
    line = b'{"v":1,"type":"hello","id":"b-1","ts":0,"token":"s3cret"}'
    out = redact_line(line)
    assert b"s3cret" not in out
    assert json.loads(out)["token"] == "***"


def test_redact_removes_a_secret_value():
    line = b'{"v":1,"type":"secret.set","id":"b-1","ts":0,"key":"wifi","value":"hunter2"}'
    out = redact_line(line)
    assert b"hunter2" not in out
    assert json.loads(out)["value"] == "***"
    assert json.loads(out)["key"] == "wifi"


def test_redact_preserves_the_trailing_newline_state():
    framed = b'{"v":1,"type":"hello","id":"b-1","ts":0,"token":"x"}\n'
    assert redact_line(framed).endswith(b"\n")
    assert not redact_line(framed[:-1]).endswith(b"\n")


def test_redact_leaves_other_frames_byte_for_byte():
    line = b'{"v":1,"type":"log","id":"d-1","ts":0,"msg":"token=abc"}'
    assert redact_line(line) == line


def test_redact_leaves_unparseable_lines_alone():
    # The logging boundary must not turn --json into a crash.
    assert redact_line(b"garbage") == b"garbage"
    assert redact_line(b"\xff\xfe") == b"\xff\xfe"
    assert redact_line(b"[1,2]") == b"[1,2]"


def test_redact_handles_a_frame_without_the_secret_member():
    line = b'{"v":1,"type":"secret.set","id":"b-1","ts":0,"key":"wifi"}'
    assert redact_line(line) == line


def test_redact_leaves_a_bare_token_member_absent():
    assert redact_line(b'{"v":1,"type":"ack","id":"b-1","ts":0}') == b'{"v":1,"type":"ack","id":"b-1","ts":0}'

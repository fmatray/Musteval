"""End to end through the real CLI: `__main__.main` against a pty-hosted agent.

`--send` is the headless path, so this exercises argv parsing, the `--target`
guard, `link`, `client` and `commands` without a terminal. Every case here either
takes `--send` or is refused before the UI starts; a bare invocation would open
the Textual app and never return.
"""

from __future__ import annotations

import json

from argparse import Namespace

import pytest

from bridge.__main__ import _headless, main
from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import Frame, encode, reset_ids

from .test_agent_session import Agent


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


def test_send_status_prints_the_status_object(agent, capsys):
    code = main(["--target", "test", "--port", agent.port, "--send", "/status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "'boot_id': '00000001'" in out
    assert "'streaming'" not in out


def test_send_prompt_prints_the_agent_final(agent, capsys):
    code = main(["--target", "test", "--port", agent.port, "--send", "hello there"])
    out = capsys.readouterr().out
    assert code == 0
    assert "echo" in out.lower()


def test_test_target_requires_a_port(capsys):
    assert main(["--target", "test"]) == 2
    assert "requires --port" in capsys.readouterr().err


def test_usb_target_refuses_a_pty(agent, capsys):
    assert main(["--port", agent.port]) == 2
    assert "pseudo-terminal" in capsys.readouterr().err


def test_wrong_token_is_refused_before_any_command(capsys):
    strict = Agent("--token", "sekrit")
    try:
        code = main(["--target", "test", "--port", strict.port,
                     "--token", "wrong", "--send", "/status"])
    finally:
        strict.stop()
    captured = capsys.readouterr()
    assert code == 1
    assert "not connected" in captured.err
    assert "sekrit" not in captured.out and "sekrit" not in captured.err


def test_json_switch_writes_redacted_frames(agent, tmp_path):
    log = tmp_path / "frames.ndjson"
    code = main(
        [
            "--target", "test", "--port", agent.port,
            "--json", "--log-file", str(log),
            "--send", "/status",
        ]
    )
    assert code == 0
    text = log.read_text()
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert lines and all("v" in frame and "type" in frame for frame in lines)


def test_a_missing_target_check_is_reported_as_usage(monkeypatch, capsys):
    """`check_target` refuses a pty for `usb` and a USB device for `test`; both are
    the reader's mistake, so both are `2`."""
    from bridge.link import TransportError

    def refuses(target: str, port: str) -> None:
        raise TransportError(f"{port} is not a usb device")

    monkeypatch.setattr("bridge.__main__.check_target", refuses)
    assert main(["--target", "usb", "--port", "/dev/ttyACM0"]) == 2
    assert "not a usb device" in capsys.readouterr().err


def test_target_test_without_a_port_is_refused(monkeypatch, capsys):
    """A test target has no autodetect: there is no test agent until the caller
    names one, so guessing a port would hand the user a stranger's device."""
    assert main(["--target", "test"]) == 2
    assert "--target test requires --port" in capsys.readouterr().err


def test_json_logging_writes_every_frame_to_the_named_file(agent, tmp_path, capsys):
    """`04` §1: `--json --log-file` exists so a bug report carries the wire, and the
    file must hold the frames rather than being created empty."""
    log = tmp_path / "frames.ndjson"
    assert main(["--target", "test", "--port", agent.port, "--json", "--log-file", str(log),
                 "--send", "/status"]) == 0
    lines = [line for line in log.read_text().splitlines() if line.strip()]
    assert any('"hello"' in line for line in lines)
    assert any('"status"' in line or '"command_result"' in line for line in lines)


def test_a_keyboard_interrupt_exits_130(monkeypatch):
    """A Ctrl-C is a user action, not a crash: the shell convention is 130."""
    import bridge.__main__ as entry

    def interrupted(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(entry.asyncio, "run", interrupted)
    assert entry.main([]) == 130


def test_no_device_is_a_usage_error_not_a_traceback(monkeypatch, capsys):
    """`05` §5.1: a missing port names what to pass, rather than opening the TUI."""
    monkeypatch.setattr("bridge.__main__.find_port", lambda: None)
    assert main([]) == 2
    assert "pass --port" in capsys.readouterr().err


def test_a_usage_error_in_a_slash_command_exits_two(agent, capsys):
    """`05` §6: a malformed command is the reader's mistake, so it is `2`, not `1`."""
    code = main(["--target", "test", "--port", agent.port, "--send", "/config"])
    assert code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_a_local_command_sends_nothing(agent, capsys):
    """`parse` may answer a slash command with no frame; the CLI must say so."""
    code = main(["--target", "test", "--port", agent.port, "--send", "/help"])
    assert code == 0
    assert "local command" in capsys.readouterr().err


def test_a_fire_and_forget_command_does_not_wait_for_a_reply(agent, capsys):
    """`/cancel` and `/ping` have no dedicated reply, so they must not block."""
    code = main(["--target", "test", "--port", agent.port, "--send", "/cancel"])
    assert code == 0


def test_two_sends_run_in_sequence(agent, capsys):
    """A `--send` may be repeated: each one runs after the previous reply."""
    code = main(
        ["--target", "test", "--port", agent.port, "--send", "/status", "--send", "hi"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "'boot_id'" in out
    assert "echo" in out.lower()


def test_a_connect_timeout_is_reported_as_not_connected(monkeypatch, capsys):
    """`--connect-timeout` bounds the handshake, and failing it is `1`."""
    from bridge.link import FakeTransport

    def silent(line: bytes) -> list[bytes]:
        return []

    real = Session

    def patched(transport, **kwargs):
        kwargs.setdefault("ack_timeout", 0.05)
        return real(transport, **kwargs)

    monkeypatch.setattr("bridge.__main__.Session", patched)
    monkeypatch.setattr(
        "bridge.__main__.SerialTransport", lambda *a, **k: FakeTransport(on_write=silent)
    )
    monkeypatch.setattr("bridge.__main__.check_target", lambda *a: None)
    assert main(["--target", "test", "--port", "/dev/irrelevant", "--send", "/status"]) == 1
    assert "not connected" in capsys.readouterr().err


async def test_send_prompt_that_is_never_answered_exits_one(capsys):
    """A linked peer that goes quiet on the prompt fails as a line, not a traceback.

    Built on `FakeTransport` because the point is the timeout, not the wire: the
    agent double would have to stall the reply without stalling `hello`.
    """
    ack = {
        "device": "test-agent", "fw_version": "test-1.0.0", "protocol": 1,
        "boot_id": "b1", "time_synced": True,
        "limits": {"rx_max": 4096, "tx_max": 16384},
        "capabilities": {"tools": [], "mcp": [], "dangerous_tools": False,
                         "streaming": False},
    }

    def answers_hello_only(line: bytes) -> list[bytes]:
        frame = json.loads(line)
        if frame["type"] == "prompt":
            return []
        if frame["type"] == "hello":
            return [encode(Frame(type="hello_ack", id="d-1", ts=0,
                                 body=ack, reply_to=frame["id"]))]
        return [encode(Frame(type="pong", id="d-2", ts=0, body={},
                             reply_to=frame["id"]))]

    session = Session(FakeTransport(on_write=answers_hello_only),
                      ack_timeout=0.05, ping_interval=99)
    await session.connect()
    args = Namespace(send=["hello there"], target="test")
    try:
        code = await _headless(session, args)
    finally:
        await session.close()
    err = capsys.readouterr().err
    assert code == 1
    assert "no reply before the timeout" in err

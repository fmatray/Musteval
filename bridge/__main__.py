"""Entry point: parse argv, build the transport and session, run the UI or one command."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .client import Session
from .client.session import SessionError
from .commands import CommandError, CommandState, parse
from .link import SerialTransport, TransportError, check_target, find_port
from .protocol import Frame, new_id

DEFAULT_BAUD = 115200


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Musteval host bridge")
    parser.add_argument("--port", help="serial device or pty (autodetected for usb)")
    parser.add_argument(
        "--target",
        choices=("usb", "test"),
        default="usb",
        help="peer kind; refuses a pty for usb and a USB device for test",
    )
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--token", help="link token, when the device requires one")
    parser.add_argument(
        "--json", action="store_true", help="write every frame to --log-file as NDJSON"
    )
    parser.add_argument("--log-file", default="bridge-frames.ndjson")
    parser.add_argument(
        "--send",
        action="append",
        default=[],
        metavar="TEXT",
        help="send a prompt (or /command) headlessly, print the reply, exit",
    )
    parser.add_argument(
        "--connect-timeout", type=float, default=5.0, help="seconds to wait for hello_ack"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.target == "test" and not args.port:
        print("--target test requires --port", file=sys.stderr)
        return 2
    port = args.port or find_port()
    if port is None:
        print("no serial device found; pass --port", file=sys.stderr)
        return 2
    try:
        check_target(args.target, port)
    except TransportError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    log_sink = None
    log_handle = None
    if args.json:
        log_handle = Path(args.log_file).open("ab")
        log_sink = log_handle.write

    transport = SerialTransport(port, args.baud, log_sink=log_sink)
    session = Session(
        transport,
        token=args.token,
        ack_timeout=args.connect_timeout,
        backoff=(1, 2, 4, 8, 15),
    )
    try:
        if args.send:
            return await _headless(session, args)
        from .ui import BridgeApp

        app = BridgeApp(session)
        await app.run_async()
        return 0
    finally:
        await session.close()
        if log_handle is not None:
            log_handle.close()


async def _headless(session: Session, args: argparse.Namespace) -> int:
    await session.connect()
    if session.state.value != "connected":
        print(f"not connected: {session.state.value}", file=sys.stderr)
        return 1
    received: list[Frame] = []
    session.install_sink(received.append)
    for text in args.send:
        if not text.startswith("/"):
            prompt = Frame(type="prompt", id=new_id("b"), ts=0, body={"text": text})
            try:
                reply = await session.request(prompt)
            except SessionError as exc:
                print(f"{exc}", file=sys.stderr)
                return 1
            print(reply.body.get("text", reply.body))
            continue
        try:
            frames = parse(text, CommandState())
        except CommandError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
        if not frames:
            print("(local command: no frame sent)", file=sys.stderr)
            continue
        for frame in frames:
            if frame.type in ("command", "prompt"):
                try:
                    reply = await session.request(frame)
                except SessionError as exc:
                    print(f"{exc}", file=sys.stderr)
                    return 1
                print(reply.body.get("result", reply.body))
            else:
                await session.send(frame)
    await asyncio.sleep(0.2)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

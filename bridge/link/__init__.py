"""L1 link: the byte stream and the two transports.

The only layer that sees bytes in both directions, which is why debug logging
lives here (`09-bridge-architecture.md` §4.2). It does not parse JSON, correlate
ids, know which frame types exist, or decide when to reconnect.
"""

from __future__ import annotations

from .transport import Transport, TransportError
from .serial_transport import SerialTransport, check_target, find_port
from .fake_transport import FakeTransport

__all__ = [
    "Transport",
    "SerialTransport",
    "FakeTransport",
    "TransportError",
    "find_port",
    "check_target",
]

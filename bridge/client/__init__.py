"""L2 client: `Session` owns correlation, heartbeat, backoff and link state."""

from .session import LinkState, Session

__all__ = ["Session", "LinkState"]

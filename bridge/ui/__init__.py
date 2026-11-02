"""L3 ui: Textual App, screens and widgets.

The only package allowed to import `textual` (`09-bridge-architecture.md` §6). It
consumes `Session` events and renders them; it holds no protocol state of its own
and builds no frame bodies except by calling `bridge.commands`.
"""

from .app import BridgeApp

__all__ = ["BridgeApp"]

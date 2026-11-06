"""L3 ui: the `/onboard` wizard (`05-bridge.md` §7), driven through the pilot.

What a reader sees and what reaches the device, against a scripted transport that
records every frame. The ordering rule — both `secret.set` frames before any
`config.set` naming their `nvs:` key — is asserted on the wire, not on the plan.
"""

from __future__ import annotations

import json

import pytest

from bridge.client import Session
from bridge.link import FakeTransport
from bridge.protocol import reset_ids
from bridge.ui import BridgeApp

from .test_ui import _reply
from .test_ui_input import _connected


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


class Device:
    """Answers like the shared fake, and remembers every frame in order."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def __call__(self, line: bytes) -> list[bytes]:
        frame = json.loads(line)
        self.seen.append(frame)
        return _reply(line)

    def order(self) -> list[str]:
        out = []
        for frame in self.seen:
            if frame["type"] == "secret.set":
                out.append(f"secret:{frame['key']}")
            elif frame["type"] == "command":
                out.append(f"{frame['name']}:{frame.get('args', {}).get('path', '')}")
        return out


def _app(device: Device) -> BridgeApp:
    return BridgeApp(Session(FakeTransport(on_write=device), ack_timeout=1, ping_interval=99))


async def _fill(pilot, widget: str, text: str) -> None:
    """Type `text` into one field of the grouped form (`05` §13.6).

    The form is one dialog, so a test fills fields instead of answering prompts
    in sequence; the ordering rule is asserted on the wire either way.
    """
    await pilot.pause(0.05)
    app = pilot.app
    field = app.screen.query_one(widget)
    field.focus()
    field.value = text
    await pilot.pause(0.02)


async def _submit(pilot) -> None:
    await pilot.pause(0.05)
    pilot.app.screen.query_one("#onboard-ssid").focus()
    await pilot.press("ctrl+s")


async def test_onboard_writes_both_secrets_before_the_config():
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        for widget, answer in (
            ("#onboard-ssid", "home"),
            ("#onboard-password", "hunter2"),
            ("#onboard-api-key", "sk-test"),
            ("#onboard-model", "gpt-4o-mini"),
        ):
            await _fill(pilot, widget, answer)
        await _submit(pilot)
        await pilot.pause(0.3)
        await pilot.press("y")
        await pilot.pause(0.4)
    order = device.order()
    secret_rows = [i for i, row in enumerate(order) if row.startswith("secret:")]
    config_rows = [i for i, row in enumerate(order) if row.startswith("config.set:")]
    assert len(secret_rows) == 2 and config_rows
    assert max(secret_rows) < min(config_rows), order


async def test_onboard_refuses_without_a_link():
    app = BridgeApp(
        Session(FakeTransport(on_write=lambda line: []), ack_timeout=1, ping_interval=99)
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await app._run_command("/onboard")
        await pilot.pause(0.2)
        log = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
    assert "needs a live link" in log


async def test_cancelling_the_confirm_writes_nothing():
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        for widget, answer in (
            ("#onboard-ssid", "home"),
            ("#onboard-password", "hunter2"),
            ("#onboard-api-key", "sk-test"),
            ("#onboard-model", "gpt"),
        ):
            await _fill(pilot, widget, answer)
        await _submit(pilot)
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.3)
    assert not [row for row in device.order()
                if row.startswith(("secret:", "config.set:"))]


async def test_a_half_filled_domain_says_which_pair_it_needs():
    """`03` `## Full schema` makes `password_ref` required, so an ssid alone
    writes nothing; the report must name the pair, not call the answer empty."""
    device = Device()
    app = _app(device)
    async with app.run_test() as pilot:
        await _connected(pilot)
        app.run_worker(app.onboard())
        await _fill(pilot, "#onboard-ssid", "home")
        await _submit(pilot)
        for _ in range(40):
            await pilot.pause(0.05)
        log = "\n".join(str(line.text) for line in app.query_one("#chat-log").lines)
    assert "an ssid and a password" in log
    assert "every answer was empty" not in log
    # The connect handshake's `status` is the only frame; no write was sent.
    assert [row for row in device.order() if row != "status:"] == []

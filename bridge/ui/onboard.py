"""`/onboard`: configure a fresh device through wizards (`05-bridge.md` §7).

The planner is pure — `writes()` takes the answers and returns frames plus the
lines a reader needs — so the ordering rules are testable without a terminal.
The wizards themselves live in `app.py`, which owns the modals.

`03-config.md` rule 9 rejects a `nvs:` reference whose key is missing, so every
`secret.set` in the plan precedes the `config.set` that names its key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: `04` §4.10 / `03` §Secrets: NVS keys are `[A-Za-z0-9_]{1,15}`.
WIFI_KEY = "wifi_psk"
LLM_KEY = "llm_api_key"

_HOST = re.compile(r"^[a-z0-9.:-]+$")
_ID = re.compile(r"[^a-z0-9_-]+")


def provider_id(base_url: str) -> str:
    """`https://api.openai.com/v1` → `openai`.

    Derived rather than asked: `03` requires a provider id, but a reader who
    pastes a URL has already named the provider.
    """
    host = base_url.split("://", 1)[-1].split("/", 1)[0].lower()
    host = host.split(":", 1)[0]
    if not _HOST.match(host):
        return "custom"
    labels = [label for label in host.split(".") if label not in ("api", "www")]
    head = labels[0] if labels else "custom"
    # A bare IP names no provider, and `id` is a label a reader will see again.
    if head.replace("-", "").isdigit():
        return "custom"
    return (_ID.sub("-", head).strip("-") or "custom")[:16]


@dataclass(frozen=True)
class Answers:
    """What the wizards collected. Empty strings mean "leave it alone"."""

    ssid: str
    password: str
    base_url: str
    api_key: str
    model: str


def writes(answers: Answers) -> list[tuple[str, dict[str, object]]]:
    """The frames to send, in order: secrets first, then the arrays naming them.

    Whole arrays, not `wifi.networks.0.ssid`: `04` §4.9 does not promise that
    setting a missing index creates the array, and a fresh device has none.

    A step is dropped when its secret is missing, because `03` §Full schema makes
    `password_ref` and `api_key_ref` required: writing the array without the key
    it names is exactly what rule 9 rejects.
    """
    secrets: list[tuple[str, dict[str, object]]] = []
    config: list[tuple[str, dict[str, object]]] = []
    if answers.password:
        secrets.append(("secret", {"key": WIFI_KEY, "value": answers.password}))
    if answers.api_key:
        secrets.append(("secret", {"key": LLM_KEY, "value": answers.api_key}))
    if answers.ssid and answers.password:
        config.append(
            (
                "config",
                {
                    "wifi.networks": [
                        {"ssid": answers.ssid, "password_ref": f"nvs:{WIFI_KEY}"}
                    ]
                },
            )
        )
    if answers.base_url and answers.model and answers.api_key:
        provider = {
            "id": provider_id(answers.base_url),
            "base_url": answers.base_url,
            "model": answers.model,
            "api_key_ref": f"nvs:{LLM_KEY}",
        }
        # `03` §Full schema: `model` is a per-provider string and there is no
        # `llm.model` key, so one write, not two.
        config.append(("config", {"llm.providers": [provider]}))
    return secrets + config


def narration(answers: Answers, current: dict[str, object]) -> list[str]:
    """What the confirm step says, naming what each write replaces."""
    lines: list[str] = []
    networks = current.get("wifi.networks")
    # Same gates as `writes`: narrating a step that will not be sent is a lie.
    if answers.ssid and answers.password:
        if isinstance(networks, list):
            lines.append(f"replaces wifi.networks ({len(networks)} entries → 1)")
        else:
            lines.append("creates wifi.networks (1 entry)")
    providers = current.get("llm.providers")
    if answers.base_url and answers.model and answers.api_key:
        if isinstance(providers, list):
            lines.append(f"replaces llm.providers ({len(providers)} entries → 1)")
        else:
            lines.append("creates llm.providers (1 entry)")
        lines.append(f"provider model {answers.model}")
    if answers.password:
        lines.append(f"stores nvs:{WIFI_KEY}")
    if answers.api_key:
        lines.append(f"stores nvs:{LLM_KEY}")
    return lines

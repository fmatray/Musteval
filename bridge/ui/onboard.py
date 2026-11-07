"""`/onboard`: configure a fresh device through one wizard (`05-bridge.md` §7).

The planner is pure — `writes()` takes the answers and returns frames plus the
lines a reader needs — so the ordering rules are testable without a terminal.
The wizard itself lives in `modals.py`; `app.py` owns the scan and the model
fetch, because both need the link and a thread.

Three domains, because a fresh device needs three things before it can answer a
prompt: a network to reach the provider over, a provider to answer, and a clock
for `https` to work. `ntp` is not a nicety here — `clock_sync` failing leaves the
certificate check unable to validate a date.

`03-config.md` rule 9 rejects a `nvs:` reference whose key is missing, so every
`secret.set` in the plan precedes the `config.set` that names its key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bridge import providers

#: `04` §4.10 / `03` §Secrets: NVS keys are `[A-Za-z0-9_]{1,15}`.
WIFI_KEY = "wifi_psk"
LLM_KEY = "llm_api_key"

#: The `auth` words `/onboard` writes a network for. `04` §4.24: the station thresholds
#: at `WIFI_AUTH_WPA2_PSK`, so `open`, `wep` and `wpa` are listed by a scan and cannot
#: associate; an enterprise SSID needs credentials no `wifi.networks[]` entry carries.
#: Writing one of those would look like success and leave the device offline, so the
#: wizard says why instead. An empty `auth` is a hand-typed SSID nobody scanned — the
#: reader's assertion, taken at face value.
JOINABLE_AUTH = frozenset({"", "wpa2", "wpa3"})


_HOST = re.compile(r"^[a-z0-9.:-]+$")


def _slug(text: str) -> str:
    return (re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-") or "custom")[:16]


def provider_id(base_url: str, provider: providers.Provider | None = None) -> str:
    """The `llm.providers[].id` for a pasted URL, or the catalog entry's own id.

    `03-config.md` requires an id. A reader who picks a catalog row has already named
    the provider, so its id is reused; one who pastes a URL has named it in the host,
    which is the only thing left to derive from.
    """
    if provider is not None and provider.base_url == base_url:
        return provider.id
    host = base_url.split("://", 1)[-1].split("/", 1)[0].lower().split(":", 1)[0]
    # A pasted string that is not a host names no provider, and `id` is a label the
    # reader will see again, so it says `custom` rather than repeating the typo.
    if not _HOST.match(host):
        return "custom"
    labels = [label for label in host.split(".") if label not in ("api", "www")]
    head = labels[0] if labels else "custom"
    if head.replace("-", "").isdigit():
        return "custom"
    return _slug(head)


@dataclass(frozen=True)
class Answers:
    """What the wizard collected. Empty strings mean "leave it alone"."""

    ssid: str
    password: str
    base_url: str
    api_key: str
    model: str
    auth: str = ""
    provider: str = ""
    token_param: str = ""
    extra_body: dict[str, object] = field(default_factory=dict)
    timezone: str = ""
    ntp_server: str = ""


def needs_model(answers: Answers) -> bool:
    """A provider the reader started filling but left without a model.

    `03-config.md` makes `model` required, so there is nothing to write. Saying so is
    the difference between a form that refused and a form that silently did nothing:
    an empty model dropdown reads as "not chosen yet", not as "this domain will be
    skipped".
    """
    return bool(answers.base_url and answers.api_key and not answers.model)


def network_writable(answers: Answers) -> bool:
    """Whether this network can be written at all.

    `auth` is carried through for exactly this: the reader picked a row off the scan,
    and the form must not accept a password for a network the station will refuse.
    """
    return bool(answers.ssid and answers.password) and answers.auth in JOINABLE_AUTH


def writes(answers: Answers) -> list[tuple[str, dict[str, object]]]:
    """The frames to send, in order: secrets first, then the values naming them.

    Whole arrays, not `wifi.networks.0.ssid`: `04` §4.9 creates a missing object
    parent but never an array or an index, and a fresh device has no array.

    A step is dropped when its secret is missing, because `03-config.md` §Full schema
    makes `password_ref` and `api_key_ref` required: writing the array without the key
    it names is exactly what rule 9 rejects.
    """
    secrets: list[tuple[str, dict[str, object]]] = []
    config: list[tuple[str, dict[str, object]]] = []
    if answers.password and network_writable(answers):
        secrets.append(("secret", {"key": WIFI_KEY, "value": answers.password}))
    if answers.api_key:
        secrets.append(("secret", {"key": LLM_KEY, "value": answers.api_key}))
    if network_writable(answers):
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
            "id": answers.provider or "custom",
            "base_url": answers.base_url,
            "model": answers.model,
            "api_key_ref": f"nvs:{LLM_KEY}",
        }
        # `03-config.md` §Provider compatibility names `token_param` per provider, and
        # `extra_body` carries the thinking level. Both are optional; writing a member
        # the provider rejects turns every call into a 400.
        if answers.token_param:
            provider["token_param"] = answers.token_param
        if answers.extra_body:
            provider["extra_body"] = dict(answers.extra_body)
        config.append(("config", {"llm.providers": [provider]}))
    if answers.timezone:
        # The pair, from one region choice: the TZ string the firmware applies with
        # `setenv`, and the pool server that keeps the clock honest. `ntp.timezone`
        # creates the `ntp` block on a device that has none (`04` §4.9).
        config.append(("config", {"ntp.timezone": answers.timezone}))
        if answers.ntp_server:
            config.append(("config", {"ntp.servers": [answers.ntp_server]}))
    return secrets + config


def narration(answers: Answers, current: dict[str, object]) -> list[str]:
    """What the confirm step says, naming what each write replaces."""
    lines: list[str] = []
    networks = current.get("wifi.networks")
    if answers.ssid and answers.auth not in JOINABLE_AUTH:
        lines.append(
            f"skips {answers.ssid}: the station cannot join {answers.auth} "
            f"(`04` §4.24)"
        )
    # Same gates as `writes`: narrating a step that will not be sent is a lie.
    if network_writable(answers):
        if isinstance(networks, list):
            lines.append(f"replaces wifi.networks ({len(networks)} entries → 1)")
        else:
            lines.append("creates wifi.networks (1 entry)")
    providers_now = current.get("llm.providers")
    if needs_model(answers):
        lines.append("skips llm.providers: no model, and the schema requires one")
    if answers.base_url and answers.model and answers.api_key:
        if isinstance(providers_now, list):
            lines.append(
                f"replaces llm.providers ({len(providers_now)} entries → 1)"
            )
        else:
            lines.append("creates llm.providers (1 entry)")
        lines.append(f"provider model {answers.model}")
        if answers.extra_body:
            lines.append(f"thinking: {answers.extra_body}")
    if answers.timezone:
        lines.append(f"sets ntp.timezone {answers.timezone}")
        if answers.ntp_server:
            ntp = current.get("ntp") or {}
            existing = ntp.get("servers") if isinstance(ntp, dict) else None
            if isinstance(existing, list) and existing:
                lines.append(f"replaces ntp.servers ({len(existing)} → 1)")
            else:
                lines.append(f"sets ntp.servers {answers.ntp_server}")
    if answers.password and network_writable(answers):
        lines.append(f"stores nvs:{WIFI_KEY}")
    if answers.api_key:
        lines.append(f"stores nvs:{LLM_KEY}")
    return lines

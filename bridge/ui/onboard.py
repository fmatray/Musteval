"""`/onboard`: configure a fresh device through three tested steps (`05-bridge.md` §7).

The planner is pure — the `writes_*` functions take the answers and return frames,
and the `verdict_*` functions turn a device reading into one line — so both are
testable without a terminal. The wizard lives in `modals.py`; `app.py` owns the scan,
the model fetch and the `reload`, because each needs the link and a thread.

Each step is tested before the next one is asked for, and the test is the device
answering, not the bridge assuming: the network step writes and then reads `status`
for a joined SSID `reload` was told to use, the provider step fetches the model list
before any write can name a model from it, and the model step checks the chosen name
is one the provider actually published.

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


@dataclass
class Answers:
    """What the wizard collected. Empty strings mean "leave it alone".

    Not frozen: the wizard fills it one step at a time, because each step's write is
    sent and tested before the next step is asked for. A frozen value would force the
    app to rebuild the whole answer set at every step, which is the single-form shape
    this replaced.
    """

    ssid: str
    password: str
    base_url: str
    api_key: str
    model: str
    auth: str = ""
    provider: str = ""
    token_param: str = ""
    extra_body: dict[str, object] = field(default_factory=dict)
    #: The model list step 2 fetched, and the efforts each name publishes for itself.
    #: The second is empty for every provider whose list says nothing about levels.
    model_list: list[str] = field(default_factory=list)
    model_efforts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    timezone: str = ""
    ntp_server: str = ""
    #: True when the reader pressed "keep what is there" on a step. The step sends no
    #: frame and its verdict is not claimed: a pass proves nothing, and saying so is
    #: cheaper than making the reader compare two screens to find out.
    skip_network: bool = False
    skip_provider: bool = False
    skip_model: bool = False


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
    if answers.skip_network:
        return False
    return bool(answers.ssid and answers.password) and answers.auth in JOINABLE_AUTH


def writes_network(answers: Answers) -> list[tuple[str, dict[str, object]]]:
    """Step 1's frames: the passphrase, then the network array naming it.

    Whole arrays, not `wifi.networks.0.ssid`: `04` §4.9 creates a missing object
    parent but never an array or an index, and a fresh device has no array. The secret
    precedes the reference because `03-config.md` rule 9 rejects a `nvs:` reference
    whose key is missing.
    """
    if not network_writable(answers):
        return []
    plan: list[tuple[str, dict[str, object]]] = []
    if answers.password:
        plan.append(("secret", {"key": WIFI_KEY, "value": answers.password}))
    plan.append(
        (
            "config",
            {
                "wifi.networks": [
                    {"ssid": answers.ssid, "password_ref": f"nvs:{WIFI_KEY}"}
                ]
            },
        )
    )
    return plan


def writes_provider(answers: Answers) -> list[tuple[str, dict[str, object]]]:
    """Step 2's frames: the API key, then the provider array naming it.

    A model is required here because `03-config.md` makes it required in the schema:
    there is no half-configured provider to write, and a device given one refuses the
    document. The step asks for the provider and the key, then for the model from the
    list that key unlocks.
    """
    # A passed model step means the stored provider already names the stored model,
    # so the entry step 3 would write is the entry already there: a no-op write is
    # still a flash write and still a config document the device must re-validate.
    if answers.skip_model:
        return []
    if not answers.model:
        return []
    if not answers.base_url:
        return []
    # `api_key_ref` is required, and a `nvs:` key that was never written is a
    # document the device rejects — `03-config.md` rule 9. A passed step is the one
    # exception, because passing asserts the key is already there.
    if not (answers.api_key or answers.skip_provider):
        return []
    provider: dict[str, object] = {
        "id": answers.provider or "custom",
        "base_url": answers.base_url,
        "model": answers.model,
        "api_key_ref": f"nvs:{LLM_KEY}",
    }
    # `03-config.md` §Provider compatibility names `token_param` per provider, and
    # `extra_body` carries the thinking level. Both are optional; writing a member the
    # provider rejects turns every call into a 400.
    if not answers.skip_provider and answers.token_param:
        provider["token_param"] = answers.token_param
    if answers.extra_body:
        provider["extra_body"] = dict(answers.extra_body)
    plan: list[tuple[str, dict[str, object]]] = []
    # The key is only written when one was typed: a passed provider step has none to
    # offer, and re-writing the secret would demand a value the reader did not enter.
    if answers.api_key and not answers.skip_provider:
        plan.append(("secret", {"key": LLM_KEY, "value": answers.api_key}))
    plan.append(("config", {"llm.providers": [provider]}))
    return plan


def writes_time(answers: Answers) -> list[tuple[str, dict[str, object]]]:
    """Step 1's second half: the TZ string and, when a region was chosen, its pool.

    The pair comes from one region choice. `ntp.timezone` creates the `ntp` block on a
    device that has none (`04` §4.9). The server is written only when the TZ string
    matches a region in the table: an arbitrary POSIX string names no pool, and
    guessing one would replace a working server with a European one.
    """
    if answers.skip_network or not answers.timezone:
        return []
    plan: list[tuple[str, dict[str, object]]] = [
        ("config", {"ntp.timezone": answers.timezone})
    ]
    if answers.ntp_server:
        plan.append(("config", {"ntp.servers": [answers.ntp_server]}))
    return plan


def writes(answers: Answers) -> list[tuple[str, dict[str, object]]]:
    """Every step's frames in order. The wizard sends one step at a time; this is the
    whole plan, which is what the tests describe. Step 1 is `writes_network` then
    `writes_time`: one reload applies both, so they travel together."""
    return (
        writes_network(answers) + writes_time(answers) + writes_provider(answers)
    )


def thinking_writes(
    level: str, providers_now: object
) -> "tuple[list[tuple[str, dict[str, object]]] | None, str]":
    """`/thinking <level>`: the frames that set one entry's level, or why not.

    A read-modify-write of `llm.providers[0].extra_body`, so the path is the whole
    array: `04` §4.9 will not create a missing index, and writing the array preserves
    every other member of the entry — `05` §7.2's whole point is that a level change
    is not a reconfiguration.

    Returns `(None, refusal)` when the level cannot be written: an unknown word, a
    provider the catalog does not describe, or a provider whose row takes no level.
    All three are refused **on the host**, because the device accepts any flat member
    and turns every call into a 400, and a 400 on the reader's next prompt is the
    worst possible way to learn that.
    """
    chain = providers_now if isinstance(providers_now, list) else []
    if not chain or not isinstance(chain[0], dict):
        return None, "no provider is configured; run /onboard first"
    entry_now = chain[0]
    provider_id_ = str(entry_now.get("id") or "")
    if not provider_id_:
        return None, "the stored provider has no id; run /onboard first"
    entry = providers.find(provider_id_)
    if entry is None:
        return None, f"provider {provider_id_!r} is not in this bridge's catalog"
    if not entry.thinking_levels:
        return None, f"{provider_id_} takes no thinking level; nothing to set"
    if level not in entry.thinking_levels:
        # "offered", not "takes": Ollama's rows list only the names *every* model of
        # that provider accepts, so `max` is a level a `glm` model takes and its row
        # still does not offer it (`05` §7.1). The message must not claim the model
        # rejects what it accepts.
        return None, (
            f"{level!r} is not offered for {provider_id_}; "
            f"choose from {', '.join(entry.thinking_levels)}"
        )
    member = entry.without_level(entry_now.get("extra_body"))
    member.update(entry.extra_body(level))
    updated = dict(entry_now)
    # `05` §7.2: `none` writes `{"reasoning_effort":"none"}` rather than deleting the
    # member, so a validated level always yields something to write.
    updated["extra_body"] = member
    return [("config", {"llm.providers": [updated] + list(chain[1:])})], ""


def thinking_line(level: str, providers_now: object) -> str:
    """What `/thinking` says for a provider it can describe: the level, or why not.

    The levels are the catalog's, so the reader learns what the provider accepts
    without being shown a list the device would refuse.
    """
    chain = providers_now if isinstance(providers_now, list) else []
    entry_now = chain[0] if chain and isinstance(chain[0], dict) else {}
    provider_id_ = str(entry_now.get("id") or "")
    if not provider_id_:
        return "thinking: no provider is configured; run /onboard first"
    entry = providers.find(provider_id_)
    if entry is None:
        return f"thinking: {provider_id_} is not in this bridge's catalog"
    if not entry.thinking_levels:
        return f"thinking: {provider_id_} takes no thinking level"
    level = entry.level_of(entry_now.get("extra_body"))
    # An absent member is not "off" where the surface has no off switch: Ollama's
    # models think at their own default, which several of them set to their highest
    # effort. Saying "none" there would claim a setting the entry does not have.
    if level == providers.THINKING_NONE and "none" not in entry.thinking_levels:
        level = "the provider's default"
    return (
        f"thinking: {level}"
        f" (choose from {', '.join(entry.thinking_levels)})"
    )


def verdict_network(status: dict[str, object] | None) -> str:
    """What the device says about the network the step just wrote.

    Read from `status`, not from the write's `ok`: `config.set` answering ok only
    means the document changed. Whether the station joined is a different fact, and it
    is the one the reader needs before typing an API key.
    """
    wifi = (status or {}).get("wifi") or {}
    if not isinstance(wifi, dict):
        return "network: the device did not report a wifi block"
    state = str(wifi.get("state") or "off")
    ssid = wifi.get("ssid")
    if state == "connected":
        return f"network: joined {ssid or '(no ssid reported)'}"
    if state == "failed":
        return (
            "network: the station did not join; check the passphrase, or that the "
            "router is in range and above WPA2 (`04` §4.24)"
        )
    if state == "connecting":
        return f"network: still connecting to {ssid or 'the configured ssid'}"
    return "network: the radio is off; the device may be in safe mode"


def verdict_provider(models: list[str], answers: Answers) -> str:
    """What the fetch proved about the provider and its key.

    An empty list is not a failure to name: the three reasons are different problems
    and the reader can only fix the one they were told about. A provider with no
    `models_url` has no list to fetch; a provider that refuses the key answers 401;
    and a provider whose host is unreachable is offline.
    """
    entry = providers.find(answers.provider) if answers.provider else None
    if not answers.base_url:
        return "provider: no base url"
    if not answers.api_key:
        return "provider: no key typed, so the list cannot be checked"
    if entry is None or not entry.models_url:
        return (
            f"provider: {answers.provider or 'custom'} publishes no model list; "
            "type the model name"
        )
    if not models:
        return (
            f"provider: {answers.provider} returned no models; the key is refused or "
            "the host is unreachable"
        )
    return f"provider: {len(models)} models"


def verdict_model(models: list[str], answers: Answers) -> str:
    """Whether the chosen model is one the provider just published.

    A name absent from a non-empty list is a typo the device would answer with a 404
    on the first prompt, which is exactly the failure this step exists to catch before
    the write. When the provider published nothing there is nothing to compare, and a
    typed name is taken at face value.
    """
    if not answers.model:
        return "model: none chosen"
    if not models:
        return f"model: {answers.model} (unchecked; the provider listed none)"
    if answers.model in models:
        return f"model: {answers.model}"
    return (
        f"model: {answers.model} is not in the provider's list of {len(models)}; "
        "pick one from the dropdown"
    )


def narration(
    answers: Answers, current: dict[str, object], step: str = "all"
) -> list[str]:
    """What the confirm step says, naming what each write replaces.

    `step` is `"network"`, `"provider"` or `"all"`. The wizard narrates one step at a
    time because each is written and tested before the next is asked for, so a
    confirmation that described all three would promise writes that have not been
    decided yet. The zone belongs to the network step: one `reload` applies both.
    """
    lines: list[str] = []
    networks = current.get("wifi.networks")
    providers_now = current.get("llm.providers")
    ntp = current.get("ntp")
    servers = ntp.get("servers") if isinstance(ntp, dict) else None

    if step in ("network", "all"):
        if answers.ssid and answers.auth not in JOINABLE_AUTH:
            lines.append(
                f"skips {answers.ssid}: the station cannot join {answers.auth} "
                f"(`04` §4.24)"
            )
        # Same gate as `writes_network`: narrating a step that will not be sent is a lie.
        if network_writable(answers):
            if isinstance(networks, list):
                lines.append(f"replaces wifi.networks ({len(networks)} entries → 1)")
            else:
                lines.append("creates wifi.networks (1 entry)")
            lines.append(f"stores nvs:{WIFI_KEY}")

    if step in ("provider", "all"):
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
                # The level, not the raw member: `{"reasoning_effort":"high"}` is the
                # catalog's encoding, and the reader chose a word.
                level = providers.find(answers.provider)
                lines.append(
                    f"thinking: {level.level_of(answers.extra_body)}"
                    if level is not None
                    else f"thinking: {answers.extra_body}"
                )
            lines.append(f"stores nvs:{LLM_KEY}")

    if step in ("network", "all") and answers.timezone:
        lines.append(f"sets ntp.timezone {answers.timezone}")
        if answers.ntp_server:
            if isinstance(servers, list) and servers:
                lines.append(f"replaces ntp.servers ({len(servers)} → 1)")
            else:
                lines.append(f"sets ntp.servers {answers.ntp_server}")
    return lines


def verdict_time(status: dict[str, object] | None, wanted: str) -> str:
    """What the device says about the zone just written.

    `status.time.tz` is the TZ string the device is actually holding, which is the
    only readable consequence of `ntp.timezone`: `setenv("TZ")` is applied at boot and
    by `reload`, and nothing else reports it. A device that reran its boot sequence
    and still holds the old string did not take the write, whatever the reply said.
    """
    block = (status or {}).get("time") or {}
    if not isinstance(block, dict):
        return "time: the device did not report a time block"
    tz = block.get("tz")
    if wanted and tz == wanted:
        suffix = "" if block.get("synced") else "; the clock has not synced yet"
        return f"time: zone {tz} applied{suffix}"
    if tz:
        return f"time: zone still {tz}; the write did not apply"
    return "time: the device reports no zone"

"""The LLM provider catalog `/onboard` offers (`05-bridge.md` §7.1).

Data, not behaviour: every entry is a name, a `base_url`, a model-list URL and the
`extra_body` member its models take a thinking level in. The device never sees any
of it — a provider reaches the firmware only as the `base_url`, `model`,
`token_param` and `extra_body` fields of `llm.providers[]` (`03-config.md`).

L2 per `09-bridge-architecture.md` §2. The two things that would make it untestable
arrive as arguments: the model fetch is a `fetch(url, key) -> bytes` callable, and
the custom-provider file is a path. Nothing here opens a socket or a terminal.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

#: `05-bridge.md` §7.1: `none` means the provider has no thinking member to write.
THINKING_NONE = "none"
THINKING_REASONING_EFFORT = "reasoning_effort"
THINKING_BUDGET = "thinking_budget"

#: The level names a picker offers, and the token budget each stands for.
THINKING_LEVELS = ("none", "low", "medium", "high")
_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}

#: The levels Ollama's request surface can express for any of its thinking models
#: (`05` §7.2). `none` and `medium` are not among them: Ollama resolves an
#: unsupported name to the model's own default, and several models default to their
#: highest effort, so `none` would make thinking more intense rather than off.
OLLAMA_LEVELS = ("low", "high")

#: The levels Mistral's documented reasoning models have in common
#: (`docs.mistral.ai/inference/conversations/reasoning`). `mistral-large-4-0` takes
#: `low`/`medium`/`high`, the small and medium models take `high` and `none`, and
#: `zai-glm-5-3` takes `low`/`high`/`max` and rejects `none`. A name outside the
#: intersection is a 400 on some model the reader may have picked.
MISTRAL_LEVELS = ("low", "high")

#: `03-config.md` §Full schema: `llm.providers[].extra_body` is at most four flat
#: members, so a provider's own extra members must leave room for a level.
EXTRA_BODY_MAX = 4


@dataclass(frozen=True)
class Provider:
    """One catalog entry. `models_url` empty means the provider publishes no list
    this bridge can read, so the model is typed."""

    id: str
    label: str
    base_url: str
    models_url: str = ""
    thinking: str = THINKING_NONE
    needs_key: bool = True
    #: The levels this provider's request surface can express, when that is narrower
    #: than `THINKING_LEVELS`. `None` means the whole tuple. Ollama names its own
    #: values per model and resolves an unsupported name to the model default, so a
    #: level it cannot express is not a weaker setting — it is a different one.
    levels: tuple[str, ...] | None = None

    @property
    def thinking_levels(self) -> tuple[str, ...]:
        """The levels to offer. A provider with no thinking member offers none, so
        the picker is absent rather than disabled — a level written into a provider
        that rejects the member turns every call into a 400."""
        if self.levels is not None:
            return self.levels
        return THINKING_LEVELS if self.thinking != THINKING_NONE else ()

    def extra_body(self, level: str) -> dict[str, object]:
        """The `extra_body` for `level`. An unusable level is no member at all, and
        `none` is an explicit "no thinking" only where the provider takes one.

        `thinking_budget` has no token form of "off", so its `none` is the absence of
        the member — which is what the caller gets, an empty dict.

        `reasoning_effort` is a free-form name, so *any* word the device already
        stores is expressible even when this row does not offer it: Ollama names its
        levels per model, so `max` on a `glm` model is a real setting that a
        re-run of the wizard has to write back rather than overwrite. What a row
        offers (`thinking_levels`) and what it can express are therefore two
        questions, and only the first is a picker's business."""
        if not level:
            return {}
        if self.thinking == THINKING_REASONING_EFFORT:
            # `none` is only a member where the provider has an off switch; on
            # Ollama's surface it is unexpressible, so it stays absent (`05` §7.2).
            if level == THINKING_NONE and THINKING_NONE not in self.thinking_levels:
                return {}
            return {"reasoning_effort": level}
        if self.thinking == THINKING_BUDGET:
            return {"thinking_budget": _BUDGETS[level]} if level in _BUDGETS else {}
        return {}

    def level_of(self, extra_body: object) -> str:
        """The level a stored `extra_body` names, or `none`.

        The inverse of `extra_body`, used to seed the wizard's picker from what the
        device already holds (`05` §13.6). A name this row does not offer is still
        reported as itself rather than as `none`: the member is the reader's, and
        answering `none` for a stored `max` would be a silent downgrade the wizard
        then writes back.
        """
        if not isinstance(extra_body, dict):
            return THINKING_NONE
        if self.thinking == THINKING_REASONING_EFFORT:
            # Reported as stored, not as offered: `max` on an Ollama model is a
            # setting this row cannot list but must not deny.
            found = extra_body.get("reasoning_effort")
            return found if isinstance(found, str) and found else THINKING_NONE
        if self.thinking == THINKING_BUDGET:
            budget = extra_body.get("thinking_budget")
            for level, tokens in _BUDGETS.items():
                if budget == tokens:
                    return level
        return THINKING_NONE

    def without_level(self, extra_body: object) -> dict[str, object]:
        """`extra_body` with this provider's level member removed, others kept.

        `/thinking` writes the whole array (`05` §7.2), so every other member the
        reader's entry carries has to survive the write."""
        keep = dict(extra_body) if isinstance(extra_body, dict) else {}
        for member in ("reasoning_effort", "thinking_budget"):
            keep.pop(member, None)
        return keep


#: The providers `03-config.md` §Provider compatibility documents, in that table's
#: order. That table is the frozen statement of what the firmware can talk to, so this
#: list is a transcription of it rather than a second opinion: adding a provider the
#: device can already reach is a row here, not a code change.
#:
#: `token_param` is not a catalog field: it belongs to the provider entry written to
#: the device, and the table names it per provider. The wizard writes it from
#: `TOKEN_PARAM` below.
BUILTIN: tuple[Provider, ...] = (
    Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        models_url="https://api.openai.com/v1/models",
        # The table's own note: Chat Completions allows function calling only at
        # `reasoning_effort: none`, so a tool call at another level would 400.
        thinking=THINKING_NONE,
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models_url="https://openrouter.ai/api/v1/models",
        thinking=THINKING_REASONING_EFFORT,
    ),
    Provider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        models_url="https://api.groq.com/openai/v1/models",
        thinking=THINKING_REASONING_EFFORT,
    ),
    Provider(
        id="ollama-cloud",
        label="Ollama Cloud",
        base_url="https://ollama.com/v1",
        # Ollama's compatibility layer serves `/v1/models` in the OpenAI shape, so
        # the list is readable here and the model no longer has to be typed. It
        # needs no key: names are public (`docs.ollama.com/cloud`).
        models_url="https://ollama.com/v1/models",
        # `docs.ollama.com/api/openai-compatibility` lists `reasoning_effort` as a
        # supported field, and `/api/show` publishes each model's own `values`.
        # The catalog cannot know which model the reader will pick, so it offers
        # the levels every published thinking model takes. A model with boolean
        # controls maps `none` to `false` and any named level to `true`.
        #
        # `low` and `high` only. The names are model-defined and an unsupported one
        # is not clamped, it resolves to the model's default — which is often the
        # highest (`glm-5.3-flash` defaults to `max`). So `none` would *maximise*
        # thinking, and `medium` would too; both are unexpressible here. `low` and
        # `high` are the two names every published thinking model accepts, and on a
        # boolean model both mean `true` (`05` §7.2).
        thinking=THINKING_REASONING_EFFORT,
        levels=OLLAMA_LEVELS,
    ),
    Provider(
        id="ollama",
        label="Ollama (local)",
        base_url="http://127.0.0.1:11434/v1",
        models_url="http://127.0.0.1:11434/v1/models",
        thinking=THINKING_REASONING_EFFORT,
        levels=OLLAMA_LEVELS,
        needs_key=False,
    ),
    Provider(
        id="vllm",
        label="vLLM (self-hosted)",
        base_url="http://127.0.0.1:8000/v1",
        models_url="http://127.0.0.1:8000/v1/models",
        needs_key=False,
    ),
    Provider(
        id="anthropic",
        label="Anthropic (compat)",
        base_url="https://api.anthropic.com/v1",
        models_url="https://api.anthropic.com/v1/models",
        thinking=THINKING_BUDGET,
    ),
    Provider(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models_url="https://generativelanguage.googleapis.com/v1beta/openai/models",
        thinking=THINKING_REASONING_EFFORT,
    ),
    Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        models_url="https://api.mistral.ai/v1/models",
        # `docs.mistral.ai/inference/conversations/reasoning` publishes the accepted
        # `reasoning_effort` names per model, and they differ:
        # `mistral-large-4-0` takes `low`/`medium`/`high` and rejects
        # `none`/`minimal`/`xhigh`/`max` outright ("not aliases"); the small and
        # medium models take `high` and `none`; the `zai-glm-5-3` models take
        # `low`/`high`/`max` and *reject* `none`. `low` and `high` are the only two
        # accepted across all three families, so they are the offer set — the same
        # rule as Ollama's row. `medium` is a real setting on Large 4 only, and a
        # stored member still round-trips (`level_of` reports it as stored).
        thinking=THINKING_REASONING_EFFORT,
        levels=MISTRAL_LEVELS,
    ),
    Provider(
        id="custom",
        label="Custom (OpenAI-compatible)",
        base_url="",
        thinking=THINKING_NONE,
    ),
)

#: `03-config.md` §Provider compatibility: the providers whose table row says to set
#: `max_completion_tokens`. Every other entry keeps the firmware default.
TOKEN_PARAM: dict[str, str] = {
    "openai": "max_completion_tokens",
    "groq": "max_completion_tokens",
}


def custom_path() -> str:
    """`~/.config/musteval/providers.json`, honouring `XDG_CONFIG_HOME`."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "musteval", "providers.json")


def _entry(raw: object) -> tuple[Provider | None, str]:
    """One row of the custom file, or the reason it was skipped.

    A half-entry cannot be written to the device, and dropping it silently would look
    like the file was never read at all.
    """
    if not isinstance(raw, dict):
        return None, "not an object"
    missing = [k for k in ("id", "base_url", "label") if not raw.get(k)]
    if missing:
        return None, f"missing {', '.join(missing)}"
    thinking = raw.get("thinking", THINKING_NONE)
    if thinking not in (THINKING_NONE, THINKING_REASONING_EFFORT, THINKING_BUDGET):
        return None, f"unknown thinking {thinking!r}"
    return (
        Provider(
            id=str(raw["id"])[:16],
            label=str(raw["label"]),
            base_url=str(raw["base_url"]),
            models_url=str(raw.get("models_url") or ""),
            thinking=str(thinking),
            needs_key=bool(raw.get("needs_key", True)),
        ),
        "",
    )


def load_custom(path: str | None = None) -> tuple[list[Provider], list[str]]:
    """The custom providers and the complaints, if any.

    Returns `([], [])` when the file is absent — the common case, and not a fault. A
    malformed file reports one line naming the path and the parse error, and the
    built-ins are used alone: a broken local file must never cost the reader the
    ability to onboard a device.
    """
    path = path or custom_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as err:
        return [], [f"providers: {path}: {err}"]

    rows = raw.get("providers") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return [], [f"providers: {path}: expected a list or {{\"providers\": [...]}}"]

    out: list[Provider] = []
    notes: list[str] = []
    for index, row in enumerate(rows):
        entry, why = _entry(row)
        if entry is None:
            notes.append(f"providers: {path}: entry {index} skipped: {why}")
        else:
            out.append(entry)
    return out, notes


def catalog(path: str | None = None) -> tuple[list[Provider], list[str]]:
    """Built-ins plus the custom file, a custom entry replacing its built-in id.

    Replacement is by `id`, which is how a reader corrects a stale `base_url` without
    patching the bridge.
    """
    custom, notes = load_custom(path)
    merged: dict[str, Provider] = {p.id: p for p in BUILTIN}
    order: list[str] = [p.id for p in BUILTIN]
    for entry in custom:
        if entry.id not in merged:
            order.append(entry.id)
        merged[entry.id] = entry
    return [merged[i] for i in order], notes


def find(provider_id: str, path: str | None = None) -> Provider | None:
    """One entry by id, or None. A miss is not an error: the id may come from a
    device configured before this catalog existed."""
    for entry in catalog(path)[0]:
        if entry.id == provider_id:
            return entry
    return None


def parse_models(payload: bytes) -> list[str]:
    """The model names in a provider's list response.

    Both shapes in use are accepted: OpenAI's `{"data":[{"id":...}]}` and Ollama's
    `{"models":[{"name":...}]}`. Anything else is an empty list rather than an
    exception — a provider answering 200 with an unfamiliar document is not broken,
    it just has no list this bridge can read.
    """
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []

    rows = doc.get("data")
    key = "id"
    if not isinstance(rows, list):
        rows = doc.get("models")
        key = "name"
    if not isinstance(rows, list):
        return []

    names: list[str] = []
    for row in rows:
        name = row.get(key) if isinstance(row, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


#: `05-bridge.md` §7.1: region → (POSIX TZ, ntppool zone). The TZ string is what the
#: firmware applies with `setenv("TZ", ...)`, which knows no IANA name. The zone is
#: the continental one, because ntppool documents that a country zone may hold one or
#: two servers or none.
REGIONS: tuple[tuple[str, str, str], ...] = (
    ("Europe / Western", "CET-1CEST,M3.5.0,M10.5.0/3", "europe"),
    ("Europe / UK & Ireland", "GMT0BST,M3.5.0/1,M10.5.0", "europe"),
    ("Europe / Eastern", "EET-2EEST,M3.5.0/3,M10.5.0/4", "europe"),
    ("US / Eastern", "EST5EDT,M3.2.0,M11.1.0", "north-america"),
    ("US / Central", "CST6CDT,M3.2.0,M11.1.0", "north-america"),
    ("US / Mountain", "MST7MDT,M3.2.0,M11.1.0", "north-america"),
    ("US / Pacific", "PST8PDT,M3.2.0,M11.1.0", "north-america"),
    ("Asia / Japan", "JST-9", "asia"),
    ("Asia / China", "CST-8", "asia"),
    ("Asia / India", "IST-5:30", "asia"),
    ("Australia / Eastern", "AEST-10AEDT,M10.1.0,M4.1.0/3", "oceania"),
    ("UTC", "UTC0", "europe"),
)


def region_names() -> list[str]:
    """What the timezone dropdown shows."""
    return [name for name, _, _ in REGIONS]


def region_for_timezone(tz: str) -> str:
    """The region name whose TZ string is `tz`, or `""` when the table has none.

    The wizard pre-fills the region dropdown from `ntp.timezone`, which holds the TZ
    string, not a region name. Comparing the two directly always missed, so the
    dropdown inserted the raw string as a row of its own — which is how
    `CET-1CEST,M3.5.0,M10.5.0/3` was shown back to a reader who expected "Europe /
    Western". A string the table does not list stays empty here and is offered through
    the manual field instead.
    """
    for name, value, _ in REGIONS:
        if value == tz:
            return name
    return ""


def timezone_for(region: str) -> tuple[str, str]:
    """`(ntp.timezone, ntp.servers[0])` for a region name.

    Two regions sharing a zone still differ in the TZ string, which is why the table
    is keyed by region and not by zone. An unknown name is accepted verbatim as the
    TZ string with the European zone: a reader who knows their own POSIX string is
    better served than one blocked by a missing table row.
    """
    for name, tz, zone in REGIONS:
        if name == region:
            return tz, f"0.{zone}.pool.ntp.org"
    return region, "0.europe.pool.ntp.org"


def models(
    provider: Provider,
    api_key: str,
    fetch: Callable[[str, str], bytes] | None = None,
) -> list[str]:
    """The provider's model names, or an empty list when it publishes none or the
    fetch fails.

    `fetch(url, key)` is supplied by `ui`, which owns the thread the reader waits on;
    this function never opens a socket. An empty result is reported by the caller as
    one chat line, and the model field goes free-text, which is the only route for a
    provider with no `models_url`.
    """
    if fetch is None or not provider.models_url:
        return []
    try:
        return parse_models(fetch(provider.models_url, api_key))
    except Exception:  # noqa: BLE001 - a failed fetch is a chat line, not a crash
        return []

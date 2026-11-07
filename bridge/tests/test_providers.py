"""`bridge/providers.py` (`05-bridge.md` §7.1).

Every assertion is the rule the doc states. The catalog is data, so most of it is
"the table says X and the module says X"; the behaviour worth testing is the merge,
the two list shapes, and the `extra_body` mapping, because those are what a wrong
answer turns into a 400 on a device.
"""

from __future__ import annotations

import json

import pytest

import bridge.providers as providers


# --- the built-in table ----------------------------------------------------


def test_builtin_covers_the_compatibility_table():
    """`03-config.md` §Provider compatibility is the frozen list of what the firmware
    can reach, so every row there has an entry and no entry invents a provider."""
    ids = [p.id for p in providers.BUILTIN]
    assert ids == [
        "openai",
        "openrouter",
        "groq",
        "ollama-cloud",
        "ollama",
        "vllm",
        "anthropic",
        "gemini",
        "custom",
    ]


def test_every_base_url_is_a_request_prefix():
    """The firmware appends `/chat/completions` verbatim, so a `base_url` missing its
    version path is a provider that would 404 on every call."""
    for entry in providers.BUILTIN:
        if not entry.base_url:
            continue
        assert entry.base_url.endswith("/v1") or entry.base_url.endswith(
            "/v1beta/openai"
        ), entry.id


def test_provider_ids_fit_the_config_schema():
    """`03-config.md`: `llm.providers[].id` is `[a-z0-9_-]{1,16}`."""
    for entry in providers.BUILTIN:
        assert 1 <= len(entry.id) <= 16, entry.id
        assert set(entry.id) <= set("abcdefghijklmnopqrstuvwxyz0123456789_-"), entry.id


# --- thinking levels -------------------------------------------------------


def test_a_provider_without_a_thinking_member_offers_no_levels():
    """No picker beats a disabled one: a level written for a provider that rejects the
    member turns every call into a 400."""
    entry = providers.Provider(id="x", label="X", base_url="u", thinking="none")
    assert entry.thinking_levels == ()
    assert entry.extra_body("high") == {}


def test_reasoning_effort_levels_map_to_the_member():
    entry = providers.Provider(
        id="x", label="X", base_url="u", thinking=providers.THINKING_REASONING_EFFORT
    )
    assert entry.thinking_levels == ("none", "low", "medium", "high")
    assert entry.extra_body("none") == {"reasoning_effort": "none"}
    assert entry.extra_body("low") == {"reasoning_effort": "low"}
    assert entry.extra_body("high") == {"reasoning_effort": "high"}


def test_thinking_budget_levels_map_to_token_counts():
    entry = providers.Provider(
        id="x", label="X", base_url="u", thinking=providers.THINKING_BUDGET
    )
    assert entry.extra_body("low") == {"thinking_budget": 1024}
    assert entry.extra_body("medium") == {"thinking_budget": 4096}
    assert entry.extra_body("high") == {"thinking_budget": 16384}
    # A budget provider takes a count, not the word; `none` is the absence of a
    # member rather than a zero budget, which some servers read as "no thinking".
    assert entry.extra_body("none") == {}


def test_a_level_the_provider_does_not_know_writes_nothing():
    entry = providers.Provider(
        id="x", label="X", base_url="u", thinking=providers.THINKING_BUDGET
    )
    assert entry.extra_body("turbo") == {}


def test_extra_body_never_exceeds_the_schema_member_cap():
    """`03-config.md`: at most four flat members, and the wizard adds at most one."""
    for entry in providers.BUILTIN:
        for level in entry.thinking_levels:
            assert len(entry.extra_body(level)) <= providers.EXTRA_BODY_MAX


# --- the custom file -------------------------------------------------------


def _write(tmp_path, payload):
    path = tmp_path / "providers.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return str(path)


def test_an_absent_file_is_the_normal_case(tmp_path):
    custom, notes = providers.load_custom(str(tmp_path / "nope.json"))
    assert custom == []
    assert notes == []


def test_a_malformed_file_reports_its_path_and_keeps_the_builtins(tmp_path):
    """A broken local file must never cost the reader the ability to onboard."""
    path = _write(tmp_path, "{not json")
    custom, notes = providers.load_custom(path)
    assert custom == []
    assert len(notes) == 1
    assert path in notes[0]

    merged, merged_notes = providers.catalog(path)
    assert [p.id for p in merged] == [p.id for p in providers.BUILTIN]
    assert merged_notes == notes


def test_a_custom_entry_with_the_same_id_replaces_the_builtin(tmp_path):
    """This is how a reader corrects a stale `base_url` without patching the bridge."""
    path = _write(
        tmp_path,
        [
            {
                "id": "groq",
                "label": "Groq (proxy)",
                "base_url": "https://proxy.example/v1",
            }
        ],
    )
    merged, notes = providers.catalog(path)
    assert notes == []
    groq = [p for p in merged if p.id == "groq"]
    assert len(groq) == 1
    assert groq[0].base_url == "https://proxy.example/v1"
    assert groq[0].label == "Groq (proxy)"
    # The replacement keeps its place in the list rather than moving to the end.
    assert [p.id for p in merged] == [p.id for p in providers.BUILTIN]


def test_a_new_custom_entry_is_appended(tmp_path):
    path = _write(
        tmp_path,
        {"providers": [{"id": "lab", "label": "Lab", "base_url": "https://lab/v1"}]},
    )
    merged, notes = providers.catalog(path)
    assert notes == []
    assert merged[-1].id == "lab"
    assert len(merged) == len(providers.BUILTIN) + 1


def test_a_half_entry_is_skipped_with_its_reason(tmp_path):
    """A half-entry cannot be written to the device, and dropping it silently would
    look like the file was never read."""
    path = _write(
        tmp_path,
        [
            {"id": "nourl", "label": "No URL"},
            {"label": "No id", "base_url": "https://x/v1"},
            {"id": "badthink", "label": "B", "base_url": "https://x/v1", "thinking": "x"},
            {"id": "ok", "label": "OK", "base_url": "https://x/v1"},
        ],
    )
    custom, notes = providers.load_custom(path)
    assert [p.id for p in custom] == ["ok"]
    assert len(notes) == 3
    assert "entry 0" in notes[0] and "base_url" in notes[0]
    assert "entry 1" in notes[1] and "id" in notes[1]
    assert "entry 2" in notes[2] and "thinking" in notes[2]


def test_a_file_that_is_not_a_list_reports_once(tmp_path):
    path = _write(tmp_path, {"providers": "groq"})
    custom, notes = providers.load_custom(path)
    assert custom == []
    assert len(notes) == 1 and path in notes[0]


def test_xdg_config_home_decides_the_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert providers.custom_path() == str(tmp_path / "musteval" / "providers.json")


# --- model lists -----------------------------------------------------------


def test_openai_list_shape_is_read():
    payload = b'{"data":[{"id":"gpt-a"},{"id":"gpt-b"},{"id":"gpt-a"}]}'
    assert providers.parse_models(payload) == ["gpt-a", "gpt-b"]


def test_ollama_list_shape_is_read():
    payload = b'{"models":[{"name":"llama3:8b"},{"name":"qwen2.5:7b"}]}'
    assert providers.parse_models(payload) == ["llama3:8b", "qwen2.5:7b"]


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"data":"nope"}',
        b'{"object":"list"}',
        b'{"data":[{"nope":1}]}',
        b"\xff\xfe",
    ],
)
def test_an_unfamiliar_document_is_no_list_not_an_error(payload):
    """A provider answering 200 with a shape this bridge does not know is not broken."""
    assert providers.parse_models(payload) == []


def test_models_uses_the_fetch_callback_and_the_key():
    seen = {}

    def fetch(url, key):
        seen["url"], seen["key"] = url, key
        return b'{"data":[{"id":"m"}]}'

    entry = providers.Provider(
        id="x", label="X", base_url="u", models_url="https://x/v1/models"
    )
    assert providers.models(entry, "sk-test", fetch) == ["m"]
    assert seen == {"url": "https://x/v1/models", "key": "sk-test"}


def test_models_without_a_models_url_or_a_fetch_is_empty():
    entry = providers.Provider(id="x", label="X", base_url="u")
    assert providers.models(entry, "k", lambda u, k: b'{"data":[{"id":"m"}]}') == []
    assert providers.models(entry, "k", None) == []


def test_a_failing_fetch_is_an_empty_list_not_a_crash():
    """A failed fetch is one chat line and a free-text field, never a broken write."""

    def fetch(url, key):
        raise OSError("no route to host")

    entry = providers.Provider(
        id="x", label="X", base_url="u", models_url="https://x/v1/models"
    )
    assert providers.models(entry, "k", fetch) == []


# --- timezones -------------------------------------------------------------


def test_a_region_maps_to_a_posix_tz_and_a_continental_zone():
    """The firmware applies the TZ string with `setenv`, which knows no IANA name.
    The zone is continental because ntppool documents that a country zone may hold one
    or two servers or none."""
    tz, server = providers.timezone_for("US / Pacific")
    assert tz == "PST8PDT,M3.2.0,M11.1.0"
    assert server == "0.north-america.pool.ntp.org"


def test_two_regions_may_share_a_zone_but_never_a_tz():
    """Which is why the table is keyed by region and not by zone."""
    seen: dict[str, str] = {}
    for name in providers.region_names():
        tz, server = providers.timezone_for(name)
        assert tz not in seen, f"{name} reuses {tz} from {seen.get(tz)}"
        seen[tz] = name
        assert server.startswith("0.") and server.endswith(".pool.ntp.org")


def test_an_unknown_region_is_accepted_as_its_own_tz():
    """A reader who knows their POSIX string is better served than one blocked by a
    missing table row."""
    tz, server = providers.timezone_for("XYZ-3")
    assert tz == "XYZ-3"
    assert server == "0.europe.pool.ntp.org"


def test_a_level_round_trips_through_extra_body():
    """`05` §7.2 seeds the picker from the stored entry, so `level_of` must invert
    `extra_body` for every mode — otherwise re-running `/onboard` silently resets a
    level the reader set."""
    for provider_id in ("groq", "gemini", "anthropic"):
        entry = providers.find(provider_id)
        for level in entry.thinking_levels:
            assert entry.level_of(entry.extra_body(level)) == level, (provider_id, level)


def test_an_unrecognised_level_member_is_reported_as_stored_not_as_none():
    """The member is the reader's, and reporting `none` for it is what made a re-run
    of the wizard overwrite it: `answers.extra_body` is rebuilt from the picker, so
    `none` for a stored `max` replaced `max` with whatever the picker showed.

    The old expectation here was `none`, from when `reasoning_effort` was assumed to
    have a closed set of four names. Ollama disproved that: it names levels per model
    and `/api/show` publishes `[low, high, max]` for `glm-5.3`, so `max` and
    `minimal` are real settings the catalog does not list (`05` §7.1)."""
    groq = providers.find("groq")
    assert groq.level_of({"reasoning_effort": "minimal"}) == "minimal"
    assert providers.find("ollama-cloud").level_of({"reasoning_effort": "max"}) == "max"
    assert groq.level_of({"unrelated": 1}) == "none"
    assert groq.level_of(None) == "none"
    # A budget between the mapped steps names no level either.
    assert providers.find("anthropic").level_of({"thinking_budget": 2000}) == "none"


def test_without_level_keeps_every_other_member():
    """`/thinking` writes the whole array (`05` §7.2), so a member the reader added to
    `extra_body` has to survive a level change."""
    groq = providers.find("groq")
    kept = groq.without_level({"reasoning_effort": "low", "seed": 7})
    assert kept == {"seed": 7}
    assert groq.without_level(None) == {}


#: A slice of the real `GET https://ollama.com/v1/models` body, recorded 2026-11-07.
#: Ollama's OpenAI compatibility layer answers in the OpenAI shape, which is why the
#: cloud entry can list its models at all (`05` §7.1).
_OLLAMA_MODELS = b"""{"object":"list","data":[
 {"id":"gemma4:31b","object":"model","created":1785164400,"owned_by":"ollama"},
 {"id":"gpt-oss:120b","object":"model","created":1785164300,"owned_by":"ollama"},
 {"id":"glm-5.3","object":"model","created":1785164200,"owned_by":"ollama"}]}"""


def test_ollama_cloud_lists_its_models_and_takes_a_level():
    """The two things the catalog used to get wrong about Ollama.

    Its `/v1/models` was recorded as unreadable, so the model had to be typed; and the
    thinking member was recorded as absent, so `/thinking` refused a level the
    provider accepts. `docs.ollama.com/api/openai-compatibility` lists
    `reasoning_effort` as supported and `/api/show` gives each model's own values.
    """
    entry = providers.find("ollama-cloud")
    assert entry is not None
    assert entry.models_url == "https://ollama.com/v1/models"
    assert providers.models(entry, "", lambda url, key: _OLLAMA_MODELS) == [
        "gemma4:31b",
        "gpt-oss:120b",
        "glm-5.3",
    ]
    # The list is public: it needs no key, so a reader can pick a model before they
    # have proved their key works.
    assert providers.models(entry, "", lambda url, key: _OLLAMA_MODELS) != []

    assert "high" in entry.thinking_levels
    assert entry.extra_body("high") == {"reasoning_effort": "high"}
    assert entry.level_of({"reasoning_effort": "high"}) == "high"
    # Local Ollama is the same compatibility layer, so it gets the same member.
    assert providers.find("ollama").extra_body("low") == {"reasoning_effort": "low"}

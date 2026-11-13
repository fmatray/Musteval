"""`bridge/ui/onboard.py`'s pure decisions, at the unit layer.

`09` §6 keeps these out of the pilot: each answers a question about one document —
the id derived from a pasted URL, the frames a step writes, the line a verdict
produces — so the cheapest layer that can fail them is this one. The wizard's own
screen behaviour is `test_ui_onboard.py`'s subject.
"""

from __future__ import annotations

from bridge import providers
from bridge.ui import onboard


def _answers(**kw):
    """`Answers` with the five required members filled and the rest defaulted."""
    base = dict(ssid="", password="", base_url="", api_key="", model="")
    base.update(kw)
    return onboard.Answers(**base)


def test_a_pasted_url_becomes_the_label_the_reader_will_see_again():
    """`03` §Provider compatibility requires an `id`, and a device with the wrong one
    is a config the reader cannot recognise. `api.` and `www.` are naming noise, and
    a host that is only digits names no provider at all."""
    cases = [
        ("https://api.groq.com/openai/v1", "groq"),
        ("https://www.example.com/v1", "example"),
        ("https://foo-bar.test/v1", "foo-bar"),
        ("https://api.www.test/v1", "test"),
        ("https://1.2.3.4/v1", "custom"),
        ("https://:443/v1", "custom"),
        ("", "custom"),
        ("not a url", "custom"),
    ]
    for url, expected in cases:
        assert onboard.provider_id(url) == expected, url


def test_a_catalog_row_keeps_its_own_id_when_the_url_is_its_own():
    """`05` §7.3: a row was chosen, so its id is the name — deriving a second one from
    the URL would give `llm.providers[].id` two spellings for one provider. A URL the
    reader edited is no longer that row, and the host names it instead."""
    row = providers.find("ollama")
    assert row is not None
    assert onboard.provider_id(row.base_url, row) == "ollama"
    assert onboard.provider_id("https://elsewhere.test/v1", row) == "elsewhere"


def test_a_long_label_is_cut_at_the_id_limit():
    """`03` bounds `id` in bytes; a label cut mid-run must not leave the trailing
    hyphen that `_slug` strips before cutting."""
    assert onboard.provider_id("https://" + "a" * 40 + ".test/v1") == "a" * 16


def test_a_network_with_no_passphrase_writes_no_secret():
    """`05` §7: an open station has nothing to keep, and a `secret.set` with an empty
    value would clobber a key another network already uses."""
    answers = _answers(ssid="Cafe", password="", auth="open")
    plan = onboard.writes_network(answers)
    assert plan == []


def test_a_network_that_cannot_join_writes_nothing():
    """`04` §4.24: the station cannot join a captive portal, so writing it would
    leave the device retrying a network it can never reach."""
    for auth in ("captive", "enterprise"):
        assert onboard.writes_network(
            _answers(ssid="Corp", password="p", auth=auth)
        ) == []


def test_a_passed_provider_step_writes_no_secret_and_no_optional_member():
    """`05` §7.2: passing asserts the stored provider is already right, so the plan
    adds no key and no member the provider has not agreed to take."""
    answers = _answers(
        provider="ollama", base_url="https://ollama.com/v1", model="m",
        api_key="typed", token_param="max_completion_tokens",
        skip_provider=True, extra_body={"reasoning_effort": "high"},
    )
    plan = onboard.writes_provider(answers)
    assert [kind for kind, _ in plan] == ["config"]
    entry = plan[0][1]["llm.providers"][0]
    assert "token_param" not in entry
    assert entry["extra_body"] == {"reasoning_effort": "high"}


def test_a_narration_names_the_thinking_level_not_the_raw_member():
    """`05` §7: the reader chose a word, so the line says the word. Echoing
    `{"reasoning_effort": "high"}` would make them translate their own choice."""
    answers = _answers(
        ssid="Home", password="p", auth="wpa2", provider="ollama",
        base_url="https://ollama.com/v1", model="m", api_key="k",
        extra_body={"reasoning_effort": "high"}, timezone="Europe/Paris",
        ntp_server="pool.test",
    )
    lines = onboard.narration(answers, {})
    assert "thinking: high" in lines
    assert "creates wifi.networks (1 entry)" in lines
    assert "creates llm.providers (1 entry)" in lines
    assert "sets ntp.servers pool.test" in lines


def test_a_narration_counts_what_it_replaces():
    """`05` §7: replacing two networks with one is a change the reader should see
    before it happens — a count is the only place the loss is visible."""
    answers = _answers(
        ssid="Home", password="p", auth="wpa2", provider="ollama",
        base_url="https://ollama.com/v1", model="m", api_key="k",
        timezone="Europe/Paris", ntp_server="pool.test",
    )
    lines = onboard.narration(
        answers,
        {
            "wifi.networks": [{"ssid": "a"}, {"ssid": "b"}],
            "llm.providers": [{"id": "old"}],
            "ntp": {"servers": ["a", "b"]},
        },
    )
    assert "replaces wifi.networks (2 entries → 1)" in lines
    assert "replaces llm.providers (1 entries → 1)" in lines
    assert "replaces ntp.servers (2 → 1)" in lines


def test_a_step_that_cannot_be_written_is_not_narrated():
    """`05` §7: the narration follows the same gate as the write. A line describing a
    step that will not be sent is a promise the run does not keep."""
    answers = _answers(
        ssid="Corp", password="p", auth="captive", provider="ollama",
        base_url="https://ollama.com/v1", model="", api_key="k",
    )
    lines = onboard.narration(answers, {})
    assert not [line for line in lines if "wifi.networks" in line]
    assert "skips llm.providers: no model, and the schema requires one" in lines


def test_a_model_the_provider_did_not_list_is_named_as_a_typo():
    """`05` §7.1: the step exists to catch this before the write. Taking it at face
    value would let the first prompt earn a 404."""
    answers = _answers(model="typo:1b")
    assert "is not in the provider's list of 2" in onboard.verdict_model(
        ["a", "b"], answers
    )
    assert onboard.verdict_model(["typo:1b"], answers) == "model: typo:1b"
    assert "unchecked" in onboard.verdict_model([], answers)
    assert onboard.verdict_model([], _answers(model="")) == "model: none chosen"


def test_an_unanswered_provider_names_the_one_thing_missing():
    """`05` §7.2: three different problems, three different lines — a reader told
    "no models" when they never typed a key would go looking at the host."""
    assert onboard.verdict_provider([], _answers()) == "provider: no base url"
    assert "no key typed" in onboard.verdict_provider(
        [], _answers(base_url="https://x/v1")
    )
    assert "publishes no model list" in onboard.verdict_provider(
        [], _answers(base_url="https://x/v1", api_key="k", provider="custom")
    )
    row = providers.find("ollama")
    assert row is not None and row.models_url
    assert "returned no models" in onboard.verdict_provider(
        [], _answers(base_url=row.base_url, api_key="k", provider=row.id)
    )
    assert onboard.verdict_provider(
        ["a", "b"], _answers(base_url=row.base_url, api_key="k", provider=row.id)
    ) == "provider: 2 models"


def test_a_thinking_reply_with_no_stored_provider_names_the_step():
    """`05` §7.4: the level is written into a provider, so with none stored there is
    nothing to write into and the reader is pointed at `/onboard`."""
    assert "run /onboard first" in onboard.thinking_line("high", [])
    assert "not in this bridge's catalog" in onboard.thinking_line(
        "high", [{"id": "ghost"}]
    )


def test_a_provider_with_a_url_but_no_key_writes_nothing():
    """`03` §rule 9: `api_key_ref` names a secret that must exist, so a step with no
    key typed — and not passed — has nothing it can legally write."""
    assert onboard.writes_provider(
        _answers(base_url="https://x/v1", api_key="", model="m")
    ) == []


def test_a_token_param_the_provider_was_not_told_about_is_not_written():
    """`03` §Provider compatibility: `token_param` is per provider. Writing the wrong
    member turns every call into a 400, so a typed step with none carries none."""
    entry = onboard.writes_provider(
        _answers(base_url="https://x/v1", api_key="k", model="m", provider="custom")
    )[1][1]["llm.providers"][0]
    assert "token_param" not in entry
    assert "extra_body" not in entry
    typed = onboard.writes_provider(
        _answers(
            base_url="https://api.openai.com/v1", api_key="k", model="m",
            provider="openai", token_param="max_completion_tokens",
        )
    )[1][1]["llm.providers"][0]
    assert typed["token_param"] == "max_completion_tokens"


def test_a_network_the_station_is_not_joining_is_reported_as_skipped():
    """`05` §7: the confirm names the skip, because a reader who picked a captive
    portal expects that row to be used and must be told it is not."""
    lines = onboard.narration(
        _answers(ssid="Corp", password="p", auth="captive"), {}
    )
    assert lines == ["skips Corp: the station cannot join captive (`04` §4.24)"]


def test_the_verdict_on_a_station_that_is_still_connecting_names_the_ssid():
    """`04` §4.24: `connecting` is not `up` and not `down`; naming the network is the
    difference between a slow join and a typo."""
    assert "still connecting to Home" in onboard.verdict_network(
        {"wifi": {"state": "connecting", "ssid": "Home"}}
    )
    assert "the configured ssid" in onboard.verdict_network(
        {"wifi": {"state": "connecting"}}
    )
    assert "joined Home" in onboard.verdict_network(
        {"wifi": {"state": "connected", "ssid": "Home"}}
    )
    assert "did not report a wifi block" in onboard.verdict_network({"wifi": "up"})
    assert "the radio is off" in onboard.verdict_network({"wifi": {"state": "off"}})

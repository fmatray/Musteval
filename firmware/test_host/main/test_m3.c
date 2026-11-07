/* Host tests for the pure M3 rules: the clock's sync interval, the network ordering
 * and backoff, the call budget and the request body. Each asserts an observable
 * result of a rule a frozen document states, not that a function was called. */

#include <string.h>

#include "unity.h"

#include "clock.h"
#include "llm.h"
#include "net.h"

/* --- clock ---------------------------------------------------------------- */

/* `02-firmware.md` §clock: the poll interval is clamped to 60..86400 s, so a config
 * typo cannot turn the device into an SNTP flood or silence it for a year. */
TEST_CASE("the clock sync interval is clamped to 60..86400 s", "[m3]")
{
    TEST_ASSERT_EQUAL_INT64(60000, clock_next_interval_ms(0));
    TEST_ASSERT_EQUAL_INT64(60000, clock_next_interval_ms(-5));
    TEST_ASSERT_EQUAL_INT64(60000, clock_next_interval_ms(60));
    TEST_ASSERT_EQUAL_INT64(3600000, clock_next_interval_ms(3600));
    TEST_ASSERT_EQUAL_INT64(86400000, clock_next_interval_ms(86400));
    TEST_ASSERT_EQUAL_INT64(86400000, clock_next_interval_ms(999999));
    TEST_ASSERT_EQUAL_INT64(86400000, clock_next_interval_ms(INT32_MAX));
}

/* --- net ordering --------------------------------------------------------- */

static net_candidate_t cand(const char *ssid, int32_t priority)
{
    net_candidate_t c = {.ssid = ssid, .priority = priority}; /* borrowed, as `net` stores it */
    return c;
}

/* `03-config.md` §wifi: highest `priority` first. */
TEST_CASE("networks are ranked highest priority first", "[m3]")
{
    net_candidate_t order[3] = {cand("low", 1), cand("high", 9), cand("mid", 5)};
    net_rank(order, 3);
    TEST_ASSERT_EQUAL_STRING("high", order[0].ssid);
    TEST_ASSERT_EQUAL_STRING("mid", order[1].ssid);
    TEST_ASSERT_EQUAL_STRING("low", order[2].ssid);
}

/* Equal priorities keep their config order: a re-sort that scrambles them would make
 * the failover order depend on the sort implementation rather than on the file. */
TEST_CASE("equal network priorities keep their config order", "[m3]")
{
    net_candidate_t order[4] = {cand("a", 5), cand("b", 5), cand("c", 5), cand("d", 7)};
    net_rank(order, 4);
    TEST_ASSERT_EQUAL_STRING("d", order[0].ssid);
    TEST_ASSERT_EQUAL_STRING("a", order[1].ssid);
    TEST_ASSERT_EQUAL_STRING("b", order[2].ssid);
    TEST_ASSERT_EQUAL_STRING("c", order[3].ssid);
}

/* `03-config.md` §wifi `retry_backoff_ms` is 1..8 entries and the wait for the last
 * attempt is the last entry, repeated. Past the table it must not read out of bounds. */
TEST_CASE("the retry backoff repeats its last entry", "[m3]")
{
    const int32_t table[3] = {1000, 2000, 5000};
    TEST_ASSERT_EQUAL_INT32(1000, net_backoff_ms(table, 3, 0));
    TEST_ASSERT_EQUAL_INT32(2000, net_backoff_ms(table, 3, 1));
    TEST_ASSERT_EQUAL_INT32(5000, net_backoff_ms(table, 3, 2));
    TEST_ASSERT_EQUAL_INT32(5000, net_backoff_ms(table, 3, 3));
    TEST_ASSERT_EQUAL_INT32(5000, net_backoff_ms(table, 3, 99));
    /* An empty table means no wait, not a read of `table[-1]`. */
    TEST_ASSERT_EQUAL_INT32(0, net_backoff_ms(table, 0, 0));
}

/* --- llm budget ----------------------------------------------------------- */

/* One hour is 3600 s. A charge inside the window increments; the next window starts
 * from zero (`03-config.md` §llm: fixed UTC windows, not a rolling one). */
TEST_CASE("the call budget counts inside a fixed UTC window", "[m3]")
{
    llm_budget_t b;
    llm_budget_reset(&b);
    const int64_t base = 1730563200; /* 2024-11-02T16:00:00Z, an hour boundary */

    TEST_ASSERT_TRUE(llm_budget_charge(&b, 3, 10, base + 10));
    TEST_ASSERT_TRUE(llm_budget_charge(&b, 3, 10, base + 20));
    int32_t hour = 0, day = 0;
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(2, hour);

    TEST_ASSERT_TRUE(llm_budget_charge(&b, 3, 10, base + 3600));
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(1, hour);
    TEST_ASSERT_EQUAL_INT32(3, day);
}

/* A refusal spends nothing: `02-firmware.md` calls this "refused calls are not
 * counted", and counting them would let a misconfigured client lock itself out. */
TEST_CASE("a refused call spends no budget", "[m3]")
{
    llm_budget_t b;
    llm_budget_reset(&b);
    const int64_t base = 1730563200;

    TEST_ASSERT_TRUE(llm_budget_charge(&b, 1, 100, base));
    TEST_ASSERT_FALSE(llm_budget_charge(&b, 1, 100, base + 60));

    int32_t hour = 0, day = 0;
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(1, hour);
    TEST_ASSERT_EQUAL_INT32(1, day);
}

/* A day refusal must roll back the hour increment it just made, or the two windows
 * drift apart by one call per refused request. */
TEST_CASE("a day refusal rolls back the hour increment", "[m3]")
{
    llm_budget_t b;
    llm_budget_reset(&b);
    const int64_t base = 1730563200;

    TEST_ASSERT_TRUE(llm_budget_charge(&b, 100, 1, base));
    /* Next hour, same day: the hour window would reset, but the day is full, so the
     * call is refused and the rollback leaves the state exactly where the last
     * accepted call put it. */
    TEST_ASSERT_FALSE(llm_budget_charge(&b, 100, 1, base + 3600));

    int32_t hour = 0, day = 0;
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(1, hour);
    TEST_ASSERT_EQUAL_INT32(1, day);

    /* The hour window really did not advance: one refill of the day and the next
     * accepted hour charge is the first of its window. */
    TEST_ASSERT_TRUE(llm_budget_charge(&b, 100, 2, base + 7200));
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(1, hour);
    TEST_ASSERT_EQUAL_INT32(2, day);
}

/* 0 disables that window, which is how a config says "no hourly cap". */
TEST_CASE("a zero cap disables its window", "[m3]")
{
    llm_budget_t b;
    llm_budget_reset(&b);
    const int64_t base = 1730563200;
    for (int i = 0; i < 50; i++) {
        TEST_ASSERT_TRUE(llm_budget_charge(&b, 0, 0, base + i));
    }
    int32_t hour = 0, day = 0;
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(50, hour);
    TEST_ASSERT_EQUAL_INT32(50, day);
}

/* A clock before 1970 is negative; floor division must still put every second of an
 * hour in the same bucket, or a device with no RTC would get one bucket per call. */
TEST_CASE("the budget buckets a negative clock consistently", "[m3]")
{
    llm_budget_t b;
    llm_budget_reset(&b);
    TEST_ASSERT_TRUE(llm_budget_charge(&b, 5, 5, -1));
    TEST_ASSERT_TRUE(llm_budget_charge(&b, 5, 5, -2));
    int32_t hour = 0, day = 0;
    llm_budget_counters(&b, &hour, &day);
    TEST_ASSERT_EQUAL_INT32(2, hour);
}

/* --- llm request body ----------------------------------------------------- */

/* The body of `02-firmware.md` §llm: model, messages, the provider's token cap and
 * `stream:false`, and no `tools` key when no tools are offered. */
TEST_CASE("the request body has the frozen shape", "[m3]")
{
    llm_request_t r = {
        .model = "gpt-4o-mini",
        .token_param = "max_completion_tokens",
        .max_output_tokens = 256,
        .system_prompt = "You are terse.",
        .user_prompt = "hi",
        .tools_json = NULL,
    };
    char out[512];
    size_t n = llm_assemble_prompt(&r, NULL, out, sizeof out);
    TEST_ASSERT_GREATER_THAN(0, n);
    TEST_ASSERT_EQUAL_STRING(
        "{\"model\":\"gpt-4o-mini\",\"messages\":["
        "{\"role\":\"system\",\"content\":\"You are terse.\"},"
        "{\"role\":\"user\",\"content\":\"hi\"}],"
        "\"max_completion_tokens\":256,\"stream\":false}",
        out);
}

/* A prompt is whatever a template holds, including quotes and newlines. Unescaped,
 * either one produces a body the provider rejects as malformed. */
TEST_CASE("prompt text is escaped into the request body", "[m3]")
{
    llm_request_t r = {
        .model = "m",
        .user_prompt = "say \"hi\"\n\tto\\me",
    };
    char out[512];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, out, sizeof out));
    TEST_ASSERT_NOT_NULL(strstr(out, "\\\"hi\\\""));
    TEST_ASSERT_NOT_NULL(strstr(out, "\\n"));
    TEST_ASSERT_NOT_NULL(strstr(out, "\\t"));
    TEST_ASSERT_NOT_NULL(strstr(out, "\\\\me"));
    /* The escaped string must itself parse: the quotes inside are all backslashed. */
    TEST_ASSERT_NOT_NULL(strstr(out, "\"content\":\"say \\\"hi\\\""));
}

/* A control byte has no short escape and must still not reach the body raw. */
TEST_CASE("a control byte is escaped as a unicode escape", "[m3]")
{
    llm_request_t r = {.model = "m", .user_prompt = "a\x01z"};
    char out[256];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, out, sizeof out));
    TEST_ASSERT_NOT_NULL(strstr(out, "\\u0001"));
}

/* A body that does not fit is refused, never truncated: half a JSON object is a 400
 * indistinguishable from a config mistake. */
TEST_CASE("a body that does not fit is refused, not truncated", "[m3]")
{
    char prompt[600];
    memset(prompt, 'x', sizeof prompt - 1);
    prompt[sizeof prompt - 1] = '\0';
    llm_request_t r = {.model = "m", .user_prompt = prompt};
    char out[128];
    TEST_ASSERT_EQUAL_size_t(0, llm_assemble_prompt(&r, NULL, out, sizeof out));
}

/* `extra_body` members join the request object rather than nesting, and cannot
 * change `model`, `messages`, `tools` or `stream` (`03-config.md` §llm). */
TEST_CASE("extra_body members join the request object", "[m3]")
{
    llm_request_t r = {.model = "m", .user_prompt = "x"};
    char out[512];
    TEST_ASSERT_GREATER_THAN(
        0, llm_assemble_prompt(&r, "{\"temperature\":0.2}", out, sizeof out));
    TEST_ASSERT_NOT_NULL(strstr(out, ",\"temperature\":0.2"));
    TEST_ASSERT_NULL(strstr(out, "extra_body"));
}

/* `tools` is omitted when there are none and passed through when there are. */
TEST_CASE("tools are sent only when offered", "[m3]")
{
    llm_request_t r = {.model = "m", .user_prompt = "x"};
    char out[1024];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, out, sizeof out));
    TEST_ASSERT_NULL(strstr(out, "\"tools\""));
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, out, sizeof out));

    llm_request_t with = r;
    with.tools_json = "[{\"type\":\"function\"}]";
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&with, NULL, out, sizeof out));
    TEST_ASSERT_NOT_NULL(strstr(out, "\"tools\":[{\"type\":\"function\"}]"));
}

/* A later round carries no `user_prompt`: the history already has it, and the last
 * message of the request is the tool result (`02-firmware.md` §4.1 step 4). Sending
 * it again put the user turn after the tool replies and asked the question twice. */
TEST_CASE("a later round sends history only, with no repeated user turn", "[m3]")
{
    llm_request_t r = {
        .model = "m",
        .system_prompt = "s",
        .user_prompt = NULL,
        .history_json = "[{\"role\":\"user\",\"content\":\"hi\"},"
                        "{\"role\":\"tool\",\"tool_call_id\":\"c1\","
                        "\"content\":\"{}\"}]",
    };
    char out[512];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, out, sizeof out));
    TEST_ASSERT_EQUAL_STRING(
        "{\"model\":\"m\",\"messages\":["
        "{\"role\":\"system\",\"content\":\"s\"},"
        "{\"role\":\"user\",\"content\":\"hi\"},"
        "{\"role\":\"tool\",\"tool_call_id\":\"c1\",\"content\":\"{}\"}],"
        "\"max_tokens\":1024,\"stream\":false}",
        out);
}

/* History alone still produces a well-formed `messages` array, with no leading or
 * trailing comma and no empty element. */
TEST_CASE("history joins without a stray comma", "[m3]")
{
    llm_request_t r = {
        .model = "m",
        .history_json = "[{\"role\":\"user\",\"content\":\"a\"}]",
    };
    char out[256];
    TEST_ASSERT_GREATER_THAN(0, llm_assemble_prompt(&r, NULL, out, sizeof out));
    TEST_ASSERT_EQUAL_STRING(
        "{\"model\":\"m\",\"messages\":["
        "{\"role\":\"user\",\"content\":\"a\"}],"
        "\"max_tokens\":1024,\"stream\":false}",
        out);
    TEST_ASSERT_NULL(strstr(out, ",]"));
}

/* The endpoint is `base_url` plus `/chat/completions`, appended verbatim
 * (`03-config.md` §Provider compatibility). */
TEST_CASE("the endpoint appends /chat/completions verbatim", "[m3]")
{
    char out[192];
    TEST_ASSERT_EQUAL_size_t(strlen("https://a/v1/chat/completions"),
                             llm_endpoint("https://a/v1", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("https://a/v1/chat/completions", out);

    /* Too small is a refusal, not a truncated URL that would reach the wrong host. */
    TEST_ASSERT_EQUAL_size_t(0, llm_endpoint("https://a/v1", out, 16));
    TEST_ASSERT_EQUAL_size_t(0, llm_endpoint(NULL, out, sizeof out));
}

/* The chain is walked once: exhausting it returns -1 rather than wrapping, which is
 * what stops a flapping provider from spending the budget in a loop. */
TEST_CASE("the provider chain is walked once", "[m3]")
{
    TEST_ASSERT_EQUAL_INT(1, llm_next_provider(0, 2));
    TEST_ASSERT_EQUAL_INT(-1, llm_next_provider(1, 2));
    TEST_ASSERT_EQUAL_INT(-1, llm_next_provider(0, 1));
    TEST_ASSERT_EQUAL_INT(-1, llm_next_provider(0, 0));
}

/* The sentinel -1 must end the walk. A `for` guard of `index < count` is true for -1,
 * so the chain walk reads `providers[-1]` and loops forever: the run never returns,
 * `agent.busy` stays true, and the device stops answering prompts. */
TEST_CASE("the exhausted chain sentinel ends the walk", "[m3]")
{
    TEST_ASSERT_TRUE(llm_provider_loop_ok(0, 2));
    TEST_ASSERT_TRUE(llm_provider_loop_ok(1, 2));
    TEST_ASSERT_FALSE(llm_provider_loop_ok(llm_next_provider(1, 2), 2));
    TEST_ASSERT_FALSE(llm_provider_loop_ok(-1, 1));
    /* An empty chain never enters the walk at all. */
    TEST_ASSERT_FALSE(llm_provider_loop_ok(llm_next_provider(0, 0), 0));
}


/* Host tests for the pure M3 rules: the clock's sync interval, the network ordering
 * and backoff, the call budget and the request body. Each asserts an observable
 * result of a rule a frozen document states, not that a function was called. */

#include <string.h>

#include "unity.h"

#include "clock.h"
#include "llm.h"
#include "net.h"
#include "net_internal.h"
#include "store.h" /* `json_parse_at`, which is strict about trailing content */

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

/* --- wifi.scan (`04-protocol.md` §4.24) ----------------------------------- */

static net_scan_entry_t ap(const char *ssid, int32_t rssi, uint8_t auth)
{
    net_scan_entry_t e = {.rssi = rssi, .auth = auth};
    snprintf(e.ssid, sizeof e.ssid, "%s", ssid);
    return e;
}

/* Rule 1: an entry with no name cannot be selected, and an empty string is not a
 * usable `wifi.networks[].ssid` under `03-config.md`'s 1..32 bound. */
TEST_CASE("a hidden SSID is dropped from a scan", "[m3]")
{
    net_scan_entry_t list[3] = {ap("", -40, 3), ap("home", -50, 3), ap("", -99, 0)};
    size_t count = 3;
    bool truncated = true;
    net_scan_condense(list, &count, NET_SCAN_MAX, &truncated);
    TEST_ASSERT_EQUAL(1, (int)count);
    TEST_ASSERT_EQUAL_STRING("home", list[0].ssid);
    TEST_ASSERT_FALSE(truncated);
}

/* Rule 2: one record per BSSID, so a router's two radios share a name. Two rows the
 * reader cannot tell apart are worse than one row with the better signal. */
TEST_CASE("duplicate SSIDs collapse to the strongest RSSI", "[m3]")
{
    net_scan_entry_t list[4] = {ap("home", -80, 3), ap("cafe", -60, 0),
                                ap("home", -45, 3), ap("home", -70, 3)};
    size_t count = 4;
    bool truncated = false;
    net_scan_condense(list, &count, NET_SCAN_MAX, &truncated);
    TEST_ASSERT_EQUAL(2, (int)count);
    TEST_ASSERT_EQUAL_STRING("home", list[0].ssid);
    TEST_ASSERT_EQUAL_INT32(-45, list[0].rssi);
    TEST_ASSERT_EQUAL_STRING("cafe", list[1].ssid);
}

/* Rule 3: strongest first, capped at 20, and `truncated` says the cap bit. */
TEST_CASE("a scan is sorted strongest first and capped", "[m3]")
{
    net_scan_entry_t list[NET_SCAN_MAX + 3];
    static const char *names[] = {"a", "b", "c", "d", "e", "f", "g", "h", "i", "j",
                                  "k", "l", "m", "n", "o", "p", "q", "r", "s", "t",
                                  "u", "v", "w"};
    for (size_t i = 0; i < NET_SCAN_MAX + 3; i++) {
        /* Ascending construction order, descending signal: the sort has real work. */
        list[i] = ap(names[i], -(int32_t)i - 30, 3);
    }
    size_t count = NET_SCAN_MAX + 3;
    bool truncated = false;
    net_scan_condense(list, &count, NET_SCAN_MAX, &truncated);
    TEST_ASSERT_EQUAL(NET_SCAN_MAX, (int)count);
    TEST_ASSERT_TRUE(truncated);
    TEST_ASSERT_EQUAL_STRING("a", list[0].ssid);
    TEST_ASSERT_EQUAL_INT32(-30, list[0].rssi);
    TEST_ASSERT_EQUAL_STRING("t", list[NET_SCAN_MAX - 1].ssid);

    /* Exactly the cap is not truncation; one fewer keeps the flag clear. */
    count = 2;
    truncated = true;
    net_scan_condense(list, &count, NET_SCAN_MAX, &truncated);
    TEST_ASSERT_EQUAL(2, (int)count);
    TEST_ASSERT_FALSE(truncated);
}

/* A scan with nothing in range is an empty list, not an error and not a stale list. */
TEST_CASE("an empty scan reports nothing", "[m3]")
{
    net_scan_entry_t list[1] = {ap("stale", -40, 3)};
    size_t count = 0;
    bool truncated = true;
    net_scan_condense(list, &count, NET_SCAN_MAX, &truncated);
    TEST_ASSERT_EQUAL(0, (int)count);
    TEST_ASSERT_FALSE(truncated);
}

/* §4.24's auth words. The numbers are `wifi_auth_mode_t`'s ABI; a reader needs to see
 * `open` and `wep` because those are the two that will not associate. */
TEST_CASE("auth modes map to the wire words", "[m3]")
{
    TEST_ASSERT_EQUAL_STRING("open", net_auth_name(0));
    TEST_ASSERT_EQUAL_STRING("wep", net_auth_name(1));
    TEST_ASSERT_EQUAL_STRING("wpa", net_auth_name(2));
    TEST_ASSERT_EQUAL_STRING("wpa2", net_auth_name(3));
    TEST_ASSERT_EQUAL_STRING("wpa", net_auth_name(4));
    TEST_ASSERT_EQUAL_STRING("wpa2_enterprise", net_auth_name(5));
    TEST_ASSERT_EQUAL_STRING("wpa3", net_auth_name(6));
    TEST_ASSERT_EQUAL_STRING("wpa2", net_auth_name(7));
    TEST_ASSERT_EQUAL_STRING("wpa3_enterprise", net_auth_name(8));
    /* A driver mode added by a newer IDF must read as unknown, not as a word that is
     * not in the schema. */
    TEST_ASSERT_EQUAL_STRING("unknown", net_auth_name(9));
    TEST_ASSERT_EQUAL_STRING("unknown", net_auth_name(255));
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

/* --- llm rejection message ------------------------------------------------ */

/* The status alone names no member: a 400 is the same word whether the model, the
 * token cap or the thinking level was wrong. The provider's body is what says so,
 * which is why it is kept rather than freed unread. */
TEST_CASE("a provider rejection keeps the provider's words", "[m3]")
{
    const char *body = "{\"error\":{\"message\":\"model \\\"gemma4:31b\\\" not found\"}}";
    char out[LLM_ERROR_MAX];
    size_t n = llm_copy_error_detail(body, strlen(body), "ollama-cloud", 400, out,
                                     sizeof out);
    TEST_ASSERT_GREATER_THAN(0, n);
    TEST_ASSERT_EQUAL_STRING(
        "ollama-cloud: http 400: "
        "{\"error\":{\"message\":\"model \\\"gemma4:31b\\\" not found\"}}",
        out);
}

/* A refusal with no body -- a transport error surfaced as a status, or a provider that
 * answers 502 with nothing -- still has to read as a sentence. */
TEST_CASE("a rejection with no body names only the status", "[m3]")
{
    char out[LLM_ERROR_MAX];
    TEST_ASSERT_GREATER_THAN(0, llm_copy_error_detail(NULL, 0, "p1", 502, out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("p1: http 502", out);
    TEST_ASSERT_EQUAL_STRING("p1: http 502", out);
}

/* A pretty-printed body would arrive in the chat as its first line and a run of
 * escapes; the message travels in one JSON string, so it is one line. */
TEST_CASE("a rejection body is flattened to one line", "[m3]")
{
    char out[LLM_ERROR_MAX];
    llm_copy_error_detail("line one\nline two\there", 24, "p1", 400, out, sizeof out);
    TEST_ASSERT_NULL(strchr(out, '\n'));
    TEST_ASSERT_NULL(strchr(out, '\t'));
    TEST_ASSERT_NOT_NULL(strstr(out, "line one line two here"));
    /* An ESC in a rejection body would be a terminal escape sequence in the pane. */
    char esc[] = "bad\x1b[31mred";
    llm_copy_error_detail(esc, strlen(esc), "p1", 400, out, sizeof out);
    TEST_ASSERT_NULL(strchr(out, '\x1b'));
}

/* The body is clipped, never the prefix: a 16 KiB upstream page must not push the
 * provider id and the status -- the only part that is always right -- out of the
 * message the reader sees. */
TEST_CASE("a huge rejection body is clipped after the prefix", "[m3]")
{
    static char big[4096];
    memset(big, 'z', sizeof big);
    char out[LLM_ERROR_MAX];
    llm_copy_error_detail(big, sizeof big, "groq", 400, out, sizeof out);
    TEST_ASSERT_NOT_NULL(strstr(out, "groq: http 400: zzz"));
    TEST_ASSERT_LESS_THAN_size_t(sizeof out, strlen(out) + 1);
}

/* A body exactly as long as the clip is not a full message: the prefix and the clip
 * together must still fit, or the buffer is undersized rather than the body long. */
TEST_CASE("the message buffer holds the prefix and the whole clip", "[m3]")
{
    TEST_ASSERT_GREATER_THAN_size_t(LLM_ERROR_CLIP + 16, LLM_ERROR_MAX);
    static char body[LLM_ERROR_CLIP];
    memset(body, 'y', sizeof body);
    char out[LLM_ERROR_MAX];
    llm_copy_error_detail(body, sizeof body, "ollama-cloud", 400, out, sizeof out);
    TEST_ASSERT_EQUAL_size_t(strlen("ollama-cloud: http 400: ") + LLM_ERROR_CLIP,
                             strlen(out));
}

/* A provider that echoes the request back in its rejection would otherwise print the
 * key into scrollback; `06-security.md` §7 says a resolved key never reaches an
 * `agent.error` message. */
TEST_CASE("a rejection echoing the key is redacted", "[m3]")
{
    char body[] = "{\"error\":\"bad key sk-live-abc123 for model m\",\"key\":\"sk-live-abc123\"}";
    char out[LLM_ERROR_MAX];
    llm_copy_error_detail(body, strlen(body), "openai", 401, out, sizeof out);
    llm_redact(out, "sk-live-abc123");
    TEST_ASSERT_NULL(strstr(out, "sk-live-abc123"));
    TEST_ASSERT_NOT_NULL(strstr(out, "***"));
}

/* Every case here was observed on the board: the reasonless event arrives after the
 * deliberate leave, and scoring it made `net_connect` report `E_WIFI` with
 * "timeout 15000 ms" after ten milliseconds while the driver was still associating. */
TEST_CASE("a disconnect is scored only when it is a real refusal", "[m3]")
{
    /* The deliberate leave `net_connect` issues before each AP. */
    TEST_ASSERT_TRUE(net_disconnect_ignored(true, false, NET_WIFI_REASON_ASSOC_LEAVE));
    TEST_ASSERT_TRUE(net_disconnect_ignored(true, true, NET_WIFI_REASON_UNSPECIFIED));
    /* Dispatched after the flag cleared, but the attempt is still live. */
    TEST_ASSERT_TRUE(net_disconnect_ignored(false, true, NET_WIFI_REASON_UNSPECIFIED));
    /* A live link that drops for no stated reason is a real drop. */
    TEST_ASSERT_FALSE(net_disconnect_ignored(false, true, 202)); /* AUTH_FAIL, not ignored */
    TEST_ASSERT_FALSE(net_disconnect_ignored(false, false, NET_WIFI_REASON_UNSPECIFIED));
    TEST_ASSERT_FALSE(net_disconnect_ignored(false, true, 201)); /* NO_AP_FOUND, not ignored */
    /* The driver's own values. A fallback `#ifndef` in this header once defined
     * UNSPECIFIED as 0 while the driver uses 1, so the predicate was true in this
     * suite and false on the board. Pinning the numbers stops that recurring. */
    TEST_ASSERT_EQUAL_INT(1, NET_WIFI_REASON_UNSPECIFIED);
    TEST_ASSERT_EQUAL_INT(8, NET_WIFI_REASON_ASSOC_LEAVE);
    /* Observed on target as the reason the driver actually reported for the leave
     * `net_connect` issues: 36, not 8, which is why the first fix changed nothing. */
    TEST_ASSERT_TRUE(net_disconnect_ignored(false, true, NET_WIFI_REASON_STA_LEAVING));
    TEST_ASSERT_EQUAL_INT(36, NET_WIFI_REASON_STA_LEAVING);
}

/* The ordinary case: most providers do not echo, and a no-op must not corrupt the
 * message or read past its end. */
TEST_CASE("redacting with no secret leaves the message alone", "[m3]")
{
    char text[] = "p1: http 502";
    TEST_ASSERT_EQUAL_size_t(0, llm_redact(text, NULL));
    TEST_ASSERT_EQUAL_STRING("p1: http 502", text);
    TEST_ASSERT_EQUAL_size_t(0, llm_redact(text, ""));
    TEST_ASSERT_EQUAL_STRING("p1: http 502", text);
    TEST_ASSERT_EQUAL_size_t(0, llm_redact(text, "absent"));
    TEST_ASSERT_EQUAL_STRING("p1: http 502", text);
}

/* A short key is where the replacement can outgrow the needle; the text after it must
 * survive either way. */
TEST_CASE("redacting a short secret keeps the surrounding text", "[m3]")
{
    char text[] = "bad k in key";
    TEST_ASSERT_EQUAL_size_t(2, llm_redact(text, "k"));
    TEST_ASSERT_EQUAL_STRING("bad * in *ey", text);
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

/* The stored `extra_body` is what reaches the assembler, so its *emitted shape* is
 * the contract, not the document's. This failed before the fix: the loader kept
 * `{"extra_body":{...}}`, the assembler stripped one level of braces as it must, and
 * the wrapper's own key reached the provider — every member under it, level included,
 * was inert on the wire. */
TEST_CASE("the stored extra_body is a flat object, not a wrapper", "[m3]")
{
    json_doc_t *doc = NULL;
    const char *src = "{\"extra_body\":{\"reasoning_effort\":\"high\",\"temperature\":0.2}}";
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(src, strlen(src), &doc));
    char out[256];
    size_t n = llm_flatten_members(json_get(json_root(doc), "extra_body"), out, sizeof out);
    TEST_ASSERT_GREATER_THAN(0, n);
    TEST_ASSERT_NULL(strstr(out, "\"extra_body\""));
    /* Asserted on the emitted document, not its bytes: `put_number` prints a double
     * at full precision (`0.20000000000000001`), which is the same JSON number, so
     * pinning the text would pin the emitter rather than the flattening. */
    json_doc_t *flat = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(out, n, &flat));
    json_node_t *root = json_root(flat);
    TEST_ASSERT_TRUE(json_is_object(root));
    TEST_ASSERT_EQUAL_size_t(2, json_object_len(root));
    TEST_ASSERT_EQUAL_STRING("high",
                             json_string(json_get(root, "reasoning_effort")));
    TEST_ASSERT_TRUE(json_is_number(json_get(root, "temperature")));
    json_free(flat);
    json_free(doc);
}

/* Refused rather than truncated (`02-firmware.md` §llm): a clipped member is not a
 * smaller request, it is a body the provider rejects. */
TEST_CASE("an extra_body that does not fit is refused, not truncated", "[m3]")
{
    json_doc_t *doc = NULL;
    char big[400];
    memset(big, 'x', sizeof big - 1);
    big[sizeof big - 1] = '\0';
    char src[512];
    snprintf(src, sizeof src, "{\"extra_body\":{\"a\":\"%s\"}}", big);
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(src, strlen(src), &doc));
    char out[64];
    out[0] = 'z';
    TEST_ASSERT_EQUAL_size_t(0, llm_flatten_members(json_get(json_root(doc), "extra_body"),
                                                    out, sizeof out));
    /* An empty buffer, never a partial one. */
    TEST_ASSERT_EQUAL_STRING("", out);
    json_free(doc);
}

/* A non-object `extra_body` is no members at all, not a crash: validation refuses it,
 * but the loader runs before any reader has been told why. */
TEST_CASE("a non-object extra_body contributes nothing", "[m3]")
{
    llm_request_t r = {.model = "m", .user_prompt = "x"};
    char out[512];
    TEST_ASSERT_GREATER_THAN(
        0, llm_assemble_prompt(&r, "[]", out, sizeof out));
    TEST_ASSERT_NULL(strstr(out, "extra_body"));
    TEST_ASSERT_NULL(strstr(out, "[]"));
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

/* The body is assembled by hand, so nothing but a parse proves it is one JSON object.
 * `tools` was sliced with `strlen` from the wrapper's colon, which carried the
 * wrapper's closing brace along: the body closed early and every provider answered
 * `invalid character ',' after top-level value`. */
TEST_CASE("the assembled body parses as exactly one JSON object", "[m3]")
{
    llm_request_t r = {.model = "m", .user_prompt = "hi", .system_prompt = "sys"};
    /* The wrapper `agent` actually builds before slicing, put through the slicer, so
     * this covers the shape that broke: a slice that kept the wrapper's closing brace
     * produced `[{...}]}` and the provider refused every request. */
    char wrapped[256];
    snprintf(wrapped, sizeof wrapped,
             "{\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"t\"}}]}");
    char sliced[128];
    TEST_ASSERT_GREATER_THAN(0, llm_slice_member(wrapped, sliced, sizeof sliced));
    TEST_ASSERT_EQUAL_STRING("[{\"type\":\"function\",\"function\":{\"name\":\"t\"}}]",
                             sliced);
    r.tools_json = sliced;
    r.history_json = "[{\"role\":\"assistant\",\"content\":\"hello\"}]";
    r.max_output_tokens = 64;
    char out[2048];
    size_t n = llm_assemble_prompt(&r, "{\"temperature\":0.2}", out, sizeof out);
    TEST_ASSERT_GREATER_THAN(0, n);

    json_doc_t *doc = NULL;
    size_t offset = 0;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse_at(out, n, &doc, &offset));
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_TRUE(json_is_object(json_root(doc)));
    /* One top-level value: a trailing member after the closing brace is a second
     * value, which is what the provider refused. */
    TEST_ASSERT_EQUAL_CHAR('}', out[n - 1]);
    json_free(doc);
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


/* The whole path, in the order `llm_init` and `llm_assemble_prompt` run it: a config
 * `extra_body` object, flattened by the loader, merged by the assembler. This is the
 * shape that was broken on the wire — the wrapper's own `extra_body` key survived the
 * merge, so the provider read a nested object where a top-level member was intended
 * and every member under it, the thinking level included, was inert. */
TEST_CASE("a config extra_body reaches the request root, not as a nested object", "[m3]")
{
    json_doc_t *cfg = NULL;
    const char *src =
        "{\"reasoning_effort\":\"high\",\"temperature\":0.2,\"seed\":7}";
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(src, strlen(src), &cfg));
    char stored[256];
    TEST_ASSERT_GREATER_THAN(0, llm_flatten_members(json_root(cfg), stored,
                                                    sizeof stored));
    json_free(cfg);

    llm_request_t r = {.model = "m", .user_prompt = "x"};
    char body[1024];
    size_t n = llm_assemble_prompt(&r, stored, body, sizeof body);
    TEST_ASSERT_GREATER_THAN(0, n);

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(body, n, &doc));
    json_node_t *root = json_root(doc);
    /* At the root, where the provider reads it. */
    TEST_ASSERT_EQUAL_STRING("high", json_string(json_get(root, "reasoning_effort")));
    TEST_ASSERT_TRUE(json_is_number(json_get(root, "temperature")));
    /* And the wrapper is gone rather than nested one level down. */
    TEST_ASSERT_NULL(json_get(root, "extra_body"));
    json_free(doc);
}

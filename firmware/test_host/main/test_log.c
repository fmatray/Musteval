/* Log bridging decisions: `00-overview.md` D9 and `04-protocol.md` §3.10. The
 * vprintf install is board-bound; what a line becomes is not. */

#include <string.h>

#include "unity.h"

#include "cfg.h"
#include "link.h"

TEST_CASE("the configured level decides what is emitted", "[log]")
{
    /* 03-config.md §device: none/error/warn/info/debug, default info. */
    TEST_ASSERT_NULL(link_log_filter('I', "error"));
    TEST_ASSERT_EQUAL_STRING("error", link_log_filter('E', "error"));

    /* `none` hides everything, including errors: it is the documented way to turn
     * the log stream off entirely. */
    TEST_ASSERT_NULL(link_log_filter('E', "none"));

    TEST_ASSERT_EQUAL_STRING("info", link_log_filter('I', "info"));
    TEST_ASSERT_NULL(link_log_filter('D', "info")); /* debug is below the bar */
    TEST_ASSERT_EQUAL_STRING("debug", link_log_filter('D', "debug"));
    TEST_ASSERT_EQUAL_STRING("warn", link_log_filter('W', "debug"));
}

TEST_CASE("an unknown configured level falls back to info", "[log]")
{
    /* A typo in config.json must not mute the device: `03-config.md` §device has a
     * documented default and this is where it applies. */
    TEST_ASSERT_EQUAL_STRING("info", link_log_filter('I', "louder"));
    TEST_ASSERT_NULL(link_log_filter('D', "louder"));
    TEST_ASSERT_EQUAL_STRING("info", link_log_filter('I', NULL));
}

TEST_CASE("verbose is reported as debug", "[log]")
{
    /* §3.10 lists `verbose`, `03-config.md` §device does not. The finer level is
     * the honest mapping and it stays hidden unless debug is on. */
    TEST_ASSERT_EQUAL_STRING("debug", link_log_filter('V', "debug"));
    TEST_ASSERT_NULL(link_log_filter('V', "info"));
}

TEST_CASE("an unknown level letter is dropped, not guessed", "[log]")
{
    TEST_ASSERT_NULL(link_log_filter('Z', "debug"));
    TEST_ASSERT_NULL(link_log_filter('\0', "debug"));
}

TEST_CASE("a log frame matches the documented shape", "[log]")
{
    char line[512];
    size_t written = 0;

    char msg[] = "provider openai timed out";
    TEST_ASSERT_EQUAL(ESP_OK, link_log_frame("llm", msg, sizeof msg, "warn",
                                             1730563700, 812340, line,
                                             sizeof(line), &written));

    /* Asserted through the parser rather than a pinned string: the id is the
     * shared per-boot counter and would make this test order-dependent, and the
     * canonical key order is already covered by `test_store` and `test_frame`. */
    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(line, written, &doc, &bad));
    TEST_ASSERT_NULL(bad);

    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("log", json_string(json_get(root, "type")));
    TEST_ASSERT_EQUAL_STRING("warn", json_string(json_get(root, "level")));
    TEST_ASSERT_EQUAL_STRING("llm", json_string(json_get(root, "tag")));
    TEST_ASSERT_EQUAL_STRING("provider openai timed out",
                             json_string(json_get(root, "msg")));

    int64_t t_ms = 0, ts = 0, v = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "t_ms"), &t_ms));
    TEST_ASSERT_EQUAL_INT64(812340, t_ms);
    TEST_ASSERT_TRUE(json_int(json_get(root, "ts"), &ts));
    TEST_ASSERT_EQUAL_INT64(1730563700, ts);
    TEST_ASSERT_TRUE(json_int(json_get(root, "v"), &v));
    TEST_ASSERT_EQUAL_INT64(1, v);

    /* A log frame is unsolicited, so it carries no `reply_to`. */
    TEST_ASSERT_NULL(json_get(root, "reply_to"));

    json_free(doc);
}

TEST_CASE("a newline in a log message cannot break framing", "[log]")
{
    /* 04 §1.1 rule 1: no literal newline inside a frame. An unescaped one would
     * split the frame in two and desynchronise the stream, so this is the property
     * that matters most in the hook. */
    char line[512];
    size_t written = 0;

    char escaped[] = "line one\nline two\twith tab \"quoted\"";
    TEST_ASSERT_EQUAL(ESP_OK,
                      link_log_frame("net", escaped, sizeof escaped, "info", 0, 1,
                                     line, sizeof(line), &written));

    /* Exactly one `\n` may appear, and only as the none of a frame: there is none,
     * because the frame has no terminator until it is written. */
    TEST_ASSERT_NULL(strchr(line, '\n'));
    TEST_ASSERT_NULL(strchr(line, '\t'));
    TEST_ASSERT_NOT_NULL(strstr(line, "line one\\nline two"));

    /* The escape survives a round trip rather than being mangled. */
    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(line, written, &doc, &bad));
    const char *msg = json_string(json_get(json_root(doc), "msg"));
    TEST_ASSERT_EQUAL_STRING("line one\nline two\twith tab \"quoted\"", msg);
    json_free(doc);
}

TEST_CASE("an over-long log line is refused, not truncated", "[log]")
{
    char big[400];
    memset(big, 'x', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';

    char small[64];
    size_t written = 999;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_SIZE,
                      link_log_frame("net", big, sizeof big, "info", 0, 0, small,
                                     sizeof(small), &written));
}

TEST_CASE("an ESP-IDF V1 log line is split into its parts", "[log]")
{
    /* V1 composes `I (1234) tag: message` (log/include/esp_log_format.h). */
    const char *line = "I (1234) net: associated with example\n";
    link_log_line_t p;

    TEST_ASSERT_TRUE(link_log_parse(line, strlen(line), &p));
    TEST_ASSERT_EQUAL_CHAR('I', p.level_letter);
    TEST_ASSERT_EQUAL_INT64(1234, p.t_ms);
    TEST_ASSERT_EQUAL_size_t(3, p.tag_len);
    TEST_ASSERT_EQUAL_STRING_LEN("net", p.tag, p.tag_len);
    TEST_ASSERT_EQUAL_STRING_LEN("associated with example", p.msg, p.msg_len);
}

TEST_CASE("the message keeps colons but the tag ends at the first separator", "[log]")
{
    const char *line = "E (9) llm: failed: E_LLM_TIMEOUT after 30000 ms\n";
    link_log_line_t p;

    TEST_ASSERT_TRUE(link_log_parse(line, strlen(line), &p));
    TEST_ASSERT_EQUAL_CHAR('E', p.level_letter);
    TEST_ASSERT_EQUAL_STRING_LEN("llm", p.tag, p.tag_len);
    TEST_ASSERT_EQUAL_STRING_LEN("failed: E_LLM_TIMEOUT after 30000 ms", p.msg,
                                 p.msg_len);
}

TEST_CASE("a colour-wrapped line still parses", "[log]")
{
    /* CONFIG_LOG_COLORS is off here, but flipping it is a menuconfig switch and a
     * hook that broke on colour codes would be a trap. */
    const char *line = "\033[0;32mI (77) boot: starting\033[0m\n";
    link_log_line_t p;

    TEST_ASSERT_TRUE(link_log_parse(line, strlen(line), &p));
    TEST_ASSERT_EQUAL_CHAR('I', p.level_letter);
    TEST_ASSERT_EQUAL_INT64(77, p.t_ms);
    TEST_ASSERT_EQUAL_STRING_LEN("boot", p.tag, p.tag_len);
    TEST_ASSERT_EQUAL_STRING_LEN("starting", p.msg, p.msg_len);
}

TEST_CASE("a line that is not an ESP_LOGx line is refused", "[log]")
{
    /* The hook falls back to plain output for these, so refusing is what keeps
     * arbitrary printf output off the link. */
    link_log_line_t p;

    TEST_ASSERT_FALSE(link_log_parse("hello world", 11, &p));
    TEST_ASSERT_FALSE(link_log_parse("Z (1) tag: msg", 13, &p)); /* no level */
    TEST_ASSERT_FALSE(link_log_parse("I (1) tag-no-colon", 16, &p));
    TEST_ASSERT_FALSE(link_log_parse("", 0, &p));
    TEST_ASSERT_FALSE(link_log_parse("I", 1, &p));
}

TEST_CASE("the hook's frame shape survives the round trip", "[log]")
{
    /* End to end for the pure half: line -> parts -> frame -> parse. This is the
     * path every ESP_LOGx takes, minus the vprintf install. */
    const char *line = "W (4242) wifi: retrying \"ssid\"\n";
    link_log_line_t p;
    TEST_ASSERT_TRUE(link_log_parse(line, strlen(line), &p));

    const char *level = link_log_filter(p.level_letter, "info");
    TEST_ASSERT_EQUAL_STRING("warn", level);

    /* Both are slices of the input line, not C strings. */
    char tag[32];
    memcpy(tag, p.tag, p.tag_len);
    tag[p.tag_len] = '\0';
    char msg[64];
    memcpy(msg, p.msg, p.msg_len);
    msg[p.msg_len] = '\0';

    char frame[512];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, link_log_frame(tag, msg, sizeof msg, level, 0,
                                             p.t_ms, frame, sizeof(frame) - 2,
                                             &written));

    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(frame, written, &doc, &bad));
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("wifi", json_string(json_get(root, "tag")));
    TEST_ASSERT_EQUAL_STRING("retrying \"ssid\"", json_string(json_get(root, "msg")));
    int64_t t_ms = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "t_ms"), &t_ms));
    TEST_ASSERT_EQUAL_INT64(4242, t_ms);
    json_free(doc);
}

/* --- the vprintf hook `link.c` installs ----------------------------------- */

/* The hook is only reachable through `esp_log_set_vprintf`, so this drives it the
 * way the logging task does: install it, log, and assert on the bytes that reach
 * the writer. `esp_log_set_vprintf` both installs `log_hook` and hands back the
 * previous handler, so the hook itself can be called with any format — which is
 * how the pass-through paths are reached without a fake log call. */
#include <stdarg.h>

#include "esp_log.h"

static vprintf_like_t hook_of(void)
{
    /* Install, then take `log_hook` straight back out: the return value is the
     * handler that was in place, which is the hook once `link_log_install` ran. */
    link_log_install("info");
    vprintf_like_t old = esp_log_set_vprintf(NULL);
    esp_log_set_vprintf(old);
    return old;
}

static int call_hook(vprintf_like_t fn, const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    int n = fn(fmt, args);
    va_end(args);
    return n;
}

/* Frames the hook emits, counted rather than parsed, so a pass-through is proven
 * by the absence of a frame. */
static char s_hook_sent[LINK_TX_MAX + 8];
static size_t s_hook_len;
static int s_hook_calls;

static esp_err_t hook_writer(const char *buf, size_t len)
{
    if (len > sizeof s_hook_sent) return ESP_ERR_INVALID_SIZE;
    memcpy(s_hook_sent, buf, len);
    s_hook_len = len;
    s_hook_calls++;
    return ESP_OK;
}

/* `04-protocol.md` §3.10: an ESP_LOGx line becomes a `log` frame carrying the
 * level name, the tag, the message and `t_ms` from the line's timestamp. `ts` is 0
 * because wall time belongs to `sys` and is unset before SNTP. */
TEST_CASE("an ESP_LOGx line becomes a log frame on the writer", "[log]")
{
    vprintf_like_t old = hook_of();
    TEST_ASSERT_NOT_NULL(old);

    link_set_writer(hook_writer);
    s_hook_len = 0;
    s_hook_calls = 0;

    ESP_LOGI("boot", "starting");
    TEST_ASSERT_EQUAL_INT(1, s_hook_calls);
    TEST_ASSERT_TRUE(s_hook_len > 2);
    TEST_ASSERT_EQUAL_CHAR('\r', s_hook_sent[s_hook_len - 2]);
    TEST_ASSERT_EQUAL_CHAR('\n', s_hook_sent[s_hook_len - 1]);

    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK,
                      link_frame_parse(s_hook_sent, s_hook_len - 2, &doc, &bad));
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("log", json_string(json_get(root, "type")));
    TEST_ASSERT_EQUAL_STRING("info", json_string(json_get(root, "level")));
    TEST_ASSERT_EQUAL_STRING("boot", json_string(json_get(root, "tag")));
    TEST_ASSERT_EQUAL_STRING("starting", json_string(json_get(root, "msg")));
    int64_t ts = -1, t_ms = -1;
    TEST_ASSERT_TRUE(json_int(json_get(root, "ts"), &ts));
    TEST_ASSERT_EQUAL_INT(0, ts);
    TEST_ASSERT_TRUE(json_int(json_get(root, "t_ms"), &t_ms));
    TEST_ASSERT_TRUE(t_ms >= 0);
    json_free(doc);

    esp_log_set_vprintf(old);
}

/* The hook must not turn arbitrary `printf` output into frames, and a line the
 * configured level hides is dropped rather than framed: both return the rendered
 * length so the caller sees success, and neither writes. */
TEST_CASE("the hook passes through what is not a frame or is filtered out", "[log]")
{
    vprintf_like_t old = hook_of();
    TEST_ASSERT_NOT_NULL(old);

    link_set_writer(hook_writer);
    s_hook_calls = 0;

    TEST_ASSERT_EQUAL_INT(12, call_hook(old, "hello %s\n", "there"));
    TEST_ASSERT_EQUAL_INT(0, s_hook_calls);

    /* `configured` is read on every call, so `error` hides an info line. */
    link_log_install("error");
    int n = call_hook(old, "I (5) boot: hidden\n");
    TEST_ASSERT_EQUAL_INT(19, n);
    TEST_ASSERT_EQUAL_INT(0, s_hook_calls);

    /* A format the C library refuses is not a frame either. */
    TEST_ASSERT_EQUAL_INT(0, call_hook(old, "%"));
    TEST_ASSERT_EQUAL_INT(0, s_hook_calls);

    link_log_install("info");
    esp_log_set_vprintf(old);
}

/* Re-entrancy guard: a log call made while the hook is formatting — here from the
 * writer it calls — must go to the previous handler instead of recursing into
 * another frame. */
static esp_err_t logging_writer(const char *buf, size_t len)
{
    s_hook_calls++;
    ESP_LOGI("recursive", "inside the writer");
    return len <= sizeof s_hook_sent ? ESP_OK : ESP_ERR_INVALID_SIZE;
}

TEST_CASE("a log from inside the hook does not recurse into a frame", "[log]")
{
    vprintf_like_t old = hook_of();
    TEST_ASSERT_NOT_NULL(old);
    link_set_writer(logging_writer);
    s_hook_calls = 0;

    ESP_LOGI("outer", "one frame");
    TEST_ASSERT_EQUAL_INT(1, s_hook_calls);

    esp_log_set_vprintf(old);
}

/* Installing twice keeps the first saved handler: replacing it with the hook again
 * would make the hook its own pass-through and lose the real output. */
TEST_CASE("installing the hook twice keeps the first previous handler", "[log]")
{
    void *first = link_log_install("info");
    void *second = link_log_install("debug");
    TEST_ASSERT_NOT_NULL(first);
    TEST_ASSERT_EQUAL_PTR(first, second);
}

TEST_CASE("a secret value never leaves in a log frame", "[log]")
{
    /* `06-security.md` §7 rule T2. Armed the way `cfg_secrets_reload` arms it, then
     * framed and parsed back through the real builder and the real parser: the
     * assertion is on what the wire would carry, not on a helper being called. */
    cfg_secrets_clear();
    TEST_ASSERT_TRUE(cfg_secrets_add("sk-live-SENTINEL"));

    char msg[] = "POST https://api.example/v1 failed, key=sk-live-SENTINEL";
    char frame[512];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK,
                      link_log_frame("llm", msg, sizeof msg, "error", 0, 12, frame,
                                     sizeof(frame), &written));

    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(frame, written, &doc, &bad));
    const char *out = json_string(json_get(json_root(doc), "msg"));
    TEST_ASSERT_NOT_NULL(out);
    TEST_ASSERT_NULL(strstr(out, "sk-live-SENTINEL"));
    TEST_ASSERT_EQUAL_STRING("POST https://api.example/v1 failed, key=[redacted]",
                             out);
    json_free(doc);

    /* Cleared, the same message passes through: the scrub is the snapshot, not an
     * unconditional rewrite of every frame. */
    cfg_secrets_clear();
    char plain[] = "POST failed, key=sk-live-SENTINEL";
    TEST_ASSERT_EQUAL(ESP_OK,
                      link_log_frame("llm", plain, sizeof plain, "error", 0, 13,
                                     frame, sizeof(frame), &written));
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(frame, written, &doc, &bad));
    TEST_ASSERT_EQUAL_STRING("POST failed, key=sk-live-SENTINEL",
                             json_string(json_get(json_root(doc), "msg")));
    json_free(doc);
}

TEST_CASE("a secret too large to hold whole is refused, not truncated", "[log]")
{
    /* A truncated secret would scrub its prefix and leave the tail on the wire,
     * which reads as redacted and is not. The snapshot therefore refuses a value it
     * cannot store whole instead of storing part of it. */
    cfg_secrets_clear();
    static char huge[2600];
    memset(huge, 'q', sizeof huge - 1);
    huge[sizeof huge - 1] = '\0';
    TEST_ASSERT_FALSE(cfg_secrets_add(huge));

    /* Nothing was armed, so a sentinel that would otherwise match passes through
     * unchanged: the refusal is the behaviour, not a side effect. */
    char msg[] = "qqqq-and-a-tail-SENTINEL";
    char frame[512];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, link_log_frame("net", msg, sizeof msg, "info", 0, 0,
                                            frame, sizeof(frame), &written));
    TEST_ASSERT_EQUAL_STRING("qqqq-and-a-tail-SENTINEL", msg);
    TEST_ASSERT_NOT_NULL(strstr(frame, "SENTINEL"));
}

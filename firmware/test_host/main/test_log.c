/* Log bridging decisions: `00-overview.md` D9 and `04-protocol.md` §3.10. The
 * vprintf install is board-bound; what a line becomes is not. */

#include <string.h>

#include "unity.h"

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

    TEST_ASSERT_EQUAL(ESP_OK, link_log_frame("llm", "provider openai timed out",
                                             "warn", 1730563700, 812340, line,
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

    TEST_ASSERT_EQUAL(ESP_OK,
                      link_log_frame("net", "line one\nline two\twith tab \"quoted\"",
                                     "info", 0, 1, line, sizeof(line), &written));

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
                      link_log_frame("net", big, "info", 0, 0, small, sizeof(small),
                                     &written));
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
    TEST_ASSERT_EQUAL(ESP_OK, link_log_frame(tag, msg, level, 0, p.t_ms, frame,
                                             sizeof(frame) - 2, &written));

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

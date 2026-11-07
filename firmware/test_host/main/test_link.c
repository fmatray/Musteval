/* NDJSON framing and envelope rules, asserted byte by byte against the frozen
 * contract in `04-protocol.md` §1.1, §1.2 and §2. */

#include <stdlib.h>
#include <string.h>

#include "unity.h"

#include "link.h"

static size_t feed(const char *bytes, size_t len, link_line_status_t *status)
{
    static link_reader_t reader;
    size_t line_len = 0;
    bool done = false;

    link_reader_reset(&reader);
    for (size_t i = 0; i < len; i++) {
        size_t n = 0;
        if (link_reader_feed(&reader, bytes[i], &n, status)) {
            done = true;
            line_len = n;
        }
    }
    TEST_ASSERT_TRUE_MESSAGE(done, "no line completed");
    return line_len;
}

TEST_CASE("a newline completes the line", "[link]")
{
    link_line_status_t status;
    size_t n = feed("{\"a\":1}\n", 8, &status);

    TEST_ASSERT_EQUAL(LINK_LINE_OK, status);
    TEST_ASSERT_EQUAL_size_t(7, n);
}

TEST_CASE("a CRLF terminator drops the CR and does not count it", "[link]")
{
    link_line_status_t status;
    size_t n = feed("{\"a\":1}\r\n", 9, &status);

    TEST_ASSERT_EQUAL(LINK_LINE_OK, status);
    TEST_ASSERT_EQUAL_size_t(7, n);
}

TEST_CASE("a line split across feeds still decodes", "[link]")
{
    static link_reader_t reader;
    link_line_status_t status = LINK_LINE_BAD_JSON;
    size_t line_len = 0;

    link_reader_reset(&reader);
    const char *head = "{\"a\"";
    const char *tail = ":1}\n";
    for (const char *p = head; *p; p++) {
        TEST_ASSERT_FALSE(link_reader_feed(&reader, *p, &line_len, &status));
    }
    bool done = false;
    for (const char *p = tail; *p; p++) {
        done = link_reader_feed(&reader, *p, &line_len, &status);
    }

    TEST_ASSERT_TRUE(done);
    TEST_ASSERT_EQUAL(LINK_LINE_OK, status);
    TEST_ASSERT_EQUAL_size_t(7, line_len);
    TEST_ASSERT_EQUAL_STRING("{\"a\":1}", link_reader_line(&reader));
}

TEST_CASE("an empty line is not a frame", "[link]")
{
    link_line_status_t status;
    feed("\n", 1, &status);
    TEST_ASSERT_EQUAL(LINK_LINE_BAD_JSON, status);
}

TEST_CASE("an oversize line is rejected once and does not desync", "[link]")
{
    static link_reader_t reader;
    link_line_status_t status = LINK_LINE_OK;
    size_t line_len = 0;
    size_t reports = 0;

    link_reader_reset(&reader);
    /* `04-protocol.md` §1.2: overflow is discarded up to the newline, so exactly
     * one report is produced and the next line is still readable. */
    for (size_t i = 0; i < LINK_RX_MAX + 100; i++) {
        if (link_reader_feed(&reader, 'x', &line_len, &status)) {
            reports++;
        }
    }
    TEST_ASSERT_TRUE(link_reader_feed(&reader, '\n', &line_len, &status));
    TEST_ASSERT_EQUAL(LINK_LINE_OVERSIZE, status);
    reports++;
    TEST_ASSERT_EQUAL_size_t(1, reports);

    bool done = false;
    const char *next = "{\"ok\":1}\n";
    for (const char *p = next; *p; p++) {
        done = link_reader_feed(&reader, *p, &line_len, &status);
    }
    TEST_ASSERT_TRUE_MESSAGE(done, "reader desynchronised after an oversize line");
    TEST_ASSERT_EQUAL(LINK_LINE_OK, status);
    TEST_ASSERT_EQUAL_STRING("{\"ok\":1}", link_reader_line(&reader));
}

TEST_CASE("the envelope carries v, type, id and ts", "[link]")
{
    char out[256];
    size_t written = 0;

    TEST_ASSERT_EQUAL(ESP_OK, link_envelope_wrap(NULL, "event", "d-1", 42, NULL,
                                                 out, sizeof out, &written));
    TEST_ASSERT_EQUAL_STRING("{\"v\":1,\"type\":\"event\",\"id\":\"d-1\",\"ts\":42}",
                             out);
    TEST_ASSERT_EQUAL_size_t(strlen(out), written);
}

TEST_CASE("the envelope omits reply_to when there is none", "[link]")
{
    char out[256];
    size_t written = 0;

    TEST_ASSERT_EQUAL(ESP_OK, link_envelope_wrap(NULL, "event", "d-2", 42, "b-1",
                                                 out, sizeof out, &written));
    TEST_ASSERT_NOT_EQUAL(NULL, strstr(out, "\"reply_to\":\"b-1\""));
}

TEST_CASE("an envelope id outside the frozen charset is refused", "[link]")
{
    char out[256];
    size_t written = 0;

    /* `04-protocol.md` §2: 1..32 chars from [A-Za-z0-9._-]. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      link_envelope_wrap(NULL, "event", "", 0, NULL, out,
                                         sizeof out, &written));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      link_envelope_wrap(NULL, "event", "d 1", 0, NULL, out,
                                         sizeof out, &written));
}

TEST_CASE("device ids are allocated per boot and never reused", "[link]")
{
    /* `04` §2 gives the device one counter, so the two tasks that emit cannot each
     * keep one: `d-1` and `a-1` existing together would make "never reused" false.
     * The caller owns the buffer because the next allocation must not overwrite an
     * id that has not been copied into its frame yet. The counter is
     * process-global, so the assertion is relative — an earlier test spends ids. */
    char first[16];
    char second[16];

    link_next_id(first, sizeof first);
    link_next_id(second, sizeof second);

    TEST_ASSERT_EQUAL_STRING("d-", strncmp(first, "d-", 2) == 0 ? "d-" : first);
    TEST_ASSERT_TRUE(strcmp(first, second) != 0);

    /* Monotonic by exactly one: `d-<n>` then `d-<n+1>`, and the earlier buffer is
     * untouched by the later allocation. */
    unsigned long a = strtoul(first + 2, NULL, 10);
    unsigned long b = strtoul(second + 2, NULL, 10);
    TEST_ASSERT_EQUAL_UINT32((uint32_t)(a + 1), (uint32_t)b);
    TEST_ASSERT_EQUAL_UINT32((uint32_t)a,
                             (uint32_t)strtoul(first + 2, NULL, 10));
}

/* --- §1.2 refusals -------------------------------------------------------- */

/* Captures the single frame the emit path writes, so the assertions are on the
 * bytes that would reach the CDC port rather than on internal state. */
static char s_sent[LINK_TX_MAX + 8];
static size_t s_sent_len;

static esp_err_t capture_writer(const char *buf, size_t len)
{
    if (len > sizeof(s_sent)) return ESP_ERR_INVALID_SIZE;
    memcpy(s_sent, buf, len);
    s_sent_len = len;
    return ESP_OK;
}

static json_doc_t *sent_doc(void)
{
    /* The writer receives frame + CRLF; stop at the CR so the parser sees the frame. */
    size_t n = 0;
    while (n < s_sent_len && s_sent[n] != '\r') n++;
    TEST_ASSERT_TRUE(n > 0);
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(s_sent, n, &doc));
    TEST_ASSERT_NOT_NULL(doc);
    return doc;
}

/* `04-protocol.md` §1.2: an oversize line answers `command.result` when an `id` is
 * recoverable, and the event's `dropped_bytes` reports the real length. Before the
 * reader kept its prefix this branch was unreachable and `dropped_bytes` was a
 * hardcoded 0, which the board reproduced: a 5000-byte command answered
 * `event{protocol.error, dropped_bytes:0}` instead of `command.result`. */
TEST_CASE("an oversize line still exposes its id prefix", "[link]")
{
    link_reader_t reader;
    link_reader_reset(&reader);
    const char *head = "{\"v\":1,\"type\":\"command\",\"id\":\"b-77\",\"name\":\"status\",\"args\":{\"x\":\"";
    size_t line_len = 0;
    link_line_status_t status = LINK_LINE_OK;
    bool done = false;
    for (const char *p = head; *p != '\0'; p++) {
        done = link_reader_feed(&reader, *p, &line_len, &status);
        TEST_ASSERT_FALSE(done);
    }
    /* Fill past the cap, then terminate the line: the reader only completes on a
     * newline, so a `while (!done)` here would spin forever. */
    int extra = 0;
    while (!reader.discarding) {
        done = link_reader_feed(&reader, 'A', &line_len, &status);
        extra++;
        TEST_ASSERT_FALSE(done);
    }
    TEST_ASSERT_FALSE(done); /* still inside the overlong line */
    done = link_reader_feed(&reader, '\n', &line_len, &status);
    TEST_ASSERT_TRUE(done);
    TEST_ASSERT_EQUAL(LINK_LINE_OVERSIZE, status);
    TEST_ASSERT_EQUAL(strlen(head) + (size_t)extra, line_len);

    char out[33];
    TEST_ASSERT_EQUAL_STRING("b-77", link_recover_id(link_reader_line(&reader),
                                                    line_len, out, sizeof out));
}

TEST_CASE("an oversize line without an id reports the bytes it dropped", "[link]")
{
    link_set_writer(capture_writer);
    const char *head = "{\"v\":1,\"type\":\"";
    size_t n = 6000;
    char *line = calloc(n + 1, 1);
    TEST_ASSERT_NOT_NULL(line);
    memcpy(line, head, strlen(head));
    memset(line + strlen(head), 'A', n - strlen(head));
    line[n] = '\0';

    TEST_ASSERT_EQUAL(ESP_OK, link_refuse_line(line, n, "", "malformed frame", 1000));
    json_doc_t *doc = sent_doc();
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("event", json_string(json_get(root, "type")));
    int64_t dropped = 0;
    TEST_ASSERT_TRUE(json_int(json_get(json_get(root, "data"), "dropped_bytes"), &dropped));
    TEST_ASSERT_EQUAL_INT64((int64_t)n, dropped);
    json_free(doc);
    free(line);
}

TEST_CASE("a line that is not JSON names the parse offset", "[link]")
{
    json_doc_t *doc = NULL;
    const char *bad = "{\"v\":1,\"type\":\"command\", nope}";
    size_t offset = 0;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, json_parse_at(bad, strlen(bad), &doc, &offset));
    TEST_ASSERT_NULL(doc);
    /* `nope` starts at byte 26. cJSON reports the position it stopped at, which is
     * that token: assert the location, not cJSON's exact convention, so a version
     * bump that still points at the bad token does not fail the test. */
    TEST_ASSERT_GREATER_OR_EQUAL_size_t(26, offset);
    TEST_ASSERT_LESS_THAN_size_t(strlen(bad), offset);

    const char *trailing = "{\"v\":1} junk";
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, json_parse_at(trailing, strlen(trailing), &doc, &offset));
    TEST_ASSERT_EQUAL_size_t(8, offset);
}

TEST_CASE("a refused line with a recoverable id answers command.result", "[link]")
{
    link_set_writer(capture_writer);
    const char *line = "{\"v\":1,\"type\":\"command\",\"id\":\"b-77\",\"name\":\"status\"}";

    TEST_ASSERT_EQUAL(ESP_OK, link_refuse_line(line, strlen(line), "status",
                                              "missing ts", 1000));

    json_doc_t *doc = sent_doc();
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("command.result", json_string(json_get(root, "type")));
    TEST_ASSERT_EQUAL_STRING("b-77", json_string(json_get(root, "reply_to")));
    TEST_ASSERT_EQUAL_STRING("status", json_string(json_get(root, "name")));
    bool ok = true;
    TEST_ASSERT_TRUE(json_bool(json_get(root, "ok"), &ok));
    TEST_ASSERT_FALSE(ok);

    json_node_t *err = json_get(root, "error");
    TEST_ASSERT_EQUAL_STRING("E_PROTOCOL", json_string(json_get(err, "code")));
    TEST_ASSERT_EQUAL_STRING("missing ts", json_string(json_get(err, "message")));
    json_free(doc);
}

TEST_CASE("a refused line with no id becomes a protocol.error event", "[link]")
{
    link_set_writer(capture_writer);
    const char *line = "not json at all";

    TEST_ASSERT_EQUAL(ESP_OK, link_refuse_line(line, strlen(line), NULL,
                                              "not valid JSON", 1000));

    json_doc_t *doc = sent_doc();
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("event", json_string(json_get(root, "type")));
    TEST_ASSERT_NULL(json_get(root, "reply_to"));
    TEST_ASSERT_EQUAL_STRING("protocol.error", json_string(json_get(root, "kind")));

    json_node_t *data = json_get(root, "data");
    TEST_ASSERT_EQUAL_STRING("E_PROTOCOL", json_string(json_get(data, "code")));
    int64_t dropped = 0;
    TEST_ASSERT_TRUE(json_int(json_get(data, "dropped_bytes"), &dropped));
    TEST_ASSERT_EQUAL_INT64((int64_t)strlen(line), dropped);
    json_free(doc);
}

TEST_CASE("an id is recovered from a line that does not parse", "[link]")
{
    char out[33]; /* `04` §2: ids are 1..32 characters */

    /* Whitespace around the colon is legal JSON, so it must still be found. */
    const char *spaced = "{\"id\" :  \"b-9\" , broken";
    TEST_ASSERT_EQUAL_STRING("b-9", link_recover_id(spaced, strlen(spaced), out,
                                                    sizeof(out)));

    /* Absent, unquoted, empty, and outside the frozen charset are all no answer. */
    const char *none = "{\"v\":1}";
    TEST_ASSERT_NULL(link_recover_id(none, strlen(none), out, sizeof(out)));

    const char *unquoted = "{\"id\":7}";
    TEST_ASSERT_NULL(link_recover_id(unquoted, strlen(unquoted), out, sizeof(out)));

    const char *empty = "{\"id\":\"\"}";
    TEST_ASSERT_NULL(link_recover_id(empty, strlen(empty), out, sizeof(out)));

    const char *bad = "{\"id\":\"a b\"}";
    TEST_ASSERT_NULL(link_recover_id(bad, strlen(bad), out, sizeof(out)));

    const char *unterminated = "{\"id\":\"b-1";
    TEST_ASSERT_NULL(link_recover_id(unterminated, strlen(unterminated), out,
                                     sizeof(out)));
}

TEST_CASE("pong replies to the ping id and carries no body", "[link]")
{
    link_set_writer(capture_writer);
    TEST_ASSERT_EQUAL(ESP_OK, link_emit_pong("b-6", 1730563500));

    json_doc_t *doc = sent_doc();
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("pong", json_string(json_get(root, "type")));
    TEST_ASSERT_EQUAL_STRING("b-6", json_string(json_get(root, "reply_to")));
    /* The id is a shared per-boot counter, so it cannot be pinned here; assert the
     * shape the codec requires instead of a literal. */
    const char *id = json_string(json_get(root, "id"));
    TEST_ASSERT_NOT_NULL(id);
    TEST_ASSERT_EQUAL_MEMORY("d-", id, 2);
    int64_t ts = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "ts"), &ts));
    TEST_ASSERT_EQUAL_INT64(1730563500, ts);
    json_free(doc);
}

TEST_CASE("every refusal frame is a legal envelope on the wire", "[link]")
{
    /* A hand-built envelope is easy to get wrong, so re-read each frame as bytes:
     * the mandatory members must be there and `link_frame_parse` must accept it. */
    link_set_writer(capture_writer);

    const char *with_id = "{\"v\":1,\"type\":\"command\",\"id\":\"b-1\",\"name\":\"x\"}";
    TEST_ASSERT_EQUAL(ESP_OK, link_refuse_line(with_id, strlen(with_id), "x", "why", 5));
    size_t n = 0;
    while (n < s_sent_len && s_sent[n] != '\r') n++;
    json_doc_t *parsed = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(s_sent, n, &parsed, &bad));
    TEST_ASSERT_EQUAL_STRING("command.result", json_string(json_get(json_root(parsed), "type")));
    json_free(parsed);

    TEST_ASSERT_EQUAL(ESP_OK, link_refuse_line("nope", 4, NULL, "why", 5));
    n = 0;
    while (n < s_sent_len && s_sent[n] != '\r') n++;
    parsed = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(s_sent, n, &parsed, &bad));
    TEST_ASSERT_EQUAL_STRING("event", json_string(json_get(json_root(parsed), "type")));
    json_free(parsed);
}

TEST_CASE("hello_ack builder generates correct shape", "[link]")
{
    link_hello_t hello = {
        .device = "test-dev",
        .fw_version = "2.1.0",
        .boot_id = "b-123",
        .time_synced = true,
    };
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_hello_ack(json_root(doc), &hello));

    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("test-dev", json_string(json_get(root, "device")));
    TEST_ASSERT_EQUAL_STRING("2.1.0", json_string(json_get(root, "fw_version")));
    TEST_ASSERT_EQUAL_STRING("b-123", json_string(json_get(root, "boot_id")));
    bool ts = false;
    TEST_ASSERT_TRUE(json_bool(json_get(root, "time_synced"), &ts));
    TEST_ASSERT_TRUE(ts);

    json_node_t *limits = json_get(root, "limits");
    TEST_ASSERT_NOT_NULL(limits);
    int64_t rx = 0;
    TEST_ASSERT_TRUE(json_int(json_get(limits, "rx_max"), &rx));
    TEST_ASSERT_EQUAL_INT(LINK_RX_MAX, rx);

    json_node_t *caps = json_get(root, "capabilities");
    TEST_ASSERT_NOT_NULL(caps);
    TEST_ASSERT_NOT_NULL(json_get(caps, "tools"));
    TEST_ASSERT_NOT_NULL(json_get(caps, "mcp"));

    json_free(doc);
}

TEST_CASE("status builder generates correct shape", "[link]")
{
    link_status_t st = {
        .heap_free = 1000,
        .psram_free = 2000,
        .safe_mode = false,
        .boot_failures = 5,
        .uptime_s = 123,
        .boot_id = "b-456",
        .userfs_used = 11,
        .userfs_total = 22,
        .wsfs_used = 33,
        .wsfs_total = 44,
    };
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));

    json_node_t *root = json_root(doc);
    int64_t uptime = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "uptime_s"), &uptime));
    TEST_ASSERT_EQUAL_INT(123, uptime);
    TEST_ASSERT_EQUAL_STRING("b-456", json_string(json_get(root, "boot_id")));

    json_node_t *mem = json_get(root, "mem");
    TEST_ASSERT_NOT_NULL(mem);
    int64_t heap = 0, psram = 0;
    TEST_ASSERT_TRUE(json_int(json_get(mem, "heap_free"), &heap));
    TEST_ASSERT_TRUE(json_int(json_get(mem, "psram_free"), &psram));
    TEST_ASSERT_EQUAL_INT(1000, heap);
    TEST_ASSERT_EQUAL_INT(2000, psram);

    json_node_t *sys = json_get(root, "sys");
    TEST_ASSERT_NOT_NULL(sys);
    bool sm = true;
    TEST_ASSERT_TRUE(json_bool(json_get(sys, "safe_mode"), &sm));
    TEST_ASSERT_FALSE(sm);
    int64_t failures = 0;
    TEST_ASSERT_TRUE(json_int(json_get(sys, "boot_failures"), &failures));
    TEST_ASSERT_EQUAL_INT(5, failures);

    /* `04` §3.9 nests usage under `storage`, and the bridge bar reads
     * `storage.wsfs.used_bytes`. A flat `{used,total}` answer left `workspace`
     * blank on the bar, so both filesystems are pinned here. */
    json_node_t *storage = json_get(root, "storage");
    TEST_ASSERT_NOT_NULL(storage);
    json_node_t *ufs = json_get(storage, "userfs");
    json_node_t *wfs = json_get(storage, "wsfs");
    TEST_ASSERT_NOT_NULL(ufs);
    TEST_ASSERT_NOT_NULL(wfs);

    int64_t used = 0, total = 0, quota = 0;
    TEST_ASSERT_TRUE(json_int(json_get(ufs, "used_bytes"), &used));
    TEST_ASSERT_EQUAL_INT(11, used);
    TEST_ASSERT_TRUE(json_int(json_get(ufs, "total_bytes"), &total));
    TEST_ASSERT_EQUAL_INT(22, total);
    TEST_ASSERT_TRUE(json_int(json_get(wfs, "used_bytes"), &used));
    TEST_ASSERT_EQUAL_INT(33, used);
    TEST_ASSERT_TRUE(json_int(json_get(wfs, "total_bytes"), &total));
    TEST_ASSERT_EQUAL_INT(44, total);
    TEST_ASSERT_TRUE(json_int(json_get(storage, "workspace_quota_bytes"), &quota));
    TEST_ASSERT_EQUAL_INT(STORE_WS_TOTAL_BYTES, quota);

    /* §3.9 also requires every subsystem key to exist even when it is idle:
     * a missing key and an idle one must not look the same to the bridge. */
    const char *const required[] = {"wifi", "time", "llm", "agent", "mcp", "mqtt", "geo"};
    for (size_t i = 0; i < sizeof required / sizeof required[0]; i++) {
        json_node_t *node = json_get(root, required[i]);
        TEST_ASSERT_NOT_NULL_MESSAGE(node, required[i]);
    }

    json_free(doc);
}

TEST_CASE("link_emit_body wraps body in envelope", "[link]")
{
    link_set_writer(capture_writer);
    json_doc_t *body = json_new_object();
    json_add_int(json_root(body), "val", 42);

    TEST_ASSERT_EQUAL(ESP_OK, link_emit_body("event", "d-1", 100, "b-1", body));

    json_doc_t *doc = sent_doc();
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("event", json_string(json_get(root, "type")));
    TEST_ASSERT_EQUAL_STRING("d-1", json_string(json_get(root, "id")));
    int64_t ts = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "ts"), &ts));
    TEST_ASSERT_EQUAL_INT(100, ts);
    TEST_ASSERT_EQUAL_STRING("b-1", json_string(json_get(root, "reply_to")));
    int64_t val = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "val"), &val));
    TEST_ASSERT_EQUAL_INT(42, val);

    json_free(body);
    json_free(doc);
}

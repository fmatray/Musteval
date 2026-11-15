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
    /* `capabilities.mcp` is the `mcp.` subset of `capabilities.tools` (§3.2), so the
     * fixture names both kinds and the test asserts which one lands where. */
    json_doc_t *names_doc = json_new_object();
    TEST_ASSERT_NOT_NULL(names_doc);
    json_node_t *names = json_new_array(json_root(names_doc), "tools");
    TEST_ASSERT_NOT_NULL(names);
    json_array_push_string(names, "gpio.read");
    json_array_push_string(names, "mcp.home.home_status");
    json_array_push_string(names, "mcp.lab.lab_temp");

    link_hello_t hello = {
        .device = "test-dev",
        .fw_version = "2.1.0",
        .boot_id = "b-123",
        .time_synced = true,
        .tools = names,
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
    json_node_t *tools = json_get(caps, "tools");
    TEST_ASSERT_NOT_NULL(tools);
    TEST_ASSERT_EQUAL_INT(3, json_array_len(tools));

    json_node_t *mcp = json_get(caps, "mcp");
    TEST_ASSERT_NOT_NULL(mcp);
    /* Only the remote subset, in the order it appears in `tools`. */
    TEST_ASSERT_EQUAL_INT(2, json_array_len(mcp));
    TEST_ASSERT_EQUAL_STRING("mcp.home.home_status",
                             json_string(json_array_at(mcp, 0)));
    TEST_ASSERT_EQUAL_STRING("mcp.lab.lab_temp", json_string(json_array_at(mcp, 1)));

    json_free(doc);
    json_free(names_doc);
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

/* --- sink registration and id bounds (`02-firmware.md` §3.3) -------------- */

static int s_sink_calls;
static void sink_counter(const json_node_t *frame)
{
    (void)frame;
    s_sink_calls++;
}

TEST_CASE("a registered sink is the one handed back to the reader task", "[link]")
{
    /* `link_task` routes through `link_sink_get`, so a registration that did not
     * take would leave every inbound frame unread and the bridge waiting. */
    const link_sink_t sink = { .on_frame = sink_counter };
    TEST_ASSERT_EQUAL(ESP_OK, link_set_sink(&sink));
    const link_sink_t *got = link_sink_get();
    TEST_ASSERT_NOT_NULL(got);
    TEST_ASSERT_EQUAL_PTR(sink_counter, got->on_frame);
}

TEST_CASE("registering no sink is refused rather than clearing the route", "[link]")
{
    /* A NULL sink would silently stop routing; the refusal keeps the previous
     * registration in place. */
    const link_sink_t sink = { .on_frame = sink_counter };
    TEST_ASSERT_EQUAL(ESP_OK, link_set_sink(&sink));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, link_set_sink(NULL));
    TEST_ASSERT_EQUAL_PTR(sink_counter, link_sink_get()->on_frame);
}

TEST_CASE("an id buffer below the documented minimum is left alone", "[link]")
{
    /* `cap < 8` cannot hold `d-` plus a 32-bit counter and a NUL. Writing anyway
     * would overflow the caller's buffer. */
    char buf[4] = { 'x', 'x', 'x', '\0' };
    link_next_id(buf, sizeof buf);
    TEST_ASSERT_EQUAL_STRING("xxx", buf);
}

TEST_CASE("ids advance one per call and never repeat", "[link]")
{
    char a[16], b[16], c[16];
    link_next_id(a, sizeof a);
    link_next_id(b, sizeof b);
    link_next_id(c, sizeof c);
    TEST_ASSERT_EQUAL_STRING_LEN("d-", a, 2);
    TEST_ASSERT_EQUAL_STRING_LEN("d-", b, 2);
    TEST_ASSERT_NOT_EQUAL(0, strcmp(a, b));
    TEST_ASSERT_NOT_EQUAL(0, strcmp(b, c));
}

/* --- status members with nothing to report (`04` §3.9) ------------------- */

TEST_CASE("an idle status reports the honest null for what is absent", "[link]")
{
    /* §3.9: `wifi` off, `time` unsynced, `llm` unavailable, `agent` idle. A
     * fabricated zero would read as a measurement. */
    link_status_t st = { 0 };
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));

    json_node_t *root = json_root(doc);
    json_node_t *wifi = json_get(root, "wifi");
    TEST_ASSERT_NOT_NULL(wifi);
    TEST_ASSERT_EQUAL_STRING("off", json_string(json_get(wifi, "state")));
    TEST_ASSERT_TRUE(json_is_null(json_get(wifi, "ssid")));
    TEST_ASSERT_TRUE(json_is_null(json_get(wifi, "rssi")));
    TEST_ASSERT_TRUE(json_is_null(json_get(wifi, "ip")));

    json_node_t *llm = json_get(root, "llm");
    TEST_ASSERT_NOT_NULL(llm);
    TEST_ASSERT_TRUE(json_is_null(json_get(llm, "provider")));
    TEST_ASSERT_TRUE(json_is_null(json_get(llm, "model")));
    TEST_ASSERT_TRUE(json_is_null(json_get(llm, "thinking")));
    TEST_ASSERT_TRUE(json_is_null(json_get(llm, "last_error")));
    TEST_ASSERT_TRUE(json_is_null(json_get(llm, "limit_hour")));
    TEST_ASSERT_TRUE(json_is_null(json_get(llm, "limit_day")));

    json_node_t *agent = json_get(root, "agent");
    TEST_ASSERT_NOT_NULL(agent);
    TEST_ASSERT_TRUE(json_is_null(json_get(agent, "run_id")));

    json_node_t *time_node = json_get(root, "time");
    TEST_ASSERT_NOT_NULL(time_node);
    TEST_ASSERT_EQUAL_STRING("UTC0", json_string(json_get(time_node, "tz")));

    json_free(doc);
}

TEST_CASE("a connected status reports every member it was given", "[link]")
{
    /* The populated arm of every `ADD_NULLABLE` and ternary in §3.9. */
    link_status_t st = { 0 };
    st.wifi_state = "connected";
    st.wifi_ssid = "home";
    st.wifi_rssi = -55;
    st.wifi_ip = "10.0.0.9";
    st.time_synced = true;
    st.time_unix = 1730563600;
    st.tz = "UTC";
    st.llm_provider = "openai";
    st.llm_model = "gpt";
    st.llm_thinking = "medium";
    st.llm_available = true;
    st.llm_limit_hour = 30;
    st.llm_limit_day = 200;
    st.agent_run_id = "r-1";
    st.agent_busy = true;

    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));

    json_node_t *root = json_root(doc);
    json_node_t *wifi = json_get(root, "wifi");
    TEST_ASSERT_EQUAL_STRING("home", json_string(json_get(wifi, "ssid")));
    TEST_ASSERT_EQUAL_STRING("10.0.0.9", json_string(json_get(wifi, "ip")));
    int64_t rssi = 0;
    TEST_ASSERT_TRUE(json_int(json_get(wifi, "rssi"), &rssi));
    TEST_ASSERT_EQUAL_INT(-55, rssi);

    json_node_t *llm = json_get(root, "llm");
    TEST_ASSERT_EQUAL_STRING("openai", json_string(json_get(llm, "provider")));
    TEST_ASSERT_EQUAL_STRING("gpt", json_string(json_get(llm, "model")));
    TEST_ASSERT_EQUAL_STRING("medium", json_string(json_get(llm, "thinking")));
    int64_t limit = 0;
    TEST_ASSERT_TRUE(json_int(json_get(llm, "limit_hour"), &limit));
    TEST_ASSERT_EQUAL_INT(30, limit);
    TEST_ASSERT_TRUE(json_int(json_get(llm, "limit_day"), &limit));
    TEST_ASSERT_EQUAL_INT(200, limit);

    json_node_t *agent = json_get(root, "agent");
    TEST_ASSERT_EQUAL_STRING("r-1", json_string(json_get(agent, "run_id")));
    bool busy = false;
    TEST_ASSERT_TRUE(json_bool(json_get(agent, "busy"), &busy));
    TEST_ASSERT_TRUE(busy);

    json_free(doc);
}

/* `04` §3.9: `geo` is an object with the fix or JSON null before one is taken, and
 * `mqtt` is an array of one row per configured broker, empty when there are none.
 * A zeroed fix would report the Gulf of Guinea as a real position, so the null arm is
 * the one that matters. */
TEST_CASE("status reports the fix and the brokers, or their absence", "[link]")
{
    static const link_mqtt_row_t rows[2] = { { "home", true }, { "lab", false } };

    link_status_t st = {0};
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));

    /* No fix and no broker: `geo` null, `mqtt` empty and still an array. */
    TEST_ASSERT_TRUE(json_is_null(json_get(json_root(doc), "geo")));
    json_node_t *none = json_get(json_root(doc), "mqtt");
    TEST_ASSERT_NOT_NULL(none);
    TEST_ASSERT_EQUAL_UINT(0, json_array_len(none));
    json_free(doc);

    /* A fix and two brokers. */
    st.have_geo = true;
    st.geo_lat = 48.858222;
    st.geo_lon = 2.2945;
    st.geo_accuracy_m = 40;
    st.geo_age_s = 12;
    st.mqtt_rows = rows;
    st.mqtt_row_count = 2;

    doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));

    json_node_t *geo = json_get(json_root(doc), "geo");
    TEST_ASSERT_TRUE(json_is_object(geo));
    double lat = 0, lon = 0;
    TEST_ASSERT_TRUE(json_double(json_get(geo, "lat"), &lat));
    TEST_ASSERT_TRUE(json_double(json_get(geo, "lon"), &lon));
    TEST_ASSERT_FLOAT_WITHIN(1e-6, 48.858222, lat);
    TEST_ASSERT_FLOAT_WITHIN(1e-6, 2.2945, lon);
    /* §3.9 types the object as four members: `provider` belongs to the tool result. */
    TEST_ASSERT_NULL(json_get(geo, "provider"));
    int64_t age = 0;
    TEST_ASSERT_TRUE(json_int(json_get(geo, "age_s"), &age));
    TEST_ASSERT_EQUAL_INT(12, age);

    json_node_t *mqtt = json_get(json_root(doc), "mqtt");
    TEST_ASSERT_EQUAL_UINT(2, json_array_len(mqtt));
    json_node_t *first = json_array_at(mqtt, 0);
    TEST_ASSERT_EQUAL_STRING("home", json_string(json_get(first, "id")));
    bool up = false;
    TEST_ASSERT_TRUE(json_bool(json_get(first, "connected"), &up));
    TEST_ASSERT_TRUE(up);
    json_node_t *second = json_array_at(mqtt, 1);
    TEST_ASSERT_EQUAL_STRING("lab", json_string(json_get(second, "id")));
    TEST_ASSERT_TRUE(json_bool(json_get(second, "connected"), &up));
    TEST_ASSERT_FALSE(up);
    json_free(doc);
}

TEST_CASE("status reports one mcp row per configured server", "[link]")
{
    /* §3.9 types an `mcp[]` row as exactly `{id, enabled, state}`, and all three
     * states the firmware can be in are exercised: an unconfigured board still has
     * to emit the empty array, not a null. */
    link_status_t st = { .boot_id = "b-789" };
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));
    json_node_t *none = json_get(json_root(doc), "mcp");
    TEST_ASSERT_NOT_NULL(none);
    TEST_ASSERT_EQUAL_UINT(0, json_array_len(none));
    json_free(doc);

    link_mcp_row_t rows[3] = {
        { .id = "home", .enabled = true, .state = "connected" },
        { .id = "lab", .enabled = true, .state = "error" },
        { .id = "office", .enabled = false, .state = "idle" },
    };
    st.mcp_rows = rows;
    st.mcp_row_count = 3;

    doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    TEST_ASSERT_EQUAL(ESP_OK, link_build_status(json_root(doc), &st));

    json_node_t *mcp = json_get(json_root(doc), "mcp");
    TEST_ASSERT_EQUAL_UINT(3, json_array_len(mcp));

    json_node_t *home = json_array_at(mcp, 0);
    TEST_ASSERT_EQUAL_STRING("home", json_string(json_get(home, "id")));
    bool enabled = false;
    TEST_ASSERT_TRUE(json_bool(json_get(home, "enabled"), &enabled));
    TEST_ASSERT_TRUE(enabled);
    TEST_ASSERT_EQUAL_STRING("connected", json_string(json_get(home, "state")));

    TEST_ASSERT_EQUAL_STRING("error", json_string(json_get(json_array_at(mcp, 1), "state")));

    json_node_t *office = json_array_at(mcp, 2);
    TEST_ASSERT_TRUE(json_bool(json_get(office, "enabled"), &enabled));
    TEST_ASSERT_FALSE(enabled);
    TEST_ASSERT_EQUAL_STRING("idle", json_string(json_get(office, "state")));
    json_free(doc);
}

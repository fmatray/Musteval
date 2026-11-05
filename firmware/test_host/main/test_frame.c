/* The inbound envelope rules of `04-protocol.md` §2, and the doc corpus itself:
 * every JSON example in §3 must parse through the shipped codec. The corpus is
 * read at test time so a protocol edit fails this test instead of a reviewer's
 * memory (`AGENTS.md` non-regression rule 3). */

#include <stdio.h>
#include <string.h>

#include "unity.h"

#include "link.h"

/* One line of the doc's §3 examples. `-DPROTOCOL_DOC_PATH=...` points at
 * `docs/04-protocol.md`; when the macro is absent the corpus test reports itself
 * skipped rather than passing on nothing. */
static size_t read_doc(char *out, size_t cap)
{
    const char *path = PROTOCOL_DOC_PATH;
    FILE *f = fopen(path, "rb");
    if (f == NULL) return 0;
    size_t n = fread(out, 1, cap - 1, f);
    fclose(f);
    out[n] = '\0';
    return n;
}

static bool parse(const char *line, size_t len)
{
    json_doc_t *doc = NULL;
    const char *bad = NULL;
    esp_err_t err = link_frame_parse(line, len, &doc, &bad);
    if (doc != NULL) json_free(doc);
    return err == ESP_OK && bad == NULL;
}

TEST_CASE("a complete frame parses", "[link]")
{
    const char *line =
        "{\"v\":1,\"type\":\"ping\",\"id\":\"b-1\",\"ts\":1730563200}";
    json_doc_t *doc = NULL;
    const char *bad = NULL;

    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(line, strlen(line), &doc, &bad));
    TEST_ASSERT_NULL(bad);
    TEST_ASSERT_NOT_NULL(doc);
    json_free(doc);
}

TEST_CASE("reply_to is optional but must be a legal id when present", "[link]")
{
    const char *ok =
        "{\"v\":1,\"type\":\"command.result\",\"id\":\"d-1\",\"ts\":0,"
        "\"reply_to\":\"b-7\"}";
    const char *bad_id =
        "{\"v\":1,\"type\":\"command.result\",\"id\":\"d-1\",\"ts\":0,"
        "\"reply_to\":\"has space\"}";

    TEST_ASSERT_TRUE(parse(ok, strlen(ok)));

    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      link_frame_parse(bad_id, strlen(bad_id), &doc, &bad));
    TEST_ASSERT_EQUAL_STRING("reply_to", bad);
}

TEST_CASE("a missing or illegal envelope member is named", "[link]")
{
    struct {
        const char *line;
        const char *field;
    } cases[] = {
        /* `v` must be exactly 1. */
        {"{\"v\":2,\"type\":\"ping\",\"id\":\"b-1\",\"ts\":0}", "v"},
        {"{\"v\":\"1\",\"type\":\"ping\",\"id\":\"b-1\",\"ts\":0}", "v"},
        {"{\"type\":\"ping\",\"id\":\"b-1\",\"ts\":0}", "v"},
        /* `type` must be in the catalog. */
        {"{\"v\":1,\"type\":\"nope\",\"id\":\"b-1\",\"ts\":0}", "type"},
        {"{\"v\":1,\"id\":\"b-1\",\"ts\":0}", "type"},
        /* `id`: 1..32 of [A-Za-z0-9._-]. */
        {"{\"v\":1,\"type\":\"ping\",\"id\":\"\",\"ts\":0}", "id"},
        {"{\"v\":1,\"type\":\"ping\",\"id\":\"has space\",\"ts\":0}", "id"},
        {"{\"v\":1,\"type\":\"ping\",\"id\":"
         "\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"ts\":0}",
         "id"},
        {"{\"v\":1,\"type\":\"ping\",\"ts\":0}", "id"},
        /* `ts` is required and an integer; 0 is legal for an unsynced clock. */
        {"{\"v\":1,\"type\":\"ping\",\"id\":\"b-1\"}", "ts"},
        {"{\"v\":1,\"type\":\"ping\",\"id\":\"b-1\",\"ts\":\"0\"}", "ts"},
        /* Not an object at all. */
        {"[]", "json"},
        {"{\"v\":1,\"type\":\"ping\",\"id\":\"b-1\",\"ts\":0} x", "json"},
    };

    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        json_doc_t *doc = (json_doc_t *)1; /* must be cleared on failure */
        const char *bad = NULL;
        esp_err_t err =
            link_frame_parse(cases[i].line, strlen(cases[i].line), &doc, &bad);
        TEST_ASSERT_EQUAL_MESSAGE(ESP_ERR_INVALID_ARG, err, cases[i].line);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(cases[i].field, bad, cases[i].line);
        TEST_ASSERT_NULL_MESSAGE(doc, "failure must not hand back a document");
    }
}

TEST_CASE("unknown members are ignored, not rejected", "[link]")
{
    /* `04` §2: forward compatibility. A newer bridge adding a field must not
     * break an older device. */
    const char *line =
        "{\"v\":1,\"type\":\"ping\",\"id\":\"b-1\",\"ts\":0,\"future\":{\"a\":[1]}}";
    TEST_ASSERT_TRUE(parse(line, strlen(line)));
}

TEST_CASE("direction is a dispatch rule, not a parse rule", "[link]")
{
    /* A `hello_ack` parses: it is a legal frame. It is only wrong when the device
     * is asked to handle it, which is where §2.1's E_PROTOCOL applies. */
    const char *inbound = "{\"v\":1,\"type\":\"hello\",\"id\":\"b-1\",\"ts\":0}";
    const char *outbound =
        "{\"v\":1,\"type\":\"hello_ack\",\"id\":\"d-1\",\"ts\":0}";

    TEST_ASSERT_TRUE(parse(inbound, strlen(inbound)));
    TEST_ASSERT_TRUE(parse(outbound, strlen(outbound)));

    TEST_ASSERT_TRUE(link_type_inbound("hello"));
    TEST_ASSERT_TRUE(link_type_inbound("command"));
    TEST_ASSERT_FALSE(link_type_inbound("hello_ack"));
    TEST_ASSERT_FALSE(link_type_inbound("status"));
    TEST_ASSERT_FALSE(link_type_inbound("nope"));
}

TEST_CASE("every 04 §3 example parses through the shipped codec", "[link][docs]")
{
    static char doc[128 * 1024];
    size_t n = read_doc(doc, sizeof(doc));
    TEST_ASSERT_GREATER_THAN_MESSAGE(0, n, "cannot read " PROTOCOL_DOC_PATH);

    /* Walk to §3, then take every ```json fence inside a `### 3.x` section. The
     * range stops at §4 because later sections also carry fences that are not
     * envelopes (command arg examples), and parsing those would test the wrong
     * thing. */
    char *catalog = strstr(doc, "## 3. Message catalog");
    TEST_ASSERT_NOT_NULL_MESSAGE(catalog, "04-protocol.md lost its §3 heading");
    char *catalog_end = strstr(catalog, "## 4. Command catalog");
    TEST_ASSERT_NOT_NULL_MESSAGE(catalog_end, "04-protocol.md lost its §4 heading");

    size_t checked = 0;
    for (char *fence = strstr(catalog, "```json"); fence != NULL && fence < catalog_end;
         fence = strstr(fence, "```json")) {
        char *body = strchr(fence, '\n');
        TEST_ASSERT_NOT_NULL(body);
        body++;
        char *end = strstr(body, "```");
        TEST_ASSERT_NOT_NULL(end);

        size_t len = (size_t)(end - body);
        /* Trim the newline that closes the fence's last line. */
        while (len > 0 && (body[len - 1] == '\n' || body[len - 1] == '\r')) len--;

        json_doc_t *parsed = NULL;
        const char *bad = NULL;
        esp_err_t err = link_frame_parse(body, len, &parsed, &bad);
        TEST_ASSERT_EQUAL_MESSAGE(ESP_OK, err, "an 04 §3 example no longer parses");
        TEST_ASSERT_NULL_MESSAGE(bad, "a shipped example has a bad envelope member");
        json_free(parsed);

        checked++;
        fence = end + 3;
    }

    /* The doc has one example per message type; a floor catches a broken walker
     * that silently matched nothing. */
    TEST_ASSERT_GREATER_THAN_MESSAGE(15, checked, "too few §3 examples found");
}

TEST_CASE("a built frame round-trips through the codec", "[link]")
{
    /* The only path that proves build+wrap+parse agree: nothing else would catch
     * an envelope field the parser needs and the wrapper never writes. */
    json_doc_t *body = json_new_object();
    TEST_ASSERT_NOT_NULL(body);
    json_node_t *root = json_root(body);
    TEST_ASSERT_NOT_NULL(root);

    TEST_ASSERT_TRUE(json_add_string(root, "device", "xiao-s3-01"));
    TEST_ASSERT_TRUE(json_add_bool(root, "time_synced", false));
    TEST_ASSERT_TRUE(json_add_int(root, "boot_id", 7));

    /* A duplicate key is refused rather than corrupting the object. */
    TEST_ASSERT_FALSE(json_add_string(root, "device", "again"));

    char line[512];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, link_envelope_wrap(body, "hello_ack", "d-1", 0, "b-1",
                                                 line, sizeof(line), &written));
    json_free(body);

    /* Canonical order, and the envelope members come first. */
    TEST_ASSERT_EQUAL_STRING(
        "{\"v\":1,\"type\":\"hello_ack\",\"id\":\"d-1\",\"ts\":0,"
        "\"reply_to\":\"b-1\",\"boot_id\":7,\"device\":\"xiao-s3-01\","
        "\"time_synced\":false}",
        line);
    TEST_ASSERT_EQUAL_size_t(strlen(line), written);

    /* And it parses back, which is what the bridge will do to it. */
    json_doc_t *back = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(line, written, &back, &bad));
    TEST_ASSERT_NULL(bad);
    int64_t boot = 0;
    TEST_ASSERT_TRUE(json_int(json_get(json_root(back), "boot_id"), &boot));
    TEST_ASSERT_EQUAL_INT64(7, boot);
    json_free(back);
}

TEST_CASE("an empty body yields a bare envelope", "[link]")
{
    /* A reply with no result object must still be exactly one valid frame. */
    char line[128];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, link_envelope_wrap(NULL, "pong", "d-2", 5, "b-6", line,
                                                 sizeof(line), &written));
    TEST_ASSERT_EQUAL_STRING("{\"v\":1,\"type\":\"pong\",\"id\":\"d-2\",\"ts\":5,"
                             "\"reply_to\":\"b-6\"}",
                             line);

    json_doc_t *doc = NULL;
    const char *bad = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, link_frame_parse(line, written, &doc, &bad));
    json_free(doc);

    /* The same body shape with an empty object also parses. */
    json_doc_t *empty = json_new_object();
    TEST_ASSERT_EQUAL(ESP_OK, link_envelope_wrap(empty, "pong", "d-3", 5, "b-7", line,
                                                 sizeof(line), &written));
    json_free(empty);
    TEST_ASSERT_EQUAL_STRING("{\"v\":1,\"type\":\"pong\",\"id\":\"d-3\",\"ts\":5,"
                             "\"reply_to\":\"b-7\"}",
                             line);
}

TEST_CASE("a full TX buffer is refused, not truncated", "[link]")
{
    /* 04 §1.2: an over-long frame is an error, never a short line on the wire. */
    json_doc_t *body = json_new_object();
    char big[300];
    memset(big, 'x', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';
    TEST_ASSERT_TRUE(json_add_string(json_root(body), "msg", big));

    char small[64];
    size_t written = 12345;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_SIZE,
                      link_envelope_wrap(body, "log", "d-4", 0, NULL, small,
                                         sizeof(small), &written));
    json_free(body);
}

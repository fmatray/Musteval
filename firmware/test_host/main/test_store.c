/* Canonical JSON and path confinement, asserted through the public contract.
 * Every expectation below names the rule it pins: `AGENTS.md` canonical JSON,
 * `02-firmware.md` §store, `06-security.md` §6.1. */

#include <string.h>

#include "unity.h"

#include "store.h"

static const char *emit(const char *src)
{
    static char buf[512];
    json_doc_t *doc = NULL;
    size_t written = 0;

    TEST_ASSERT_EQUAL(ESP_OK, json_parse(src, strlen(src), &doc));
    esp_err_t err = json_emit(doc, buf, sizeof buf, &written);
    json_free(doc);
    TEST_ASSERT_EQUAL(ESP_OK, err);
    TEST_ASSERT_EQUAL(strlen(buf), written);
    return buf;
}

TEST_CASE("emit sorts object keys bytewise", "[store]")
{
    TEST_ASSERT_EQUAL_STRING("{\"a\":2,\"b\":1}",
                             emit("{\"b\":1,\"a\":2}"));
    TEST_ASSERT_EQUAL_STRING("{\"a\":[],\"z\":{}}",
                             emit("{\"z\":{},\"a\":[]}"));
}

TEST_CASE("emit keeps array order and escapes strings", "[store]")
{
    TEST_ASSERT_EQUAL_STRING("[3,1,2]", emit("[3,1,2]"));
    TEST_ASSERT_EQUAL_STRING("{\"s\":\"a\\\"b\\n\"}",
                             emit("{\"s\":\"a\\\"b\\n\"}"));
}

TEST_CASE("emit of an empty object terminates", "[store]")
{
    /* The sort must not underflow on zero keys; a regression here hangs, so this
     * test is the one that would have caught it. */
    TEST_ASSERT_EQUAL_STRING("{}", emit("{}"));
}

TEST_CASE("parse refuses trailing garbage", "[store]")
{
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, json_parse("{} {}", 5, &doc));
    TEST_ASSERT_NULL(doc);
}

TEST_CASE("a write may not escape the workspace", "[store]")
{
    char out[256];

    /* `06-security.md` §6.1: `..` above the root is a refusal, never a clamp. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("/workspace/../config.json",
                                               out, sizeof out));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("/workspace/a/../../x",
                                               out, sizeof out));
    /* The workspace root itself has no name, so it is not a file target. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("/workspace/", out, sizeof out));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("/workspace", out, sizeof out));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("workspace/a", out, sizeof out));
    /* userfs is host-only, so the agent-facing resolver refuses it. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("/fs/tasks.json", out,
                                               sizeof out));
}

TEST_CASE("a write inside the workspace is canonicalised", "[store]")
{
    char out[256];

    TEST_ASSERT_EQUAL(ESP_OK, store_resolve_write_path("/workspace/note.txt",
                                                       out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace/note.txt", out);

    /* `.` and `..` that stay inside are resolved, not refused. */
    TEST_ASSERT_EQUAL(ESP_OK, store_resolve_write_path("/workspace/a/../b.txt",
                                                       out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace/b.txt", out);
}

TEST_CASE("map roots anything outside the workspace at userfs", "[store]")
{
    char out[256];

    TEST_ASSERT_EQUAL(ESP_OK, store_map_path("/workspace/a", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace/a", out);

    TEST_ASSERT_EQUAL(ESP_OK, store_map_path("/prompts/x.md", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/fs/prompts/x.md", out);
}

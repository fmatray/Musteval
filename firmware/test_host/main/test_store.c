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

TEST_CASE("a listing admits the workspace root, a write does not", "[store]")
{
    char out[256];

    /* `04` §4.17 defaults `sandbox.list` to the root, so the listing admission
     * returns it rather than refusing it. The write admission must keep refusing
     * it: the root has no name as a file target. */
    TEST_ASSERT_EQUAL(ESP_OK,
                      store_resolve_sandbox_path("/workspace/", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace", out);
    TEST_ASSERT_EQUAL(ESP_OK,
                      store_resolve_sandbox_path("/workspace", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace", out);
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_write_path("/workspace/", out, sizeof out));

    /* Everything below the root behaves identically in both admissions. */
    TEST_ASSERT_EQUAL(ESP_OK,
                      store_resolve_sandbox_path("/workspace/runs/", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace/runs", out);

    /* And the listing still refuses an escape. */
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_sandbox_path("/workspace/../config.json",
                                                 out, sizeof out));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_sandbox_path("/fs/skills/", out, sizeof out));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_resolve_sandbox_path("workspace/a", out, sizeof out));
}

TEST_CASE("map roots anything outside the workspace at userfs", "[store]")
{
    char out[256];

    TEST_ASSERT_EQUAL(ESP_OK, store_map_path("/workspace/a", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/workspace/a", out);

    TEST_ASSERT_EQUAL(ESP_OK, store_map_path("/prompts/x.md", out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("/fs/prompts/x.md", out);
}
TEST_CASE("a move canonicalises both paths before comparing them", "[store]")
{
    char from[256], to[256];
    TEST_ASSERT_EQUAL(ESP_OK, store_resolve_write_path("/workspace/x/../runs", from, sizeof from));
    TEST_ASSERT_EQUAL_STRING("/workspace/runs", from);
    TEST_ASSERT_EQUAL(ESP_OK, store_resolve_write_path("/workspace/./archive/", to, sizeof to));
    TEST_ASSERT_EQUAL_STRING("/workspace/archive", to);
    TEST_ASSERT_EQUAL(STORE_MOVE_OK, store_move_verdict(true, false, from, to));

    TEST_ASSERT_EQUAL(ESP_OK, store_resolve_write_path("/workspace/runs/./2026/..", to, sizeof to));
    TEST_ASSERT_EQUAL(STORE_MOVE_INTO_ITSELF, store_move_verdict(true, false, from, to));

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_resolve_write_path("/workspace/../fs/x", to, sizeof to));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_resolve_write_path("/workspace/../../x", to, sizeof to));
}

TEST_CASE("path containment compares whole segments", "[store]")
{
    TEST_ASSERT_TRUE(store_path_is_under("/workspace/a", "/workspace/a"));
    TEST_ASSERT_TRUE(store_path_is_under("/workspace/a", "/workspace/a/b"));
    TEST_ASSERT_FALSE(store_path_is_under("/workspace/a", "/workspace/ab"));
    TEST_ASSERT_FALSE(store_path_is_under("/workspace/a", "/workspace"));
    TEST_ASSERT_FALSE(store_path_is_under(NULL, "/workspace/a"));
    TEST_ASSERT_FALSE(store_path_is_under("/workspace/a", NULL));
}

static const char *emit_doc(json_doc_t *doc)
{
    static char buf[512];
    size_t written = 0;
    TEST_ASSERT_EQUAL(ESP_OK, json_emit(doc, buf, sizeof buf, &written));
    TEST_ASSERT_EQUAL(strlen(buf), written);
    return buf;
}

/* Every character cJSON can hold but JSON cannot write raw is escaped: a config
 * string or a reply body with a tab must round-trip to valid JSON or the file the
 * device writes is unreadable by its own parser. */
TEST_CASE("emit escapes backslash, quote and every control character", "[store]")
{
    /* The parsed value holds a literal backslash, quote, \b, \f, \n, \r and \t, so
     * the emitted text must be the same escapes it came in as. */
    const char *escaped = "{\"s\":\"\\\\\\\"\\b\\f\\n\\r\\t\"}";
    TEST_ASSERT_EQUAL_STRING(escaped, emit(escaped));

    /* A `\u0001` escape is legal JSON but the raw byte is not, so emit has to write
     * the escape back rather than the control character. */
    TEST_ASSERT_EQUAL_STRING("{\"s\":\"\\u0001\"}", emit("{\"s\":\"\\u0001\"}"));

    /* `null` is a value every config file uses; it is written as itself. */
    TEST_ASSERT_EQUAL_STRING("{\"a\":null}", emit("{\"a\":null}"));
}

/* `03-config.md` orders provider lists, so a builder must not sort: the emit sorts
 * object keys only, and array order is the caller's. */
TEST_CASE("the array builders append in insertion order", "[store]")
{
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_NOT_NULL(doc);
    json_node_t *root = json_root(doc);
    json_node_t *arr = json_new_array(root, "a");
    TEST_ASSERT_NOT_NULL(arr);

    TEST_ASSERT_NOT_NULL(json_array_push_string(arr, "z"));
    TEST_ASSERT_NOT_NULL(json_array_push_int(arr, -7));
    TEST_ASSERT_NOT_NULL(json_array_push_string(arr, "y"));
    TEST_ASSERT_EQUAL_STRING("{\"a\":[\"z\",-7,\"y\"]}", emit_doc(doc));

    /* Appending to something that is not an array, or a null string, is refused and
     * leaves the document as it was. */
    TEST_ASSERT_NULL(json_array_push_string(root, "x"));
    TEST_ASSERT_NULL(json_array_push_int(root, 1));
    TEST_ASSERT_NULL(json_array_push_string(arr, NULL));
    TEST_ASSERT_EQUAL_STRING("{\"a\":[\"z\",-7,\"y\"]}", emit_doc(doc));
    json_free(doc);
}

/* `json_add_node` deep-copies, because the source document may be freed first: this
 * test frees it and then reads the copy back. */
TEST_CASE("json_add_node copies the subtree it is given", "[store]")
{
    static const char *SRC_TREE = "{\"v\":{\"k\":[1,2]}}";
    json_doc_t *src = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(SRC_TREE, strlen(SRC_TREE), &src));
    json_doc_t *dst = json_new_object();
    TEST_ASSERT_NOT_NULL(dst);

    TEST_ASSERT_NOT_NULL(json_add_node(json_root(dst), "v", json_get(json_root(src), "v")));
    json_free(src);
    TEST_ASSERT_EQUAL_STRING("{\"v\":{\"k\":[1,2]}}", emit_doc(dst));

    /* A missing key, a missing node and a parent that is not an object are refusals,
     * not silent inserts. */
    TEST_ASSERT_NULL(json_add_node(json_root(dst), NULL, json_get(json_root(dst), "v")));
    TEST_ASSERT_NULL(json_add_node(json_root(dst), "k", NULL));
    TEST_ASSERT_NULL(json_add_node(NULL, "k", json_get(json_root(dst), "v")));
    json_node_t *arr = json_new_array(json_root(dst), "arr");
    TEST_ASSERT_NULL(json_add_node(arr, "k", json_get(json_root(dst), "v")));
    TEST_ASSERT_EQUAL_STRING("{\"arr\":[],\"v\":{\"k\":[1,2]}}", emit_doc(dst));
    json_free(dst);
}

/* `04-protocol.md` §4.9: an index that exists is replaced, one that does not is
 * `E_CONFIG`. The distinction is what stops `wifi.networks.3.ssid` from inventing a
 * network with every other field empty. */
TEST_CASE("a set replaces an array element and refuses a missing index", "[store]")
{
    static const char *ARR_DOC = "{\"a\":[1,2],\"o\":{\"k\":1}}";
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(ARR_DOC, strlen(ARR_DOC), &doc));
    json_doc_t *val = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse("{\"v\":9}", 7, &val));
    json_node_t *nine = json_get(json_root(val), "v");

    TEST_ASSERT_TRUE(json_path_set(doc, "a.0", nine));
    TEST_ASSERT_EQUAL_STRING("{\"a\":[9,2],\"o\":{\"k\":1}}", emit_doc(doc));

    /* Past the end, and a numeric key under an object, are both refusals that leave
     * the document exactly as it was. */
    TEST_ASSERT_FALSE(json_path_set(doc, "a.2", nine));
    TEST_ASSERT_FALSE(json_path_set(doc, "a.9", nine));
    TEST_ASSERT_FALSE(json_path_set(doc, "o.0", nine));
    TEST_ASSERT_EQUAL_STRING("{\"a\":[9,2],\"o\":{\"k\":1}}", emit_doc(doc));

    /* An existing key is replaced in place, which is the case the set exists for. */
    TEST_ASSERT_TRUE(json_path_set(doc, "o.k", nine));
    TEST_ASSERT_EQUAL_STRING("{\"a\":[9,2],\"o\":{\"k\":9}}", emit_doc(doc));

    json_free(val);
    json_free(doc);
}

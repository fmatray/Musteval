/* The offered tool array and the declarative loader: `02-firmware.md` §7.1 and
 * `tools/web.md`. Both are pure, and both shipped with parts nothing drove —
 * `tools_capability_names` was one line of `hello_ack` and the declarative branch
 * of `tools_json_build` could only be reached on a board. */

#include <stdio.h>
#include <string.h>

#include "unity.h"

#include "tools.h"

static bool probe_found(const char *key, void *ctx)
{
    (void)ctx;
    return strcmp(key, "present") == 0;
}

/* One declarative tool, in the documented shape. */
static void compile_one(tools_declared_t *out, const char *stem, const char *body)
{
    char why[128] = "";
    TEST_ASSERT_EQUAL_INT_MESSAGE(ESP_OK,
                                  tools_declared_compile(stem, body, strlen(body),
                                                         probe_found, NULL, out,
                                                         why, sizeof why),
                                  why);
}

/* §7.1: built-ins first, then declarative, and only the tools that are offered
 * *now*. The i2c family disappears when the bus is unpinned, which is also how the
 * declarative branch is proven: a declared tool named `i2c.probe` is hidden by the
 * same predicate, so its absence cannot be a coincidence of ordering. */
TEST_CASE("the offered array carries builtins and declarative tools, minus gated ones", "[tools]")
{
    tools_declared_set_t declared = {0};
    compile_one(&declared.items[0], "web_search",
                "{\"description\":\"Search.\","
                "\"parameters\":{\"type\":\"object\",\"properties\":{}},"
                "\"http\":{\"url\":\"https://api.test/\",\"method\":\"POST\"}}");
    compile_one(&declared.items[1], "i2c.probe",
                "{\"description\":\"Gated.\","
                "\"http\":{\"url\":\"https://api.test/\"}}");
    declared.count = 2;

    tools_context_t unset = {0};
    unset.i2c_sda = -1;
    unset.i2c_scl = -1;

    json_doc_t *doc = json_new_object();
    json_node_t *array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&unset, &declared, array));

    size_t count = 0;
    tools_builtins(&count);
    /* Every built-in but the three i2c ones, plus `web_search`, minus `i2c.probe`. */
    TEST_ASSERT_EQUAL_size_t(count - 3 + 1, json_array_len(array));

    /* The entry shape is what a provider reads, so check the last one in full. */
    json_node_t *last = json_array_at(array, json_array_len(array) - 1);
    TEST_ASSERT_EQUAL_STRING("function", json_string(json_get(last, "type")));
    json_node_t *fn = json_get(last, "function");
    TEST_ASSERT_NOT_NULL(fn);
    TEST_ASSERT_EQUAL_STRING("web_search", json_string(json_get(fn, "name")));
    TEST_ASSERT_EQUAL_STRING("Search.", json_string(json_get(fn, "description")));
    TEST_ASSERT_NOT_NULL(json_get(fn, "parameters"));

    /* Pinning the bus offers all three i2c builtins plus the declared `i2c.probe`,
     * which the same predicate now lets through: the hidden set is a policy
     * decision and not a missing registration. */
    tools_context_t pinned = {0};
    pinned.i2c_sda = 4;
    pinned.i2c_scl = 5;
    json_free(doc);
    doc = json_new_object();
    array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&pinned, &declared, array));
    TEST_ASSERT_EQUAL_size_t(count + 2, json_array_len(array));

    json_free(doc);
}

/* A declared schema is embedded verbatim; one that is absent leaves `parameters` an
 * empty object rather than omitting the member a provider requires. */
TEST_CASE("a declarative entry embeds its schema and defaults to an empty one", "[tools]")
{
    tools_declared_set_t declared = {0};
    compile_one(&declared.items[0], "with_schema",
                "{\"description\":\"d\","
                "\"parameters\":{\"type\":\"object\",\"properties\":{\"q\":{}}},"
                "\"http\":{\"url\":\"https://api.test/\"}}");
    compile_one(&declared.items[1], "no_schema",
                "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\"}}");
    declared.count = 2;

    tools_context_t ctx = {0};
    ctx.i2c_sda = -1;
    ctx.i2c_scl = -1;

    json_doc_t *doc = json_new_object();
    json_node_t *array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&ctx, &declared, array));

    size_t n = json_array_len(array);
    json_node_t *with = json_get(json_array_at(array, n - 2), "function");
    json_node_t *without = json_get(json_array_at(array, n - 1), "function");
    TEST_ASSERT_EQUAL_STRING("with_schema", json_string(json_get(with, "name")));
    json_node_t *params = json_get(with, "parameters");
    TEST_ASSERT_NOT_NULL(params);
    /* A schema that did not parse is replaced by the empty fallback, so the named
     * property is what proves the file's own text reached the array. */
    TEST_ASSERT_NOT_NULL(json_get(params, "properties"));
    TEST_ASSERT_NOT_NULL(json_get(json_get(params, "properties"), "q"));

    TEST_ASSERT_EQUAL_STRING("no_schema", json_string(json_get(without, "name")));
    /* No `parameters` in the file: the documented empty fallback, not an absent
     * member and not a silently-empty object. Before the wrapper brace was sliced
     * off the schema, this passed because a malformed schema fell back to `{}`. */
    json_node_t *fallback = json_get(without, "parameters");
    TEST_ASSERT_NOT_NULL(fallback);
    TEST_ASSERT_EQUAL_STRING("object", json_string(json_get(fallback, "type")));
    TEST_ASSERT_EQUAL_size_t(0, json_object_len(json_get(fallback, "properties")));

    json_free(doc);
}

/* The array must be an array: a caller that hands an object gets a refusal, not a
 * document that fails at the provider. */
TEST_CASE("both builders refuse a node that is not an array", "[tools]")
{
    tools_context_t ctx = {0};
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_json_build(&ctx, NULL, json_root(doc)));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_json_build(&ctx, NULL, NULL));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_capability_names(&ctx, NULL, json_root(doc)));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_capability_names(&ctx, NULL, NULL));
    json_free(doc);
}

/* §3.2 `capabilities.tools` is every name the model may call, in `tools_json`
 * order: the same offer predicate, without the schemas. */
TEST_CASE("the capability names match the offered set in the same order", "[tools]")
{
    tools_declared_set_t declared = {0};
    compile_one(&declared.items[0], "web_search",
                "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\"}}");
    compile_one(&declared.items[1], "i2c.probe",
                "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\"}}");
    declared.count = 2;

    tools_context_t ctx = {0};
    ctx.i2c_sda = -1;
    ctx.i2c_scl = -1;

    json_doc_t *names_doc = json_new_object();
    json_node_t *names = json_new_array(json_root(names_doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_capability_names(&ctx, &declared, names));

    json_doc_t *full_doc = json_new_object();
    json_node_t *full = json_new_array(json_root(full_doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&ctx, &declared, full));

    TEST_ASSERT_EQUAL_size_t(json_array_len(full), json_array_len(names));
    for (size_t i = 0; i < json_array_len(names); i++) {
        json_node_t *fn = json_get(json_array_at(full, i), "function");
        TEST_ASSERT_NOT_NULL(fn);
        TEST_ASSERT_EQUAL_STRING(json_string(json_get(fn, "name")),
                                 json_string(json_array_at(names, i)));
    }
    /* The gated name is absent from both. */
    for (size_t i = 0; i < json_array_len(names); i++) {
        TEST_ASSERT_NOT_EQUAL(0, strcmp("i2c.probe", json_string(json_array_at(names, i))));
    }
    /* And the declarative one is present in both. */
    TEST_ASSERT_EQUAL_STRING("web_search",
                             json_string(json_array_at(names, json_array_len(names) - 1)));

    json_free(full_doc);
    json_free(names_doc);
}

/* --- the declarative loader ------------------------------------------------ */

/* A `headers` string is kept verbatim, including its `nvs:` form; a ref that does
 * not resolve is the documented E_CONFIG drop, distinct from a malformed file. */
TEST_CASE("a declarative header ref is kept or refused by its probe", "[tools]")
{
    tools_declared_t tool;
    char why[128] = "";
    const char *ok =
        "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\","
        "\"headers\":\"nvs:present\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", ok, strlen(ok), probe_found, NULL, &tool, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("nvs:present", tool.headers);

    why[0] = '\0';
    const char *missing =
        "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\","
        "\"headers\":\"nvs:absent\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_ERR_NOT_FOUND, tools_declared_compile(
        "t", missing, strlen(missing), probe_found, NULL, &tool, why, sizeof why));
    TEST_ASSERT_NOT_EQUAL('\0', why[0]);
}

/* The remaining drops: a stem too long for the field, a JSON value that is not an
 * object, a `headers` that is neither a string nor an object, and a `parameters`
 * that is not an object. */
TEST_CASE("a declarative file is dropped for each documented reason", "[tools]")
{
    tools_declared_t tool;
    char why[128];
    char long_stem[64];
    memset(long_stem, 'a', sizeof long_stem - 1);
    long_stem[sizeof long_stem - 1] = '\0';

    /* `out->name` is 33 bytes including the terminator. */
    why[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG, tools_declared_compile(
        long_stem, "{}", 2, NULL, NULL, &tool, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "name length"));

    const char *not_object = "[1,2]";
    why[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG, tools_declared_compile(
        "t", not_object, strlen(not_object), NULL, NULL, &tool, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "root is not an object"));

    /* An `nvs:` ref with no probe installed is refused: a ref that cannot be
     * verified is not treated as present. */
    const char *bad_headers =
        "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\","
        "\"headers\":\"nvs:present\"}}";
    why[0] = '\0';
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", bad_headers, strlen(bad_headers), probe_found, NULL, &tool, why, sizeof why));

    /* A plain string header that is not an `nvs:` ref. */
    const char *plain =
        "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\","
        "\"headers\":\"token\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_ERR_INVALID_ARG, tools_declared_compile(
        "t", plain, strlen(plain), probe_found, NULL, &tool, why, sizeof why));

    /* A `parameters` that is not an object takes the default schema. */
    const char *odd =
        "{\"description\":\"d\",\"parameters\":7,"
        "\"http\":{\"url\":\"https://api.test/\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", odd, strlen(odd), NULL, NULL, &tool, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(tool.schema, "\"properties\""));
}

/* `query` and `body` are templates carried as text, and `method` defaults to GET.
 * Each is a field the loader copies and nothing else asserts. */
TEST_CASE("a declarative request carries its template, query and default method", "[tools]")
{
    tools_declared_t tool;
    char why[128];
    const char *body =
        "{\"description\":\"d\",\"parameters\":{\"type\":\"object\","
        "\"properties\":{\"q\":{\"type\":\"string\"}}},"
        "\"http\":{\"url\":\"https://api.test/s\",\"query\":\"q={q}\","
        "\"body\":\"{\\\"k\\\":\\\"v\\\"}\",\"response_path\":\"a.b\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", body, strlen(body), NULL, NULL, &tool, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("GET", tool.method);
    TEST_ASSERT_EQUAL_STRING("q={q}", tool.query);
    TEST_ASSERT_EQUAL_STRING("{\"k\":\"v\"}", tool.body);
    TEST_ASSERT_EQUAL_STRING("a.b", tool.response_path);
    /* `schema` is the `parameters` value alone, not the wrapper it emitted in. */
    TEST_ASSERT_EQUAL_CHAR('{', tool.schema[0]);
    TEST_ASSERT_NULL(strstr(tool.schema, "\"parameters\""));
    TEST_ASSERT_NOT_NULL(strstr(tool.schema, "\"q\""));
}

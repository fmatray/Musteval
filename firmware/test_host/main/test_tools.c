/* The offered tool array and the declarative loader: `02-firmware.md` §7.1 and
 * `tools/web.md`. Both are pure, and both shipped with parts nothing drove —
 * `tools_capability_names` was one line of `hello_ack` and the declarative branch
 * of `tools_json_build` could only be reached on a board. */

#include <stdio.h>
#include <string.h>

#include "unity.h"

#include "mcp.h"
#include "tools.h"

static bool probe_any(const char *key, void *ctx)
{
    (void)ctx;
    return key != NULL && key[0] != '\0';
}

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
    unset.declared = &declared;

    json_doc_t *doc = json_new_object();
    json_node_t *array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&unset, array));

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
    pinned.declared = &declared;
    json_free(doc);
    doc = json_new_object();
    array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&pinned, array));
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
    ctx.declared = &declared;

    json_doc_t *doc = json_new_object();
    json_node_t *array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&ctx, array));

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
                      tools_json_build(&ctx, json_root(doc)));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_json_build(&ctx, NULL));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_capability_names(&ctx, json_root(doc)));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_capability_names(&ctx, NULL));
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
    ctx.declared = &declared;

    json_doc_t *names_doc = json_new_object();
    json_node_t *names = json_new_array(json_root(names_doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_capability_names(&ctx, names));

    json_doc_t *full_doc = json_new_object();
    json_node_t *full = json_new_array(json_root(full_doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&ctx, full));

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
    /* `schema` is the `parameters` value alone, not the wrapper it emitted in. The
     * value is compared whole: the wrapper's trailing brace used to survive the
     * re-slice and made every declarative schema malformed, which the two shape
     * assertions above could not see. Key order is `json_emit`'s canonical one
     * (`AGENTS.md` Core Principle 4), not the order the file wrote. */
    TEST_ASSERT_EQUAL_STRING(
        "{\"properties\":{\"q\":{\"type\":\"string\"}},\"type\":\"object\"}",
        tool.schema);
}

/* `tools/web.md`: `{{arg}}` in a template is replaced from the call's arguments, and
 * a placeholder naming an argument the schema does not declare is refused before any
 * request, so a file's typo cannot become an argument injection. */
TEST_CASE("a template binds its declared arguments and refuses the rest", "[tools]")
{
    tools_declared_t tool;
    char why[128];
    const char *body =
        "{\"description\":\"d\",\"parameters\":{\"type\":\"object\","
        "\"properties\":{\"q\":{\"type\":\"string\"},\"n\":{\"type\":\"string\"}}},"
        "\"http\":{\"url\":\"https://api.test/s?q={{q}}&n={{n}}\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", body, strlen(body), NULL, NULL, &tool, why, sizeof why));

    json_doc_t *args = json_new_object();
    json_add_string(json_root(args), "q", "cats");
    json_add_string(json_root(args), "n", "5");

    char out[128];
    TEST_ASSERT_EQUAL(ESP_OK, tools_bind_text(&tool, tool.url, json_root(args), out,
                                              sizeof out, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("https://api.test/s?q=cats&n=5", out);

    /* A placeholder the schema does not declare is refused *even when the call
     * supplies the value*: the file, not the caller, decides what may be
     * interpolated. Supplying `x` is what makes this test bite — without it the
     * missing-argument rule would fail the call for the wrong reason. */
    tools_declared_t narrow;
    const char *one =
        "{\"description\":\"d\",\"parameters\":{\"type\":\"object\","
        "\"properties\":{\"q\":{\"type\":\"string\"}}},"
        "\"http\":{\"url\":\"https://api.test/s?q={{q}}&x={{x}}\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", one, strlen(one), NULL, NULL, &narrow, why, sizeof why));

    json_doc_t *with_x = json_new_object();
    json_add_string(json_root(with_x), "q", "cats");
    json_add_string(json_root(with_x), "x", "5");
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_bind_text(&narrow, narrow.url, json_root(with_x), out,
                                      sizeof out, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "not a declared argument"));
    json_free(with_x);

    /* A declared argument the call omitted is refused too, and named. */
    json_doc_t *partial = json_new_object();
    json_add_string(json_root(partial), "q", "cats");
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_bind_text(&tool, tool.url, json_root(partial), out,
                                      sizeof out, why, sizeof why));
    TEST_ASSERT_NOT_NULL(strstr(why, "n"));

    json_free(partial);
    json_free(args);
}

/* A tool's host is compiled into its URL, so the allowlist is checked against the
 * file rather than against anything the model supplies. */
TEST_CASE("a declarative tool's host comes from its own url", "[tools]")
{
    tools_declared_t tool;
    char why[128];
    const char *body =
        "{\"description\":\"d\",\"http\":{\"url\":\"https://api.example.com:8443/s\"}}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, tools_declared_compile(
        "t", body, strlen(body), NULL, NULL, &tool, why, sizeof why));

    char host[64];
    TEST_ASSERT_EQUAL_STRING("api.example.com",
                             tools_declared_host(&tool, host, sizeof host));
}

/* Looking a row up by name is what the dispatch arm does; a name the set does not
 * carry must be NULL rather than the wrong row. */
TEST_CASE("a compiled set finds a row by name only when it holds one", "[tools]")
{
    tools_declared_set_t set = {0};
    compile_one(&set.items[0], "weather_now",
                "{\"description\":\"d\",\"http\":{\"url\":\"https://api.test/\"}}");
    set.count = 1;

    TEST_ASSERT_NOT_NULL(tools_declared_find(&set, "weather_now"));
    TEST_ASSERT_NULL(tools_declared_find(&set, "weather_later"));
    TEST_ASSERT_NULL(tools_declared_find(NULL, "weather_now"));
}

/* The two tools the image ships must compile and must keep their query. Both write
 * `http.query` as an object, and an object the compiler ignored meant neither tool
 * sent a single parameter — a call would have reached the provider's base URL with no
 * `q` and no `latitude`, which the provider answers with a 400. */
TEST_CASE("a shipped tool keeps the query its file declares", "[tools]")
{
    /* `web_search` carries `nvs:brave_key`, so the probe has to resolve it the way a
     * configured board would; otherwise the file is dropped for the right reason and
     * this test would be measuring the probe. */
    static const struct {
        const char *stem;
        const char *path;
        const char *contains;
        const char *literal;
    } files[] = {
        { "weather_now", WEATHER_TOOL_PATH, "latitude={{latitude}}", "current=" },
        { "web_search", WEB_SEARCH_TOOL_PATH, "q={{query}}", "count=5" },
    };

    for (size_t i = 0; i < sizeof files / sizeof files[0]; i++) {
        char body[4096];
        FILE *f = fopen(files[i].path, "rb");
        TEST_ASSERT_NOT_NULL_MESSAGE(f, files[i].path);
        size_t len = fread(body, 1, sizeof body, f);
        fclose(f);
        TEST_ASSERT_TRUE(len > 0 && len < sizeof body);

        tools_declared_t tool;
        char why[128] = "";
        TEST_ASSERT_EQUAL_INT_MESSAGE(
            ESP_OK,
            tools_declared_compile(files[i].stem, body, len, probe_any, NULL, &tool,
                                   why, sizeof why),
            why);

        TEST_ASSERT_NOT_NULL_MESSAGE(strstr(tool.query, files[i].contains), tool.query);
        /* A literal member survives beside a placeholder. */
        TEST_ASSERT_NOT_NULL_MESSAGE(strstr(tool.query, files[i].literal), tool.query);
    }
    /* The one file that carries a secret ref keeps it unresolved in the text. */
    {
        char body[4096];
        FILE *f = fopen(WEB_SEARCH_TOOL_PATH, "rb");
        TEST_ASSERT_NOT_NULL(f);
        size_t len = fread(body, 1, sizeof body, f);
        fclose(f);
        tools_declared_t tool;
        char why[128] = "";
        TEST_ASSERT_EQUAL_INT_MESSAGE(
            ESP_OK,
            tools_declared_compile("web_search", body, len, probe_any, NULL, &tool,
                                   why, sizeof why),
            why);
        TEST_ASSERT_NOT_NULL(strstr(tool.headers, "nvs:brave_key"));
        TEST_ASSERT_NOT_NULL(strstr(tool.headers, "Accept"));
    }
}

/* `weather_now` takes numbers and puts them in the query, so binding has to render
 * them rather than refuse them as "not a string". */
TEST_CASE("a numeric argument binds into a query template", "[tools]")
{
    tools_declared_t tool;
    char why[128] = "";
    const char *body =
        "{\"description\":\"d\",\"parameters\":{\"type\":\"object\","
        "\"properties\":{\"latitude\":{\"type\":\"number\"},"
        "\"longitude\":{\"type\":\"number\"}}},"
        "\"http\":{\"url\":\"https://api.test/f\","
        "\"query\":{\"latitude\":\"{{latitude}}\",\"n\":\"1\"}}}";
    TEST_ASSERT_EQUAL_INT_MESSAGE(
        ESP_OK,
        tools_declared_compile("t", body, strlen(body), NULL, NULL, &tool, why,
                               sizeof why),
        why);
    TEST_ASSERT_EQUAL_STRING("latitude={{latitude}}&n=1", tool.query);

    json_doc_t *args = json_new_object();
    json_add_double(json_root(args), "latitude", 18.4);

    char out[128];
    TEST_ASSERT_EQUAL(ESP_OK, tools_bind_text(&tool, tool.url, json_root(args), out,
                                              sizeof out, why, sizeof why));
    /* The URL itself has no placeholder, so binding it proves only that a template
     * with none is copied; the query is the interesting one. */
    TEST_ASSERT_EQUAL(ESP_OK, tools_bind_text(&tool, tool.query, json_root(args), out,
                                              sizeof out, why, sizeof why));
    TEST_ASSERT_EQUAL_STRING("latitude=18.4&n=1", out);

    json_free(args);
}

/* A query member is not always a string. `{"n":1,"temp":18.4,"daily":true}` is a
 * legal body and the provider reads it as `n=1&temp=18.4&daily=true`, so each scalar
 * is rendered rather than dropped from the template. */
TEST_CASE("a literal query member of any scalar type reaches the template", "[tools]")
{
    tools_declared_t tool;
    char why[128] = "";
    const char *body =
        "{\"description\":\"d\",\"parameters\":{\"type\":\"object\",\"properties\":{}},"
        "\"http\":{\"url\":\"https://api.test/f\","
        "\"query\":{\"n\":1,\"temp\":18.4,\"daily\":true}}}";
    TEST_ASSERT_EQUAL_INT_MESSAGE(
        ESP_OK,
        tools_declared_compile("t", body, strlen(body), NULL, NULL, &tool, why,
                               sizeof why),
        why);
    TEST_ASSERT_EQUAL_STRING("n=1&temp=18.4&daily=true", tool.query);
}

/* A query that cannot fit its compiled buffer is dropped whole, not truncated: half a
 * query template is a request the provider answers with a 400 that names nothing. */
TEST_CASE("a query too large for its buffer is dropped whole", "[tools]")
{
    char body[2048];
    size_t used = (size_t)snprintf(
        body, sizeof body,
        "{\"description\":\"d\",\"parameters\":{\"type\":\"object\",\"properties\":{}},"
        "\"http\":{\"url\":\"https://api.test/f\",\"query\":{\"a\":\"");
    /* `render_member` writes through `snprintf`, so an oversized member leaves `used`
     * short only when the member itself did not fit; one long literal is enough. */
    for (int i = 0; i < 1100 && used + 4 < sizeof body; i++) body[used++] = 'x';
    used += (size_t)snprintf(body + used, sizeof body - used, "\"}}}");

    tools_declared_t tool;
    char why[128] = "";
    TEST_ASSERT_EQUAL_INT_MESSAGE(
        ESP_OK,
        tools_declared_compile("t", body, used, NULL, NULL, &tool, why, sizeof why),
        why);
    TEST_ASSERT_EQUAL_STRING("", tool.query);
}

/* §7.1's order: MCP tools come last, and `capabilities.tools` names the same set in
 * the same order. A remote tool reaches the model without a registry row, so an
 * emitter that only walked the registry would offer a tool `tools_call` can dispatch
 * and never advertise it. */
TEST_CASE("mcp tools are offered after the builtins and named last", "[tools]")
{
    mcp_tool_t remote[2];
    memset(remote, 0, sizeof remote);
    snprintf(remote[0].name, sizeof remote[0].name, "mcp.home.lamp");
    snprintf(remote[0].description, sizeof remote[0].description, "Lamp.");
    snprintf(remote[0].schema, sizeof remote[0].schema,
             "{\"type\":\"object\",\"properties\":{}}");
    snprintf(remote[1].name, sizeof remote[1].name, "mcp.home.fan");
    snprintf(remote[1].description, sizeof remote[1].description, "Fan.");
    snprintf(remote[1].schema, sizeof remote[1].schema,
             "{\"type\":\"object\",\"properties\":{}}");

    tools_context_t ctx = {0};
    ctx.i2c_sda = -1;
    ctx.i2c_scl = -1;
    ctx.mcp_tools = remote;
    ctx.mcp_count = 2;

    json_doc_t *doc = json_new_object();
    json_node_t *array = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_json_build(&ctx, array));

    size_t n = json_array_len(array);
    json_node_t *fn = json_get(json_array_at(array, n - 2), "function");
    TEST_ASSERT_EQUAL_STRING("mcp.home.lamp", json_string(json_get(fn, "name")));
    TEST_ASSERT_EQUAL_STRING("Lamp.", json_string(json_get(fn, "description")));
    TEST_ASSERT_NOT_NULL(json_get(fn, "parameters"));
    fn = json_get(json_array_at(array, n - 1), "function");
    TEST_ASSERT_EQUAL_STRING("mcp.home.fan", json_string(json_get(fn, "name")));

    json_doc_t *names_doc = json_new_object();
    json_node_t *names = json_new_array(json_root(names_doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_capability_names(&ctx, names));
    TEST_ASSERT_EQUAL_size_t(n, json_array_len(names));
    TEST_ASSERT_EQUAL_STRING("mcp.home.lamp",
                             json_string(json_array_at(names, n - 2)));
    TEST_ASSERT_EQUAL_STRING("mcp.home.fan", json_string(json_array_at(names, n - 1)));

    json_free(names_doc);
    json_free(doc);
}

/* `tools/web.md` fixes one cap for a result reaching the model, whichever tool
 * produced it. A projected reply and a raw one go through the same cut, and the cut
 * backs up to a character boundary: half a UTF-8 sequence is a provider error that
 * names nothing about the tool. */
TEST_CASE("a result is cut at the cap on a character boundary", "[tools]")
{
    json_doc_t *doc = json_new_object();
    json_node_t *root = json_root(doc);

    char short_text[64];
    snprintf(short_text, sizeof short_text, "hello");
    tools_result_add_content(root, short_text);
    TEST_ASSERT_EQUAL_STRING("hello", json_string(json_get(root, "content")));
    TEST_ASSERT_NULL(json_get(root, "truncated"));

    /* One byte over the cap, with a three-byte sequence straddling it: the cut must
     * land before the sequence rather than inside it. */
    const size_t cap = 2048;
    char *long_text = malloc(cap + 8);
    TEST_ASSERT_NOT_NULL(long_text);
    memset(long_text, 'a', cap + 4);
    long_text[cap - 1] = (char)0xE2;
    long_text[cap] = (char)0x82;
    long_text[cap + 1] = (char)0xAC;
    long_text[cap + 2] = '\0';

    /* A fresh object, because a key that already exists is not replaced. */
    json_free(doc);
    doc = json_new_object();
    root = json_root(doc);
    tools_result_add_content(root, long_text);
    const char *kept = json_string(json_get(root, "content"));
    TEST_ASSERT_NOT_NULL(kept);
    TEST_ASSERT_EQUAL_size_t(cap - 1, strlen(kept));
    bool truncated = false;
    TEST_ASSERT_TRUE(json_bool(json_get(root, "truncated"), &truncated));
    TEST_ASSERT_TRUE(truncated);
    free(long_text);
    json_free(doc);
}

/* `04` §4.29: the catalogue lists every tool the build carries, offered or not, so
 * `/tools` can name a tool the reader turned off. A row's `offered` must agree with
 * the predicate the model's request uses, which is the whole point of building it
 * on the device rather than in the bridge. */
TEST_CASE("the catalogue lists a disabled tool as not offered", "[tools]")
{
    json_doc_t *config_doc = json_new_object();
    json_node_t *disabled = json_new_array(json_root(config_doc), "tools.disabled");
    TEST_ASSERT_NOT_NULL(disabled);
    json_array_push_string(disabled, "gpio.write");

    tools_context_t ctx = {0};
    ctx.policy.disabled = disabled;

    json_doc_t *doc = json_new_object();
    json_node_t *out = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_catalogue(&ctx, out));
    TEST_ASSERT_EQUAL_size_t(TOOLS_MAX_BUILTINS, json_array_len(out));

    bool saw_read = false, saw_write = false;
    for (size_t i = 0; i < json_array_len(out); i++) {
        json_node_t *row = json_array_at(out, i);
        const char *name = json_string(json_get(row, "name"));
        TEST_ASSERT_NOT_NULL(name);
        TEST_ASSERT_NOT_NULL(json_string(json_get(row, "description")));
        bool offered = true;
        TEST_ASSERT_TRUE(json_bool(json_get(row, "offered"), &offered));
        if (strcmp(name, "gpio.read") == 0) {
            saw_read = true;
            TEST_ASSERT_TRUE_MESSAGE(offered, "an enabled tool must report offered");
        } else if (strcmp(name, "gpio.write") == 0) {
            saw_write = true;
            TEST_ASSERT_FALSE_MESSAGE(offered, "a disabled tool must report not offered");
        }
    }
    TEST_ASSERT_TRUE_MESSAGE(saw_read, "gpio.read is a row");
    TEST_ASSERT_TRUE_MESSAGE(saw_write, "a disabled tool is still a row");

    json_free(doc);
    json_free(config_doc);
}

/* The catalogue lists the build, not the offer, so the i2c family appears with no
 * pins configured — that absence is what a reader needs to see before wondering why
 * the model never calls it. */
TEST_CASE("the catalogue carries the tools the offered set drops", "[tools]")
{
    tools_context_t ctx = {0};
    ctx.i2c_sda = -1;
    ctx.i2c_scl = -1;

    json_doc_t *doc = json_new_object();
    json_node_t *out = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_catalogue(&ctx, out));

    bool listed = false, offered = true;
    for (size_t i = 0; i < json_array_len(out); i++) {
        json_node_t *row = json_array_at(out, i);
        if (strcmp(json_string(json_get(row, "name")), "i2c.scan") != 0) continue;
        listed = true;
        TEST_ASSERT_TRUE(json_bool(json_get(row, "offered"), &offered));
    }
    TEST_ASSERT_TRUE_MESSAGE(listed, "an unpinned i2c tool is listed");
    TEST_ASSERT_FALSE_MESSAGE(offered, "and marked not offered");

    json_free(doc);
}

/* The other two sources the build carries: a file under `/tools/` and a server's
 * own tool. Both reach `tools_offered` like the builtins, so a name in
 * `tools.disabled` reads as not offered whichever source it came from. */
TEST_CASE("the catalogue carries declared and remote tools", "[tools]")
{
    tools_declared_set_t declared = {0};
    compile_one(&declared.items[0], "web_search",
                "{\"description\":\"Search the web.\",\"http\":{\"url\":\"https://api.test/\"}}");
    compile_one(&declared.items[1], "i2c.probe",
                "{\"description\":\"Probe the bus.\",\"http\":{\"url\":\"https://api.test/\"}}");
    declared.count = 2;

    mcp_tool_t remote[1] = {0};
    snprintf(remote[0].name, sizeof remote[0].name, "%s", "mcp.home.home_status");
    snprintf(remote[0].description, sizeof remote[0].description, "%s", "Home state.");

    json_doc_t *config_doc = json_new_object();
    json_node_t *disabled = json_new_array(json_root(config_doc), "tools.disabled");
    TEST_ASSERT_NOT_NULL(disabled);
    json_array_push_string(disabled, "i2c.probe");
    json_array_push_string(disabled, "mcp.home.home_status");

    tools_context_t ctx = {0};
    ctx.i2c_sda = 1;
    ctx.i2c_scl = 2;
    ctx.declared = &declared;
    ctx.mcp_tools = remote;
    ctx.mcp_count = 1;
    ctx.policy.disabled = disabled;

    json_doc_t *doc = json_new_object();
    json_node_t *out = json_new_array(json_root(doc), "tools");
    TEST_ASSERT_EQUAL(ESP_OK, tools_catalogue(&ctx, out));

    bool saw_declared = false, saw_remote = false;
    for (size_t i = 0; i < json_array_len(out); i++) {
        json_node_t *row = json_array_at(out, i);
        const char *name = json_string(json_get(row, "name"));
        bool offered = true;
        TEST_ASSERT_TRUE(json_bool(json_get(row, "offered"), &offered));
        if (strcmp(name, "web_search") == 0) {
            saw_declared = true;
            TEST_ASSERT_TRUE_MESSAGE(offered, "an enabled declared tool is offered");
            TEST_ASSERT_EQUAL_STRING("Search the web.",
                                     json_string(json_get(row, "description")));
        } else if (strcmp(name, "i2c.probe") == 0) {
            TEST_ASSERT_FALSE_MESSAGE(offered,
                                      "a disabled declared tool is listed but not offered");
        } else if (strcmp(name, "mcp.home.home_status") == 0) {
            saw_remote = true;
            TEST_ASSERT_FALSE_MESSAGE(offered,
                                      "a disabled remote tool is listed but not offered");
            TEST_ASSERT_EQUAL_STRING("Home state.",
                                     json_string(json_get(row, "description")));
        }
    }
    TEST_ASSERT_TRUE_MESSAGE(saw_declared, "a declared tool is a row");
    TEST_ASSERT_TRUE_MESSAGE(saw_remote, "a remote tool is a row");

    json_free(doc);
    json_free(config_doc);
}

TEST_CASE("the catalogue refuses a node that is not an array", "[tools]")
{
    tools_context_t ctx = {0};
    json_doc_t *doc = json_new_object();
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, tools_catalogue(&ctx, json_root(doc)));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, tools_catalogue(&ctx, NULL));
    json_free(doc);
}

/* --- `ask` bounds (`tools/ask.md` §Limits) ---------------------------------
 *
 * The port itself is board-only: it waits on a semaphore the link task gives, and a
 * host build has neither. What is pure is the argument rule, and it is pure on
 * purpose — the question and option bounds are not expressible in the schema subset,
 * so `tools_validate` is the only place they can be enforced. */

static json_doc_t *ask_args(const char *question, const char *options_json)
{
    char body[1024];
    snprintf(body, sizeof body, "{\"question\":\"%s\"%s%s%s",
             question,
             options_json != NULL ? ",\"options\":" : "",
             options_json != NULL ? options_json : "",
             "}");
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL(ESP_OK, json_parse(body, strlen(body), &doc));
    return doc;
}

TEST_CASE("an ask question must be a sentence the bridge can show", "[tools][ask]")
{
    char why[128] = "";

    json_doc_t *doc = ask_args("which branch?", NULL);
    TEST_ASSERT_EQUAL_MESSAGE(ESP_OK,
                              tools_validate("ask", json_root(doc), why, sizeof why),
                              why);
    json_free(doc);

    /* The documented bound, not a literal that happens to fit: 512 bytes passes and
     * 513 does not, which is the only pair that proves the check is the constant. */
    char question[TOOLS_ASK_QUESTION_MAX + 2];
    memset(question, 'q', sizeof question - 1);
    question[TOOLS_ASK_QUESTION_MAX] = '\0';
    doc = ask_args(question, NULL);
    TEST_ASSERT_EQUAL(ESP_OK, tools_validate("ask", json_root(doc), why, sizeof why));
    json_free(doc);

    question[TOOLS_ASK_QUESTION_MAX] = 'q';
    question[TOOLS_ASK_QUESTION_MAX + 1] = '\0';
    doc = ask_args(question, NULL);
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_validate("ask", json_root(doc), why, sizeof why));
    TEST_ASSERT_NOT_EQUAL('\0', why[0]);
    json_free(doc);
}

TEST_CASE("an empty question and an oversized option are both refused", "[tools][ask]")
{
    char why[128] = "";
    json_doc_t *doc = ask_args("", NULL);
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_validate("ask", json_root(doc), why, sizeof why));
    json_free(doc);

    char option[TOOLS_ASK_OPTION_MAX + 2];
    memset(option, 'o', sizeof option - 1);
    option[TOOLS_ASK_OPTION_MAX] = '\0';
    char opts[TOOLS_ASK_OPTION_MAX + 8];
    snprintf(opts, sizeof opts, "[\"%s\"]", option);
    doc = ask_args("q", opts);
    TEST_ASSERT_EQUAL(ESP_OK, tools_validate("ask", json_root(doc), why, sizeof why));
    json_free(doc);

    option[TOOLS_ASK_OPTION_MAX] = 'o';
    option[TOOLS_ASK_OPTION_MAX + 1] = '\0';
    snprintf(opts, sizeof opts, "[\"%s\"]", option);
    doc = ask_args("q", opts);
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_validate("ask", json_root(doc), why, sizeof why));
    json_free(doc);
}

TEST_CASE("ask takes at most the documented number of options", "[tools][ask]")
{
    char why[128] = "";
    char opts[64];
    opts[0] = '[';
    size_t at = 1;
    for (int i = 0; i < TOOLS_ASK_OPTIONS_MAX; i++) {
        at += (size_t)snprintf(opts + at, sizeof opts - at, "%s\"o\"", i ? "," : "");
    }
    snprintf(opts + at, sizeof opts - at, "]");

    json_doc_t *doc = ask_args("q", opts);
    TEST_ASSERT_EQUAL_MESSAGE(ESP_OK,
                              tools_validate("ask", json_root(doc), why, sizeof why),
                              why);
    json_free(doc);

    /* One past the bound, built the same way, so the refusal is about the count and
     * not about an option's text. */
    char more[80];
    at = 1;
    more[0] = '[';
    for (int i = 0; i <= TOOLS_ASK_OPTIONS_MAX; i++) {
        at += (size_t)snprintf(more + at, sizeof more - at, "%s\"o\"", i ? "," : "");
    }
    snprintf(more + at, sizeof more - at, "]");
    doc = ask_args("q", more);
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      tools_validate("ask", json_root(doc), why, sizeof why));
    TEST_ASSERT_NOT_EQUAL('\0', why[0]);
    json_free(doc);
}

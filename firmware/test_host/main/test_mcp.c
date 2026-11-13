/* Host tests for `mcp_parse.c` — the pure half of `mcp`.
 *
 * The remote server is untrusted input (`06-security.md` T9), so what these assert is
 * what the device does with a hostile reply, not that a socket opened. */

#include <string.h>

#include "unity.h"

#include "mcp.h"
#include "store.h"

/* --- The namespace --------------------------------------------------------
 * `00-overview.md` §7 fixes the model-facing name as `mcp.<server_id>.<tool_name>`.
 * A name that does not survive a round trip would dispatch to the wrong server or to
 * a built-in, so it is checked at both ends. */

TEST_CASE("a server id survives the namespace and a dotted one is refused", "[mcp]")
{
    TEST_ASSERT_TRUE(mcp_id_is_valid("home"));
    TEST_ASSERT_TRUE(mcp_id_is_valid("lab-2"));
    TEST_ASSERT_TRUE(mcp_id_is_valid("a_b"));
    TEST_ASSERT_TRUE(mcp_id_is_valid("0123456789abcdef")); /* 16, the cap */

    /* `.` is the separator: an id carrying one makes the name ambiguous. */
    TEST_ASSERT_FALSE(mcp_id_is_valid("a.b"));
    TEST_ASSERT_FALSE(mcp_id_is_valid(""));
    TEST_ASSERT_FALSE(mcp_id_is_valid(NULL));
    TEST_ASSERT_FALSE(mcp_id_is_valid("0123456789abcdefg")); /* 17, one past the cap */
    TEST_ASSERT_FALSE(mcp_id_is_valid("Home"));              /* uppercase is not in the set */
    TEST_ASSERT_FALSE(mcp_id_is_valid("a b"));
}

TEST_CASE("a namespaced tool name round trips", "[mcp]")
{
    char name[MCP_NAME_MAX + 1];
    TEST_ASSERT_TRUE(mcp_tool_name("home", "status", name, sizeof name));
    TEST_ASSERT_EQUAL_STRING("mcp.home.status", name);

    char id[MCP_ID_MAX + 1];
    char tool[MCP_TOOL_NAME_MAX + 1];
    TEST_ASSERT_TRUE(mcp_tool_split(name, id, sizeof id, tool, sizeof tool));
    TEST_ASSERT_EQUAL_STRING("home", id);
    TEST_ASSERT_EQUAL_STRING("status", tool);

    /* A name that does not fit is refused, never truncated: a truncated name would
     * dispatch somewhere else. */
    char small[8];
    TEST_ASSERT_FALSE(mcp_tool_name("home", "status", small, sizeof small));
}

TEST_CASE("a name outside the namespace does not split", "[mcp]")
{
    char id[MCP_ID_MAX + 1];
    char tool[MCP_TOOL_NAME_MAX + 1];
    TEST_ASSERT_FALSE(mcp_tool_split("gpio.read", id, sizeof id, tool, sizeof tool));
    TEST_ASSERT_FALSE(mcp_tool_split("mcp.home", id, sizeof id, tool, sizeof tool));
    TEST_ASSERT_FALSE(mcp_tool_split("mcp..status", id, sizeof id, tool, sizeof tool));
    TEST_ASSERT_FALSE(mcp_tool_split("mcp.home.", id, sizeof id, tool, sizeof tool));
    TEST_ASSERT_FALSE(mcp_tool_split(NULL, id, sizeof id, tool, sizeof tool));
}

TEST_CASE("a remote tool name is filtered, not escaped", "[mcp]")
{
    TEST_ASSERT_TRUE(mcp_remote_tool_is_valid("get_weather"));
    TEST_ASSERT_TRUE(mcp_remote_tool_is_valid("Weather-2"));
    /* A name carrying the separator would nest one namespace inside another. */
    TEST_ASSERT_FALSE(mcp_remote_tool_is_valid("a.b"));
    TEST_ASSERT_FALSE(mcp_remote_tool_is_valid("a b"));
    TEST_ASSERT_FALSE(mcp_remote_tool_is_valid(""));
    TEST_ASSERT_FALSE(mcp_remote_tool_is_valid(NULL));
    TEST_ASSERT_FALSE(mcp_remote_tool_is_valid(
        "0123456789012345678901234567890123456789012345678")); /* 49, past the cap */
}

TEST_CASE("a server url must be https", "[mcp]")
{
    TEST_ASSERT_TRUE(mcp_url_is_valid("https://mcp.example.com/mcp"));
    TEST_ASSERT_FALSE(mcp_url_is_valid("http://mcp.example.com/mcp"));
    TEST_ASSERT_FALSE(mcp_url_is_valid("https://"));
    TEST_ASSERT_FALSE(mcp_url_is_valid(NULL));
}

/* --- Request bodies ------------------------------------------------------- */

TEST_CASE("the handshake names the protocol version and itself", "[mcp]")
{
    char body[MCP_BODY_MAX];
    size_t len = mcp_initialize_body(7, body, sizeof body);
    TEST_ASSERT_GREATER_THAN(0, len);

    /* Assert the parsed content, not a substring of the text: the server reads it as
     * JSON, so what matters is that it parses. */
    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(body, len, &doc));
    json_node_t *root = json_root(doc);
    TEST_ASSERT_EQUAL_STRING("initialize", json_string(json_get(root, "method")));
    int64_t id = 0;
    TEST_ASSERT_TRUE(json_int(json_get(root, "id"), &id));
    TEST_ASSERT_EQUAL_INT(7, id);
    json_node_t *params = json_get(root, "params");
    TEST_ASSERT_NOT_NULL(json_string(json_get(params, "protocolVersion")));
    json_free(doc);
}

TEST_CASE("an oversized body is refused rather than cut", "[mcp]")
{
    char small[16];
    TEST_ASSERT_EQUAL_UINT(0, mcp_tools_list_body(1, small, sizeof small));
    char body[MCP_BODY_MAX];
    TEST_ASSERT_GREATER_THAN(0, mcp_tools_list_body(1, body, sizeof body));
}

TEST_CASE("a call body splices the model's arguments", "[mcp]")
{
    json_doc_t *args = NULL;
    const char *args_text = "{\"city\":\"Utrecht\",\"days\":3}";
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(args_text, strlen(args_text), &args));

    char body[MCP_BODY_MAX];
    size_t len = mcp_call_body(2, "forecast", json_root(args), body, sizeof body);
    TEST_ASSERT_GREATER_THAN(0, len);

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(body, len, &doc));
    json_node_t *params = json_get(json_root(doc), "params");
    TEST_ASSERT_EQUAL_STRING("forecast", json_string(json_get(params, "name")));
    json_node_t *sent = json_get(params, "arguments");
    TEST_ASSERT_EQUAL_STRING("Utrecht", json_string(json_get(sent, "city")));
    int64_t days = 0;
    TEST_ASSERT_TRUE(json_int(json_get(sent, "days"), &days));
    TEST_ASSERT_EQUAL_INT(3, days);
    json_free(doc);
    json_free(args);
}

TEST_CASE("a call with no arguments sends an empty object", "[mcp]")
{
    char body[MCP_BODY_MAX];
    size_t len = mcp_call_body(1, "ping", NULL, body, sizeof body);
    TEST_ASSERT_GREATER_THAN(0, len);

    json_doc_t *doc = NULL;
    TEST_ASSERT_EQUAL_INT(ESP_OK, json_parse(body, len, &doc));
    json_node_t *sent = json_get(json_get(json_root(doc), "params"), "arguments");
    TEST_ASSERT_NOT_NULL(sent);
    TEST_ASSERT_EQUAL_UINT(0, json_object_len(sent));
    json_free(doc);
}

/* --- The tools projection ------------------------------------------------- */

static const char *TOOLS_REPLY =
    "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":["
    "{\"name\":\"status\",\"description\":\"Read the door\","
    "\"inputSchema\":{\"type\":\"object\",\"properties\":{}}},"
    "{\"name\":\"set_state\",\"description\":\"Write it\","
    "\"inputSchema\":{\"type\":\"object\"}}]}}";

TEST_CASE("tools/list yields both tools with their schemas", "[mcp]")
{
    mcp_tool_set_t set = {0};
    bool truncated = false;
    TEST_ASSERT_TRUE(mcp_parse_tools(TOOLS_REPLY, strlen(TOOLS_REPLY), &set, &truncated));
    TEST_ASSERT_FALSE(truncated);
    TEST_ASSERT_EQUAL_UINT(2, set.count);
    TEST_ASSERT_EQUAL_STRING("status", set.items[0].name);
    TEST_ASSERT_EQUAL_STRING("Read the door", set.items[0].description);
    /* The schema is forwarded as the server sent it, so the model is offered the
     * server's own contract rather than a guess. */
    /* The schema is the server's object itself, not `{"parameters":{...}}`: the
     * wrapper would be a malformed `parameters` value and the tool uncallable. */
    TEST_ASSERT_EQUAL_STRING("{\"properties\":{},\"type\":\"object\"}", set.items[0].schema);
}

TEST_CASE("a tool whose name would not survive the namespace is dropped", "[mcp]")
{
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":["
        "{\"name\":\"a.b\",\"description\":\"nested\"},"
        "{\"name\":\"fine\",\"description\":\"kept\"}]}}";
    mcp_tool_set_t set = {0};
    bool truncated = false;
    TEST_ASSERT_TRUE(mcp_parse_tools(body, strlen(body), &set, &truncated));
    /* Dropped, not escaped: an escaped name would be a different tool from the one
     * the server expects a call to. */
    TEST_ASSERT_EQUAL_UINT(1, set.count);
    TEST_ASSERT_EQUAL_STRING("fine", set.items[0].name);
}

TEST_CASE("a tool with no schema still gets the accepting one", "[mcp]")
{
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":[{\"name\":\"bare\"}]}}";
    mcp_tool_set_t set = {0};
    bool truncated = false;
    TEST_ASSERT_TRUE(mcp_parse_tools(body, strlen(body), &set, &truncated));
    TEST_ASSERT_EQUAL_UINT(1, set.count);
    /* `{"type":"object"}` with no `properties` accepts every argument; an empty
     * schema object would be refused by a strict provider. */
    TEST_ASSERT_NOT_NULL(strstr(set.items[0].schema, "\"type\":\"object\""));
}

TEST_CASE("a tool list past the cap reports truncation", "[mcp]")
{
    char body[4096];
    size_t used = (size_t)snprintf(body, sizeof body,
                                   "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":[");
    for (int i = 0; i < MCP_MAX_TOOLS + 2; i++) {
        used += (size_t)snprintf(body + used, sizeof body - used, "%s{\"name\":\"t%d\"}",
                                 i > 0 ? "," : "", i);
    }
    snprintf(body + used, sizeof body - used, "]}}");

    mcp_tool_set_t set = {0};
    bool truncated = false;
    TEST_ASSERT_TRUE(mcp_parse_tools(body, strlen(body), &set, &truncated));
    TEST_ASSERT_EQUAL_UINT(MCP_MAX_TOOLS, set.count);
    /* Said, not silent: an absent tool the model cannot call is a fact the log owes. */
    TEST_ASSERT_TRUE(truncated);
}

TEST_CASE("a reply that is not a tool list is refused whole", "[mcp]")
{
    mcp_tool_set_t set = {0};
    bool truncated = false;
    /* A JSON-RPC error reply. */
    const char *error = "{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32601,"
                        "\"message\":\"Method not found\"}}";
    TEST_ASSERT_FALSE(mcp_parse_tools(error, strlen(error), &set, &truncated));
    TEST_ASSERT_EQUAL_UINT(0, set.count);
    /* A result that is not an array. */
    const char *bad = "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":\"no\"}}";
    TEST_ASSERT_FALSE(mcp_parse_tools(bad, strlen(bad), &set, &truncated));
    /* Not JSON at all. */
    TEST_ASSERT_FALSE(mcp_parse_tools("not json", 8, &set, &truncated));
}

/* --- A call's reply ------------------------------------------------------- */

TEST_CASE("a call reply joins its text items", "[mcp]")
{
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"content\":["
        "{\"type\":\"text\",\"text\":\"door:\"},"
        "{\"type\":\"text\",\"text\":\"open\"}],\"isError\":false}}";
    char out[256];
    bool is_error = true;
    TEST_ASSERT_TRUE(mcp_parse_call(body, strlen(body), out, sizeof out, &is_error));
    TEST_ASSERT_EQUAL_STRING("door: open", out);
    TEST_ASSERT_FALSE(is_error);
}

TEST_CASE("a non-text item contributes its type", "[mcp]")
{
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"content\":["
        "{\"type\":\"image\",\"data\":\"AAAA\"}]}}";
    char out[256];
    bool is_error = false;
    TEST_ASSERT_TRUE(mcp_parse_call(body, strlen(body), out, sizeof out, &is_error));
    /* Visible rather than empty: an empty result reads as success with no answer. */
    TEST_ASSERT_EQUAL_STRING("[image]", out);
}

TEST_CASE("a tool-level failure is reported as one", "[mcp]")
{
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"content\":["
        "{\"type\":\"text\",\"text\":\"door jammed\"}],\"isError\":true}}";
    char out[256];
    bool is_error = false;
    TEST_ASSERT_TRUE(mcp_parse_call(body, strlen(body), out, sizeof out, &is_error));
    TEST_ASSERT_TRUE(is_error);
    TEST_ASSERT_EQUAL_STRING("door jammed", out);
}

TEST_CASE("a reply with no content is an empty success", "[mcp]")
{
    const char *body = "{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{}}";
    char out[256];
    bool is_error = true;
    TEST_ASSERT_TRUE(mcp_parse_call(body, strlen(body), out, sizeof out, &is_error));
    /* A tool with only a side effect answers with nothing; that is not a failure. */
    TEST_ASSERT_EQUAL_STRING("", out);
    TEST_ASSERT_FALSE(is_error);
}

TEST_CASE("a reply cut at the cap is marked", "[mcp]")
{
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"content\":["
        "{\"type\":\"text\",\"text\":\"0123456789012345678901234567890123456789\"}]}}";
    char out[16];
    bool is_error = false;
    TEST_ASSERT_TRUE(mcp_parse_call(body, strlen(body), out, sizeof out, &is_error));
    /* The ellipsis is the same marker `tools` uses for a cut result. */
    TEST_ASSERT_NOT_NULL(strstr(out, "…"));
    TEST_ASSERT_LESS_THAN(16, (int)strlen(out) + 1);
}

TEST_CASE("a call reply that is an error envelope is not a result", "[mcp]")
{
    /* A JSON-RPC error carries no `result`, so there is nothing to join. The caller
     * reports it through `mcp_parse_error`, and this must not be mistaken for an
     * empty success. */
    const char *body =
        "{\"jsonrpc\":\"2.0\",\"id\":3,\"error\":{\"code\":-32601,"
        "\"message\":\"Method not found\"}}";
    char out[64];
    bool is_error = false;
    TEST_ASSERT_FALSE(mcp_parse_call(body, strlen(body), out, sizeof out, &is_error));
    TEST_ASSERT_EQUAL_STRING("", out);
}

TEST_CASE("the error message is what a failure reports", "[mcp]")
{
    const char *body = "{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32602,"
                       "\"message\":\"Invalid params\"}}";
    char out[128];
    TEST_ASSERT_TRUE(mcp_parse_error(body, strlen(body), out, sizeof out));
    TEST_ASSERT_EQUAL_STRING("Invalid params", out);

    /* A success reply carries no message, so the caller has nothing to quote. */
    const char *ok = "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}";
    TEST_ASSERT_FALSE(mcp_parse_error(ok, strlen(ok), out, sizeof out));
}

/* mcp_parse — the pure half of `mcp`: JSON-RPC envelopes, the tools projection and
 * the namespace rules. No socket, no clock, no config read, so every rule
 * `02-firmware.md` §6 states is a host test rather than a capture from a live server.
 *
 * The remote side is untrusted input (`06-security.md` T9). A tool name from a server
 * becomes part of the model-facing name, so it is filtered against a strict charset
 * rather than escaped; a description is bounded; a schema is forwarded only if it is
 * an object.
 */

#include "mcp.h"

#include <stdio.h>
#include <string.h>

#include "store.h"

/* --- Names and the namespace ---------------------------------------------- */

bool mcp_id_is_valid(const char *id)
{
    if (id == NULL) return false;
    size_t len = strlen(id);
    if (len == 0 || len > MCP_ID_MAX) return false;
    for (size_t i = 0; i < len; i++) {
        char c = id[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
        /* `.` is the namespace separator, so an id carrying one would make the
         * namespaced name ambiguous (`00-overview.md` §7). */
        if (!ok) return false;
    }
    return true;
}

bool mcp_remote_tool_is_valid(const char *name)
{
    if (name == NULL) return false;
    size_t len = strlen(name);
    if (len == 0 || len > MCP_TOOL_NAME_MAX) return false;
    for (size_t i = 0; i < len; i++) {
        char c = name[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '-';
        if (!ok) return false;
    }
    return true;
}

bool mcp_url_is_valid(const char *url)
{
    return url != NULL && strncmp(url, "https://", 8) == 0 && strlen(url) > 8;
}

bool mcp_tool_name(const char *server_id, const char *tool, char *out, size_t cap)
{
    if (server_id == NULL || tool == NULL || out == NULL || cap == 0) return false;
    int n = snprintf(out, cap, "mcp.%s.%s", server_id, tool);
    return n > 0 && (size_t)n < cap;
}

bool mcp_tool_split(const char *name, char *server_id, size_t id_cap, char *tool,
                    size_t tool_cap)
{
    if (name == NULL || strncmp(name, "mcp.", 4) != 0) return false;
    const char *rest = name + 4;
    const char *dot = strchr(rest, '.');
    /* A name with no second separator is not a namespaced tool: it is either a
     * built-in or a mistake. */
    if (dot == NULL) return false;
    size_t id_len = (size_t)(dot - rest);
    const char *tool_name = dot + 1;
    size_t tool_len = strlen(tool_name);
    if (id_len == 0 || tool_len == 0) return false;
    if (server_id != NULL) {
        if (id_len >= id_cap) return false;
        memcpy(server_id, rest, id_len);
        server_id[id_len] = '\0';
    }
    if (tool != NULL) {
        if (tool_len >= tool_cap) return false;
        memcpy(tool, tool_name, tool_len);
        tool[tool_len] = '\0';
    }
    return true;
}

/* --- Request bodies ------------------------------------------------------- */

/* A request body is built by hand rather than through `json_emit`, because the
 * `arguments` member must be spliced from the caller's already-parsed document and a
 * clone-and-emit round trip would cost more code than it saves. */
size_t mcp_initialize_body(int32_t id, char *buf, size_t cap)
{
    if (buf == NULL) return 0;
    int n = snprintf(buf, cap,
                     "{\"jsonrpc\":\"2.0\",\"id\":%d,\"method\":\"initialize\","
                     "\"params\":{\"protocolVersion\":\"2025-03-26\","
                     "\"capabilities\":{},\"clientInfo\":{\"name\":\"musteval\","
                     "\"version\":\"1.0.0\"}}}",
                     (int)id);
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

size_t mcp_tools_list_body(int32_t id, char *buf, size_t cap)
{
    if (buf == NULL) return 0;
    int n = snprintf(buf, cap,
                     "{\"jsonrpc\":\"2.0\",\"id\":%d,\"method\":\"tools/list\"}", (int)id);
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

size_t mcp_call_body(int32_t id, const char *tool, const json_node_t *args, char *buf,
                     size_t cap)
{
    if (buf == NULL || tool == NULL) return 0;

    /* The arguments are emitted straight into the body's tail, so an argument set too
     * large for the remaining room is refused before the head is written: a
     * half-written body is worse than none. `json_emit` needs a document of its own,
     * and the caller's `args` belongs to another one, so its members are copied. */
    size_t head;
    if (args != NULL) {
        head = (size_t)snprintf(buf, cap,
                                "{\"jsonrpc\":\"2.0\",\"id\":%d,\"method\":\"tools/call\","
                                "\"params\":{\"name\":\"%s\",\"arguments\":",
                                (int)id, tool);
        if (head == 0 || head >= cap) return 0;

        json_doc_t *doc = json_new_object();
        if (doc == NULL) return 0;
        json_node_t *root = json_root(doc);
        /* Every index below the length yields a key and a value: `json_object_key`
         * returns "" rather than NULL for a keyless member, and a value at an index
         * inside the length exists. A guard here would be unreachable. */
        size_t count = json_object_len(args);
        for (size_t i = 0; i < count; i++) {
            json_add_node(root, json_object_key(args, i), json_object_value(args, i));
        }
        /* Two bytes held back: the `arguments` object's brace and the envelope's, and
         * one more for the terminator. */
        if (cap - head < 3) return 0;
        size_t written = 0;
        esp_err_t err = json_emit(doc, buf + head, cap - head - 2, &written);
        json_free(doc);
        if (err != ESP_OK) return 0;
        size_t total = head + written;
        buf[total++] = '}';
        buf[total++] = '}';
        buf[total] = '\0';
        return total;
    }

    int n = snprintf(buf, cap,
                     "{\"jsonrpc\":\"2.0\",\"id\":%d,\"method\":\"tools/call\","
                     "\"params\":{\"name\":\"%s\",\"arguments\":{}}}",
                     (int)id, tool);
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

/* --- Responses ------------------------------------------------------------ */

/* The `result` object of a JSON-RPC reply, or NULL when the body is not one. */
static json_node_t *reply_result(json_doc_t **doc, const char *body, size_t len)
{
    if (body == NULL || len == 0) return NULL;
    if (json_parse(body, len, doc) != ESP_OK) return NULL;
    json_node_t *root = json_root(*doc);
    if (root == NULL || !json_is_object(root)) return NULL;
    return json_get(root, "result");
}

bool mcp_parse_tools(const char *body, size_t len, mcp_tool_set_t *out, bool *truncated)
{
    if (out == NULL) return false;
    out->count = 0;
    if (truncated != NULL) *truncated = false;

    json_doc_t *doc = NULL;
    json_node_t *result = reply_result(&doc, body, len);
    if (result == NULL) {
        json_free(doc);
        return false;
    }

    json_node_t *tools = json_get(result, "tools");
    if (tools == NULL || !json_is_array(tools)) {
        json_free(doc);
        return false;
    }

    size_t n = json_array_len(tools);
    for (size_t i = 0; i < n; i++) {
        json_node_t *entry = json_array_at(tools, i);
        const char *name = json_string(json_get(entry, "name"));
        /* A name that would not survive the namespace is dropped, not escaped: it is
         * what the model sees and what dispatch splits on. */
        if (!mcp_remote_tool_is_valid(name)) continue;
        if (out->count >= MCP_MAX_TOOLS) {
            if (truncated != NULL) *truncated = true;
            break;
        }
        mcp_tool_t *tool = &out->items[out->count];
        memset(tool, 0, sizeof *tool);
        snprintf(tool->name, sizeof tool->name, "%s", name);

        const char *desc = json_string(json_get(entry, "description"));
        if (desc != NULL) {
            /* A description is untrusted prose that reaches the model. Bounded, and a
             * longer one is cut at a character: unlike a name, a short description is
             * not a dispatch hazard. */
            snprintf(tool->description, sizeof tool->description, "%s", desc);
        }

        json_node_t *schema = json_get(entry, "inputSchema");
        if (schema != NULL && json_is_object(schema)) {
            json_doc_t *wrap = json_new_object();
            if (wrap != NULL) {
                json_add_node(json_root(wrap), "parameters", schema);
                char text[sizeof tool->schema];
                size_t written = 0;
                if (json_emit(wrap, text, sizeof text, &written) == ESP_OK) {
                    /* Re-slice just the value: `json_emit` wrapped it under
                     * "parameters", so the wrapper's `{"parameters":` prefix and its
                     * closing brace have to go. The same fix as `tools_declared.c`,
                     * and for the same reason: keeping the wrapper makes the schema
                     * malformed and the model is offered a tool it cannot call. */
                    const char *colon = strchr(text, ':');
                    if (colon != NULL) {
                        const char *value = colon + 1;
                        size_t value_len = strlen(value);
                        if (value_len > 0 && value[value_len - 1] == '}') value_len--;
                        snprintf(tool->schema, sizeof tool->schema, "%.*s", (int)value_len,
                                 value);
                    }
                }
                json_free(wrap);
            }
        }
        if (tool->schema[0] == '\0') {
            /* The only schema that accepts every argument. */
            snprintf(tool->schema, sizeof tool->schema, "{\"type\":\"object\"}");
        }
        out->count++;
    }

    json_free(doc);
    return true;
}

bool mcp_parse_call(const char *body, size_t len, char *out, size_t cap, bool *is_error)
{
    if (out == NULL || cap == 0) return false;
    out[0] = '\0';
    if (is_error != NULL) *is_error = false;

    json_doc_t *doc = NULL;
    json_node_t *result = reply_result(&doc, body, len);
    if (result == NULL) {
        json_free(doc);
        return false;
    }

    bool flagged = false;
    if (json_bool(json_get(result, "isError"), &flagged) && is_error != NULL) {
        *is_error = flagged;
    }

    /* A server may answer with `content[]`, with `structuredContent`, or with neither.
     * The text joined from `content[]` is what every server documents; a non-text item
     * contributes its type so a refusal is visible rather than empty. */
    json_node_t *content = json_get(result, "content");
    size_t used = 0;
    if (content != NULL && json_is_array(content)) {
        size_t n = json_array_len(content);
        for (size_t i = 0; i < n; i++) {
            json_node_t *item = json_array_at(content, i);
            const char *text = json_string(json_get(item, "text"));
            char bracket[MCP_DESC_MAX + 2];
            if (text == NULL) {
                const char *type = json_string(json_get(item, "type"));
                snprintf(bracket, sizeof bracket, "[%s]", type != NULL ? type : "unknown");
                text = bracket;
            }
            size_t need = strlen(text) + (used > 0 ? 1 : 0);
            if (used + need >= cap) {
                /* Cut at the cap and say so, the same rule `tools` applies to a long
                 * result (`TOOLS_RESULT_MAX`). */
                snprintf(out + used, cap - used, "%s…", used > 0 ? " " : "");
                json_free(doc);
                return true;
            }
            if (used > 0) out[used++] = ' ';
            memcpy(out + used, text, strlen(text));
            used += strlen(text);
            out[used] = '\0';
        }
    }

    bool err_flag = false;
    json_bool(json_get(result, "isError"), &err_flag);
    if (used == 0 && !err_flag) {
        /* No content at all is not a failure: a tool with a side effect answers with
         * an empty list. The caller reports an empty result rather than a refusal. */
        out[0] = '\0';
    }
    json_free(doc);
    return true;
}

bool mcp_parse_error(const char *body, size_t len, char *out, size_t cap)
{
    if (out == NULL || cap == 0) return false;
    out[0] = '\0';
    if (body == NULL || len == 0) return false;

    json_doc_t *doc = NULL;
    if (json_parse(body, len, &doc) != ESP_OK) return false;
    json_node_t *root = json_root(doc);
    json_node_t *error = root != NULL ? json_get(root, "error") : NULL;
    if (error == NULL || !json_is_object(error)) {
        json_free(doc);
        return false;
    }
    const char *message = json_string(json_get(error, "message"));
    if (message != NULL) snprintf(out, cap, "%s", message);
    json_free(doc);
    return message != NULL;
}

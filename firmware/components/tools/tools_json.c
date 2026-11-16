/* tools_json — the tool array the model is offered.
 *
 * Pure: it walks the registry and the declarative set and emits
 * `{"type":"function","function":{...}}` entries. It lived in board-only `tools.c`,
 * which meant the array the device actually sends could only be inspected by
 * spending a provider call: every request was answered `invalid character ','` and
 * nothing on the host could reproduce it.
 */

#include "tools.h"

#include <stdio.h>
#include <string.h>

/* --- `tools_json` and the offered set ------------------------------------- */

/* One `{"type":"function","function":{"name","description","parameters"}}`. The
 * `parameters` text is embedded verbatim rather than rebuilt, because the document
 * order of a schema is what the provider sees and a rebuild would only re-copy it. */
static void push_function(json_node_t *out, const char *name, const char *desc,
                          const char *schema_text)
{
    json_node_t *entry = json_array_push_object(out);
    if (entry == NULL) return;
    json_add_string(entry, "type", "function");
    json_node_t *fn = json_add_object(entry, "function");
    if (fn == NULL) return;
    json_add_string(fn, "name", name);
    json_add_string(fn, "description", desc != NULL ? desc : "");
    /* A schema is valid JSON by construction (`tools_spec.c` holds literals), so it
     * parses; a failure is a firmware bug and leaves the tool without parameters
     * rather than emitting malformed JSON. */
    json_doc_t *doc = NULL;
    if (schema_text != NULL && json_parse(schema_text, strlen(schema_text), &doc) == ESP_OK) {
        json_add_node(fn, "parameters", json_root(doc));
        json_free(doc);
    } else {
        json_add_object(fn, "parameters");
    }
}

esp_err_t tools_json_build(const tools_context_t *ctx, json_node_t *out)
{
    if (out == NULL || !json_is_array(out)) return ESP_ERR_INVALID_ARG;

    size_t count = 0;
    const tools_builtin_t *builtins = tools_builtins(&count);
    for (size_t i = 0; i < count; i++) {
        if (!tools_offered(builtins[i].name, ctx)) continue;
        push_function(out, builtins[i].name, builtins[i].description, builtins[i].schema);
    }

    const tools_declared_set_t *declared = ctx != NULL ? ctx->declared : NULL;
    if (declared != NULL) {
        for (size_t i = 0; i < declared->count; i++) {
            if (!tools_offered(declared->items[i].name, ctx)) continue;
            push_function(out, declared->items[i].name, declared->items[i].description,
                          declared->items[i].schema);
        }
    }

    /* MCP last, the order `02-firmware.md` §7.1 fixes. A remote tool's schema is the
     * server's own, already in the frozen subset by construction: it is forwarded in
     * the shape the server advertises, and a server whose schema the provider refuses
     * fails its own tool rather than the array. */
    if (ctx != NULL && ctx->mcp_tools != NULL) {
        for (size_t i = 0; i < ctx->mcp_count; i++) {
            push_function(out, ctx->mcp_tools[i].name, ctx->mcp_tools[i].description,
                          ctx->mcp_tools[i].schema);
        }
    }
    return ESP_OK;
}

esp_err_t tools_capability_names(const tools_context_t *ctx, json_node_t *out)
{
    if (out == NULL || !json_is_array(out)) return ESP_ERR_INVALID_ARG;

    size_t count = 0;
    const tools_builtin_t *builtins = tools_builtins(&count);
    for (size_t i = 0; i < count; i++) {
        if (!tools_offered(builtins[i].name, ctx)) continue;
        json_array_push_string(out, builtins[i].name);
    }
    const tools_declared_set_t *declared = ctx != NULL ? ctx->declared : NULL;
    if (declared != NULL) {
        for (size_t i = 0; i < declared->count; i++) {
            if (!tools_offered(declared->items[i].name, ctx)) continue;
            json_array_push_string(out, declared->items[i].name);
        }
    }
    if (ctx != NULL && ctx->mcp_tools != NULL) {
        for (size_t i = 0; i < ctx->mcp_count; i++) {
            json_array_push_string(out, ctx->mcp_tools[i].name);
        }
    }
    return ESP_OK;
}

/* One catalogue row: `{name, description, offered}`. The bridge renders `/tools` and
 * `/tool list` from this, so the two surfaces cannot show different sets, and it adds
 * the state the device cannot know — whether a subsystem's own switch is off — from
 * the config it already holds. */
static void push_catalogue_row(json_node_t *out, const char *name, const char *desc,
                               bool offered)
{
    json_node_t *row = json_array_push_object(out);
    if (row == NULL) return;
    json_add_string(row, "name", name);
    json_add_string(row, "description", desc != NULL ? desc : "");
    json_add_bool(row, "offered", offered);
}

esp_err_t tools_catalogue(const tools_context_t *ctx, json_node_t *out)
{
    if (out == NULL || !json_is_array(out)) return ESP_ERR_INVALID_ARG;

    size_t count = 0;
    const tools_builtin_t *builtins = tools_builtins(&count);
    for (size_t i = 0; i < count; i++) {
        push_catalogue_row(out, builtins[i].name, builtins[i].description,
                           tools_offered(builtins[i].name, ctx));
    }
    const tools_declared_set_t *declared = ctx != NULL ? ctx->declared : NULL;
    if (declared != NULL) {
        for (size_t i = 0; i < declared->count; i++) {
            push_catalogue_row(out, declared->items[i].name,
                               declared->items[i].description,
                               tools_offered(declared->items[i].name, ctx));
        }
    }
    /* A remote tool's description is the server's own; `mcp` already holds it. It
     * goes through `tools_offered` like the rest, so a name in `tools.disabled` is
     * reported as not offered here too — the frame and `hello_ack` must agree. */
    if (ctx != NULL && ctx->mcp_tools != NULL) {
        for (size_t i = 0; i < ctx->mcp_count; i++) {
            push_catalogue_row(out, ctx->mcp_tools[i].name,
                               ctx->mcp_tools[i].description,
                               tools_offered(ctx->mcp_tools[i].name, ctx));
        }
    }
    return ESP_OK;
}

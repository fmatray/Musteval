/* mcp.c — the board half of `mcp`: config, the TLS session and the two calls
 * (`02-firmware.md` §6). Board-only, like `geo.c`: it opens sockets and reads NVS, so
 * its defects are found by reading, and every rule that can be pure lives in
 * `mcp_parse.c` where a host test can fail it.
 *
 * Streamable HTTP is request/response over `POST`: one JSON-RPC call per request, the
 * reply in the body. The `initialize` handshake is sent once per server before its
 * first `tools/list`, which is what the transport requires and what a server will
 * refuse a bare `tools/list` for.
 */

#include "mcp.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "net.h"
#include "store.h"

static const char *TAG = "mcp";

/* One request body and one reply body, `MCP_BODY_MAX` each. Taken from the heap per
 * call rather than held as statics: two 4 KiB statics per call path overflowed DIRAM
 * by 96 KiB when there were six of them. A call is serialized by the caller (`agent`
 * runs one tool at a time), so one allocation per call costs nothing it was not
 * already paying in blocking time. */
static char *scratch(void)
{
    return malloc(2 * MCP_BODY_MAX);
}

typedef struct {
    mcp_server_t server;
    mcp_state_t  state;
    bool         initialized; /* the handshake completed for this session */
    int32_t      next_id;
    mcp_tool_set_t tools;
} mcp_runtime_t;

/* The per-server state is `MCP_MAX_SERVERS` times `mcp_tool_set_t`, which is
 * 16 tools of 1.4 KiB each: 88 KiB as an array, and DIRAM has 25 KiB. So it is one
 * heap block, PSRAM first with internal as the fallback, the rule `sched` and `agent`
 * already follow for anything bigger than a few hundred bytes. */
static mcp_runtime_t *s_servers;
static size_t s_count;
static mcp_event_fn s_event;

void mcp_set_event(mcp_event_fn fn) { s_event = fn; }

static void raise_event(const char *event, const char *server_id, const char *message)
{
    if (s_event != NULL) s_event(event, server_id, message);
}

static mcp_runtime_t *find_server(const char *id)
{
    if (id == NULL) return NULL;
    for (size_t i = 0; i < s_count; i++) {
        if (strcmp(s_servers[i].server.id, id) == 0) return &s_servers[i];
    }
    return NULL;
}

size_t mcp_rows(mcp_row_t *out, size_t cap)
{
    size_t n = 0;
    for (size_t i = 0; i < s_count && n < cap; i++) {
        out[n].id = s_servers[i].server.id;
        out[n].enabled = s_servers[i].server.enabled;
        out[n].state = s_servers[i].state;
        n++;
    }
    return n;
}

/* The discovered set, flattened across servers. Held here rather than derived on every
 * ask: `tools_json` runs once per model round and a copy per round would be waste.
 * Heap, not static, for the same 88 KiB reason as `s_servers`. */
typedef struct {
    mcp_tool_t items[MCP_MAX_SERVERS * MCP_MAX_TOOLS];
    size_t     count;
} mcp_discovered_t;

static mcp_discovered_t *s_discovered;

const mcp_tool_t *mcp_discovered(size_t *count)
{
    if (s_discovered == NULL || s_discovered->count == 0) {
        if (count != NULL) *count = 0;
        return NULL;
    }
    if (count != NULL) *count = s_discovered->count;
    return s_discovered->items;
}

esp_err_t mcp_discover_all(void)
{
    if (s_discovered == NULL) {
        s_discovered = heap_caps_malloc(sizeof *s_discovered,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_discovered == NULL) s_discovered = heap_caps_malloc(sizeof *s_discovered,
                                                                 MALLOC_CAP_8BIT);
        if (s_discovered == NULL) return ESP_ERR_NO_MEM;
    }
    s_discovered->count = 0;
    size_t answered = 0;

    for (size_t i = 0; i < s_count; i++) {
        if (!s_servers[i].server.enabled) continue;

        /* Heap: a `mcp_tool_set_t` is 16 tools of fixed strings, about 22 KiB, and
         * this frame sits on the 3584-byte main task (`boot`, `start_services`). */
        mcp_tool_set_t *set = heap_caps_malloc(sizeof *set, MALLOC_CAP_SPIRAM |
                                                          MALLOC_CAP_8BIT);
        if (set == NULL) set = heap_caps_malloc(sizeof *set, MALLOC_CAP_8BIT);
        if (set == NULL) return ESP_ERR_NO_MEM;
        memset(set, 0, sizeof *set);

        char why[MCP_ERR_MAX] = "";
        if (mcp_discover(s_servers[i].server.id, set, why, sizeof why) != ESP_OK) {
            free(set);
            /* One server down is that server's tools absent, not a refused boot. */
            ESP_LOGW(TAG, "%s: %s", s_servers[i].server.id,
                     why[0] != '\0' ? why : "discovery failed");
            continue;
        }
        answered++;
        for (size_t t = 0; t < set->count; t++) {
            if (s_discovered->count >= MCP_MAX_SERVERS * MCP_MAX_TOOLS) break;
            mcp_tool_t *dst = &s_discovered->items[s_discovered->count];
            memset(dst, 0, sizeof *dst);
            /* Namespaced here, once: the model-facing name is what `tools_json`
             * offers and what `tools_call` splits, so it is fixed in one place. */
            if (!mcp_tool_name(s_servers[i].server.id, set->items[t].name, dst->name,
                               sizeof dst->name)) {
                continue;
            }
            snprintf(dst->description, sizeof dst->description, "%s",
                     set->items[t].description);
            snprintf(dst->schema, sizeof dst->schema, "%s", set->items[t].schema);
            s_discovered->count++;
        }
        free(set);
    }

    if (s_count == 0) return ESP_OK; /* nothing configured is not a failure */
    return answered > 0 ? ESP_OK : ESP_ERR_INVALID_STATE;
}

esp_err_t mcp_init(void)
{
    /* `reload` reaches this again (`main.c` `start_services`), so the block is reused
     * rather than reallocated and the previous run's tools are forgotten. */
    if (s_servers == NULL) {
        s_servers = heap_caps_malloc(sizeof(mcp_runtime_t) * MCP_MAX_SERVERS,
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_servers == NULL) {
            s_servers = heap_caps_malloc(sizeof(mcp_runtime_t) * MCP_MAX_SERVERS,
                                         MALLOC_CAP_8BIT);
        }
        if (s_servers == NULL) return ESP_ERR_NO_MEM;
    }
    s_count = 0;
    memset(s_servers, 0, sizeof(mcp_runtime_t) * MCP_MAX_SERVERS);

    json_node_t *mcp = cfg_get("mcp");
    if (mcp == NULL) return ESP_OK; /* no `mcp` section: no server, not a broken one */

    json_node_t *list = json_get(mcp, "servers");
    size_t n = list == NULL ? 0 : json_array_len(list);
    if (n > MCP_MAX_SERVERS) return ESP_ERR_INVALID_ARG;

    for (size_t i = 0; i < n; i++) {
        json_node_t *entry = json_array_at(list, i);
        const char *id = json_string(json_get(entry, "id"));
        const char *url = json_string(json_get(entry, "url"));
        if (!mcp_id_is_valid(id) || !mcp_url_is_valid(url)) {
            /* One bad entry is one absent server, not a refused boot: `06-security.md`
             * T9's mitigation is a server that fails alone. */
            ESP_LOGW(TAG, "server %u: id or url refused", (unsigned)i);
            continue;
        }

        mcp_server_t *srv = &s_servers[s_count].server;
        snprintf(srv->id, sizeof srv->id, "%s", id);
        snprintf(srv->url, sizeof srv->url, "%s", url);
        srv->enabled = true;
        json_bool(json_get(entry, "enabled"), &srv->enabled);
        srv->timeout_ms = MCP_TIMEOUT_MS_DEFAULT;
        int64_t v = 0;
        if (json_int(json_get(entry, "timeout_ms"), &v)) {
            /* The validator also bounds this; clamped here because a config can reach
             * this point without having passed the validator. */
            if (v < MCP_TIMEOUT_MS_MIN) v = MCP_TIMEOUT_MS_MIN;
            if (v > MCP_TIMEOUT_MS_MAX) v = MCP_TIMEOUT_MS_MAX;
            srv->timeout_ms = (int32_t)v;
        }
        const char *ref = json_string(json_get(entry, "auth_header_ref"));
        if (ref != NULL) snprintf(srv->auth_header_ref, sizeof srv->auth_header_ref, "%s", ref);

        s_servers[s_count].state = MCP_STATE_IDLE;
        s_servers[s_count].next_id = 1;
        s_count++;
    }
    return ESP_OK;
}

/* One JSON-RPC request to `rt`'s server. `auth` is the resolved header value, or NULL.
 * The body is the caller's and is bounded by `MCP_BODY_MAX`. */
static esp_err_t post(mcp_runtime_t *rt, const char *body, size_t len, char *resp,
                      size_t resp_cap, char *why, size_t why_cap)
{
    char auth[CFG_SECRET_MAX + 1] = "";
    size_t header_count = 1;
    const char *names[2] = { "Content-Type", NULL };
    const char *values[2] = { "application/json", NULL };
    if (rt->server.auth_header_ref[0] != '\0') {
        esp_err_t err = cfg_resolve_secret(rt->server.auth_header_ref, auth, sizeof auth, NULL);
        if (err != ESP_OK) {
            if (why != NULL) snprintf(why, why_cap, "%s not resolved", rt->server.auth_header_ref);
            return err;
        }
        names[1] = "Authorization";
        values[1] = auth;
        header_count = 2;
    }

    net_response_t res = {0};
    esp_err_t err = net_request("POST", rt->server.url, names, values, header_count, body,
                                len, rt->server.timeout_ms, &res);
    /* Out of scope after the request: a stack dump of a failed call is not a
     * credential dump (`06-security.md` §11). */
    memset(auth, 0, sizeof auth);

    if (err != ESP_OK) {
        free(res.body);
        if (why != NULL) snprintf(why, why_cap, "%s: %s", rt->server.id, esp_err_to_name(err));
        return err;
    }
    if (res.body == NULL) {
        if (why != NULL) snprintf(why, why_cap, "%s: empty reply", rt->server.id);
        return ESP_ERR_INVALID_RESPONSE;
    }
    if (res.status >= 400) {
        /* The message is the server's, so it is worth surfacing: it names the reason a
         * config the operator wrote did not work. */
        char message[MCP_ERR_MAX];
        message[0] = '\0';
        mcp_parse_error(res.body, res.body_len, message, sizeof message);
        if (why != NULL) {
            snprintf(why, why_cap, "%s: http %d%.*s%s", rt->server.id, res.status,
                     message[0] != '\0' ? 2 : 0, ": ", message);
        }
        free(res.body);
        return ESP_ERR_INVALID_RESPONSE;
    }

    size_t copy = res.body_len < resp_cap - 1 ? res.body_len : resp_cap - 1;
    memcpy(resp, res.body, copy);
    resp[copy] = '\0';
    free(res.body);
    return ESP_OK;
}

/* Send `initialize`, then the `notifications/initialized` the spec requires. A server
 * that refuses the handshake has its tools absent for the run. */
static esp_err_t handshake(mcp_runtime_t *rt, char *why, size_t why_cap)
{
    if (rt->initialized) return ESP_OK;

    char *body = scratch();
    char *resp = body != NULL ? body + MCP_BODY_MAX : NULL;
    if (body == NULL) return ESP_ERR_NO_MEM;

    size_t len = mcp_initialize_body(rt->next_id++, body, MCP_BODY_MAX);
    if (len == 0) {
        free(body);
        return ESP_ERR_INVALID_SIZE;
    }

    esp_err_t err = post(rt, body, len, resp, MCP_BODY_MAX, why, why_cap);
    if (err != ESP_OK) {
        rt->state = MCP_STATE_ERROR;
        raise_event("mcp.error", rt->server.id, why != NULL ? why : "handshake failed");
        return err;
    }

    /* The notification carries no id and expects no reply, so its answer is ignored. */
    len = (size_t)snprintf(body, MCP_BODY_MAX,
                           "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}");
    if (len > 0 && len < MCP_BODY_MAX) {
        post(rt, body, len, resp, MCP_BODY_MAX, NULL, 0);
    }

    rt->initialized = true;
    rt->state = MCP_STATE_CONNECTED;
    raise_event("mcp.connected", rt->server.id, NULL);
    return ESP_OK;
}

esp_err_t mcp_discover(const char *server_id, mcp_tool_set_t *out, char *why,
                       size_t why_cap)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    out->count = 0;
    if (why != NULL && why_cap > 0) why[0] = '\0';

    mcp_runtime_t *rt = find_server(server_id);
    if (rt == NULL) {
        if (why != NULL) snprintf(why, why_cap, "mcp server %s not configured", server_id);
        return ESP_ERR_NOT_FOUND;
    }
    if (!rt->server.enabled) {
        if (why != NULL) snprintf(why, why_cap, "mcp server %s is disabled", server_id);
        return ESP_ERR_NOT_FOUND;
    }
    /* Already discovered: the second ask is free. */
    if (rt->tools.count > 0) {
        *out = rt->tools;
        return ESP_OK;
    }

    esp_err_t err = handshake(rt, why, why_cap);
    if (err != ESP_OK) return err;

    char *body = scratch();
    char *resp = body != NULL ? body + MCP_BODY_MAX : NULL;
    if (body == NULL) return ESP_ERR_NO_MEM;

    size_t len = mcp_tools_list_body(rt->next_id++, body, MCP_BODY_MAX);
    if (len == 0) {
        free(body);
        return ESP_ERR_INVALID_SIZE;
    }

    err = post(rt, body, len, resp, MCP_BODY_MAX, why, why_cap);
    if (err != ESP_OK) {
        free(body);
        rt->state = MCP_STATE_ERROR;
        raise_event("mcp.error", rt->server.id, why);
        return err;
    }

    bool truncated = false;
    bool parsed = mcp_parse_tools(resp, strlen(resp), &rt->tools, &truncated);
    if (!parsed) {
        char message[MCP_ERR_MAX];
        message[0] = '\0';
        mcp_parse_error(resp, strlen(resp), message, sizeof message);
        if (why != NULL) {
            snprintf(why, why_cap, "%s: %s", rt->server.id,
                     message[0] != '\0' ? message : "tools/list unreadable");
        }
        free(body);
        rt->state = MCP_STATE_ERROR;
        raise_event("mcp.error", rt->server.id, why);
        return ESP_ERR_INVALID_RESPONSE;
    }
    free(body);
    if (truncated) {
        ESP_LOGW(TAG, "%s: more than %d tools; the rest are absent", rt->server.id,
                 MCP_MAX_TOOLS);
    }
    *out = rt->tools;
    return ESP_OK;
}

esp_err_t mcp_call(const char *server_id, const char *tool, const json_node_t *args,
                   char *out, size_t cap, char *why, size_t why_cap)
{
    if (out == NULL || cap == 0 || tool == NULL) return ESP_ERR_INVALID_ARG;
    out[0] = '\0';
    if (why != NULL && why_cap > 0) why[0] = '\0';

    mcp_runtime_t *rt = find_server(server_id);
    if (rt == NULL) {
        if (why != NULL) snprintf(why, why_cap, "mcp server %s not configured", server_id);
        return ESP_ERR_NOT_FOUND;
    }
    if (!rt->server.enabled) {
        if (why != NULL) snprintf(why, why_cap, "mcp server %s is disabled", server_id);
        return ESP_ERR_NOT_FOUND;
    }

    esp_err_t err = handshake(rt, why, why_cap);
    if (err != ESP_OK) return err;

    /* The remote tool's schema is the server's; the call is forwarded as the model
     * produced it, which is what keeps `mcp` free of a second validator that could
     * disagree with the schema the model was shown. */
    char *body = scratch();
    char *resp = body != NULL ? body + MCP_BODY_MAX : NULL;
    if (body == NULL) return ESP_ERR_NO_MEM;

    size_t len = mcp_call_body(rt->next_id++, tool, args, body, MCP_BODY_MAX);
    if (len == 0) {
        free(body);
        if (why != NULL) snprintf(why, why_cap, "arguments too large for one request");
        return ESP_ERR_INVALID_SIZE;
    }

    err = post(rt, body, len, resp, MCP_BODY_MAX, why, why_cap);
    if (err != ESP_OK) {
        free(body);
        raise_event("mcp.error", rt->server.id, why);
        return err;
    }

    bool is_error = false;
    bool parsed_call = mcp_parse_call(resp, strlen(resp), out, cap, &is_error);
    free(body);
    if (!parsed_call) {
        char message[MCP_ERR_MAX];
        message[0] = '\0';
        mcp_parse_error(resp, strlen(resp), message, sizeof message);
        if (why != NULL) {
            snprintf(why, why_cap, "%s: %s", rt->server.id,
                     message[0] != '\0' ? message : "reply unreadable");
        }
        raise_event("mcp.error", rt->server.id, why);
        return ESP_ERR_INVALID_RESPONSE;
    }
    if (is_error) {
        /* A tool-level failure the transport reported as success: `E_MCP`, per
         * `04` §5. */
        if (why != NULL) snprintf(why, why_cap, "%s: %s", rt->server.id, out);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

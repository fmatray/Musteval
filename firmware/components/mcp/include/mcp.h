/* mcp — remote Model Context Protocol servers over Streamable HTTP.
 * Layering: L1 (`08-code-architecture.md` §1), above `net` and `cfg` and below
 * `tools` and `agent`. A server's tools reach the model as
 * `mcp.<server_id>.<tool_name>`, which is the namespace `00-overview.md` §7 fixes.
 *
 * The split is the testability line, as everywhere else: `mcp_parse.c` holds the
 * JSON-RPC envelopes, the tool-list projection and the namespace rules, all pure and
 * host-tested; `mcp.c` opens the TLS session, which a stub would only pretend to test.
 *
 * Contracts: `02-firmware.md` §6, `03-config.md` §mcp, `06-security.md` §6 (T9). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "store.h"

/* `03-config.md` §mcp: 0..4 servers. */
#define MCP_MAX_SERVERS 4
#define MCP_ID_MAX 16
#define MCP_URL_MAX 192
/* `03-config.md` §mcp: `timeout_ms` bounds and its default. */
#define MCP_TIMEOUT_MS_DEFAULT 15000
#define MCP_TIMEOUT_MS_MIN 1000
#define MCP_TIMEOUT_MS_MAX 60000
/* `02-firmware.md` §7.1: an MCP server contributes at most this many tools. */
#define MCP_MAX_TOOLS 16
/* A remote tool name is bounded by what a schema can carry; the namespace prefix is
 * `mcp.` + server id + `.`, which `MCP_NAME_MAX` already includes. */
#define MCP_TOOL_NAME_MAX 48
#define MCP_NAME_MAX (4 + MCP_ID_MAX + 1 + MCP_TOOL_NAME_MAX)
/* The description a remote server offers is untrusted text. It reaches the model, so
 * it is bounded rather than copied whole. */
#define MCP_DESC_MAX 300
/* A tool's `inputSchema` is forwarded verbatim; this bounds what one may be. */
#define MCP_SCHEMA_MAX 1024
/* The refusal text one call can carry, matching the `tools` cap so a message reaches
 * the model whole. `mcp` cannot import `tools`: that is L2 and this is L1. */
#define MCP_ERR_MAX 160
/* One JSON-RPC request or reply body. `04` §5 caps a tool result at
 * `TOOLS_RESULT_MAX`; a request cannot be larger than the reply it provokes, so the
 * same number bounds both. */
#define MCP_BODY_MAX 4096

/* `04` §5: the error class an MCP failure is reported under. */
#define MCP_WIRE_CODE "E_MCP"

typedef enum {
    MCP_STATE_IDLE = 0, /* configured, no session opened yet */
    MCP_STATE_CONNECTED,
    MCP_STATE_ERROR,
} mcp_state_t;

typedef struct {
    char     id[MCP_ID_MAX + 1];
    char     url[MCP_URL_MAX + 1];
    bool     enabled;
    char     auth_header_ref[16]; /* empty when unset */
    int32_t  timeout_ms;
} mcp_server_t;

/* One tool as a server advertised it, before namespacing. */
typedef struct {
    char name[MCP_TOOL_NAME_MAX + 1];
    char description[MCP_DESC_MAX + 1];
    char schema[MCP_SCHEMA_MAX + 1]; /* the `inputSchema` object, verbatim JSON */
} mcp_tool_t;

typedef struct {
    mcp_tool_t items[MCP_MAX_TOOLS];
    size_t     count;
} mcp_tool_set_t;

/* A row for `status.mcp[]` (`04` §3.9). */
typedef struct {
    const char *id;
    bool        enabled;
    mcp_state_t state;
} mcp_row_t;

/* --- Pure rules (`mcp_parse.c`) ------------------------------------------- */

/* Validate a server id against `03-config.md` §mcp: `[a-z0-9_-]{1,16}`, and it must
 * not contain `.` because that is the namespace separator (`00-overview.md` §7).
 * The charset already excludes it; the check is stated so the reason survives. */
bool mcp_id_is_valid(const char *id);

/* Build the namespaced tool name `mcp.<server_id>.<tool_name>` into `out`. Returns
 * false when it does not fit, so a caller never offers a truncated name that would
 * dispatch to the wrong tool. Pure. */
bool mcp_tool_name(const char *server_id, const char *tool, char *out, size_t cap);

/* Split `name` back into its server id and tool name. Returns false when `name` is not
 * in the namespace, which is how `tools` decides the call belongs to MCP at all.
 * Either output may be NULL. Pure. */
bool mcp_tool_split(const char *name, char *server_id, size_t id_cap, char *tool,
                    size_t tool_cap);

/* Build the `initialize` request body: the JSON-RPC handshake every Streamable HTTP
 * server answers before it will list tools. `id` is the JSON-RPC correlation member,
 * separate from the frame id. Returns the byte count, or 0 when it does not fit.
 * Pure, so the handshake the device sends is a host-test fact rather than a capture. */
size_t mcp_initialize_body(int32_t id, char *buf, size_t cap);

/* Build a `tools/list` request body. Pure. */
size_t mcp_tools_list_body(int32_t id, char *buf, size_t cap);

/* Build a `tools/call` request body for `tool` with `args` — a JSON object the model
 * produced. `args` is spliced as the `arguments` member; NULL becomes `{}`. Returns 0
 * when the body does not fit, so an oversized call is refused rather than truncated
 * into invalid JSON. `args` must already have passed `tools_validate`. Pure. */
size_t mcp_call_body(int32_t id, const char *tool, const json_node_t *args, char *buf,
                     size_t cap);

/* Read the `tools/list` result into `out`. A server that answers with an error
 * object, a result that is not an array, or one with no `name` is refused whole
 * rather than half-loaded. Tools past `MCP_MAX_TOOLS` are dropped and `*truncated`
 * says so; a tool whose `inputSchema` is missing gets `{"type":"object"}`, which is
 * the only schema that accepts every argument. Untrusted input (`06-security.md`
 * T9): a name that is not `[A-Za-z0-9_-]{1,48}` is dropped, because it becomes the
 * tool name the model sees. Pure. */
bool mcp_parse_tools(const char *body, size_t len, mcp_tool_set_t *out, bool *truncated);

/* Read a `tools/call` reply: `result.content[]` joined into `out` as text, where a
 * `text` item contributes its `text` and a non-text item its `type` in brackets.
 * `*is_error` mirrors the JSON-RPC result's own `isError`, which a server uses for a
 * tool-level failure the transport reported as success. Returns false when the body
 * is not a JSON-RPC result at all. Pure. */
bool mcp_parse_call(const char *body, size_t len, char *out, size_t cap, bool *is_error);

/* The human-readable `error.message` of a JSON-RPC error reply, or false when the
 * body carries no error. Used for the `mcp.error` frame and the log line. Pure. */
bool mcp_parse_error(const char *body, size_t len, char *out, size_t cap);

/* True when `name` is a tool name a remote server may advertise: a remote name
 * becomes part of the model-facing tool name, so it is filtered rather than escaped.
 * Pure. */
bool mcp_remote_tool_is_valid(const char *name);

/* The URL a server's requests go to, unchanged. Exists so `mcp.c` does not have to
 * decide anything about the value, and so `https`-only is a host-test fact. Returns
 * false for a URL that is not `https://`. Pure. */
bool mcp_url_is_valid(const char *url);

/* --- Board half (`mcp.c`) ------------------------------------------------- */

/* Read `mcp.servers[]` and validate it. A malformed entry is dropped with a log line
 * rather than refusing boot: an unreachable server isolates its own tools
 * (`02-firmware.md` §6 step 5). `enabled: false` is kept as a row so `status` can
 * report it, but contributes no tool. Resolves every `auth_header_ref`, because a ref
 * that does not resolve would only fail at the first call. */
esp_err_t mcp_init(void);

/* The rows for `status.mcp[]`, in config order. Returns the count, and 0 when no
 * server is configured — which is not an error. */
size_t mcp_rows(mcp_row_t *out, size_t cap);

/* Discover a server's tools. Opens the session if needed. Returns ESP_OK with `out`
 * filled, or the failure whose wire code is `E_MCP`. A server that is disabled,
 * missing or already known-failed returns that failure without reaching the wire. */
esp_err_t mcp_discover(const char *server_id, mcp_tool_set_t *out, char *why,
                       size_t why_cap);

/* Call one remote tool. `server_id` and `tool` are already split from the namespaced
 * name. Returns ESP_OK with the joined text in `out`, or a failure whose wire code is
 * `E_MCP`. Blocking: one TLS round trip, bounded by that server's `timeout_ms`. */
esp_err_t mcp_call(const char *server_id, const char *tool, const json_node_t *args,
                   char *out, size_t cap, char *why, size_t why_cap);

/* Discover every enabled server's tools into the set the model is offered. Called at
 * start, after the network is up (`02-firmware.md` §6 step 1). A server that fails
 * contributes nothing and its failure is logged; the call returns ESP_OK when at least
 * one server answered, so a board with every server down still boots with its
 * built-ins. */
esp_err_t mcp_discover_all(void);

/* The discovered set, in config order, for `tools_json` and `tools_offered`. Valid
 * until the next `mcp_init`. Returns NULL with `*count` 0 when nothing was discovered,
 * which is a board with no server and stays the M4 arrangement. */
const mcp_tool_t *mcp_discovered(size_t *count);

/* Install the port that raises `mcp.connected` / `mcp.error` (`04` §3.10). `mcp` is
 * L1 and `link` is L2, so the frame goes through the caller, the same shape as
 * `mqtt_set_sink()`. */
typedef void (*mcp_event_fn)(const char *event, const char *server_id, const char *message);
void mcp_set_event(mcp_event_fn fn);

/* tools — the execution engine behind the frozen tool surface
 * (`00-overview.md` §7). Layering: L2 (`08-code-architecture.md` §1).
 *
 * Three halves:
 *   `tools_spec.c`  the built-in registry: names, descriptions, schemas, and the
 *                   argument validation every call passes before it executes.
 *   `tools_gate.c`  the authorization decision per tool (`06-security.md` §6).
 *   `tools.c`       dispatch: run one validated call and hand back a result.
 *
 * The split is the testability line. Names, schemas, validation and gating are pure
 * and host-tested; dispatch touches hardware, the filesystem and the network and is
 * proven on target. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "mcp.h"
#include "net.h"
#include "store.h"

/* `03-config.md` §tools: the built-in names are frozen (`00-overview.md` §7) and a
 * declarative file cannot redefine one. */
#define TOOLS_MAX_BUILTINS 26
/* `03-config.md` §tools: `/tools/` `.json` is capped at 16 entries. */
#define TOOLS_MAX_DECLARED 16
/* `02-firmware.md` §7.1: `tools_json` carries built-ins, declarative and MCP
 * entries. An MCP server contributes at most this many, and M5 fills them. */
#define TOOLS_MAX_TOTAL 64

/* `03-config.md` §tools binding step 4 / `tools/web.md`: a result reaching the
 * model is capped here and the excess is reported, never silently dropped. */
#define TOOLS_RESULT_MAX 4096

/* What `http.get` returns before cutting a response. The same number as the file
 * reader's cap (`TOOLS_TEXT_MAX`), which leaves over half of `TOOLS_RESULT_MAX` for
 * JSON escaping and the rest of the result object; a body reaching this is cut at a
 * character boundary and reported with `truncated: true` and its real `bytes`
 * (`tools/web.md`). A response too large for `net_get` is `E_TOOL`. */
#define TOOLS_HTTP_BODY_MAX 2048

/* `fs.list`'s entry cap: the same workspace file count `03-config.md` §Storage limits
 * bounds, so a listing can never be refused for a directory the quota allows. */
#define TOOLS_LIST_MAX 64
#define TOOLS_ERR_MAX 160
/* `03-config.md` §tools: `http.timeout_ms` defaults to this and is bounded to
 * 1..60000. `tools/web.md` states the same default. */
#define TOOLS_HTTP_TIMEOUT_MS 10000

typedef enum {
    TOOLS_GATE_NONE = 0,      /* always available */
    TOOLS_GATE_DANGEROUS,     /* `tools.allow_dangerous: true` */
    TOOLS_GATE_ALLOWLIST,     /* host must be in `tools.http_allow_hosts` */
} tools_gate_t;

/* One tool the model may call. `schema` is the canonical JSON text of the
 * `parameters` object, already in the frozen subset (`02-firmware.md` §7.1). */
typedef struct {
    const char *name;
    const char *description;
    tools_gate_t gate;
    const char *schema;
} tools_builtin_t;

/* The registry. Always `TOOLS_MAX_BUILTINS` entries, in `00-overview.md` §7 order. */
const tools_builtin_t *tools_builtins(size_t *count);

/* Resolve a name to its built-in, or NULL. */
const tools_builtin_t *tools_find(const char *name);

/* True when `name` is a built-in. A declarative file with this stem is dropped
 * (`tools/web.md` §Declarative tools). */
bool tools_is_builtin(const char *name);

/* Validate `args` (a parsed object) against the built-in's schema.
 *
 * Returns ESP_OK, or ESP_ERR_INVALID_ARG with a reason in `why` — the single
 * refusal path `02-firmware.md` §7.1 states: "rejected as `E_TOOL` without
 * executing, and no partial effect is applied". A missing required argument, a
 * wrong type, an out-of-range integer and a value outside an `enum` all land here.
 *
 * The check is driven by the schema text rather than a hand-written branch per
 * tool, so a schema edit and the validator cannot disagree. Pure. */
esp_err_t tools_validate(const char *tool, const json_node_t *args, char *why,
                         size_t why_cap);

/* --- Gating (`06-security.md` §6) ----------------------------------------- */

/* What a call needs, and whether the current config grants it. `allow_dangerous`
 * and the allowlist come from `tools.*` in `config.json`.
 *
 * `host` is the request host for TOOLS_GATE_ALLOWLIST and NULL otherwise. The match
 * is exact: no wildcard, no subdomain, no port (`tools/web.md` §`http.get`). */
typedef struct {
    bool allow_dangerous;
    const json_node_t *http_allow_hosts; /* the config array, or NULL */
} tools_policy_t;

bool tools_host_allowed(const tools_policy_t *policy, const char *host);

/* The host of an absolute URL, without scheme, port or path, written into `buf`.
 * NULL when there is no host. One definition, because the built-in gate and a
 * declarative tool's compiled URL must agree about what "host" means. Pure. */
const char *tools_url_host(const char *url, char *buf, size_t cap);

/* The verdict for one call. ESP_OK, or ESP_ERR_NOT_ALLOWED with the refusal text —
 * the exact wording `tools/gpio.md` and `tools/web.md` quote. `host` may be NULL;
 * a TOOLS_GATE_ALLOWLIST tool with no host is refused, not assumed allowed. */
esp_err_t tools_gate_check(const tools_builtin_t *tool, const tools_policy_t *policy,
                           const char *host, char *why, size_t why_cap);

/* The host `http.get` would reach, into `buf`, or NULL when the tool has no URL
 * argument or the URL is not absolute `https`. A plain-`http://` target returns NULL,
 * which the gate then refuses — that is how `tools/web.md`'s scheme rule is
 * enforced at the same point as the allowlist. */
const char *tools_gate_host_for(const tools_builtin_t *tool, const json_node_t *args,
                                char *buf, size_t cap);

/* --- Dispatch (board) ----------------------------------------------------- */

/* The `todo` tree lives in `agent` (L3) and `tools` is L2, so the dispatch reaches
 * it through a port the caller installs (`08-code-architecture.md` §1.3: the
 * `tool_provider_t` shape, narrowed to the one tool whose state is `agent`'s).
 * Implemented by `agent`, wired in `agent.c`, never a direct upward call. */
typedef struct {
    esp_err_t (*apply)(const json_node_t *args, json_doc_t **out, char *why,
                       size_t why_cap, void *ctx);
    void *ctx;
} tools_todo_port_t;

/* `MCP_NAME_MAX`, `MCP_DESC_MAX` and `MCP_SCHEMA_MAX` bound one discovered tool;
 * `tools_context_t` carries `mcp_tool_t` arrays from `mcp.h` rather than a second
 * copy of the same three fields. */

/* Everything one call needs beyond its arguments. */
typedef struct {
    tools_policy_t policy;
    /* Installed by `agent`; NULL means `todo` is refused as unavailable. */
    tools_todo_port_t todo;
    /* `tools.memory_max_bytes`; the memory tools enforce it. */
    size_t memory_max_bytes;
    /* `tools.i2c_sda` / `tools.i2c_scl`, or < 0 when the bus is not configured:
     * the three i2c tools are then absent from `tools_json` (`tools/i2c.md`). */
    int i2c_sda;
    int i2c_scl;
    /* The MCP servers' discovered tools, already namespaced, in config order.
     * `mcp_count` 0 is a board with none, which is the shape M4 shipped and stays
     * valid. `tools` cannot derive the set: it arrives over the network at run start
     * (`02-firmware.md` §6 step 1), so it is data on the context. */
    const mcp_tool_t *mcp_tools;
    size_t mcp_count;
    /* The compiled set of files under `/tools/`, or NULL for a board with none. One
     * owner: `tools_json_build`, `tools_capability_names`, `tools_offered` and a
     * call's dispatch all read it here, so what is offered and what is callable
     * cannot drift apart. */
    const struct tools_declared_set *declared;
    /* `tools.task_max`: the cap `task.set` enforces, tighter than the schema's own
     * `SCHED_MAX_TASKS` (`tools/tasks.md` §Authority split: the tool is bounded, the
     * bridge commands are not). */
    size_t task_max;
} tools_context_t;

/* Fill `ctx` from the loaded `config.json`: `tools.allow_dangerous`,
 * `tools.http_allow_hosts`, `tools.memory_max_bytes`, `tools.i2c_sda`/`i2c_scl`.
 * One owner for the key names, so `agent`, `status` and `hello_ack` cannot disagree
 * about what the config says. `todo` is left uninstalled; the caller supplies it.
 * Board-only only because `cfg_get` reads the mounted file. */
void tools_context_from_config(tools_context_t *ctx);

/* Calls and failures since boot, for `status` (`04` §3.9 `sys.tool_calls`) and
 * `sys.diagnostics`. */
typedef struct {
    int32_t calls;
    int32_t failures;
} tool_counters_t;

void tools_counters(tool_counters_t *out);

/* True when the tool should be offered at all. The only absent-when-unconfigured
 * family is i2c (`tools/i2c.md`): all three appear together or not at all. */
bool tools_offered(const char *name, const tools_context_t *ctx);

/* `tools/memory.md` §Limits: a key is 1-15 bytes of `[A-Za-z0-9_]` and must not
 * start with a reserved prefix (`sys_`, `cfg_`, `secret_`, `rt_`). Pure, so the
 * bound is host-tested rather than only provable on a board. */
bool tools_memory_key_ok(const char *key);

/* The reserved prefix `key` starts with, or NULL. */
const char *tools_memory_reserved_prefix(const char *key);

/* The `04` §5 wire code for a tool failure, or NULL for ESP_OK. Pure, so the
 * mapping is host-tested rather than only exercised on a board. `tools.c`'s
 * `tools_call` is the only caller. */
const char *tools_wire_code(const char *tool, esp_err_t err);

/* The code a failed call finally reports: `chosen` when the handler classified its
 * own failure, else `tools_wire_code`. `geo.locate`, `mqtt.*`, `mcp.*` and `task.*`
 * pass a code in — `E_CONFIG`, `E_GEO`, `E_MQTT`, `E_MCP` — and the generic table
 * only answers for a handler that left the default `E_TOOL` standing. Pure. */
const char *tools_wire_code_refine(const char *tool, esp_err_t err, const char *chosen);

/* True when `task.set` may write: an update never consumes a slot, so only an id the
 * schedule does not already hold is counted against `tools.task_max`. Split out of
 * `tools.c` because that file is board-only, and this is the one rule the model's cap
 * adds over the schema's own (`tools/tasks.md` §Authority split). Pure. */
bool tools_task_set_fits(size_t scheduled, bool known_id, size_t task_max);

/* `tools/events.md` §Arguments: `type` is 1..32 bytes of `[a-z0-9._-]`, `data` at
 * most 512. Pure, because neither bound is expressible in the model-facing schema
 * subset, so the tool is the only place the rule can be enforced and a host test is
 * the only way to prove it. */
bool tools_event_args_ok(const char *type, const char *data, char *why, size_t why_cap);

/* Execute one call whose arguments already passed `tools_validate`. `out` receives
 * the result document, owned by the caller.
 *
 * Returns ESP_OK and a populated `out`, or an `esp_err_t` whose `*wire_code` is the
 * `04` §5 code for the failure and whose `why` text is human-readable and carries no
 * secret. Refusals are `E_TOOL`; a storage failure is `E_STORAGE`; a refused network
 * call is `E_WIFI`/`E_TIMEOUT` (`tools/web.md` §Errors). Board-only. */
esp_err_t tools_call(const tools_context_t *ctx, const char *name,
                     const json_node_t *args, json_doc_t **out,
                     const char **wire_code, char *why, size_t why_cap);

/* --- `tools_json` (`02-firmware.md` §7.1) --------------------------------- */

/* Declarative tools compiled from `/tools/` `.json`. Loading is board-side (it reads
 * `userfs`), but the compiled shape and its validation are pure so a bad file is a
 * test case rather than a flashing session. */
typedef struct {
    char name[33];
    char description[301];
    char url[256];
    char method[8];          /* GET or POST */
    char response_path[128];
    char headers[TOOLS_RESULT_MAX];  /* raw JSON object text, verbatim */
    char query[1024];
    char body[2048];
    char schema[2048];
    int32_t timeout_ms;
} tools_declared_t;

typedef struct tools_declared_set {
    tools_declared_t items[TOOLS_MAX_DECLARED];
    size_t count;
} tools_declared_set_t;

/* Compile one `/tools/<stem>.json` body. Returns ESP_OK, or ESP_ERR_INVALID_ARG for
 * a file that fails the `03-config.md` §tools validation rules — which is a drop,
 * not a fatal error: the run continues with one fewer tool (`tools/web.md`).
 *
 * `secret_resolves` is called for every `nvs:` reference found in `http.headers`.
 * A ref that does not resolve is the `E_CONFIG` drop `03-config.md` check 10 names,
 * answered through ESP_ERR_NOT_FOUND so the caller can log it as such. Pure. */
typedef bool (*tools_secret_probe_t)(const char *key, void *ctx);

esp_err_t tools_declared_compile(const char *stem, const char *body, size_t len,
                                 tools_secret_probe_t secret_resolves, void *probe_ctx,
                                 tools_declared_t *out, char *why, size_t why_cap);

/* Load every `/tools/<stem>.json`, compiling each into `set`. A file that fails the
 * `03-config.md` §tools rules is dropped with a log line and does not stop the others
 * (`tools/web.md`), so a bad file costs one tool, not boot. Board-only: it reads
 * `userfs`. `set` is the caller's, and `TOOLS_MAX_DECLARED` past it is truncation. */
esp_err_t tools_declared_load(tools_declared_set_t *set);

/* The set loaded once at first use and shared by every context, or NULL. Board-only;
 * `tools_context_from_config` points `declared` at it so a call can find its row. */
const tools_declared_set_t *tools_declared_loaded(void);

/* Expand `{{name}}` in `tpl` from `args`, into `out`. A placeholder whose name the
 * compiled schema does not declare, or a declared argument that is absent, is
 * ESP_ERR_INVALID_ARG with `why` naming it, so binding fails before a request.
 * Pure: a file's typo is a test case rather than a boot. */
esp_err_t tools_bind_text(const tools_declared_t *tool, const char *tpl,
                          const json_node_t *args, char *out, size_t cap,
                          char *why, size_t why_cap);

/* Bind a call's arguments into `tool` and perform its request, writing the result to
 * `out` as a `{"content": ...}` object. The host is the compiled URL's and must be in
 * `tools.http_allow_hosts`; the model cannot point a declarative tool elsewhere.
 * Board-only: it performs the HTTP exchange. */
esp_err_t tools_declared_call(const tools_declared_t *tool, const json_node_t *args,
                              const tools_policy_t *policy, json_doc_t **out,
                              char *why, size_t why_cap);

/* The row named `name` in `set`, or NULL. Pure. */
const tools_declared_t *tools_declared_find(const tools_declared_set_t *set,
                                            const char *name);

/* Add a result's `content` to `root`, cut at `TOOLS_HTTP_BODY_MAX` on a UTF-8
 * boundary with `truncated: true` when the cut happened. One rule for a built-in
 * `http.get` reply and a declarative tool's reply, because a limit with two
 * behaviours is two limits. Pure. */
void tools_result_add_content(json_node_t *root, const char *text);

/* The host a declarative tool would reach, for the allowlist gate. Pure. */
const char *tools_declared_host(const tools_declared_t *tool, char *buf, size_t cap);

/* Append every offered tool to `out` as one `{"type":"function","function":{…}}`.
 * `out` must be an array node. Built-ins first, then declarative, then MCP — the
 * order `02-firmware.md` §7.1 fixes. A NULL context offers built-ins only. Pure. */
esp_err_t tools_json_build(const tools_context_t *ctx, json_node_t *out);

/* The `hello_ack.capabilities.tools` array: every tool the model may call, in the
 * same order as `tools_json`. Pure. */
esp_err_t tools_capability_names(const tools_context_t *ctx, json_node_t *out);

/* Resolve a §4.25 target, probe it and summarise. One implementation behind both
 * the host command (`04-protocol.md` §4.25) and the model's `net.ping` tool
 * (`tools/net.md`): resolving needs `net`, `clock` and `llm` at once, none of
 * which may read the others (`08-code-architecture.md` §1.1), so the resolver
 * lives here at L2 where all three are reachable. Board-only.
 *
 * Returns ESP_OK on success (stats filled, incl. 100 % loss), ESP_ERR_INVALID_ARG
 * (target not one of the three), ESP_ERR_INVALID_STATE (link down),
 * ESP_ERR_NOT_FOUND (no gateway/provider/NTP server), ESP_ERR_INVALID_SIZE
 * (host_cap too small), or ESP_ERR_NO_MEM/ESP_FAIL (probe failed). On failure,
 * `why` carries one line of model-facing prose. */
esp_err_t tools_ping(const char *target, char *host, size_t host_cap,
                     net_ping_stats_t *stats, char *why, size_t why_cap);

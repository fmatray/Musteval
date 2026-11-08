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

#include "store.h"

/* `03-config.md` §tools: the built-in names are frozen (`00-overview.md` §7) and a
 * declarative file cannot redefine one. */
#define TOOLS_MAX_BUILTINS 25
/* `03-config.md` §tools: `/tools/` `.json` is capped at 16 entries. */
#define TOOLS_MAX_DECLARED 16
/* `02-firmware.md` §7.1: `tools_json` carries built-ins, declarative and MCP
 * entries. An MCP server contributes at most this many, and M5 fills them. */
#define TOOLS_MAX_TOTAL 64

/* `03-config.md` §tools binding step 4 / `tools/web.md`: a result reaching the
 * model is capped here and the excess is reported, never silently dropped. */
#define TOOLS_RESULT_MAX 4096

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

/* The refusal for a tool whose milestone has not shipped — `geo.locate` and
 * `mqtt.*` are M6, `task.*` and `event.emit` are M5 — or NULL for a tool that is
 * built. Pure, so each family's message is host-tested. `tools.c` is the only
 * caller. */
const char *tools_unbuilt_reason(const char *name);

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

typedef struct {
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

/* Append every offered tool to `out` as one `{"type":"function","function":{…}}`.
 * `out` must be an array node. Built-ins first, then declarative, then MCP — the
 * order `02-firmware.md` §7.1 fixes. `mcp_json` is the MCP block (M5); passing NULL
 * adds none. Pure apart from the read of the caller's set. */
esp_err_t tools_json_build(const tools_context_t *ctx,
                           const tools_declared_set_t *declared,
                           json_node_t *out);

/* The `hello_ack.capabilities.tools` array: every tool the model may call, in the
 * same order as `tools_json`. Pure. */
esp_err_t tools_capability_names(const tools_context_t *ctx,
                                 const tools_declared_set_t *declared,
                                 json_node_t *out);

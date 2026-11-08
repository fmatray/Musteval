/* agent.c — the serialized run loop.
 *
 * Board-only, and deliberately thin. Everything that decides *whether* a run may
 * start, *what* its frames say and *when* it may finalize is pure and lives in
 * `agent_todo.c`, `agent_closeout.c`, `agent_history.c` and `agent_reply.c`. What
 * is left here is a queue, a task, and the calls to `llm`, `tools` and `link`
 * (`02-firmware.md` §4).
 *
 * Stack: `link_task` and the agent task are 8192 B, so nothing large is a local.
 * Every buffer below is a static, and dispatch is single-threaded by construction —
 * one run at a time (`02-firmware.md` §4, "serialized"). */

#include "agent.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "cfg.h"
#include "clock.h"
#include "link.h"
#include "llm.h"
#include "skills.h"
#include "store.h"
#include "tools.h"

static const char *TAG = "agent";

/* `02-firmware.md` §3.3: the agent task is not the link task, because a run blocks
 * on the network for seconds and the reader must keep answering `ping`. */
#define AGENT_TASK_STACK 8192
#define AGENT_TASK_PRIORITY 5

/* `03-config.md` §agent defaults, used when a key is absent. */
#define AGENT_DEFAULT_MAX_INPUT_BYTES 8192
#define AGENT_DEFAULT_MAX_TOOL_ROUNDS 6
#define AGENT_DEFAULT_MAX_HISTORY  8

/* The history buffer's own cap. It stays below `LLM_BODY_MAX` because the body also
 * carries the tool array and the system prompt; a history that could fill the body
 * alone would make every later request fail. */
#define AGENT_HISTORY_MAX 8192

/* `03-config.md` §agent: the default value of `agent.system_prompt`. */
#define AGENT_DEFAULT_SYSTEM_PROMPT "/prompts/system.md"

/* `/skills/` index budget inside the system prompt (`02-firmware.md` §2). */
#define AGENT_SKILL_INDEX_MAX 1024

/* The job carries a *pointer* to its prompt, never the prompt itself: it is queued
 * from the link task, whose stack is 4 KiB (`02-firmware.md` §3.3), and an inline
 * 8 KiB buffer overflowed it on the first prompt. The prompt is allocated from PSRAM
 * by `agent_submit` and freed by whichever side ends up owning it. */
typedef struct {
    char *prompt;
    size_t len;
    agent_source_t source;
    char reply_to[32];
} agent_job_t;

static QueueHandle_t s_queue;
static TaskHandle_t s_task;
static volatile bool s_cancel;

/* Run identity. `run_id` is reported in every frame of the run and in `status`. */
static char s_run_id[24];
static uint32_t s_run_counter;
static volatile bool s_busy;

/* History as JSON text, handed to `llm` verbatim (`agent.h`). */
/* PSRAM, not static: `s_tools_json` alone is `TOOLS_MAX_TOTAL * 512` = 32 KiB and the
 * three text buffers are 8 KiB each, which together overflow `dram0_0_seg` by 21 KiB.
 * The board has 8 MiB of PSRAM idle and these are touched once per round, not per
 * instruction, so the slow path is the right trade (`01-hardware.md` §5). Allocated in
 * `agent_start`, before any run can read them. */
static char *s_history;
static size_t s_history_used;
static char *s_system_prompt;
static char *s_tools_json;
static char *s_response;
static agent_reply_t *s_reply;
/* `json_add_string` copies out of this immediately, so it may sit in PSRAM with the
 * rest; keeping it static in DRAM cost 16 KiB of a 36 KiB margin. */
static char *s_final_text;

#define AGENT_PSRAM_CAP (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)

static char *agent_alloc(size_t bytes)
{
    void *p = heap_caps_malloc(bytes, AGENT_PSRAM_CAP);
    if (p == NULL) p = heap_caps_malloc(bytes, MALLOC_CAP_8BIT);
    if (p != NULL) memset(p, 0, bytes);
    return (char *)p;
}

/* `llm.max_tool_rounds`-shaped caps read once per run, so a `config.set` mid-run
 * cannot change the bound the run is being held to. */
static int32_t s_rounds_max;
static int32_t s_history_max;
static size_t s_input_max;

const char *agent_source_name(agent_source_t source)
{
    switch (source) {
    case AGENT_SOURCE_PROMPT:   return "prompt";
    case AGENT_SOURCE_TASK:     return "task";
    case AGENT_SOURCE_TEMPLATE: return "template";
    }
    return "prompt";
}

/* Defined below, after the tree it touches. */
static esp_err_t todo_port_apply(const json_node_t *args, json_doc_t **out,
                                 char *why, size_t why_cap, void *ctx);

/* --- Configuration ------------------------------------------------------- */

static int32_t cfg_int_or(const char *path, int32_t fallback)
{
    int64_t value = 0;
    json_node_t *node = cfg_get(path);
    return (node != NULL && json_int(node, &value)) ? (int32_t)value : fallback;
}

static void load_run_settings(void)
{
    s_rounds_max = cfg_int_or("llm.max_tool_rounds", AGENT_DEFAULT_MAX_TOOL_ROUNDS);
    s_history_max = cfg_int_or("llm.max_history_messages", AGENT_DEFAULT_MAX_HISTORY);
    s_input_max = (size_t)cfg_int_or("agent.max_input_bytes",
                                     AGENT_DEFAULT_MAX_INPUT_BYTES);
    if (s_input_max > AGENT_DEFAULT_MAX_INPUT_BYTES) {
        s_input_max = AGENT_DEFAULT_MAX_INPUT_BYTES;
    }
}

/* The system prompt with the skill index appended. A skill that does not fit is
 * dropped from the end and the omission is logged (`tools/skills.md`), never
 * silently. */
static void build_system_prompt(void)
{
    /* `03-config.md` §prompts: `agent.system_prompt` is a *path* (`CFG_PATH`,
     * default `/prompts/system.md`), not the text. Reading it here is what makes the
     * config key mean what the document says; using the string itself sent the
     * literal `/prompts/system.md` as the persona. */
    const char *path = NULL;
    json_node_t *node = cfg_get("agent.system_prompt");
    if (node != NULL) path = json_string(node);
    /* `03-config.md` §agent: the default is `/prompts/system.md`. `cfg_get` returns
     * NULL for a key that is absent rather than its default, so an unconfigured
     * device had no system prompt at all and the model answered without a persona —
     * every other setting here goes through a `*_or` helper that applies its default,
     * and this one did not. */
    if (path == NULL) path = AGENT_DEFAULT_SYSTEM_PROMPT;
    s_system_prompt[0] = '\0';
    if (path != NULL && path[0] == '/') {
        size_t len = 0;
        esp_err_t err = store_read_file(path, s_system_prompt,
                                        AGENT_DEFAULT_MAX_INPUT_BYTES + 1, &len);
        if (err == ESP_OK) {
            s_system_prompt[len] = '\0';
        } else {
            /* A missing or oversized prompt is reported and the run continues with
             * its skill index only: the model is still usable, and refusing the run
             * would turn a bad path into a dead agent. */
            ESP_LOGW(TAG, "system prompt %s unreadable (%s); running without it",
                     path, esp_err_to_name(err));
            s_system_prompt[0] = '\0';
        }
    }

    size_t used = strlen(s_system_prompt);
    if (used >= AGENT_DEFAULT_MAX_INPUT_BYTES - 1) return;

    size_t count = 0;
    const skill_meta_t *skills = skills_current(&count);
    if (skills == NULL || count == 0) return;

    size_t budget = AGENT_SKILL_INDEX_MAX;
    size_t cap = AGENT_DEFAULT_MAX_INPUT_BYTES + 1;
    if (budget > cap - used - 2) budget = cap - used - 2;
    size_t written = 0;
    size_t dropped = 0;
    if (skills_index(skills, count, s_system_prompt + used,
                     cap - used, budget,
                     &written, &dropped) == ESP_OK) {
        if (dropped > 0) {
            ESP_LOGW(TAG, "skills: %u dropped from the index to fit %u bytes",
                     (unsigned)dropped, (unsigned)budget);
        }
    }
}

static void build_tools_json(const tools_context_t *ctx)
{
    s_tools_json[0] = '\0';
    json_doc_t *doc = json_new_object();
    if (doc == NULL) return;
    json_node_t *array = json_new_array(json_root(doc), "tools");
    if (array != NULL) {
        /* Declarative tools land at M5's mcp/sched pass with the loader; the shape is
         * already the frozen order (built-ins, then declarative, then MCP). */
        tools_json_build(ctx, NULL, array);
        size_t written = 0;
        json_doc_t *wrap = json_new_object();
        if (wrap != NULL) {
            if (json_add_node(json_root(wrap), "tools", array)) {
                json_emit(wrap, s_tools_json, TOOLS_MAX_TOTAL * 512, &written);
                /* Re-slice the array value: `llm` wants the array, not the wrapper.
                 * A slice that took everything past the colon carried the wrapper's
                 * closing brace, so `llm` spliced `[{...}]}` into its own object and
                 * closed it early; the provider answered `invalid character ',' after
                 * top-level value` to every request. */
                if (llm_slice_member(s_tools_json, s_tools_json,
                                     TOOLS_MAX_TOTAL * 512) == 0) {
                    s_tools_json[0] = '\0';
                }
            }
            json_free(wrap);
        }
    }
    json_free(doc);
}

/* --- Frames -------------------------------------------------------------- */

static int64_t now_s(void)
{
    return (int64_t)time(NULL);
}

/* `link_emit_body` refuses a NULL id, so a frame without one is dropped silently,
 * which reads exactly like a hang. `04` §2 gives the device one counter for the
 * whole link, so this goes through `link_next_id` rather than keeping a second
 * one: `d-1` and `a-1` coexisting would make "never reused" false. The buffer is
 * per call because the next allocation would otherwise overwrite an id that has
 * not been copied into its frame yet. */
static const char *agent_frame_id(void)
{
    static char s_id[16];
    link_next_id(s_id, sizeof s_id);
    return s_id;
}

static void emit_agent_error(const char *run_id, const char *reply_to,
                             const char *code, const char *message, bool retryable)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "code", code);
    json_add_string(root, "message", message);
    if (run_id != NULL && run_id[0] != '\0') json_add_string(root, "run_id", run_id);
    if (reply_to != NULL && reply_to[0] != '\0') {
        json_add_string(root, "reply_to", reply_to);
    }
    json_add_bool(root, "retryable", retryable);
    link_emit_body("agent.error", agent_frame_id(), now_s(), NULL, body);
    json_free(body);
}

static void emit_agent_tool(const char *run_id, int round,
                            const agent_tool_call_t *call, const json_node_t *args)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "run_id", run_id);
    json_add_int(root, "round", round);
    json_add_string(root, "call_id", call->id);
    json_add_string(root, "tool", call->name);
    if (args != NULL) {
        json_add_node(root, "args", args);
    } else {
        json_add_object(root, "args");
    }
    link_emit_body("agent.tool", agent_frame_id(), now_s(), NULL, body);
    json_free(body);
}

static void emit_agent_tool_result(const char *run_id, int round,
                                   const agent_tool_call_t *call, bool ok,
                                   json_doc_t *result, const char *code,
                                   const char *message, int64_t duration_ms)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "run_id", run_id);
    json_add_int(root, "round", round);
    json_add_string(root, "call_id", call->id);
    json_add_string(root, "tool", call->name);
    json_add_bool(root, "ok", ok);
    if (ok && result != NULL) {
        json_add_node(root, "result", json_root(result));
    } else if (!ok) {
        json_node_t *error = json_add_object(root, "error");
        if (error != NULL) {
            json_add_string(error, "code", code != NULL ? code : "E_TOOL");
            json_add_string(error, "message", message != NULL ? message : "tool failed");
        }
    }
    json_add_int(root, "duration_ms", duration_ms);
    link_emit_body("agent.tool_result", agent_frame_id(), now_s(), NULL, body);
    json_free(body);
}

static void emit_agent_start(const char *run_id, const char *reply_to,
                             agent_source_t source, size_t input_bytes,
                             int32_t queue_depth)
{
    const char *provider = NULL;
    const char *model = NULL;
    llm_last_answer(&provider, &model);

    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "reply_to", reply_to);
    json_add_string(root, "run_id", run_id);
    json_add_string(root, "trigger", "manual");
    json_add_string(root, "source", agent_source_name(source));
    json_add_string(root, "provider", provider != NULL ? provider : "");
    json_add_string(root, "model", model != NULL ? model : "");
    json_add_int(root, "input_bytes", (int64_t)input_bytes);
    json_add_int(root, "queue_depth", queue_depth);
    link_emit_body("agent.start", agent_frame_id(), now_s(), NULL, body);
    json_free(body);
}

static void emit_agent_final(const char *run_id, const char *reply_to,
                             const char *text, size_t text_bytes,
                             int32_t prompt_tokens, int32_t completion_tokens,
                             int rounds, bool todo_open, int64_t duration_ms,
                             size_t full_bytes)
{
    const char *provider = NULL;
    const char *model = NULL;
    llm_last_answer(&provider, &model);

    bool truncated = text_bytes > AGENT_TEXT_MAX;
    size_t emit_len = truncated ? AGENT_TEXT_MAX : text_bytes;

    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "run_id", run_id);
    if (reply_to != NULL && reply_to[0] != '\0') {
        json_add_string(root, "reply_to", reply_to);
    }
    /* Copy rather than borrow: `json_add_string` needs a terminator at `emit_len`. */
    memcpy(s_final_text, text, emit_len);
    s_final_text[emit_len] = '\0';
    json_add_string(root, "text", s_final_text);

    json_node_t *usage = json_add_object(root, "usage");
    if (usage != NULL) {
        json_add_int(usage, "prompt_tokens", prompt_tokens);
        json_add_int(usage, "completion_tokens", completion_tokens);
        json_add_int(usage, "total_tokens", prompt_tokens + completion_tokens);
    }
    json_add_int(root, "rounds", rounds);
    json_add_string(root, "provider", provider != NULL ? provider : "");
    json_add_string(root, "model", model != NULL ? model : "");
    json_add_int(root, "duration_ms", duration_ms);
    json_add_bool(root, "truncated", truncated);
    json_add_bool(root, "todo_open", todo_open);
    if (truncated) json_add_int(root, "full_bytes", (int64_t)full_bytes);
    link_emit_body("agent.final", agent_frame_id(), now_s(), reply_to, body);
    json_free(body);
}

/* --- Tool execution ------------------------------------------------------ */

/* Rendered messages go into statics, never locals: the agent task has 8 KiB of stack
 * and one message reaches `AGENT_TEXT_MAX`. Dispatch is single-threaded, so these need
 * no lock. `s_result_text` is separate from `s_message` because a tool result is
 * rendered and then embedded in the message that carries it; one buffer would alias. */
static char s_message[AGENT_TEXT_MAX + 256];
static char s_result_text[TOOLS_RESULT_MAX + 1];

/* The `role:"tool"` message for one call result. `02-firmware.md` §4.1 step 4:
 * collect result, append to history. Without it the provider sees a tool call and no
 * answer, and every following round re-asks the same call. */
static void history_push_tool_result(const agent_tool_call_t *call, bool ok,
                                     json_doc_t *result, const char *code,
                                     const char *message)
{
    json_doc_t *doc = json_new_object();
    if (doc == NULL) return;
    json_node_t *root = json_root(doc);
    json_add_string(root, "role", "tool");
    json_add_string(root, "tool_call_id", call->id);

    if (ok && result != NULL) {
        size_t rendered = 0;
        if (json_emit(result, s_result_text, sizeof s_result_text, &rendered) == ESP_OK) {
            json_add_string(root, "content", s_result_text);
        }
    } else {
        char text[TOOLS_ERR_MAX + 64];
        snprintf(text, sizeof text, "{\"error\":{\"code\":\"%s\",\"message\":\"%s\"}}",
                 code != NULL ? code : "E_TOOL",
                 message != NULL ? message : "tool failed");
        json_add_string(root, "content", text);
    }

    size_t written = 0;
    if (json_emit(doc, s_message, sizeof s_message, &written) == ESP_OK) {
        agent_history_append(s_history, AGENT_HISTORY_MAX, &s_history_used, s_message,
                             s_history_max);
    }
    json_free(doc);
}

/* Append one message to the history as the provider's own shape, so the next round
 * sees what happened without the loop re-deriving it.
 *
 * The rendered message goes into a static scratch, not a local: the agent task has
 * 8 KiB of stack and a message reaches `AGENT_TEXT_MAX`. Dispatch is single-threaded,
 * so the static needs no lock. */
static bool history_push(const char *role, const char *content,
                         const char *tool_call_id)
{
    json_doc_t *doc = json_new_object();
    if (doc == NULL) return false;
    json_node_t *root = json_root(doc);
    json_add_string(root, "role", role);
    json_add_string(root, "content", content);
    if (tool_call_id != NULL) json_add_string(root, "tool_call_id", tool_call_id);

    size_t written = 0;
    esp_err_t err = ESP_FAIL;
    if (json_emit(doc, s_message, sizeof s_message, &written) == ESP_OK) {
        err = agent_history_append(s_history, AGENT_HISTORY_MAX, &s_history_used, s_message,
                                   s_history_max);
    }
    json_free(doc);
    return err == ESP_OK;
}

/* The assistant turn that asked for tools, with its `tool_calls` array: the provider
 * rejects a `role:"tool"` message whose `tool_call_id` has no matching call, so the
 * request and its results must travel together (`02-firmware.md` §llm). */
static void history_push_assistant(const agent_reply_t *reply)
{
    json_doc_t *doc = json_new_object();
    if (doc == NULL) return;
    json_node_t *root = json_root(doc);
    json_add_string(root, "role", "assistant");
    json_add_string(root, "content", reply->content);
    json_node_t *calls = json_new_array(root, "tool_calls");
    if (calls != NULL) {
        for (size_t i = 0; i < reply->call_count; i++) {
            json_node_t *entry = json_array_push_object(calls);
            if (entry == NULL) break;
            json_add_string(entry, "id", reply->calls[i].id);
            json_add_string(entry, "type", "function");
            json_node_t *fn = json_add_object(entry, "function");
            if (fn == NULL) continue;
            json_add_string(fn, "name", reply->calls[i].name);
            json_add_string(fn, "arguments", reply->calls[i].args);
        }
    }
    size_t written = 0;
    if (json_emit(doc, s_message, sizeof s_message, &written) == ESP_OK) {
        agent_history_append(s_history, AGENT_HISTORY_MAX, &s_history_used, s_message,
                             s_history_max);
    }
    json_free(doc);
}

/* One tool call: validate, gate, dispatch, report. Returns true when it executed.
 * A refusal is not a run failure — the model sees it and may try another tool
 * (`04` §3.13). */
static void history_push_tool_result(const agent_tool_call_t *call, bool ok,
                                     json_doc_t *result, const char *code,
                                     const char *message);

static bool run_one_tool(const tools_context_t *ctx, const char *run_id, int round,
                         const agent_tool_call_t *call)
{
    json_doc_t *args_doc = NULL;
    if (call->args[0] != '\0') {
        if (json_parse(call->args, strlen(call->args), &args_doc) != ESP_OK) {
            emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                                   "arguments are not a JSON object", 0);
            history_push_tool_result(call, false, NULL, "E_TOOL",
                                     "arguments are not a JSON object");
            return false;
        }
    }
    const json_node_t *args = args_doc != NULL ? json_root(args_doc) : NULL;

    /* `tools_offered` is the same predicate `tools_json` used, so a call to a tool
     * the model was not given is refused rather than executed. */
    if (!tools_offered(call->name, ctx)) {
        emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                               "tool is not available on this device", 0);
        history_push_tool_result(call, false, NULL, "E_TOOL",
                                 "tool is not available on this device");
        json_free(args_doc);
        return false;
    }

    const tools_builtin_t *builtin = tools_find(call->name);
    if (builtin == NULL) {
        /* Declarative and MCP tools have no registry entry at M4; they are refused
         * with the same code rather than reaching `tools_call`'s fallthrough. */
        emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                               "unknown tool", 0);
        history_push_tool_result(call, false, NULL, "E_TOOL", "unknown tool");
        json_free(args_doc);
        return false;
    }

    char why[TOOLS_ERR_MAX] = "";
    emit_agent_tool(run_id, round, call, args);

    esp_err_t err = tools_validate(call->name, args, why, sizeof why);
    if (err != ESP_OK) {
        emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                               why[0] != '\0' ? why : "arguments rejected", 0);
        history_push_tool_result(call, false, NULL, "E_TOOL",
                                 why[0] != '\0' ? why : "arguments rejected");
        json_free(args_doc);
        return false;
    }

    char host[128];
    const char *host_name = tools_gate_host_for(builtin, args, host, sizeof host);
    err = tools_gate_check(builtin, &ctx->policy, host_name, why, sizeof why);
    if (err != ESP_OK) {
        emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                               why[0] != '\0' ? why : "tool refused", 0);
        history_push_tool_result(call, false, NULL, "E_TOOL",
                                 why[0] != '\0' ? why : "tool refused");
        json_free(args_doc);
        return false;
    }

    int64_t started = esp_timer_get_time();
    json_doc_t *out = NULL;
    const char *wire_code = "E_TOOL";
    err = tools_call(ctx, call->name, args, &out, &wire_code, why, sizeof why);
    int64_t duration_ms = (esp_timer_get_time() - started) / 1000;

    emit_agent_tool_result(run_id, round, call, err == ESP_OK, out,
                           wire_code, why, duration_ms);
    history_push_tool_result(call, err == ESP_OK, out, wire_code, why);
    if (out != NULL) json_free(out);
    json_free(args_doc);
    return err == ESP_OK;
}

/* --- The `todo` port (`08-code-architecture.md` §1.3) --------------------- */

/* `tools` is L2 and the tree is L3, so this is the one call that crosses upward and
 * it crosses as an installed function, not as an include. `out` is the rendered
 * view, which is what the tool returns (`tools/todo.md` §Result). */
static esp_err_t todo_port_apply(const json_node_t *args, json_doc_t **out,
                                 char *why, size_t why_cap, void *ctx)
{
    (void)ctx;
    esp_err_t err = todo_apply(agent_todo_phases(), args, why, why_cap);
    if (err != ESP_OK) return err;
    if (out == NULL) return ESP_OK;

    json_doc_t *doc = json_new_object();
    if (doc == NULL) return ESP_ERR_NO_MEM;
    err = todo_render(agent_todo_phases(), json_root(doc));
    if (err != ESP_OK) {
        json_free(doc);
        return err;
    }
    *out = doc;
    return ESP_OK;
}

/* --- The run ------------------------------------------------------------- */

static void run_once(const agent_job_t *job, int32_t queue_depth)
{
    load_run_settings();
    build_system_prompt();

    tools_context_t ctx;
    tools_context_from_config(&ctx);
    ctx.todo.apply = todo_port_apply;
    build_tools_json(&ctx);

    snprintf(s_run_id, sizeof s_run_id, "r-%" PRIu32, s_run_counter++);
    int64_t started_ms = esp_timer_get_time();

    emit_agent_start(s_run_id, job->reply_to, job->source, job->len, queue_depth);

    /* The prompt is pushed once and is the last message of the first request, so
     * later rounds pass `user_prompt = NULL` rather than repeating it. It must be in
     * the history rather than only in the first request, or a later run would see
     * the answers to a question that is no longer there (`02-firmware.md` §4.1).
     *
     * `history_floor` is the depth before this run, so an aborted run can drop the
     * turns it did not finish and leave the conversation exactly as it found it. */
    uint32_t history_floor = (uint32_t)agent_history_count(s_history);
    /* A successful append means the prompt is the last message, even when the bound
     * evicted an older one and the count did not grow — comparing counts made a full
     * history send the question twice, once from the history and once as
     * `user_prompt`. `max_history_messages` of 0 is the deliberate no-op, so the
     * prompt travels as `user_prompt` there and nowhere else. */
    bool prompt_in_history = s_history_max > 0 && history_push("user", job->prompt, NULL);
    bool finalized = false;
    int rounds = 0;
    bool ever_initialised = false;
    int32_t prompt_tokens = 0;
    int32_t completion_tokens = 0;
    esp_err_t last_err = ESP_OK;
    bool todo_open = false;

    while (rounds < s_rounds_max) {
        if (s_cancel) {
            emit_agent_error(s_run_id, job->reply_to, "E_CANCELLED",
                             "run cancelled", false);
            goto done;
        }

        llm_request_t request = {
            .model = NULL,   /* chosen by `llm` from the answering provider */
            .token_param = NULL,
            .max_output_tokens = 0,
            .system_prompt = s_system_prompt,
            .history_json = s_history_used > 0 ? s_history : NULL,
            .user_prompt = (rounds == 0 && !prompt_in_history) ? job->prompt : NULL,
            .tools_json = s_tools_json[0] != '\0' ? s_tools_json : NULL,
        };

        s_response[0] = '\0';
        last_err = llm_query(&request, s_response, LLM_BODY_MAX + 1, NULL);
        /* `llm` refuses when system prompt + tool array + history exceed its buffer,
         * and no provider can take a body the assembler never produced. Re-sending
         * the same history fails forever, which is what made the device answer
         * `request body too large` to every prompt until a reboot. Halve the history
         * until it fits; an empty history that still fails is a system prompt or tool
         * array problem, and the error names it instead of looping. */
        while (last_err == ESP_ERR_INVALID_SIZE) {
            size_t count = agent_history_count(s_history);
            if (count == 0) break;
            agent_history_truncate(s_history, &s_history_used, (uint32_t)(count / 2));
            if (agent_history_count(s_history) >= count) break;
            request.history_json = s_history_used > 0 ? s_history : NULL;
            s_response[0] = '\0';
            last_err = llm_query(&request, s_response, LLM_BODY_MAX + 1, NULL);
        }
        if (last_err != ESP_OK) {
            const char *code = last_err == ESP_ERR_INVALID_STATE ? "E_LLM_RATE"
                                                                : "E_LLM_UNAVAILABLE";
            emit_agent_error(s_run_id, job->reply_to, code, llm_last_error(), true);
            goto done;
        }

        /* The check at the loop top runs before a call that blocks on the network for
         * seconds, which is exactly when a user cancels. Without this second check a
         * cancel arriving during `llm_query` was silently discarded and the run
         * finished as if nothing had been asked (`04-protocol.md` §3.4: the terminal
         * outcome of a cancelled run is `agent.error`/`E_CANCELLED`). */
        if (s_cancel) {
            emit_agent_error(s_run_id, job->reply_to, "E_CANCELLED",
                             "run cancelled", false);
            goto done;
        }

        if (agent_parse_reply(s_response, strlen(s_response), s_reply) != ESP_OK) {
            emit_agent_error(s_run_id, job->reply_to, "E_LLM_UNAVAILABLE",
                             "provider reply was not a chat completion", true);
            goto done;
        }

        prompt_tokens += s_reply->prompt_tokens;
        completion_tokens += s_reply->completion_tokens;

        if (s_reply->call_count == 0) {
            /* The close-out gate (`02-firmware.md` §4.1 step 5). While it fails and
             * rounds remain, take another round with the frozen message appended;
             * a run never finalizes with work it did not account for. */
            closeout_verdict_t verdict =
                agent_closeout_check(agent_todo_phases(), rounds, ever_initialised);
            if (verdict != CLOSEOUT_OK) {
                const char *prompt = agent_closeout_prompt(verdict);
                if (prompt != NULL && rounds + 1 < s_rounds_max) {
                    history_push("assistant", s_reply->content, NULL);
                    history_push("system", prompt, NULL);
                    rounds++;
                    continue;
                }
                todo_open = agent_todo_open_flag(verdict, true);
            }
            break;
        }

        rounds++;
        /* The assistant turn carrying `tool_calls` must precede its results, and the
         * results follow it in call order. */
        history_push_assistant(s_reply);
        for (size_t i = 0; i < s_reply->call_count; i++) {
            if (s_cancel) {
                emit_agent_error(s_run_id, job->reply_to, "E_CANCELLED",
                                 "run cancelled", false);
                goto done;
            }
            bool ok = run_one_tool(&ctx, s_run_id, rounds, &s_reply->calls[i]);
            (void)ok;
            if (strcmp(s_reply->calls[i].name, "todo") == 0) {
                json_doc_t *args = NULL;
                if (s_reply->calls[i].args[0] != '\0' &&
                    json_parse(s_reply->calls[i].args, strlen(s_reply->calls[i].args),
                               &args) == ESP_OK) {
                    json_node_t *op = json_get(json_root(args), "op");
                    if (op != NULL && json_string(op) != NULL &&
                        strcmp(json_string(op), "init") == 0) {
                        ever_initialised = true;
                    }
                    json_free(args);
                }
            }
        }
    }

    /* The answer belongs to the conversation: without it the next run replays the
     * question with no reply to it (`02-firmware.md` §4.1 step 6). */
    history_push("assistant", s_reply->content, NULL);

    emit_agent_final(s_run_id, job->reply_to, s_reply->content, s_reply->content_len,
                     prompt_tokens, completion_tokens, rounds, todo_open,
                     (esp_timer_get_time() - started_ms) / 1000,
                     s_reply->content_len);
    finalized = true;

done:
    /* A cancelled or failed run leaves no half-finished turns behind: a replayed
     * `tool` message whose matching call was dropped is refused by the provider, and
     * a question with no answer would poison every later run. */
    if (!finalized) agent_history_truncate(s_history, &s_history_used, history_floor);
    s_busy = false;
    s_cancel = false;
}

static void agent_task(void *arg)
{
    (void)arg;
    agent_job_t job;
    for (;;) {
        if (xQueueReceive(s_queue, &job, portMAX_DELAY) != pdTRUE) continue;
        s_busy = true;
        s_cancel = false;
        run_once(&job, (int32_t)uxQueueMessagesWaiting(s_queue));
        /* The run owned the prompt from the moment it left the queue, so it frees it
         * whether it finished, failed or was cancelled. */
        heap_caps_free(job.prompt);
        job.prompt = NULL;
    }
}

esp_err_t agent_start(void)
{
    if (s_task != NULL) return ESP_OK;  /* idempotent: boot calls it once */

    s_history = agent_alloc(AGENT_HISTORY_MAX);
    s_system_prompt = agent_alloc(AGENT_DEFAULT_MAX_INPUT_BYTES + 1);
    s_tools_json = agent_alloc(TOOLS_MAX_TOTAL * 512);
    s_response = agent_alloc(LLM_BODY_MAX + 1);
    s_final_text = agent_alloc(AGENT_TEXT_MAX + 1);
    s_reply = (agent_reply_t *)agent_alloc(sizeof(agent_reply_t));
    if (s_history == NULL || s_system_prompt == NULL || s_tools_json == NULL ||
        s_response == NULL || s_final_text == NULL || s_reply == NULL) {
        heap_caps_free(s_history);
        heap_caps_free(s_system_prompt);
        heap_caps_free(s_tools_json);
        heap_caps_free(s_response);
        heap_caps_free(s_final_text);
        heap_caps_free(s_reply);
        return ESP_ERR_NO_MEM;
    }

    s_queue = xQueueCreate(AGENT_MAX_QUEUE, sizeof(agent_job_t));
    if (s_queue == NULL) return ESP_ERR_NO_MEM;

    todo_list_clear(agent_todo_phases());

    BaseType_t ok = xTaskCreate(agent_task, "agent", AGENT_TASK_STACK, NULL,
                                AGENT_TASK_PRIORITY, &s_task);
    if (ok != pdPASS) {
        vQueueDelete(s_queue);
        s_queue = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t agent_submit(const char *prompt, size_t len, agent_source_t source,
                       const char *reply_to)
{
    if (prompt == NULL || reply_to == NULL) return ESP_ERR_INVALID_ARG;
    /* Its own code: sharing `ESP_ERR_INVALID_STATE` with the full queue told the
     * bridge to retry a boot-time allocation failure (`04` §3.3 E_BUSY is
     * `retryable:true`; a missing loop is not). */
    if (s_queue == NULL) return ESP_ERR_NOT_FOUND;

    load_run_settings();
    if (len > s_input_max || len > AGENT_DEFAULT_MAX_INPUT_BYTES) {
        return ESP_ERR_INVALID_SIZE;
    }

    /* Rejected by the queue's depth, not by `s_busy`: a run in flight plus
     * `AGENT_MAX_QUEUE` waiting is the documented ceiling (`04` §3.15). */
    if (uxQueueMessagesWaiting(s_queue) >= AGENT_MAX_QUEUE) {
        return ESP_ERR_INVALID_STATE;
    }

    char *copy = agent_alloc(len + 1);
    if (copy == NULL) return ESP_ERR_NO_MEM;
    memcpy(copy, prompt, len);
    copy[len] = '\0';

    agent_job_t job;
    job.prompt = copy;
    job.len = len;
    job.source = source;
    snprintf(job.reply_to, sizeof job.reply_to, "%s", reply_to);

    if (xQueueSend(s_queue, &job, 0) != pdTRUE) {
        heap_caps_free(copy);
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

bool agent_cancel(void)
{
    if (!s_busy) return false;
    s_cancel = true;
    return true;
}

void agent_status(agent_status_t *out)
{
    if (out == NULL) return;
    out->busy = s_busy;
    out->run_id = s_busy ? s_run_id : NULL;
    out->queue_depth = s_queue != NULL ? (int32_t)uxQueueMessagesWaiting(s_queue) : 0;
}

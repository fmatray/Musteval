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

/* `tools/ask.md` §Limits and `03-config.md` `agent.ask_timeout_s`: the bounds a
 * config file cannot push past, because an unbounded wait would hang a run and an
 * instant one would make the tool useless. */
#define AGENT_ASK_TIMEOUT_MIN 10
#define AGENT_ASK_TIMEOUT_MAX 600

/* The history buffer's own cap. It stays below `LLM_BODY_MAX` because the body also
 * carries the tool array and the system prompt; a history that could fill the body
 * alone would make every later request fail. */
#define AGENT_HISTORY_MAX 8192

/* `03-config.md` §agent: the default value of `agent.system_prompt`. */
#define AGENT_DEFAULT_SYSTEM_PROMPT "/prompts/system.md"

/* `/skills/` index budget inside the system prompt (`02-firmware.md` §2). */
#define AGENT_SKILL_INDEX_MAX 1024

/* The job carries a *pointer* to its prompt, never the prompt itself: it is queued
 * from the link task, whose stack is 8 KiB (`02-firmware.md` §3.3), and an inline
 * 8 KiB buffer overflowed the 4 KiB it had then on the first prompt. The prompt is
 * allocated from PSRAM
 * by `agent_submit` and freed by whichever side ends up owning it. */
typedef struct {
    char *prompt;
    size_t len;
    agent_source_t source;
    char reply_to[32];
    /* Assigned at submit, not at run: `tasks.run` (`04` §4.5) and `task.fired`
     * (`04` §3.16) both have to name the run in the reply that starts it, and a run
     * id minted inside `run_once` is not knowable to the caller that asked. */
    char run_id[24];
} agent_job_t;

static QueueHandle_t s_queue;
static TaskHandle_t s_task;
static volatile bool s_cancel;

/* --- `ask` round trip (`tools/ask.md`, `04-protocol.md` §3.22) ----------------
 *
 * The agent task is the only waiter. `ask.reply` arrives on the link task, so the
 * handoff is a binary semaphore plus buffers the reply is copied into before the
 * waiter is released. No allocation happens on the link task: a reply that arrives
 * while nothing is waiting is dropped rather than queued (`04` §3.23 makes a late
 * reply ignorable), and that is why there is exactly one slot rather than a queue.
 *
 * `s_ask_cap` is the answer capacity the waiter asked for; a longer answer is
 * truncated rather than refused, because the human already typed it and dropping
 * the whole reply would fail a call the bridge answered correctly. */
static SemaphoreHandle_t s_ask_done;
static char s_ask_id[40];
static char s_ask_answer[TOOLS_ASK_ANSWER_MAX + 1];
static bool s_ask_cancelled;
static volatile bool s_ask_waiting;
static volatile bool s_ask_pending;
/* Per-call context, set by `run_one_tool` before the port is entered, so the frame
 * carries the call it belongs to without `tools` having to know about runs. */
static unsigned s_ask_seq;
static const char *s_ask_call_id = "";
static int s_ask_round;

/* Run identity. `run_id` is reported in every frame of the run and in `status`. */
static char s_run_id[24];
static uint32_t s_run_counter;
static volatile bool s_busy;

/* `todo_apply` commits with `*list = s_scratch`, an 8 KiB non-atomic copy, and it
 * is now reachable from two tasks: the agent task through the `todo` tool, and the
 * link task through the `todo.apply` command (`04` §4.27). Without a lock a `/todo`
 * arriving mid-run would tear the list the model is reading, so both paths take this
 * one mutex — the same shape as `net.c`'s in-flight lock. */
static SemaphoreHandle_t s_todo_lock;

/* History as JSON text, handed to `llm` verbatim (`agent.h`). */
/* PSRAM, not static: `s_tools_json` alone is `TOOLS_MAX_TOTAL * 512` = 32 KiB and the
 * three text buffers are 8 KiB each, which together overflow `dram0_0_seg` by 21 KiB.
 * The board has 8 MiB of PSRAM idle and these are touched once per round, not per
 * instruction, so the slow path is the right trade (`01-hardware.md` §5). Allocated in
 * `agent_start`, before any run can read them. */
static char *s_history;
static size_t s_history_used;
static char *s_system_prompt;
/* `04` §4.30: how many bytes of `s_system_prompt` are the appended skill index, so the
 * two can be reported as a whole and a part rather than as two blocks that add up to
 * more than the buffer they share. Set by `build_system_prompt`, which is the only
 * writer. */
static size_t s_skills_bytes;
/* `04` §4.30 `counts`: the two blocks the history does not hold. Both are set by the
 * builders that own them, so the counter is the length of what was actually built
 * rather than a second count that could drift from it. */
static size_t s_skill_count;
static size_t s_tool_count;
static char *s_tools_json;
static char *s_response;
static agent_reply_t *s_reply;
/* `json_add_string` copies out of this immediately, so it may sit in PSRAM with the
 * rest; keeping it static in DRAM cost 16 KiB of a 36 KiB margin. */
static char *s_final_text;
/* `04` §4.33: the summarising text and then the rewrite's scratch, one 8 KiB buffer
 * for both. PSRAM for the reason above: a `static` here dropped the internal heap
 * below the agent task's 8 KiB stack and `agent_start` refused to boot the agent. */
static char *s_compact_buf;

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
static int32_t s_autocompact_pct;
static char *s_autocompact_summary;

/* `04` §4.33's body without its busy check, defined under `agent_context_compact`.
 * `run_once` compacts itself at the end of a run, where `s_busy` is still set, so it
 * calls this rather than the public entry. */
static esp_err_t compact_impl(uint32_t keep, uint32_t *summarised, uint32_t *kept,
                              size_t *before_bytes, size_t *after_bytes,
                              char *summary, size_t summary_cap);

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

esp_err_t agent_todo_apply(const json_node_t *args, json_doc_t **out, char *why,
                           size_t why_cap)
{
    return todo_port_apply(args, out, why, why_cap, NULL);
}

/* --- Configuration ------------------------------------------------------- */

static void load_run_settings(void)
{
    /* Read what the config says, then let the pure resolver apply `03-config.md`'s
     * defaults and the buffer clamp. Both halves are separated so the half that a
     * test can fail is the half that carries the rule (`agent_settings.c`). */
    int64_t value = 0;
    json_node_t *node = cfg_get("llm.max_tool_rounds");
    bool has_rounds = node != NULL && json_int(node, &value);
    agent_raw_settings_t raw = {0};
    raw.has_max_tool_rounds = has_rounds;
    raw.max_tool_rounds = (int32_t)value;

    value = 0;
    node = cfg_get("llm.max_history_messages");
    raw.has_max_history = node != NULL && json_int(node, &value);
    raw.max_history = (int32_t)value;

    value = 0;
    node = cfg_get("agent.max_input_bytes");
    raw.has_max_input = node != NULL && json_int(node, &value);
    raw.max_input = (int32_t)value;

    value = 0;
    node = cfg_get("agent.autocompact_pct");
    raw.has_autocompact = node != NULL && json_int(node, &value);
    raw.autocompact_pct = (int32_t)value;

    agent_run_settings_t resolved;
    agent_run_settings_resolve(&raw, &resolved);
    s_rounds_max = resolved.max_tool_rounds;
    s_history_max = resolved.max_history_messages;
    s_input_max = resolved.max_input_bytes;
    s_autocompact_pct = resolved.autocompact_pct;
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
                                        AGENT_INPUT_BUFFER_MAX + 1, &len);
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
    s_skills_bytes = 0;
    /* Reset with its byte sibling: the early return below skips the rebuild, and a
     * count left over from the previous build would report skills the prompt no
     * longer holds. */
    s_skill_count = 0;
    if (used >= AGENT_INPUT_BUFFER_MAX - 1) return;

    size_t count = 0;
    const skill_meta_t *skills = skills_current(&count);
    if (skills == NULL || count == 0) return;

    size_t budget = AGENT_SKILL_INDEX_MAX;
    size_t cap = AGENT_INPUT_BUFFER_MAX + 1;
    if (budget > cap - used - 2) budget = cap - used - 2;
    size_t written = 0;
    size_t dropped = 0;
    if (skills_index(skills, count, s_system_prompt + used,
                     cap - used, budget,
                     &written, &dropped) == ESP_OK) {
        s_skills_bytes = written;
        /* The skills the index had room for, not the files on disk: `skills_index`
         * reports what it left out, and a reader asking "how many skills are in my
         * context" is asking about the prompt. */
        s_skill_count = count > dropped ? count - dropped : 0;
        if (dropped > 0) {
            ESP_LOGW(TAG, "skills: %u dropped from the index to fit %u bytes",
                     (unsigned)dropped, (unsigned)budget);
        }
    }
}

static void build_tools_json(const tools_context_t *ctx)
{
    s_tools_json[0] = '\0';
    s_tool_count = 0;
    json_doc_t *doc = json_new_object();
    if (doc == NULL) return;
    json_node_t *array = json_new_array(json_root(doc), "tools");
    if (array != NULL) {
        tools_json_build(ctx, array);
        /* The length of the array the model is offered: `tools_json_build` applies
         * `tools_offered`, so a disabled tool and a tool an unconfigured subsystem
         * removes are already absent and the count cannot include them. */
        s_tool_count = json_array_len(array);
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
    json_add_bool(root, "retryable", retryable);
    link_emit_body("agent.error", agent_frame_id(), now_s(), reply_to, body);
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

/* --- `ask` port (`tools/ask.md`) -------------------------------------------- */

/* Bounded by `agent.ask_timeout_s`, and the wait is a poll rather than a single
 * blocking take because three different events end it and only one of them gives the
 * semaphore. `s_cancel` is checked between wakeups so an Esc key is honoured within
 * one tick instead of after the full timeout (`04-protocol.md` §3.5 makes `cancel`
 * the reader's interrupt). */
static esp_err_t agent_ask_call(const char *question, const json_node_t *options,
                                char *answer, size_t answer_cap, const char **code,
                                char *why, size_t why_cap, void *ctx)
{
    (void)ctx;
    if (question == NULL || answer == NULL || answer_cap == 0) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "ask: no question");
        if (code != NULL) *code = "E_TOOL";
        return ESP_ERR_INVALID_ARG;
    }
    if (s_ask_done == NULL) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "ask is unavailable");
        if (code != NULL) *code = "E_TOOL";
        return ESP_ERR_NOT_SUPPORTED;
    }

    int64_t raw_timeout = 0;
    json_node_t *node = cfg_get("agent.ask_timeout_s");
    int timeout_s = (node != NULL && json_int(node, &raw_timeout)) ? (int)raw_timeout
                                                                   : 120;
    if (timeout_s < AGENT_ASK_TIMEOUT_MIN) timeout_s = AGENT_ASK_TIMEOUT_MIN;
    if (timeout_s > AGENT_ASK_TIMEOUT_MAX) timeout_s = AGENT_ASK_TIMEOUT_MAX;

    /* One waiter at a time is guaranteed by the serialized loop, so the id needs no
     * lock: it is written before the frame is emitted and read after the waiter
     * wakes. It has to be unique per call rather than per run, because a run may ask
     * twice and the second reply must not be taken by the first call. */
    snprintf(s_ask_id, sizeof s_ask_id, "q-%u", (unsigned)++s_ask_seq);
    s_ask_answer[0] = '\0';
    s_ask_cancelled = false;
    xSemaphoreTake(s_ask_done, 0); /* a stale give from an abandoned wait */
    s_ask_pending = true;
    s_ask_waiting = true;

    json_doc_t *body = json_new_object();
    if (body == NULL) {
        s_ask_pending = false;
        s_ask_waiting = false;
        if (code != NULL) *code = "E_TOOL";
        return ESP_ERR_NO_MEM;
    }
    json_node_t *root = json_root(body);
    json_add_string(root, "run_id", s_run_id);
    json_add_int(root, "round", s_ask_round);
    json_add_string(root, "call_id", s_ask_call_id);
    json_add_string(root, "ask_id", s_ask_id);
    json_add_string(root, "question", question);
    if (options != NULL && json_is_array(options)) {
        json_node_t *list = json_new_array(root, "options");
        for (size_t i = 0; i < json_array_len(options); i++) {
            json_array_push_string(list, json_string(json_array_at(options, i)));
        }
    }
    json_add_int(root, "timeout_s", timeout_s);
    esp_err_t emitted = link_emit_body("agent.ask", agent_frame_id(), now_s(), NULL, body);
    json_free(body);
    if (emitted != ESP_OK) {
        /* The question never left the device, so no reply can arrive and waiting the
         * full timeout would burn `agent.ask_timeout_s` on a frame the bridge never
         * saw. Fail the call with the cause instead. */
        s_ask_waiting = false;
        s_ask_pending = false;
        if (code != NULL) *code = "E_TOOL";
        snprintf(why, why_cap, "ask: the question could not be sent");
        return emitted;
    }

    esp_err_t err = ESP_OK;
    bool answered = false;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_s * 1000ULL);
    while (xTaskGetTickCount() < deadline) {
        if (xSemaphoreTake(s_ask_done, pdMS_TO_TICKS(100)) == pdTRUE) {
            answered = true;
            break;
        }
        if (s_cancel) break;
    }
    s_ask_waiting = false;
    s_ask_pending = false;

    if (answered && !s_ask_cancelled) {
        size_t n = strlen(s_ask_answer);
        if (n >= answer_cap) n = answer_cap - 1;
        memcpy(answer, s_ask_answer, n);
        answer[n] = '\0';
        return ESP_OK;
    }
    if (answered) {
        err = ESP_ERR_INVALID_STATE;
        if (code != NULL) *code = "E_CANCELLED";
        snprintf(why, why_cap, "ask: the question was dismissed");
        return err;
    }
    if (s_cancel) {
        if (code != NULL) *code = "E_CANCELLED";
        snprintf(why, why_cap, "ask: run cancelled while waiting");
        return ESP_ERR_INVALID_STATE;
    }
    if (code != NULL) *code = "E_TIMEOUT";
    snprintf(why, why_cap, "ask: no answer within %d s", timeout_s);
    return ESP_ERR_TIMEOUT;
}

/* Called on the link task by `agent_ask_reply`. Returns false when the reply names a
 * question nobody is waiting on, which is the ignored case in `04` §3.23. */
bool agent_ask_reply(const char *ask_id, const char *answer, bool cancelled)
{
    if (!s_ask_waiting || !s_ask_pending || ask_id == NULL) return false;
    if (strcmp(ask_id, s_ask_id) != 0) return false;
    s_ask_pending = false;
    s_ask_cancelled = cancelled;
    s_ask_answer[0] = '\0';
    if (!cancelled && answer != NULL) {
        /* Truncated here, not by the answer buffer's owner: the capacity is known
         * before the waiter wakes, so both sides agree on the bound. */
        size_t n = strlen(answer);
        if (n > TOOLS_ASK_ANSWER_MAX) n = TOOLS_ASK_ANSWER_MAX;
        memcpy(s_ask_answer, answer, n);
        s_ask_answer[n] = '\0';
    }
    if (s_ask_done != NULL) xSemaphoreGive(s_ask_done);
    return true;
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
    json_add_string(root, "run_id", run_id);
    json_add_string(root, "trigger", "manual");
    json_add_string(root, "source", agent_source_name(source));
    json_add_string(root, "provider", provider != NULL ? provider : "");
    json_add_string(root, "model", model != NULL ? model : "");
    json_add_int(root, "input_bytes", (int64_t)input_bytes);
    json_add_int(root, "queue_depth", queue_depth);
    link_emit_body("agent.start", agent_frame_id(), now_s(), reply_to, body);
    json_free(body);
}

static void emit_agent_final(const char *run_id, const char *reply_to,
                             const char *text, size_t text_bytes,
                             int32_t prompt_tokens, int32_t completion_tokens,
                             int rounds, bool todo_open, int64_t duration_ms,
                             size_t full_bytes, bool reply_truncated)
 {
    const char *provider = NULL;
    const char *model = NULL;
    llm_last_answer(&provider, &model);

    bool truncated = text_bytes > AGENT_TEXT_MAX || reply_truncated;
    size_t emit_len = truncated ? AGENT_TEXT_MAX : text_bytes;
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "run_id", run_id);
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

/* The model's reasoning trace, in its own frame (`04-protocol.md` §3.21).
 *
 * It never rides `agent.final`: that text is the answer, `AGENT_TEXT_MAX` is the whole
 * frame budget, and a trace presented as the reply is a different answer rather than
 * a longer one. Nothing is emitted when a reply carried no trace, so a model that
 * thinks nothing costs no frame. */
static void emit_agent_thinking(const char *run_id, int round, const agent_reply_t *reply)
{
    if (run_id == NULL || reply == NULL || reply->thinking_len == 0) return;

    /* The reply's own buffer is already NUL-terminated inside `AGENT_THINKING_MAX`,
     * so it is serialised in place — no second copy, and no way for the two to
     * disagree about where the cut fell. */
    bool truncated = reply->thinking_full > 0;
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    json_add_string(root, "run_id", run_id);
    json_add_int(root, "round", round);
    json_add_string(root, "text", reply->thinking);
    json_add_bool(root, "truncated", truncated);
    if (truncated) json_add_int(root, "full_bytes", (int64_t)reply->thinking_full);
    link_emit_body("agent.thinking", agent_frame_id(), now_s(), NULL, body);
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

    if (ok) {
        size_t rendered = 0;
        if (json_emit(result, s_result_text, sizeof s_result_text, &rendered) == ESP_OK) {
            json_add_string(root, "content", s_result_text);
        } else {
            /* A `role:"tool"` message without `content` is refused by providers. The
             * call itself succeeded, so this is reported as a successful result that
             * omits the payload — not as an error: an `error` body for a mutation
             * that already committed invites the model to retry it, and a retried
             * `done` on a completed task is a second write. */
            char note[192];
            snprintf(note, sizeof note,
                     "{\"ok\":true,\"note\":\"result omitted: over %d bytes; the "
                     "operation was applied, read the state back with "
                     "{\\\"op\\\":\\\"view\\\"}\"}",
                     (int)TOOLS_RESULT_MAX);
            json_add_string(root, "content", note);
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
                             s_history_max, true);
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
                                   s_history_max, true);
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
                             s_history_max, true);
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

    /* The `ask` port cannot see the run it belongs to, and the frame needs `run_id`,
     * `round` and `call_id` (`04` §3.22). Publishing them here is safe because the
     * loop is serialized: exactly one call is in flight, and the port is entered
     * below in the same iteration. */
    s_ask_round = round;
    s_ask_call_id = call->id;

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
    bool remote = builtin == NULL && strncmp(call->name, "mcp.", 4) == 0;
    /* A declarative tool has no registry row to carry its gate or its schema, so it
     * reaches `tools_call`, which checks the host allowlist and validates nothing
     * beyond the compiled parameter list the model was shown. `tools_offered` above
     * already refused a name the loaded set does not carry. */
    bool declared = builtin == NULL && !remote &&
                    tools_declared_find(ctx->declared, call->name) != NULL;
    if (builtin == NULL && !remote && !declared) {
        emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                               "unknown tool", 0);
        history_push_tool_result(call, false, NULL, "E_TOOL", "unknown tool");
        json_free(args_doc);
        return false;
    }

    char why[TOOLS_ERR_MAX] = "";
    emit_agent_tool(run_id, round, call, args);

    /* An MCP tool's arguments are validated by the server that published the schema
     * the model was shown, so only the registry's tools go through the registry
     * validator. The `mcp.` namespace was checked to be offered above. */
    esp_err_t err = (remote || declared) ? ESP_OK
                                        : tools_validate(call->name, args, why, sizeof why);
    if (err != ESP_OK) {
        emit_agent_tool_result(run_id, round, call, false, NULL, "E_TOOL",
                               why[0] != '\0' ? why : "arguments rejected", 0);
        history_push_tool_result(call, false, NULL, "E_TOOL",
                                 why[0] != '\0' ? why : "arguments rejected");
        json_free(args_doc);
        return false;
    }

    char host[128];
    const char *host_name =
        (remote || declared) ? NULL : tools_gate_host_for(builtin, args, host, sizeof host);
    /* A remote tool's gate is inside `tools_call`, because its name has no registry
     * row to carry it; a declarative tool's is too, and its host is the file's. */
    err = (remote || declared)
              ? ESP_OK
              : tools_gate_check(builtin, &ctx->policy, host_name, why, sizeof why);
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
    if (s_todo_lock != NULL) xSemaphoreTake(s_todo_lock, portMAX_DELAY);
    esp_err_t err = todo_apply(agent_todo_phases(), args, why, why_cap);
    if (err != ESP_OK) {
        if (s_todo_lock != NULL) xSemaphoreGive(s_todo_lock);
        return err;
    }
    if (out == NULL) {
        if (s_todo_lock != NULL) xSemaphoreGive(s_todo_lock);
        return ESP_OK;
    }

    json_doc_t *doc = json_new_object();
    if (doc == NULL) {
        if (s_todo_lock != NULL) xSemaphoreGive(s_todo_lock);
        return ESP_ERR_NO_MEM;
    }
    err = todo_render(agent_todo_phases(), json_root(doc));
    if (s_todo_lock != NULL) xSemaphoreGive(s_todo_lock);
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
    ctx.ask.ask = agent_ask_call;
    build_tools_json(&ctx);

    snprintf(s_run_id, sizeof s_run_id, "%s", job->run_id);
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
            /* An abort is `llm_abort`'s own return code and shares
             * `ESP_ERR_INVALID_STATE` with the budget refusal; only `s_cancel` tells
             * them apart. Reporting `E_LLM_RATE` for a cancelled run would blame the
             * budget for the operator's own Esc. */
            if (s_cancel) {
                emit_agent_error(s_run_id, job->reply_to, "E_CANCELLED",
                                 "run cancelled", false);
                goto done;
            }
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

        /* The trace is emitted as soon as the reply is parsed, before any branch, so
         * a round that ends in a tool call still shows its reasoning. A model that
         * returns no trace costs no frame (`04-protocol.md` §3.21). */
        emit_agent_thinking(s_run_id, rounds + 1, s_reply);

        prompt_tokens += s_reply->prompt_tokens;
        completion_tokens += s_reply->completion_tokens;

        if (s_reply->call_count == 0) {
            /* The close-out gate (`02-firmware.md` §4.1 step 5). While it fails and
             * rounds remain, take another round with the frozen message appended;
             * a run never finalizes with work it did not account for. */
            /* The list is read here on the agent task and written by the link task
             * through `agent_todo_apply` (§4.27), so this read takes the same lock:
             * without it a `/todo` mid-closeout could hand the check a half-committed
             * list and let a run finalize with tasks it never accounted for. */
            if (s_todo_lock != NULL) xSemaphoreTake(s_todo_lock, portMAX_DELAY);
            closeout_verdict_t verdict =
                agent_closeout_check(agent_todo_phases(), rounds, ever_initialised);
            if (s_todo_lock != NULL) xSemaphoreGive(s_todo_lock);
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
            /* Truncated arguments must never reach a tool: the JSON that did arrive
             * is a prefix, and executing it would act on a half-written path or pin. */
            if (s_reply->calls[i].args_truncated) {
                const char *err_msg = "tool call arguments were truncated by the output limit";
                emit_agent_tool_result(s_run_id, rounds, &s_reply->calls[i], false, NULL,
                                       "E_TOOL", err_msg, 0);
                history_push_tool_result(&s_reply->calls[i], false, NULL, "E_TOOL",
                                         err_msg);
                continue;
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
                     s_reply->content_len,
                     strcmp(s_reply->finish_reason, AGENT_FINISH_LENGTH) == 0);
    finalized = true;

done:
    /* A cancelled or failed run leaves no half-finished turns behind: a replayed
     * `tool` message whose matching call was dropped is refused by the provider, and
     * a question with no answer would poison every later run. */
    if (!finalized) agent_history_truncate(s_history, &s_history_used, history_floor);
    /* Autocompact runs here, before `s_busy` is cleared, so a `/compact` that arrives
     * now is still refused rather than rewriting the three buffers this call is
     * using. `compact_impl` is the body of `/compact` without its busy check, which is
     * what makes this the same operation instead of a second one that could drift.
     *
     * The history is rewritten only between runs: a compaction inside one would
     * replace the `assistant` tool-call turn and its `tool` results, and a provider
     * refuses a history whose results no longer answer a call.
     *
     * A failed compaction is logged and the answer stands: the run succeeded, and
     * turning a full history into an error would blame the question for it.
     *
     * The measure is `agent_context_usage`'s, not the history's own length: the prompt
     * and the tool array are in every body too, and they are 8.4 KiB of a 32 KiB
     * budget. A history-only figure would fire at 26 KiB of history, by which point
     * the body was already over. It is also the number `/context` shows, so the
     * threshold a reader sets is the percentage of the figure they can see. */
    if (s_autocompact_pct < AGENT_AUTOCOMPACT_MAX) {
        agent_context_usage_t usage;
        if (agent_context_usage(&usage) == ESP_OK &&
            agent_autocompact_due(usage.used, usage.budget, s_autocompact_pct)) {
            uint32_t summarised = 0, kept = 0;
            size_t before = 0, after = 0;
            esp_err_t cerr = compact_impl(0, &summarised, &kept, &before, &after,
                                          s_autocompact_summary,
                                          AGENT_COMPACT_SUMMARY_MAX + 1);
            if (cerr == ESP_OK) {
                /* Two figures, both named: the share that fired the check, and the
                 * history bytes it replaced. Unlabelled, "88 -> 1870 bytes" beside
                 * "26% used" reads as a contradiction — the 88 is the history and the
                 * 26% is the whole body, and a summary can honestly be longer than a
                 * two-word exchange. */
                ESP_LOGI(TAG, "autocompact at %d%%: body at %u%%; history %u -> %u bytes, "
                              "%u messages summarised", (int)s_autocompact_pct,
                         (int)((usage.used * 100u) / usage.budget), (unsigned)before,
                         (unsigned)after, (unsigned)summarised);
            } else if (cerr != ESP_ERR_NOT_SUPPORTED) {
                /* `NOT_SUPPORTED` is "one message or fewer": nothing to replace, the
                 * ordinary case for a short history. Anything else is news. */
                ESP_LOGW(TAG, "autocompact: %s", esp_err_to_name(cerr));
            }
        }
    }
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
    s_system_prompt = agent_alloc(AGENT_INPUT_BUFFER_MAX + 1);
    s_tools_json = agent_alloc(TOOLS_MAX_TOTAL * 512);
    s_response = agent_alloc(LLM_BODY_MAX + 1);
    s_final_text = agent_alloc(AGENT_TEXT_MAX + 1);
    s_reply = (agent_reply_t *)agent_alloc(sizeof(agent_reply_t));
    s_compact_buf = agent_alloc(AGENT_HISTORY_MAX);
    /* Autocompact's summary only needs somewhere to land: the counts are what the log
     * line reports. It is a buffer rather than a local because 4 KiB more on the 8 KiB
     * agent task stack is an overflow, not a cost. */
    s_autocompact_summary = agent_alloc(AGENT_COMPACT_SUMMARY_MAX + 1);
    if (s_history == NULL || s_system_prompt == NULL || s_tools_json == NULL ||
        s_response == NULL || s_final_text == NULL || s_reply == NULL ||
        s_compact_buf == NULL || s_autocompact_summary == NULL) {
        heap_caps_free(s_history);
        heap_caps_free(s_system_prompt);
        heap_caps_free(s_tools_json);
        heap_caps_free(s_response);
        heap_caps_free(s_final_text);
        heap_caps_free(s_reply);
        heap_caps_free(s_compact_buf);
        heap_caps_free(s_autocompact_summary);
        s_compact_buf = NULL;
        s_autocompact_summary = NULL;
        return ESP_ERR_NO_MEM;
    }

    s_todo_lock = xSemaphoreCreateMutex();
    if (s_todo_lock == NULL) return ESP_ERR_NO_MEM;

    s_queue = xQueueCreate(AGENT_MAX_QUEUE, sizeof(agent_job_t));
    if (s_queue == NULL) return ESP_ERR_NO_MEM;

    todo_list_clear(agent_todo_phases());

    /* Build the prompt and the tool array once here, before the agent task exists, so
     * `context.budget` (`04` §4.30) can answer on a device that has run nothing yet.
     * `run_once` rebuilds both per run, so this is the pre-first-run value rather than
     * a cached one; doing it here rather than lazily on the link task is what keeps it
     * off a buffer a run could be writing. */
    load_run_settings();
    build_system_prompt();
    tools_context_t boot_ctx;
    tools_context_from_config(&boot_ctx);
    boot_ctx.todo.apply = todo_port_apply;
    if (s_ask_done == NULL) s_ask_done = xSemaphoreCreateBinary();
    boot_ctx.ask.ask = agent_ask_call;
    build_tools_json(&boot_ctx);

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
                       const char *reply_to, char *run_id_out, size_t run_id_cap)
{
    /* Cleared up front so a refusal never leaves the caller reading a stale id from
     * a previous run of its own buffer. */
    if (run_id_out != NULL && run_id_cap > 0) run_id_out[0] = '\0';
    if (prompt == NULL || reply_to == NULL) return ESP_ERR_INVALID_ARG;
    /* Its own code: sharing `ESP_ERR_INVALID_STATE` with the full queue told the
     * bridge to retry a boot-time allocation failure (`04` §3.3 E_BUSY is
     * `retryable:true`; a missing loop is not). */
    if (s_queue == NULL) return ESP_ERR_NOT_FOUND;

    load_run_settings();
    if (len > s_input_max || len > AGENT_INPUT_BUFFER_MAX) {
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
    /* Minted before the enqueue and under the same single producer the queue
     * already serialises: two submits cannot be handed the same id, and the id is
     * fixed before any task can start the run. */
    snprintf(job.run_id, sizeof job.run_id, "r-%" PRIu32, s_run_counter++);

    if (xQueueSend(s_queue, &job, 0) != pdTRUE) {
        heap_caps_free(copy);
        return ESP_ERR_INVALID_STATE;
    }
    if (run_id_out != NULL && run_id_cap > 0) {
        snprintf(run_id_out, run_id_cap, "%s", job.run_id);
    }
    return ESP_OK;
}

bool agent_cancel(void)
{
    if (!s_busy) return false;
    s_cancel = true;
    llm_abort();
    return true;
}

void agent_status(agent_status_t *out)
{
    if (out == NULL) return;
    out->busy = s_busy;
    out->run_id = s_busy ? s_run_id : NULL;
    out->queue_depth = s_queue != NULL ? (int32_t)uxQueueMessagesWaiting(s_queue) : 0;
}

/* `04` §4.30: the eight blocks of the next request. Every block is a measurement of a
 * buffer the loop already holds, so the reply costs one walk of the history and no
 * allocation. `used` adds the history's own `[`, `]` and the commas between messages,
 * because those bytes are part of the body too and a budget that ignored them would be
 * optimistic exactly where the reader is deciding whether to clear.
 *
 * `free` floors at 0: a device whose prompt and history together exceed `LLM_BODY_MAX`
 * has a real problem, and reporting a negative number would hide it behind a wrap. */
esp_err_t agent_context_usage(agent_context_usage_t *out)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    if (s_history == NULL || s_system_prompt == NULL || s_tools_json == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    /* `s_history_used == 0` is the authoritative "nothing written yet" (`04` §4.28):
     * the buffer is uninitialized then, so parsing it would read whatever the
     * allocation held and the first `/context` after a boot would fail. Every other
     * reader guards the same way. (`agent_start` builds the prompt and the tool array
     * once before the agent task exists, so the two blocks that live in those buffers
     * are already meaningful here without a rebuild that could race a run.) */
    esp_err_t err = agent_history_measure(s_history_used > 0 ? s_history : "[]", out);
    if (err != ESP_OK) return err;

    out->system_prompt = strlen(s_system_prompt);
    out->skills = s_skills_bytes > out->system_prompt ? out->system_prompt : s_skills_bytes;
    out->tools = strlen(s_tools_json);
    out->thinking = 0;
    out->count_skills = s_skill_count;
    out->count_tools = s_tool_count;
    out->count_thinking = 0;

    size_t used = out->system_prompt + out->tools + out->user + out->assistant +
                  out->tool_calls + out->tool_output;
    if (out->messages > 1) used += out->messages - 1; /* the commas */
    if (out->messages > 0) used += 2;                /* `[` and `]` */

    out->used = used;
    out->budget = LLM_BODY_MAX;
    out->free = used < LLM_BODY_MAX ? LLM_BODY_MAX - used : 0;
    return ESP_OK;
}

/* `04` §4.32: one block of the next request, as text.
 *
 * The three prompt-side blocks are slices of buffers the builders own, so this
 * copies rather than reading past them; the five history blocks go through the pure
 * extractor. `system_prompt` carries the skill index because the index was appended
 * into that same buffer, which `04` §4.30 also says. */
esp_err_t agent_context_block(const char *block, char *out, size_t cap,
                              size_t *written, size_t *total)
{
    if (block == NULL || out == NULL || cap == 0) return ESP_ERR_INVALID_ARG;
    if (s_history == NULL || s_system_prompt == NULL || s_tools_json == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (written != NULL) *written = 0;
    if (total != NULL) *total = 0;

    /* The two prompt-side blocks and the tool array are NUL-terminated buffers, so
     * they share one clip. A `system_prompt` above the cap is a prompt nobody can
     * read in a modal, and reporting the whole length keeps `truncated` honest. */
    const char *whole = NULL;
    if (strcmp(block, "system_prompt") == 0) {
        whole = s_system_prompt;
    } else if (strcmp(block, "skills") == 0) {
        /* The index is a suffix of the prompt: its start is the prompt's end minus its
         * measured length. An index longer than the prompt cannot happen, but the
         * clamp is what keeps a stale `s_skills_bytes` from reading before the
         * buffer. */
        size_t plen = strlen(s_system_prompt);
        size_t slen = s_skills_bytes > plen ? plen : s_skills_bytes;
        whole = s_system_prompt + (plen - slen);
    } else if (strcmp(block, "tools") == 0) {
        whole = s_tools_json;
    }

    if (whole != NULL) {
        size_t len = strlen(whole);
        size_t n = len < cap - 1 ? len : cap - 1;
        if (n > 0) memcpy(out, whole, n);
        out[n] = '\0';
        if (written != NULL) *written = n;
        if (total != NULL) *total = len;
        return ESP_OK;
    }

    /* `thinking` is a measured zero (`04` §4.30): the trace never enters the history,
     * so there is no text to return and an empty block is the truthful answer. */
    if (strcmp(block, "thinking") == 0) {
        out[0] = '\0';
        return ESP_OK;
    }

    const char *role = NULL;
    if (strcmp(block, "user") == 0) {
        role = "user";
    } else if (strcmp(block, "assistant") == 0) {
        role = "assistant";
    } else if (strcmp(block, "tool_calls") == 0) {
        role = "tool_calls"; /* not a role: the extractor selects assistant turns */
    } else if (strcmp(block, "tool_output") == 0) {
        role = "tool";
    } else {
        /* `04` §4.32: a mistyped block is `E_CONFIG`, never an empty block. */
        return ESP_ERR_INVALID_ARG;
    }
    return agent_history_block(s_history_used > 0 ? s_history : "[]", role, out, cap,
                               written, total);
}

/* `04` §4.33: replace the conversation with a model-written summary of it.
 *
 * Board-only, and therefore tested on the target rather than here: it calls
 * `llm_query`, which needs the network stack and the provider config. The two
 * selection and rewriting steps it depends on are pure and live in `agent_history.c`,
 * where the host suite exercises them.
 *
 * Nothing is written until a usable summary is in hand, which is what makes the
 * command safe to send on a marginal radio: a provider failure, a budget refusal or an
 * empty answer leaves the conversation exactly as it was. */
static esp_err_t compact_impl(uint32_t keep, uint32_t *summarised, uint32_t *kept,
                             size_t *before_bytes, size_t *after_bytes,
                             char *summary, size_t summary_cap)
{
    if (summarised != NULL) *summarised = 0;
    if (kept != NULL) *kept = 0;
    if (before_bytes != NULL) *before_bytes = 0;
    if (after_bytes != NULL) *after_bytes = 0;
    if (summary != NULL && summary_cap > 0) summary[0] = '\0';
    if (summary == NULL || summary_cap == 0) return ESP_ERR_INVALID_ARG;
    if (s_history == NULL || s_system_prompt == NULL || s_tools_json == NULL ||
        s_compact_buf == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (keep > AGENT_COMPACT_KEEP_MAX) return ESP_ERR_INVALID_ARG;

    size_t count = agent_history_count(s_history);
    if (count <= (size_t)keep) {
        /* Nothing would be replaced, so a summary here would be a summary of nothing
         * and the caller would be told the history shrank when it did not. */
        return ESP_ERR_NOT_SUPPORTED;
    }

    size_t before = s_history_used;
    size_t to_summarise = count - (size_t)keep;

    /* The messages the model reads: every message except the newest `keep`, which are
     * never both summarised and kept (`04` §4.33). */
    /* One buffer for both halves: it holds the text sent for summarising, and once
     * that call has returned nothing reads it again, so the rewrite reuses it as
     * scratch. It sits in PSRAM with the other agent buffers rather than being a
     * `static`: 8 KiB of DIRAM here left the internal heap below the 8 KiB the agent
     * task's stack needs, so `xTaskCreate` failed and every `context.*` command
     * answered `E_INTERNAL` on a board that had booted. */
    char *buf = s_compact_buf;
    size_t in_written = 0, in_total = 0;
    esp_err_t err = agent_history_slice(s_history, keep, buf, AGENT_HISTORY_MAX,
                                        &in_written, &in_total);
    if (err != ESP_OK) return err;

    /* One call, no tools: the model is summarising, not continuing, so the fixed
     * prompt says so and the tool array is withheld. `s_response` is the run's buffer
     * and is free here because `s_busy` is false. */
    llm_request_t request = {
        .model = NULL,
        .token_param = NULL,
        .max_output_tokens = 0,
        .system_prompt =
            "You summarise a conversation so another model can continue it. Write "
            "Markdown under these headings: Goal, Progress, Decisions, Next Steps. "
            "State facts the conversation established, paths and identifiers it named, "
            "and what remains unfinished. Do not answer the conversation, do not "
            "continue it, and do not invent anything it does not say.",
        .history_json = NULL,
        .user_prompt = buf,
        .tools_json = NULL,
    };
    s_response[0] = '\0';
    err = llm_query(&request, s_response, LLM_BODY_MAX + 1, NULL);
    /* `llm_query` answers `ESP_ERR_INVALID_STATE` for an exhausted call budget, and the
     * busy guard above answers the same code; a caller that mapped the pair together
     * would tell a reader "a run is in flight" when the budget is what refused. The
     * distinct code is the whole reason this line exists. */
    if (err == ESP_ERR_INVALID_STATE) return ESP_ERR_INVALID_RESPONSE;
    if (err != ESP_OK) return err;

    /* The reply is the model's prose, which `agent_parse_reply` would reject: it is
     * looking for the `agent.reply` shape. Trim the whitespace and take it whole, and
     * treat an empty answer as `E_TOOL` rather than storing a blank checkpoint. */
    char *text = s_response;
    while (*text == ' ' || *text == '\t' || *text == '\n' || *text == '\r') text++;
    size_t len = strlen(text);
    while (len > 0) {
        char c = text[len - 1];
        if (c != ' ' && c != '\t' && c != '\n' && c != '\r') break;
        text[--len] = '\0';
    }
    if (len == 0) return ESP_ERR_NOT_FOUND;
    if (len >= summary_cap) {
        /* A summary cut mid-sentence is a checkpoint that silently lost its ending,
         * which is worse than having compacted nothing (`04` §4.33). */
        return ESP_ERR_INVALID_SIZE;
    }

    /* Rewritten only now, into a scratch so a buffer that cannot hold the result is
     * left alone. The scratch is the block buffer from above, already spent. */
    size_t new_used = s_history_used;
    err = agent_history_replace_with_summary(s_history, AGENT_HISTORY_MAX, &new_used,
                                             text, keep, buf, AGENT_HISTORY_MAX);
    if (err != ESP_OK) return err;
    s_history_used = new_used;

    snprintf(summary, summary_cap, "%s", text);
    if (summarised != NULL) *summarised = (uint32_t)to_summarise;
    if (kept != NULL) *kept = (uint32_t)keep;
    if (before_bytes != NULL) *before_bytes = before;
    if (after_bytes != NULL) *after_bytes = s_history_used;
    return ESP_OK;
}

/* `04` §4.33's public entry. The busy check is the whole of it, and it lives here
 * rather than inside `compact_impl` because the loop compacts itself at the end of a
 * run, where `s_busy` is still set. Sharing the body is what makes autocompact the
 * same operation `/compact` performs rather than a second implementation of it; the
 * refusal is what keeps a `/compact` from rewriting `s_history`, `s_response` and
 * `s_compact_buf` while the loop owns all three.
 *
 * `E_BUSY`: a run appends to and trims this buffer between rounds, and a rewrite
 * under it is one whose tool results no longer answer its calls. */
esp_err_t agent_context_compact(uint32_t keep, uint32_t *summarised, uint32_t *kept,
                                size_t *before_bytes, size_t *after_bytes,
                                char *summary, size_t summary_cap)
{
    if (s_busy) {
        if (summarised != NULL) *summarised = 0;
        if (kept != NULL) *kept = 0;
        if (before_bytes != NULL) *before_bytes = 0;
        if (after_bytes != NULL) *after_bytes = 0;
        if (summary != NULL && summary_cap > 0) summary[0] = '\0';
        return ESP_ERR_INVALID_STATE;
    }
    return compact_impl(keep, summarised, kept, before_bytes, after_bytes, summary,
                         summary_cap);
}

/* `04` §4.28: the one writer from outside a run. Refused while one is live, because
 * the run appends its round to this same buffer and a tool result dropped behind its
 * own call is a message the provider rejects. `agent_history_clear` is pure and
 * host-tested; this is the borrow plus the refusal. */
esp_err_t agent_history_reset(size_t *dropped)
{
    if (dropped != NULL) *dropped = 0;
    if (s_busy || s_history == NULL) return ESP_ERR_INVALID_STATE;
    if (dropped != NULL) *dropped = agent_history_count(s_history);
    return agent_history_clear(s_history, &s_history_used);
}

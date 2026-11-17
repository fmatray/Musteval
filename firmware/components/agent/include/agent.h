/* agent — the serialized run loop, its todo list and its conversation history.
 * Layering: L3 (`08-code-architecture.md` §1), above `llm`, `tools`, `skills`.
 * Contracts: `02-firmware.md` §4 (loop, close-out), `04-protocol.md` §3.11-§3.15
 * (frames), `tools/todo.md` (the list the close-out check reads).
 *
 * One run at a time. Everything that decides when a run may start, when it may
 * finalize, and what its frames say is pure and lives in `agent_todo.c`,
 * `agent_closeout.c`, `agent_history.c` and `agent_reply.c`, which build for the
 * host. `agent.c` owns the queue, the FreeRTOS task and the `link` writes. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "store.h"

/* `tools/todo.md` §Bounds. A breach is `E_TOOL` with the list unchanged, never a
 * truncation, so the model's copy and the heap copy cannot disagree. */
#define AGENT_TODO_MAX_PHASES 8
#define AGENT_TODO_MAX_TASKS  32
#define AGENT_TODO_CONTENT_MAX 120
#define AGENT_TODO_PHASE_MAX   40
#define AGENT_TODO_BLOCKER_MAX 120

/* `04-protocol.md` §3.14: `text` is shortened to fit 16384 bytes and `truncated`
 * reports it. The bound is the frame cap, not the body cap. */
#define AGENT_TEXT_MAX 16384

/* `04-protocol.md` §3.21: the reasoning trace rides `agent.thinking` instead of
 * `agent.final`, and its own bound is this one. 2048 is the largest trace whose frame
 * can never be refused: JSON escaping is at worst 6 bytes per source byte (`\u00XX`),
 * so 2048 * 6 + the ~200-byte envelope is inside `LINK_TX_MAX`. A trace cut here is
 * reported through `truncated`/`full_bytes`, and the frame is advisory, so shortening
 * it costs the reader a preview rather than the answer. It matches `TOOLS_TEXT_MAX`,
 * which is the same kind of "human-readable preview in one frame" bound. */
#define AGENT_THINKING_MAX 2048

/* Runs accepted but not yet started. `04` §3.15's `E_BUSY` example names 2. */
#define AGENT_MAX_QUEUE 2

typedef enum {
    TODO_PENDING = 0,
    TODO_IN_PROGRESS,
    TODO_COMPLETED,
    TODO_ABANDONED,
    TODO_BLOCKED,
} todo_status_t;

const char *todo_status_name(todo_status_t status);

typedef struct {
    char content[AGENT_TODO_CONTENT_MAX + 1];
    todo_status_t status;
    bool has_blocker;
    char blocker[AGENT_TODO_BLOCKER_MAX + 1];
    /* Index into `todo_list_t.phases`. Tasks are stored grouped by phase, in
     * phase/task order, so "the first in phase/task order" is an array walk. */
    uint8_t phase_index;
} todo_task_t;

/* The whole checklist. Flat rather than an array of phases holding their own task
 * arrays: nested, the fixed bound would be 8 x 32 tasks (~63 KiB), which is more
 * than half of DIRAM. Flat is 32 tasks (~8 KiB) and the same rules. */
typedef struct {
    char phases[AGENT_TODO_MAX_PHASES][AGENT_TODO_PHASE_MAX + 1];
    size_t phase_count;
    todo_task_t tasks[AGENT_TODO_MAX_TASKS];
    size_t task_count;
} todo_list_t;

/* One op needs a pre-image to fall back to; both fit comfortably in statics. */
_Static_assert(sizeof(todo_list_t) < 12288, "todo_list_t must stay near 8 KiB");

/* Total tasks across every phase. */
size_t todo_task_total(const todo_list_t *list);

void todo_list_clear(todo_list_t *list);

/* One op from `tools/todo.md` §Operations applied to `list`.
 *
 * `args` is the tool's argument object. On any failure the list is byte-identical to
 * what it was on entry: the op is applied to a scratch copy and committed only on
 * success, which is what makes "list unchanged" true rather than merely intended.
 * `view` never writes.
 *
 * Returns ESP_OK, ESP_ERR_INVALID_ARG for a schema or targeting failure, or
 * ESP_ERR_INVALID_SIZE for a bound breach; all three are `E_TOOL` on the wire.
 * `why`, when non-NULL, receives a short reason for the refusal `message`. */
esp_err_t todo_apply(todo_list_t *list, const json_node_t *args, char *why,
                     size_t why_cap);

/* Render the list as `tools/todo.md` §Result:
 * `{"phases":[{"name":…,"tasks":[{"content":…,"status":…}]}]}`. */
esp_err_t todo_render(const todo_list_t *list, json_node_t *out);

/* --- Close-out (`02-firmware.md` §4.1 step 5) ------------------------------ */

typedef enum {
    CLOSEOUT_OK = 0,
    CLOSEOUT_OPEN_TASKS,   /* a task is pending, in_progress or blocked */
    CLOSEOUT_UNTRACKED,    /* two or more tool rounds, never opened a list */
} closeout_verdict_t;

/* The check itself. `tool_rounds` is the rounds used so far in this run, and
 * `ever_initialised` says a `todo init` ran at any point in it — a list emptied by
 * `done` is tracked, a list never opened is not. Pure. */
closeout_verdict_t agent_closeout_check(const todo_list_t *list, int tool_rounds,
                                        bool ever_initialised);

/* The synthetic message appended when the check fails (`02-firmware.md` §4.1),
 * or NULL for CLOSEOUT_OK. Frozen prose: the loop's control and the doc agree
 * because both read this one string. */
const char *agent_closeout_prompt(closeout_verdict_t verdict);

/* `false` only when the run may finalize with a clean list. `02-firmware.md`
 * §4.1/§4.2: true means the budget ran out with the check still failing. */
bool agent_todo_open_flag(closeout_verdict_t verdict, bool budget_spent);

/* `/todo` (`04` §4.27): apply one `tools/todo.md` §Operations op to the run's list
 * and render the result. This *is* the `todo` tool's apply path, not a copy of it —
 * same op set, same bounds, same all-or-nothing rule, same lock — so the command and
 * the model cannot end up with two opinions of what `start` means. `out` receives
 * `tools/todo.md` §Result; `view` writes nothing.
 *
 * Returns ESP_OK, ESP_ERR_INVALID_ARG (schema or targeting), ESP_ERR_INVALID_SIZE
 * (bound breach) or ESP_ERR_NO_MEM (the result could not be built). The two
 * refusals are `E_TOOL` on the wire and `NO_MEM` is `E_INTERNAL`. */
esp_err_t agent_todo_apply(const json_node_t *args, json_doc_t **out, char *why,
                           size_t why_cap);

/* --- History (`02-firmware.md` §4.1 step 6, `llm.max_history_messages`) ----- */

/* A conversation is a JSON array of `{"role":…,"content":…}` messages, plus the
 * provider's own assistant/user turns. It is kept as text because it is handed to
 * `llm` verbatim as `history_json`, and re-encoding it every round would copy the
 * whole conversation twice.
 *
 * `max_messages` is `llm.max_history_messages`. When appending would exceed it, the
 * oldest message is dropped first (`02-firmware.md` §4.2 "Buffer Full"). A value of
 * 0 keeps no history at all. Returns the new length, or 0 when the message does not
 * fit `cap`; a message that does not fit is dropped rather than written half.
 *
 * `pin_head` is forwarded to every trim this call performs. The loop passes true
 * while it is answering a question, so the question cannot be evicted by the rounds
 * it is paying for; the append is refused instead when the buffer cannot hold it. */
esp_err_t agent_history_append(char *history, size_t cap, size_t *used,
                               const char *message_json, int32_t max_messages,
                               bool pin_head);

/* Drop the oldest messages until at most `max_messages` remain. Pure; `history` is
 * a JSON array of objects.
 *
 * `pin_head` keeps message 0 out of the drop set: while a run is answering it, the
 * question is the only message whose loss makes every later round incoherent
 * (`02-firmware.md` §4.1). The total stays at most `max_messages`, so a pinned head
 * leaves `max_messages - 1` movable slots; with nothing but the head left the call is
 * a no-op. */
esp_err_t agent_history_trim(char *history, size_t *used, int32_t max_messages,
                             bool pin_head);

/* `04` §4.30: what one `context.budget` reply reports. Every field is measured, never
 * estimated — a byte count the reader is meant to act on ("you are near the cap") has
 * to be the count the loop will actually send, or the number is decoration.
 *
 * `skills` is the part of `system_prompt` this build appended: the index is written
 * into the same buffer (`build_system_prompt`), so the two are reported as a whole and
 * a part, not as two independent blocks. `free` is `LLM_BODY_MAX` minus every measured
 * block and the frame's own envelope, and it is a fact about the *next* request: the
 * prompt, the history and the tool array are all rebuilt per round. */
typedef struct {
    size_t system_prompt; /* bytes of `agent.system_prompt`, escaped size not counted */
    size_t skills;        /* the appended skill index, a suffix of the above */
    size_t tools;         /* the tool array spliced into `tools` */
    size_t user;          /* `role:"user"` messages in the history */
    size_t assistant;     /* `role:"assistant"` content, tool calls excluded */
    size_t thinking;      /* always 0: a trace is never appended to the history */
    size_t tool_calls;    /* the `tool_calls` arrays on assistant turns */
    size_t tool_output;   /* `role:"tool"` messages */
    size_t used;          /* the sum of the eight blocks, plus the array's own syntax */
    size_t free;          /* `LLM_BODY_MAX` minus `used` */
    size_t budget;        /* `LLM_BODY_MAX`, so a client needs no second source */
    size_t messages;      /* history messages measured */

    /* `04` §4.30 `counts`: how many items each block holds, beside how many bytes.
     * A reader asking what is in the request wants "18 tools", and a byte count
     * cannot answer that. The history-derived five are filled by
     * `agent_history_measure`; `skills` and `tools` are filled by the caller that
     * built those blocks, because neither lives in the history. */
    size_t count_skills;     /* skills the index had room for, not the files on disk */
    size_t count_tools;      /* the array `tools_json` actually built */
    size_t count_user;       /* `role:"user"` messages */
    size_t count_assistant;  /* `role:"assistant"` messages */
    size_t count_thinking;   /* always 0, for the reason `thinking` is */
    size_t count_tool_calls; /* `tool_calls` entries, not assistant messages */
    size_t count_tool_output;/* `role:"tool"` messages */
} agent_context_usage_t;

/* Classify each history message by its `role` into `out`. Pure: the history is text
 * the loop already holds, and the classification is arithmetic over it, so this is a
 * host test rather than a claim about the board.
 *
 * `thinking` is always 0. The reasoning trace rides its own `agent.thinking` frame
 * (`04` §3.21) and is never pushed to the history, so it consumes no part of the
 * request body — reporting it as a block with a byte count would be a lie that grows
 * when the model thinks more. */
esp_err_t agent_history_measure(const char *history, agent_context_usage_t *out);

/* How many messages the array holds. Pure. */
size_t agent_history_count(const char *history);

/* Keep only the first `keep` messages, in place. An aborted run calls this at the
 * depth it started from so its unfinished turns never replay. Pure. */
esp_err_t agent_history_truncate(char *history, size_t *used, uint32_t keep);

/* Empty the buffer in place: `[]`, and `*used` left at 2 rather than 0, because 0
 * means "never written" to `agent_history_append` and it would then rescan
 * uninitialized bytes. Pure, so the pair `clear` then `append` is a host test rather
 * than a claim about the board. */
esp_err_t agent_history_clear(char *history, size_t *used);

/* Collect the messages whose `role` matches into `out`, one per line, in history
 * order. `04` §4.32: a block's content is its messages, not the array wrapper and
 * its commas, so this writes each message's own JSON text separated by `\n`.
 *
 * `role` of `NULL` selects every message, which is what the `tool_calls` and
 * `tool_output` blocks want. `tool_calls` selects the assistant turns that carry
 * calls, so a reader sees the calls in the turn that made them.
 *
 * Writes at most `cap - 1` bytes and always NUL-terminates; `*written` is what was
 * written and `*total` how long the full text would have been, so a caller can report
 * `truncated` without a second pass. Returns `ESP_ERR_INVALID_SIZE` when `cap` is 0,
 * which is the only case where nothing can be reported. Pure. */
esp_err_t agent_history_block(const char *history, const char *role, char *out,
                              size_t cap, size_t *written, size_t *total);

/* The serialised conversation for the summarisation call: every message except the
 * newest `keep`, one per line, in history order. `04` §4.33 applies `keep` before
 * summarising, so the messages the summary will sit beside are never also summarised.
 *
 * Same contract as `agent_history_block`: at most `cap - 1` bytes, NUL-terminated,
 * with `*written` and `*total` so the caller can report truncation. Pure. */
esp_err_t agent_history_slice(const char *history, uint32_t keep, char *out,
                              size_t cap, size_t *written, size_t *total);

/* Replace the whole buffer with a summary message followed by the newest `keep`
 * messages verbatim. `04` §4.33: the summary is `role:"user"` because a provider
 * rejects a history opening on `assistant` and a `system` message would be a second
 * persona beside the prompt, and it carries a trailing sentence so the model reads it
 * as history rather than as a fresh request.
 *
 * Refused with `ESP_ERR_INVALID_SIZE` when the result would not fit `cap`, leaving
 * `history` untouched: a half-written buffer is a conversation that cannot be sent.
 * Pure, so the rewrite is a host test rather than a claim about the board. */
esp_err_t agent_history_replace_with_summary(char *history, size_t cap, size_t *used,
                                             const char *summary, uint32_t keep,
                                             char *scratch, size_t scratch_cap);

/* The newest `keep` messages, one per line, in history order: what
 * `agent_history_replace_with_summary` keeps verbatim beside the summary. Same
 * contract as its siblings. Pure. */
esp_err_t agent_history_suffix(const char *history, uint32_t keep, char *out,
                               size_t cap, size_t *written, size_t *total);

/* --- Provider reply (`llm_assemble_prompt`'s inverse) ---------------------- */

/* One tool call the model asked for, with its arguments left as the provider's own
 * JSON text: `tools` validates them, so this layer never re-parses.
 *
 * `args_truncated` marks a call whose `arguments` string was cut by the provider's
 * output limit: the call is kept, with `args` empty, so the loop can answer it with
 * a `tool` message naming the cause. Dropping it instead would leave the assistant
 * turn referencing a `tool_call_id` that never gets a result, which providers reject
 * for the rest of the run. */
typedef struct {
    char id[64];
    char name[65];
    char args[2048];
    bool args_truncated;
} agent_tool_call_t;

#define AGENT_MAX_TOOL_CALLS 4

typedef struct {
    char content[AGENT_TEXT_MAX];
    size_t content_len;
    agent_tool_call_t calls[AGENT_MAX_TOOL_CALLS];
    size_t call_count;
    int32_t prompt_tokens;
    int32_t completion_tokens;
    /* `choices[0].finish_reason` (`02-firmware.md` §4.1). `AGENT_FINISH_LENGTH` means
     * the provider stopped at the output token cap, so `content` is a prefix and any
     * tool call it cut is incomplete. Empty when the provider sent no reason. */
    char finish_reason[24];
    /* The model's reasoning trace, from `message.thinking`, `reasoning_content` or
     * `reasoning` — the three names the providers in `03-config.md` §Provider
     * compatibility use. It is *never* the answer: `agent.c` emits it as its own
     * `agent.thinking` frame (`04-protocol.md` §3.21). `thinking_len` is 0 when the
     * reply carried no trace, and then no frame is emitted at all.
     *
     * A fixed 4096 here rather than PSRAM: one buffer per run, and the struct is
     * already heap-allocated by `agent_alloc`. */
    char thinking[AGENT_THINKING_MAX];
    size_t thinking_len;
    /* The trace's real length when `thinking` had to be cut, else 0. Kept here rather
     * than recomputed in `agent.c`: the parse is the only place that sees the whole
     * string, and a silent cut would reach the reader as a complete trace. */
    size_t thinking_full;
} agent_reply_t;

/* The `finish_reason` that means "output cap reached". Frozen because the loop
 * branches on it: a value read differently here and in the doc turns a truncated
 * reply into a finished one. */
#define AGENT_FINISH_LENGTH "length"

/* Parse one provider response body into `out`. Accepts the `choices[0].message`
 * shape every provider in `03-config.md` §Provider compatibility returns.
 *
 * Returns ESP_OK, or ESP_ERR_INVALID_ARG when the body is not a chat completion —
 * which the loop reports as `E_LLM_UNAVAILABLE`, because a malformed reply is a
 * provider fault and the next provider may answer (`02-firmware.md` §4.2).
 * Arguments that overflow their field are refused, not clamped: a silently
 * truncated argument would reach a tool as a different call. */
esp_err_t agent_parse_reply(const char *body, size_t len, agent_reply_t *out);

/* --- Run control (board) -------------------------------------------------- */

typedef enum {
    AGENT_SOURCE_PROMPT = 0,
    AGENT_SOURCE_TASK,
    AGENT_SOURCE_TEMPLATE,
} agent_source_t;

const char *agent_source_name(agent_source_t source);

/* The rejection a `prompt` frame carries, per `04-protocol.md` §3.3: the code, the
 * message, `retryable`, and whether the `detail` object names the queue depth. Pure,
 * so the frame handler's answers are host-tested (`agent_prompt.c`). */
typedef struct {
    esp_err_t err;
    const char *code;
    const char *message;
    bool retryable;
    bool queue_detail;
} agent_rejection_t;

const agent_rejection_t *agent_prompt_rejection(esp_err_t err);

/* `03-config.md` §agent: the documented defaults, and the input buffer's hard
 * ceiling. These are the numbers the document promises; a default that disagrees with
 * it is a defect in the code, not in the document. */
#define AGENT_DEFAULT_MAX_INPUT_BYTES 3072
#define AGENT_DEFAULT_MAX_TOOL_ROUNDS 8
#define AGENT_DEFAULT_MAX_HISTORY     8

/* The input buffer's own cap, above the documented default so a raised
 * `agent.max_input_bytes` still has room to grow and the whole line fits. */
#define AGENT_INPUT_BUFFER_MAX 8192

/* `04` §4.33: how many of the newest messages `/context compact` may keep verbatim
 * beside the summary. A ceiling rather than an unbounded `keep`: the point of the
 * command is to shrink the history, and a caller asking to keep everything has the
 * answer "then do not compact". */
#define AGENT_COMPACT_KEEP_MAX 8

/* `04` §4.33: the summary's own cap. Well under `LLM_BODY_MAX`, because the summary
 * has to fit the history beside the kept messages and be reported whole in one reply;
 * a summary above it is refused rather than cut, which is the difference between a
 * checkpoint that lost its ending and a compaction that did not happen. */
#define AGENT_COMPACT_SUMMARY_MAX 4096

/* What the config said, before the defaults are applied. `has_*` distinguishes an
 * absent key from a configured zero. */
typedef struct {
    bool has_max_tool_rounds;
    int32_t max_tool_rounds;
    bool has_max_history;
    int32_t max_history;
    bool has_max_input;
    int32_t max_input;
} agent_raw_settings_t;

typedef struct {
    int32_t max_tool_rounds;
    int32_t max_history_messages;
    size_t max_input_bytes;
} agent_run_settings_t;

/* Apply `03-config.md`'s defaults and clamp the input bound to the buffer. Pure, so a
 * documented default the code does not apply fails a host test (`agent_settings.c`). */
void agent_run_settings_resolve(const agent_raw_settings_t *raw,
                                agent_run_settings_t *out);

/* Hand a prompt to the loop. Returns ESP_OK when accepted, ESP_ERR_INVALID_STATE
 * when the queue is full (`E_BUSY`), ESP_ERR_INVALID_SIZE when the prompt exceeds
 * `agent.max_input_bytes`, ESP_ERR_NOT_FOUND when no loop is running, and
 * ESP_ERR_INVALID_ARG when the frame has no text. Each maps to its own
 * `04` §3.3 code through `agent_prompt_rejection`; "queue full" and "no loop" shared
 * `ESP_ERR_INVALID_STATE`, so a boot-time allocation failure was reported as a
 * retryable busy queue. `reply_to` is the frame id the run answers.
 *
 * Asynchronous by construction: the caller is the link reader task, which must not
 * block for a whole run (`02-firmware.md` §3.3).
 *
 * `run_id_out` is optional. When non-NULL and the prompt was accepted it holds the
 * run's identity, `r-<n>`, NUL-terminated and never longer than 23 characters; on a
 * refusal it holds `""`. The id is minted here rather than in the run because two
 * callers have to name the run in the reply that starts it — `tasks.run`
 * (`04` §4.5) and the `fired` frame (`04` §3.16) — and neither can observe an id
 * allocated after the enqueue. */
esp_err_t agent_submit(const char *prompt, size_t len, agent_source_t source,
                       const char *reply_to, char *run_id_out, size_t run_id_cap);

/* `04-protocol.md` §3.23: the bridge's answer to `agent.ask`. Called on the link
 * task. Returns false when the id names no question currently waiting, which is the
 * late-reply case the protocol says to ignore — a reply that lost the race with the
 * timeout is late, not malformed. */
bool agent_ask_reply(const char *ask_id, const char *answer, bool cancelled);

/* `cancel` (`04` §3.5, §4.14). Returns true when a run was live and has been
 * signalled, false when nothing was running (`cancel.noop`, `04` §3.19). */
bool agent_cancel(void);

/* The status fields `04` §3.9 reports. Borrowed values stay valid until the next
 * run starts. */
typedef struct {
    bool busy;
    const char *run_id;   /* NULL when idle */
    int32_t queue_depth;
} agent_status_t;

void agent_status(agent_status_t *out);

/* Start the run task. Called once at boot, after `link_start` so frames have a
 * writer (`02-firmware.md` §3.1). Board-only. */
esp_err_t agent_start(void);

/* Forget the conversation (`04` §4.28). Returns how many messages were dropped,
 * through `dropped` when it is non-NULL, and `true` when the buffer is now empty.
 *
 * Refused with `ESP_ERR_INVALID_STATE` while a run is in flight: the run appends to
 * and trims this same buffer between rounds, and a tool result dropped behind its
 * call is a message the provider rejects. The caller reports that as `E_BUSY`.
 *
 * Only the conversation is dropped. The todo tree, the task list and the counters
 * `status` reports are other owners' state. */
esp_err_t agent_history_reset(size_t *dropped);

/* `04` §4.30: the eight blocks of the next request, measured against `LLM_BODY_MAX`.
 * Reads the prompt, the skill index and the tool array the last run built, plus the
 * live history, so the answer describes what a prompt sent *now* would carry. Safe
 * during a run: the history is one buffer, and a byte count read while the run appends
 * to it is a snapshot of a moment, not a claim about the whole run. */
esp_err_t agent_context_usage(agent_context_usage_t *out);

/* `04` §4.32: the text of one block of the next request.
 *
 * `block` is one of the eight names `agent_context_usage_t` measures. The two
 * prompt-side blocks (`system_prompt`, `skills`) and the one tool block (`tools`) are
 * read from the buffers the last build filled. The five history blocks go through
 * `agent_history_block`, which selects messages by role rather than returning the
 * array wrapper.
 *
 * `cap` bounds `out` and `*written`/`*total` answer "is this the whole block" the way
 * `sandbox.read` does, so a caller reports `truncated` honestly. Returns
 * `ESP_ERR_INVALID_ARG` for a name that is not a block. Board-only. */
esp_err_t agent_context_block(const char *block, char *out, size_t cap,
                              size_t *written, size_t *total);

/* `04` §4.33: replace the conversation with a model-written summary of it.
 *
 * Refused with `ESP_ERR_INVALID_STATE` while a run is in flight — the run appends to
 * and trims this same buffer between rounds, so a history rewritten under it is one
 * whose tool results no longer answer its calls. The caller reports `E_BUSY`.
 *
 * Nothing is written until a usable summary is in hand, so a provider failure, a
 * budget refusal or an empty answer leaves the history exactly as it was.
 * `keep` is clamped to `AGENT_COMPACT_KEEP_MAX`. Board-only: it needs the resolved
 * API key, which never leaves `llm`. */
esp_err_t agent_context_compact(uint32_t keep, uint32_t *summarised, uint32_t *kept,
                                size_t *before_bytes, size_t *after_bytes,
                                char *summary, size_t summary_cap);

/* The live todo tree, which is the state the close-out check reads across runs
 * (`02-firmware.md` §4.1). `reload` clears it. */
todo_list_t *agent_todo_phases(void);

void agent_todo_reset(void);

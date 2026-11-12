/* llm — provider chain, request assembly and the spend budget.
 * Layering: L1 (`08-code-architecture.md` §1), above `net`, `cfg`, `clock` and
 * `store`. It owns no socket: `net_post` is the only way onto the wire, so TLS
 * policy stays in one file.
 * Contracts: `02-firmware.md` §`llm`, `03-config.md` §llm, `04-protocol.md` §4.11
 * (`E_LLM_UNAVAILABLE`) and §4.12 (`E_LLM_RATE`). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "net.h"
#include "store.h"

/* `03-config.md` §llm bounds. */
#define LLM_MAX_PROVIDERS 4

/* The request body is assembled into a caller buffer. `docs/02-firmware.md` §llm
 * gives `llm_query(prompt, history, tools_json, response_buf, len)`, and the assembly
 * rule is the frozen contract, so this is the same shape with the provider chosen by
 * the module rather than the caller.
 *
 * It must exceed the parts that are always present: the 25-entry tool array is
 * ~5.1 KiB and the system prompt may be up to 8 KiB (`03-config.md`
 * §`agent.system_prompt`), so an 8 KiB body could not carry a request even with an
 * empty history — every run failed `request body too large` and, because history is
 * never reset, stayed failed until reboot. Matched to `net.h`'s `NET_BODY_MAX`, the
 * largest reply this transport holds. */
#define LLM_BODY_MAX 16384

/* How much of a provider's rejection body `llm_last_error()` keeps. The status alone
 * never names the offending member; the provider's own words do, and the first 192
 * bytes of an OpenAI-shaped error carry the `message` field. */
#define LLM_ERROR_CLIP 160

/* The whole message buffer, and it must exceed `LLM_ERROR_CLIP`: the buffer has to
 * hold `<id>: http <status>: ` as well as the clip, or a long body pushes the prefix
 * — the only part that is always right — out of the string. */
#define LLM_ERROR_MAX (LLM_ERROR_CLIP + 64)

/* One fixed UTC window. `bucket` is the hour (`unix / 3600`) or day (`unix / 86400`)
 * index the count belongs to; a mismatch resets the count rather than trusting a
 * stale value (`03-config.md` §llm). */
typedef struct {
    int32_t count;
    int64_t bucket;
} llm_window_t;

typedef struct {
    llm_window_t hour;
    llm_window_t day;
} llm_budget_t;

/* Zero both windows. */
void llm_budget_reset(llm_budget_t *budget);

/* Charge one request *attempt* against both windows. Returns false — the caller
 * answers `E_LLM_RATE` — when either is exhausted, and then counts nothing, so a
 * refusal never spends budget (`02-firmware.md` §llm). `max_hour` or `max_day` of 0
 * disables that window. */
bool llm_budget_charge(llm_budget_t *budget, int32_t max_hour, int32_t max_day,
                       int64_t unix_now);

/* Read the counters for `status` (`04-protocol.md` §3.9 `llm.calls_hour`,
 * `llm.calls_day`). */
void llm_budget_counters(const llm_budget_t *budget, int32_t *hour, int32_t *day);

/* --- Request assembly ------------------------------------------------------ */

typedef struct {
    const char *model;
    const char *token_param;      /* "max_tokens" or "max_completion_tokens" */
    int32_t     max_output_tokens;
    const char *system_prompt;
    const char *history_json;     /* a JSON array of prior messages, or NULL */
    const char *user_prompt;
    const char *tools_json;       /* a JSON array, or NULL for no tools */
} llm_request_t;

/* Render the request body of `02-firmware.md` §llm's "Request body" into `out`.
 *
 * Everything not listed there is deliberately absent: a key a provider rejects fails
 * the whole request, while a missing key is ignored. Returns the length written, or
 * 0 when `out` is too small — a truncated body is a 400 the caller cannot tell from
 * a config mistake, so it is refused instead. */
size_t llm_assemble_prompt(const llm_request_t *request, const char *extra_body_json,
                           char *out, size_t cap);

/* Append `in` to `out` at `*used` as a quoted JSON string, escaping what must be
 * escaped. Returns false when it does not fit, having written nothing usable, so a
 * caller never emits half a string. */
bool llm_escape_json(const char *in, char *out, size_t cap, size_t *used);

/* Where a provider's request goes: `base_url` with `/chat/completions` appended
 * verbatim, never normalised (`03-config.md` §Provider compatibility). Returns the
 * length, or 0 when it does not fit. */
size_t llm_endpoint(const char *base_url, char *out, size_t cap);

/* Copies the value of the first member object in `json` into `out`, dropping the
 * enclosing object's braces. Returns the copied length. Hand-written because the
 * device builds this wrapper only to splice one array out of it, and the slice used to
 * carry the closing brace along: `llm` spliced `[{...}]}` into its own object, closed
 * it early, and the provider refused the body. */
size_t llm_slice_member(const char *json, char *out, size_t cap);

/* Emits `extra`'s members as one flat object into `out`. Returns the written length,
 * or 0 when it does not fit or `extra` is not an object.
 *
 * `00-overview.md` D4 and `02-firmware.md` §llm: `extra_body`'s members *are* request
 * members, so they are emitted at the root. Emitting `{"extra_body":{...}}` and
 * keeping that text is the defect this function exists to remove: the body builder
 * strips one level of braces, so the wrapper's own `extra_body` key survived and every
 * member under it was inert on the wire. */
size_t llm_flatten_members(const json_node_t *extra, char *out, size_t cap);

/* Which provider to try, given the chain and an index that has already failed.
 * Returns the next index, or -1 when the chain is exhausted. */
int llm_next_provider(int current, int count);

/* Whether the chain walk may visit `index`. `llm_next_provider` returns -1 to end the
 * walk, so a caller must test this rather than `index < count`, which is true for -1
 * and would read `providers[-1]` forever. */
bool llm_provider_loop_ok(int index, int count);

/* At most this many extra attempts per provider: one. A chain of N providers
 * therefore costs at most 2N requests, which keeps a wrong provider from doubling the
 * budget on every prompt (`02-firmware.md` §llm). */
#define LLM_RETRY_ATTEMPTS 1

/* The ceiling on a `Retry-After`-directed wait. */
#define LLM_RETRY_MAX_MS 2000

/* The wait before the first retry when the provider named no delay. Short, because
 * the failure being retried is usually a reset rather than a rate limit. */
#define LLM_RETRY_BASE_MS 250

/* Whether a failed attempt is worth repeating against the same provider, and how long
 * to wait first (`02-firmware.md` §llm "Retry").
 *
 * `err` is the transport result, `status` the HTTP status when one arrived (0 when the
 * exchange never completed). Only a transient failure retries: a transport error or a
 * 408/429/5xx. A 400, 401, 403 or 404 is per-entry configuration and repeating it
 * spends the budget to learn nothing. `retry_after_s` is the provider's own
 * `Retry-After` in seconds, or negative when it sent none; the wait is clamped to
 * `LLM_RETRY_MAX_MS`, because a device that sleeps for the server's full request is
 * indistinguishable from a hung one.
 *
 * `attempt` is 0 for the first retry. Pure, so the decision the chain makes is
 * host-tested. */
bool llm_retry_after(esp_err_t err, int status, int retry_after_s, int attempt,
                     int32_t *wait_ms);

/* --- Board-only ----------------------------------------------------------- */

/* Load `llm.providers[]`, resolve each `api_key_ref`, and restore the persisted
 * counters. Called at boot step 8 (`02-firmware.md` §3.1). */
esp_err_t llm_init(void);

/* Try each provider in order until one answers. A transport or provider failure moves
 * to the next; a budget refusal stops the chain immediately, because a different
 * provider would spend the same budget (`02-firmware.md` §llm).
 *
 * Returns ESP_OK and the reply body in `response_buf`, `E_LLM_RATE` when the budget
 * refuses, `E_LLM_UNAVAILABLE` when every provider failed. `provider_out`, when
 * non-NULL, receives the index that answered or -1. */
esp_err_t llm_query(const llm_request_t *request, char *response_buf, size_t response_cap,
                    int *provider_out);

/* The budget as of now. */
void llm_counters(int32_t *hour, int32_t *day);

/* Abort the request `llm_query` is blocked in, if any, so `agent_cancel` reaches a
 * call that is already on the wire (`02-firmware.md` §4.1). Safe to call from another
 * task and with no request in flight: the handle is published only while
 * `esp_http_client_perform` is running, and the caller's own thread cleans it up.
 * The aborted call returns `ESP_ERR_NOT_FINISHED` from `llm_query`, which is
 * distinct from the budget refusal so the run reports `E_CANCELLED` rather than
 * `E_LLM_RATE`. */
void llm_abort(void);

/* The provider that answered most recently — `id` and `model`, both borrowed and
 * stable — and whether that call succeeded. `status` reports all three
 * (`04-protocol.md` §3.9: `llm.available` is "last call succeeded"). */
bool llm_last_answer(const char **id, const char **model);

/* The configured ceilings, for `status` (`04-protocol.md` §3.9 `llm.limit_hour`,
 * `llm.limit_day`). 0 means the window is disabled and is reported as null. */
void llm_limits(int32_t *hour, int32_t *day);

/* Copy the host only — no scheme, no port, no path — of the first configured
 * provider's `base_url` into `out` (`04-protocol.md` §4.25, `net.ping` target `llm`).
 * Returns false when no provider is loaded or the host does not fit. The API key is
 * never part of the result: the caller wants a name to probe, not a credential. */
bool llm_active_host(char *out, size_t cap);

/* Human-readable reason the chain last failed, for the `error.message` of
 * `E_LLM_UNAVAILABLE`. Borrowed and stable until the next call. When the last
 * attempt was rejected with a body, it carries that body's words (`02-firmware.md`
 * §llm). */
const char *llm_last_error(void);

/* Format a provider rejection into `out` as `<id>: http <status>: <body>`, the body
 * clipped to `LLM_ERROR_CLIP` and flattened to one line. Pure: no socket, no clock, so
 * the shape `E_LLM_UNAVAILABLE`'s message takes is host-tested rather than inferred
 * from a live 400. A NULL or empty body yields `<id>: http <status>` alone. Returns
 * the length written. */
size_t llm_copy_error_detail(const char *body, size_t body_len, const char *id,
                             int status, char *out, size_t cap);

/* Replace every occurrence of `secret` in `text` with `***`. Third-party text goes
 * into `agent.error`, and `06-security.md` §7 forbids a resolved key from reaching it;
 * a provider that echoes the `Authorization` header back in a 400 body would otherwise
 * print the key into the reader's scrollback. A NULL or empty secret is a no-op, which
 * is the ordinary case: most providers do not echo. Returns the number of replacements. */
size_t llm_redact(char *text, const char *secret);

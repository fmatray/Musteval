/* agent_history — the conversation buffer `llm` receives as `history_json`.
 *
 * Kept as JSON text rather than a struct array: it is handed to `llm` verbatim, and
 * re-encoding every round would copy the whole conversation twice per round for no
 * benefit. `llm.max_history_messages` bounds it, oldest dropped first
 * (`02-firmware.md` §4.2 "Buffer Full"). Pure. */

#include "agent.h"

#include <stdio.h>
#include <string.h>

/* Walk to the end of the JSON value starting at `p`, returning its byte length, or 0
 * when malformed. Bracket counting with string state tracked, so a `]` inside a
 * message body cannot end the array early. The bytes are known-valid (they came from
 * `store`'s builders), so this is arithmetic rather than a second parse. */
static size_t value_end(const char *p)
{
    int depth = 0;
    bool in_string = false;
    bool escape = false;
    for (size_t i = 0; p[i] != '\0'; i++) {
        char c = p[i];
        if (in_string) {
            if (escape) {
                escape = false;
            } else if (c == '\\') {
                escape = true;
            } else if (c == '"') {
                in_string = false;
            }
            continue;
        }
        if (c == '"') {
            in_string = true;
        } else if (c == '{' || c == '[') {
            depth++;
        } else if (c == '}' || c == ']') {
            depth--;
            if (depth == 0) return i + 1;
            if (depth < 0) return 0;
        }
    }
    return 0;
}

/* Where the first message starts: the byte after `[` and any whitespace. Returns 0
 * when `history` is not an array. */
static const char *first_message(const char *history)
{
    if (history == NULL) return NULL;
    const char *p = history;
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    if (*p != '[') return NULL;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    return p;
}

size_t agent_history_count(const char *history)
{
    const char *p = first_message(history);
    if (p == NULL) return 0;

    size_t count = 0;
    for (;;) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) break;
        count++;
        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    return count;
}

/* True when the `n`-byte message at `p` has `"role"` equal to `role`. The bytes are
 * known-valid JSON built by `store`/`agent`, so this scans for the key rather than
 * parsing a second time.
 *
 * Bounded by `n` because `agent_history_measure` (`04` §4.30) classifies one message
 * at a time: an unbounded scan would find the *next* message's `"role"` and bill this
 * message's bytes to the wrong block. */
static bool message_has_role_n(const char *p, size_t n, const char *role)
{
    const char *end = p + n;
    const char *key = NULL;
    for (const char *q = p; q + 6 <= end; q++) {
        if (memcmp(q, "\"role\"", 6) == 0) {
            key = q + 6;
            break;
        }
    }
    if (key == NULL) return false;
    while (key < end && (*key == ' ' || *key == '\t' || *key == '\n' || *key == '\r')) key++;
    if (key >= end || *key != ':') return false;
    key++;
    while (key < end && (*key == ' ' || *key == '\t' || *key == '\n' || *key == '\r')) key++;
    if (key >= end || *key != '"') return false;
    key++;
    size_t len = strlen(role);
    if ((size_t)(end - key) < len + 1) return false;
    return strncmp(key, role, len) == 0 && key[len] == '"';
}

/* The same question against a NUL-terminated message, for the callers that already
 * know where the array ends (trim and truncate walk it themselves). */
static bool message_has_role(const char *p, const char *role)
{
    return message_has_role_n(p, strlen(p), role);
}

/* `04` §4.30: which block of the prompt a message's bytes are billed to. One message
 * is one role plus, for the assistant turn that asked for tools, the calls it carries;
 * `agent_history_measure` adds the tool results to the `tool_output` block.
 *
 * `thinking` stays 0: the model's reasoning trace is never written to the history. It
 * arrives on `agent_reply_t.thinking` and leaves in its own `agent.thinking` frame
 * (`04` §3.21) without being appended, so there is nothing to measure and pretending
 * otherwise would report a block that can never grow. */
static size_t measure_tool_calls(const char *p, size_t n)
{
    for (size_t i = 0; i + 12 <= n; i++) {
        if (memcmp(p + i, "\"tool_calls\"", 12) == 0) return n - i;
    }
    return 0;
}

/* Walk a JSON array of messages and add each message's byte length to the block its
 * `role` names. Pure, so the arithmetic behind `04` §4.30 is a host test rather than
 * a claim about the board (`agent_history.c`).
 *
 * A message with no recognised role, or bytes that are not an array, contribute
 * nothing: `04` §4.30 reports what the loop will send, and a message the loop cannot
 * classify is one the provider will refuse rather than one that consumes budget.
 *
 * There is no `system` arm: `agent_history_push` writes only `user`, `assistant` and
 * `tool`, and the prompt lives in its own buffer, so a `role:"system"` message here
 * would be one the loop never wrote. */
esp_err_t agent_history_measure(const char *history, agent_context_usage_t *out)
{
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof *out);
    if (history == NULL) return ESP_ERR_INVALID_ARG;

    const char *p = first_message(history);
    if (p == NULL) return ESP_ERR_INVALID_ARG;

    for (;;) {
        /* A truncated array is not an empty one: `[` alone ends on `\0`, and reporting
         * it as a clean zero-message history would tell a reader their context is
         * empty when the buffer is simply unreadable. */
        if (*p == '\0') return ESP_ERR_INVALID_ARG;
        if (*p == ']') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;

        size_t calls = measure_tool_calls(p, n);
        if (message_has_role_n(p, n, "tool")) {
            out->tool_output += n;
        } else if (message_has_role_n(p, n, "assistant")) {
            out->assistant += n - calls;
            out->tool_calls += calls;
        } else if (message_has_role_n(p, n, "user")) {
            out->user += n;
        }
        out->messages += 1;

        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    return ESP_OK;
}

esp_err_t agent_history_trim(char *history, size_t *used, int32_t max_messages,
                             bool pin_head)
 {
    if (history == NULL || used == NULL) return ESP_ERR_INVALID_ARG;
    if (max_messages < 0) max_messages = 0;

    size_t count = agent_history_count(history);
    if (count <= (size_t)max_messages) {
        *used = strlen(history);
        return ESP_OK;
    }

    size_t skip = count - (size_t)max_messages;
    if (pin_head && max_messages > 0) {
        /* Reserve one slot for the run's question (index 0). The kept suffix
         * therefore has one fewer slot than the bound. */
        skip = count - ((size_t)max_messages - 1);
        if (skip < 1) skip = 1;
    }
    const char *p = first_message(history);
    if (p == NULL) return ESP_ERR_INVALID_ARG;

    /* Never start the kept suffix with a `role:"tool"` message: its assistant turn
     * was just dropped, and a provider rejects a tool message whose `tool_call_id`
     * has no matching call. `02-firmware.md` §4.1 step 4 makes the pair atomic. */
    {
        size_t index = 0;
        const char *q = p;
        for (;;) {
            if (*q == ']' || *q == '\0') break;
            size_t n = value_end(q);
            if (n == 0) return ESP_ERR_INVALID_ARG;
            if (index >= skip) {
                if (!message_has_role(q, "tool")) break;
                skip = index + 1;
            }
            index++;
            q += n;
            while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r' || *q == ',') q++;
        }
    }

    /* Rebuild in place. Every write lands at or before the byte it came from: the
     * opening `[` and the messages before `skip` both collapse, and the kept suffix
     * is copied from a strictly greater offset once skip > 0. skip is > 0 here
     * because the count exceeded the bound. */
    size_t at = 0;
    history[at++] = '[';
    size_t index_copy = 0;
    bool first = true;
    for (;;) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;
        bool keep = index_copy >= skip || (pin_head && max_messages > 0 && index_copy == 0);
        index_copy++;
        if (!keep) {
            p += n;
            while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') {
                p++;
            }
            continue;
        }
        if (!first) history[at++] = ',';
        memmove(history + at, p, n);
        at += n;
        first = false;
        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    history[at++] = ']';
    history[at] = '\0';
    *used = at;
    return ESP_OK;
}

/* Empty the buffer. `*used` becomes 2, not 0: `append` treats 0 as "this static was
 * never written" and would scan uninitialized bytes for `[` (`02-firmware.md` §4.2).
 * The two-byte `[]` is what every other path leaves behind for an empty history, so
 * `clear` then `append` costs no special case in the writer. */
esp_err_t agent_history_clear(char *history, size_t *used)
{
    if (history == NULL || used == NULL) return ESP_ERR_INVALID_ARG;
    history[0] = '[';
    history[1] = ']';
    history[2] = '\0';
    *used = 2;
    return ESP_OK;
}

/* Keep only the first `keep` messages, in place. Used when a run aborts part-way:
 * its half-finished assistant/tool turns must not be replayed, because a
 * `role:"tool"` message without its matching call is refused by the provider. */
esp_err_t agent_history_truncate(char *history, size_t *used, uint32_t keep)
{
    if (history == NULL || used == NULL) return ESP_ERR_INVALID_ARG;

    const char *p = first_message(history);
    if (p == NULL) return ESP_ERR_INVALID_ARG;

    /* `02-firmware.md` §4.1: the assistant turn carrying `tool_calls` and the
     * `role:"tool"` messages answering it travel together. Keeping the first `keep`
     * messages can cut the pair at the tail, leaving a call with no result — the
     * mirror of the orphan a trim avoids at the head. When the first dropped message
     * is a tool result, one fewer is kept, which drops its call too. */
    while (keep > 0) {
        const char *q = p;
        uint32_t index = 0;
        while (index < keep && *q != ']' && *q != '\0') {
            size_t n = value_end(q);
            if (n == 0) return ESP_ERR_INVALID_ARG;
            q += n;
            index++;
            while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r' || *q == ',') q++;
        }
        if (*q == ']' || *q == '\0' || !message_has_role(q, "tool")) break;
        keep--;
    }

    size_t at = 0;
    history[at++] = '[';
    uint32_t index = 0;
    bool first = true;
    for (;;) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;
        if (index++ >= keep) break;
        if (!first) history[at++] = ',';
        memmove(history + at, p, n);
        at += n;
        first = false;
        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    history[at++] = ']';
    history[at] = '\0';
    *used = at;
    return ESP_OK;
}

esp_err_t agent_history_append(char *history, size_t cap, size_t *used,
                               const char *message_json, int32_t max_messages,
                               bool pin_head)
{
    if (history == NULL || used == NULL || message_json == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (max_messages < 0) max_messages = 0;

    /* `max_messages == 0` keeps nothing, so an append is a successful no-op: a run
     * configured that way still completes, it just carries no memory forward. */
    if (max_messages == 0) {
        if (*used == 0) {
            if (cap < 3) return ESP_ERR_INVALID_SIZE;
            memcpy(history, "[]", 3);
            *used = 2;
        }
        return ESP_OK;
    }

    size_t msg_len = strlen(message_json);
    /* A message larger than the whole buffer can never fit, and trimming the history
     * for it would destroy entries for nothing. Refuse it before touching a byte. */
    if (msg_len + 3 > cap) return ESP_ERR_INVALID_SIZE;

    /* `02-firmware.md` §4.2 "Buffer Full": truncate the oldest entry, do not drop the
     * new one. The message being appended is the round just completed, so losing it
     * would leave a tool call with no answer or a question with no reply. Either form
     * costs the message plus `[`, `]`, and a NUL.
     *
     * `*used == 0` is the authoritative "nothing written yet": the buffer is
     * uninitialized then, so scanning it for `[` would read whatever the static held.
     * Every other path keeps `*used` in step with the text it describes. */
    for (;;) {
        size_t count = *used == 0 ? 0 : agent_history_count(history);
        size_t have = *used;
        if (count == 0 || have + msg_len + 3 <= cap) break;
        /* Drop one message by re-trimming to `count - 1`. The trim honours
         * call/result pairing and may drop more than one, which only helps. */
        if (agent_history_trim(history, &have, (int32_t)(count - 1), pin_head) != ESP_OK) {
        }
        *used = have;
        /* Trimming is monotone, so an unchanged count means it cannot free a byte and
         * looping would spin. */
        if (agent_history_count(history) >= count) return ESP_ERR_INVALID_SIZE;
    }

    /* A buffer holding `[]` is empty even though `*used` is 2: `trim`/`truncate`
     * leave exactly that, and reading `*used == 0` as "empty" would overwrite the
     * `]` with `,` and produce `[,{…}]`. `*used == 0` is still checked first because
     * the buffer is uninitialized then and must not be scanned. */
    bool empty = *used == 0 || agent_history_count(history) == 0;
    size_t have = *used;

    if (empty) {
        history[0] = '[';
        memcpy(history + 1, message_json, msg_len);
        history[1 + msg_len] = ']';
        history[2 + msg_len] = '\0';
        *used = 2 + msg_len;
    } else {
        /* Overwrite the closing `]` with the separator, then re-close. */
        history[have - 1] = ',';
        memcpy(history + have, message_json, msg_len);
        history[have + msg_len] = ']';
        history[have + msg_len + 1] = '\0';
        *used = have + msg_len + 1;
    }

    /* Trim here rather than at the caller, so a history can never exceed the bound
     * even if a caller forgets to ask. */
    if (agent_history_count(history) > (size_t)max_messages) {
        return agent_history_trim(history, used, max_messages, pin_head);
    }
    return ESP_OK;
}

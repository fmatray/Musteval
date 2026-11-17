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

/* How many entries the `tool_calls` array on this assistant message holds.
 *
 * Counted rather than measured: `measure_tool_calls` returns the byte span so it can
 * be billed, which is not the number of calls. The bytes are known-valid JSON built by
 * `store`, so this is arithmetic over the same span. Returns 0 when the key is absent,
 * which is every plain assistant turn. */
static size_t tool_call_count(const char *p, size_t n)
{
    /* The key's offset, which is not what `measure_tool_calls` returns: that function
     * answers the span to bill, and using it as an offset read past the message. */
    size_t start = 0;
    for (size_t i = 0; i + 12 <= n; i++) {
        if (memcmp(p + i, "\"tool_calls\"", 12) == 0) {
            start = i;
            break;
        }
    }
    if (start == 0) return 0;
    const char *q = p + start + 12; /* past `"tool_calls"` */
    while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r') q++;
    if (*q != ':') return 0;
    q++;
    while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r') q++;
    if (*q != '[') return 0;

    size_t calls = 0;
    q++;
    for (;;) {
        while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r' || *q == ',') q++;
        if (*q == ']' || *q == '\0') break;
        size_t len = value_end(q);
        if (len == 0) break;
        calls++;
        q += len;
    }
    return calls;
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
            out->count_tool_output += 1;
        } else if (message_has_role_n(p, n, "assistant")) {
            out->assistant += n - calls;
            out->tool_calls += calls;
            out->count_assistant += 1;
            /* `count_tool_calls` counts the calls, not the turns that made them: a
             * reader asking "how many tools ran" is asking about calls, and one
             * assistant turn can carry several. */
            out->count_tool_calls += tool_call_count(p, n);
        } else if (message_has_role_n(p, n, "user")) {
            out->user += n;
            out->count_user += 1;
        }
        out->messages += 1;

        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    return ESP_OK;
}


/* `04` §4.32: the messages of one block, one per line. `role` of `NULL` selects
 * every message; `"tool_calls"` is not a role, so it selects the assistant turns that
 * carry calls rather than a role no message has. Pure, like the measure beside it. */
esp_err_t agent_history_block(const char *history, const char *role, char *out,
                              size_t cap, size_t *written, size_t *total)
{
    if (out == NULL || cap == 0) return ESP_ERR_INVALID_SIZE;
    if (written != NULL) *written = 0;
    if (total != NULL) *total = 0;
    if (history == NULL) return ESP_ERR_INVALID_ARG;

    const char *p = first_message(history);
    if (p == NULL) return ESP_ERR_INVALID_ARG;

    /* Two counters, not one: `at` stops at the cap while `all` keeps counting, which
     * is how `truncated` is answered without walking the history twice. */
    size_t at = 0;
    size_t all = 0;
    bool first = true;
    for (;;) {
        if (*p == '\0') return ESP_ERR_INVALID_ARG;
        if (*p == ']') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;

        bool want = role == NULL;
        if (!want && strcmp(role, "tool_calls") == 0) {
            want = message_has_role_n(p, n, "assistant") && measure_tool_calls(p, n) != 0;
        } else if (!want) {
            want = message_has_role_n(p, n, role);
        }

        if (want) {
            if (!first) {
                all += 1;
                if (at + 1 < cap) out[at++] = '\n';
            }
            /* A block larger than the buffer keeps as much of the message as fits
             * rather than returning nothing: an empty pane reads as "this block is
             * empty", which is the one thing truncation must not say. */
            size_t room = cap - 1 - at;
            size_t take = n < room ? n : room;
            memcpy(out + at, p, take);
            at += take;
            all += n;
            first = false;
        }

        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    out[at] = '\0';
    if (written != NULL) *written = at;
    if (total != NULL) *total = all;
    return ESP_OK;
}


/* `04` §4.33: the messages to summarise, which is every message except the newest
 * `keep`. Pure. */
esp_err_t agent_history_slice(const char *history, uint32_t keep, char *out,
                              size_t cap, size_t *written, size_t *total)
{
    if (out == NULL || cap == 0) return ESP_ERR_INVALID_SIZE;
    if (written != NULL) *written = 0;
    if (total != NULL) *total = 0;
    if (history == NULL) return ESP_ERR_INVALID_ARG;

    const char *p = first_message(history);
    if (p == NULL) return ESP_ERR_INVALID_ARG;

    size_t count = agent_history_count(history);
    size_t take = keep < count ? count - keep : 0;

    size_t at = 0;
    size_t all = 0;
    bool first = true;
    for (size_t index = 0; index < take; index++) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;
        if (!first) {
            all += 1;
            if (at + 1 < cap) out[at++] = '\n';
        }
        if (at + n < cap) {
            memcpy(out + at, p, n);
            at += n;
        }
        all += n;
        first = false;
        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    out[at] = '\0';
    if (written != NULL) *written = at;
    if (total != NULL) *total = all;
    return ESP_OK;
}

/* The newest `keep` messages, in history order. The mirror of `agent_history_suffix`
 * and `agent_history_slice`, and separate for the same reason `truncate` is: one
 * selection rule, one function, so the two halves of a compaction cannot disagree
 * about which messages they took. Pure. */
esp_err_t agent_history_suffix(const char *history, uint32_t keep, char *out,
                               size_t cap, size_t *written, size_t *total)
{
    if (out == NULL || cap == 0) return ESP_ERR_INVALID_SIZE;
    if (written != NULL) *written = 0;
    if (total != NULL) *total = 0;
    if (history == NULL) return ESP_ERR_INVALID_ARG;

    const char *p = first_message(history);
    if (p == NULL) return ESP_ERR_INVALID_ARG;

    size_t count = agent_history_count(history);
    size_t skip = count > keep ? count - keep : 0;

    /* Walk to the first kept message. */
    for (size_t index = 0; index < skip; index++) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;
        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }

    size_t at = 0;
    size_t all = 0;
    bool first = true;
    for (;;) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;
        if (!first) {
            all += 1;
            if (at + 1 < cap) out[at++] = '\n';
        }
        if (at + n < cap) {
            memcpy(out + at, p, n);
            at += n;
        }
        all += n;
        first = false;
        p += n;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') p++;
    }
    out[at] = '\0';
    if (written != NULL) *written = at;
    if (total != NULL) *total = all;
    return ESP_OK;
}

/* `04` §4.33: the summary opens the history and the kept messages follow it.
 *
 * `scratch` is the caller's, not a `static`: a file-static buffer would be shared
 * state in a file whose whole point is that it is pure. The result is built there
 * first and copied over `history` only once it is known to fit, so a buffer that
 * cannot hold the result is left exactly as it was. */
esp_err_t agent_history_replace_with_summary(char *history, size_t cap, size_t *used,
                                             const char *summary, uint32_t keep,
                                             char *scratch, size_t scratch_cap)
{
    if (history == NULL || used == NULL || summary == NULL) return ESP_ERR_INVALID_ARG;
    if (scratch == NULL || scratch_cap == 0) return ESP_ERR_INVALID_SIZE;

    /* The kept suffix is taken from the live history before anything is written, so a
     * failure here leaves the conversation alone. */
    size_t kept_written = 0, kept_total = 0;
    esp_err_t kerr = agent_history_suffix(history, keep, scratch, scratch_cap,
                                          &kept_written, &kept_total);
    if (kerr != ESP_OK) return kerr;

    /* The opening message: `04` §4.33's `role:"user"` shape with the summary escaped
     * and the trailing sentence that stops the model reading it as a fresh request.
     * Built by hand because `store`'s builders are not reachable from this file. */
    static const char k_head[] = "{\"role\":\"user\",\"content\":\"";
    static const char k_tail[] =
        "\\n\\nThe conversation above was compacted into this summary. "
        "Continue from it.\"}";
    size_t head_len = sizeof k_head - 1;
    size_t tail_len = sizeof k_tail - 1;

    /* Written directly into `history` after the `[`, but only after the size is
     * proven: `need` is computed with the escaped length, so the write cannot
     * overrun. */
    size_t escaped = 0;
    for (const char *q = summary; *q != '\0'; q++) {
        unsigned char c = (unsigned char)*q;
        escaped += (c == '"' || c == '\\') ? 2 : (c == '\n' || c == '\r' || c == '\t') ? 2
                                                 : (c < 0x20)                ? 6
                                                                             : 1;
    }

    size_t need = 1 + head_len + escaped + tail_len +
                  (kept_written > 0 ? 1 + kept_written : 0) + 1 + 1;
    if (need > cap) return ESP_ERR_INVALID_SIZE;

    size_t w = 0;
    history[w++] = '[';
    memcpy(history + w, k_head, head_len);
    w += head_len;
    for (const char *q = summary; *q != '\0'; q++) {
        unsigned char c = (unsigned char)*q;
        if (c == '"') {
            history[w++] = '\\';
            history[w++] = '"';
        } else if (c == '\\') {
            history[w++] = '\\';
            history[w++] = '\\';
        } else if (c == '\n') {
            history[w++] = '\\';
            history[w++] = 'n';
        } else if (c == '\r') {
            history[w++] = '\\';
            history[w++] = 'r';
        } else if (c == '\t') {
            history[w++] = '\\';
            history[w++] = 't';
        } else if (c < 0x20) {
            int n = snprintf(history + w, cap - w, "\\u%04x", c);
            if (n < 0) return ESP_ERR_INVALID_SIZE;
            w += (size_t)n;
        } else {
            history[w++] = (char)c;
        }
    }
    memcpy(history + w, k_tail, tail_len);
    w += tail_len;
    if (kept_written > 0) {
        history[w++] = ',';
        memcpy(history + w, scratch, kept_written);
        w += kept_written;
    }
    history[w++] = ']';
    history[w] = '\0';
    *used = w;
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

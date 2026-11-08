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

/* True when the message object at `p` has `"role"` equal to `role`. The bytes are
 * known-valid JSON built by `store`/`agent`, so this scans for the key rather than
 * parsing a second time. */
static bool message_has_role(const char *p, const char *role)
{
    const char *key = strstr(p, "\"role\"");
    if (key == NULL) return false;
    key += 6;
    while (*key == ' ' || *key == '\t' || *key == '\n' || *key == '\r') key++;
    if (*key != ':') return false;
    key++;
    while (*key == ' ' || *key == '\t' || *key == '\n' || *key == '\r') key++;
    if (*key != '"') return false;
    key++;
    size_t len = strlen(role);
    return strncmp(key, role, len) == 0 && key[len] == '"';
}

esp_err_t agent_history_trim(char *history, size_t *used, int32_t max_messages)
{
    if (history == NULL || used == NULL) return ESP_ERR_INVALID_ARG;
    if (max_messages < 0) max_messages = 0;

    size_t count = agent_history_count(history);
    if (count <= (size_t)max_messages) {
        *used = strlen(history);
        return ESP_OK;
    }

    size_t skip = count - (size_t)max_messages;
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
    size_t index = 0;
    bool first = true;
    for (;;) {
        if (*p == ']' || *p == '\0') break;
        size_t n = value_end(p);
        if (n == 0) return ESP_ERR_INVALID_ARG;
        if (index++ < skip) {
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
                               const char *message_json, int32_t max_messages)
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
        if (agent_history_trim(history, &have, (int32_t)(count - 1)) != ESP_OK) {
            return ESP_ERR_INVALID_SIZE;
        }
        *used = have;
        /* Trimming is monotone, so an unchanged count means it cannot free a byte and
         * looping would spin. */
        if (agent_history_count(history) >= count) return ESP_ERR_INVALID_SIZE;
    }

    bool empty = *used == 0;
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
        return agent_history_trim(history, used, max_messages);
    }
    return ESP_OK;
}

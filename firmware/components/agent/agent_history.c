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
    size_t have = *used;
    bool empty = agent_history_count(history) == 0;

    /* Either form costs the message plus `[`, `]`, and a NUL. */
    if (have + msg_len + 3 > cap) return ESP_ERR_INVALID_SIZE;

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

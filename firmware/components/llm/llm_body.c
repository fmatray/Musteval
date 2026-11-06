/* llm_body.c — endpoint, body assembly and chain order, with no socket.
 * Pure so the request shape that `02-firmware.md` §llm freezes is proven on the host.
 * A missing `stream: false` or a `max_tokens` sent to a provider that wants
 * `max_completion_tokens` is a 400 that only appears once a real key is spent. */

#include <stdio.h>
#include <string.h>

#include "llm.h"

size_t llm_endpoint(const char *base_url, char *out, size_t cap)
{
    if (base_url == NULL || out == NULL || cap == 0) return 0;
    /* Appended verbatim. `base_url` already carries the version prefix, and a
     * trailing slash is the provider's own form, so no trimming: normalising here
     * would break a base_url that legitimately ends in one. */
    int n = snprintf(out, cap, "%s/chat/completions", base_url);
    if (n <= 0 || (size_t)n >= cap) return 0;
    return (size_t)n;
}

size_t llm_assemble_prompt(const llm_request_t *request, const char *extra_body_json,
                      char *out, size_t cap)
{
    if (request == NULL || request->model == NULL || request->user_prompt == NULL ||
        out == NULL || cap == 0) {
        return 0;
    }

    const char *token_param = request->token_param != NULL &&
                                      request->token_param[0] != '\0'
                                  ? request->token_param
                                  : "max_tokens";
    int32_t max_tokens = request->max_output_tokens > 0 ? request->max_output_tokens
                                                        : 1024;

    /* Assembled by hand rather than through the JSON tree: the body is a fixed shape
     * with two variable strings, and building a document to emit it would need a
     * second allocation per call on a device with no PSRAM budget for one. The
     * strings are escaped below instead. */
    size_t used = 0;
    int n = snprintf(out, cap, "{\"model\":\"%s\",\"messages\":[", request->model);
    if (n <= 0) return 0;
    used = (size_t)n;

#define APPEND(...)                                                                    \
    do {                                                                               \
        if (used >= cap) return 0;                                                      \
        n = snprintf(out + used, cap - used, __VA_ARGS__);                               \
        if (n <= 0 || (size_t)n >= cap - used) return 0;                                \
        used += (size_t)n;                                                              \
    } while (0)

    if (request->system_prompt != NULL && request->system_prompt[0] != '\0') {
        APPEND("{\"role\":\"system\",\"content\":");
        if (!llm_escape_json(request->system_prompt, out, cap, &used)) return 0;
        APPEND("},");
    }
    /* History is spliced verbatim after the system message and before the user's:
     * it is already the `role`/`content` object shape the provider expects, and
     * re-encoding it here would have to re-implement tool-call messages. */
    if (request->history_json != NULL) {
        const char *open = strchr(request->history_json, '[');
        const char *close = strrchr(request->history_json, ']');
        if (open != NULL && close != NULL && close > open + 1) {
            size_t inner = (size_t)(close - open - 1);
            APPEND("%*.*s,", (int)inner, (int)inner, open + 1);
        }
    }
    APPEND("{\"role\":\"user\",\"content\":");
    if (!llm_escape_json(request->user_prompt, out, cap, &used)) return 0;
    APPEND("}]");

    /* `tools` is omitted rather than sent empty: an empty array is accepted by some
     * providers and rejected by others, while the absent key is ignored by all
     * (`02-firmware.md` §llm). */
    if (request->tools_json != NULL && request->tools_json[0] != '\0' &&
        strcmp(request->tools_json, "[]") != 0) {
        APPEND(",\"tools\":%s", request->tools_json);
    }

    /* The token cap's *name* differs per provider, which is why it is not hard-coded. */
    APPEND(",\"%s\":%d", token_param, (int)max_tokens);

    /* `stream` is always false (overview D8: non-streaming is permanent). */
    APPEND(",\"stream\":false");

    /* `extra_body` is merged last and verbatim; the validator has already refused the
     * four keys that would rewrite the structure (`03-config.md` §llm). Its braces are
     * stripped so its members join this object rather than nesting. */
    if (extra_body_json != NULL) {
        const char *open = strchr(extra_body_json, '{');
        const char *close = strrchr(extra_body_json, '}');
        if (open != NULL && close != NULL && close > open + 1) {
            size_t inner = (size_t)(close - open - 1);
            APPEND(",%*.*s", (int)inner, (int)inner, open + 1);
        }
    }

    APPEND("}");
    if (used >= cap) return 0;
    out[used] = '\0';
    return used;
#undef APPEND
}

bool llm_escape_json(const char *in, char *out, size_t cap, size_t *used)
{
    if (in == NULL || out == NULL || used == NULL) return false;
    size_t at = *used;
    if (at + 2 >= cap) return false;
    out[at++] = '"';
    for (const char *p = in; *p != '\0'; p++) {
        unsigned char c = (unsigned char)*p;
        const char *escape = NULL;
        char unicode[8];
        switch (c) {
        case '"': escape = "\\\""; break;
        case '\\': escape = "\\\\"; break;
        case '\n': escape = "\\n"; break;
        case '\r': escape = "\\r"; break;
        case '\t': escape = "\\t"; break;
        case '\b': escape = "\\b"; break;
        case '\f': escape = "\\f"; break;
        default: break;
        }
        if (escape == NULL && c < 0x20) {
            /* A bare control byte is invalid JSON, and a prompt can contain one from
             * a template file. */
            snprintf(unicode, sizeof unicode, "\\u%04x", c);
            escape = unicode;
        }
        if (escape != NULL) {
            size_t len = strlen(escape);
            if (at + len + 2 >= cap) return false;
            memcpy(out + at, escape, len);
            at += len;
        } else {
            if (at + 2 >= cap) return false;
            out[at++] = (char)c;
        }
    }
    out[at++] = '"';
    out[at] = '\0';
    *used = at;
    return true;
}

int llm_next_provider(int current, int count)
{
    int next = current + 1;
    /* The chain is ordered and tried once per attempt: wrapping would let a flapping
     * provider spend the budget in a loop (`02-firmware.md` §llm). */
    return next < count ? next : -1;
}

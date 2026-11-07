/* llm_body.c — endpoint, body assembly and chain order, with no socket.
 * Pure so the request shape that `02-firmware.md` §llm freezes is proven on the host.
 * A missing `stream: false` or a `max_tokens` sent to a provider that wants
 * `max_completion_tokens` is a 400 that only appears once a real key is spent. */

#include <stdio.h>
#include <string.h>

#include "llm.h"

size_t llm_redact(char *text, const char *secret)
{
    if (text == NULL || secret == NULL || secret[0] == '\0') return 0;
    size_t slen = strlen(secret);
    size_t count = 0;
    char *hit;
    /* `strstr` from the start each time is right here: the replacement is shorter than
     * the needle for any real key, but it is not shorter for a one-character secret, so
     * the cursor advances past the replacement rather than trusting length. */
    char *at = text;
    while ((hit = strstr(at, secret)) != NULL) {
        memset(hit, '*', 3 < slen ? 3 : slen);
        if (slen > 3) {
            size_t tail = strlen(hit + slen);
            memmove(hit + 3, hit + slen, tail + 1);
        }
        at = hit + (slen > 3 ? 3 : slen);
        count++;
    }
    return count;
}

size_t llm_copy_error_detail(const char *body, size_t body_len, const char *id,
                             int status, char *out, size_t cap)
{
    if (out == NULL || cap == 0) return 0;
    const char *who = id != NULL && id[0] != '\0' ? id : "provider";

    /* No body is the ordinary case for a transport-level refusal and for a bare
     * status; the prefix is still the whole message. */
    if (body == NULL || body_len == 0) {
        int n = snprintf(out, cap, "%s: http %d", who, status);
        return n <= 0 ? 0 : (size_t)n;
    }

    /* Flattened, because this string travels in a JSON `message` and gets read on one
     * line: a provider's pretty-printed error would otherwise arrive as the first
     * line and a run of escapes. */
    int n = snprintf(out, cap, "%s: http %d: ", who, status);
    if (n <= 0 || (size_t)n >= cap) { out[cap - 1] = '\0'; return n <= 0 ? 0 : (size_t)n; }
    size_t used = (size_t)n;

    size_t limit = body_len < LLM_ERROR_CLIP ? body_len : LLM_ERROR_CLIP;
    for (size_t i = 0; i < limit && used + 1 < cap; i++) {
        unsigned char c = (unsigned char)body[i];
        /* Control bytes become spaces rather than reaching the pane: `05` §13 has the
         * bridge strip them on arrival, but the message also lands in the device log
         * and a raw ESC in an error body is a terminal escape sequence, not text. */
        out[used++] = (c < 0x20 || c == 0x7f) ? ' ' : (char)c;
    }
    out[used] = '\0';
    return used;
}

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
    if (request == NULL || request->model == NULL || out == NULL || cap == 0) {
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

    /* Each message is joined with a leading comma rather than a trailing one, so the
     * last message needs no special case and `messages` is never left ending in `,`. */
    bool first = true;

    if (request->system_prompt != NULL && request->system_prompt[0] != '\0') {
        APPEND("{\"role\":\"system\",\"content\":");
        if (!llm_escape_json(request->system_prompt, out, cap, &used)) return 0;
        APPEND("}");
        first = false;
    }
    /* History is spliced verbatim: it is already the `role`/`content` object shape the
     * provider expects, and re-encoding it here would have to re-implement tool-call
     * messages. */
    if (request->history_json != NULL) {
        const char *open = strchr(request->history_json, '[');
        const char *close = strrchr(request->history_json, ']');
        if (open != NULL && close != NULL && close > open + 1) {
            size_t inner = (size_t)(close - open - 1);
            APPEND("%s%*.*s", first ? "" : ",", (int)inner, (int)inner, open + 1);
            first = false;
        }
    }
    /* `user_prompt` is the run's own prompt, which is only the *last* message on the
     * first round. On every later round the history already carries it, and the last
     * message is a tool result — appending it again would send it twice and put the
     * user turn after the tool replies (`02-firmware.md` §4.1 step 4). */
    if (request->user_prompt != NULL) {
        if (!first) APPEND(",");
        APPEND("{\"role\":\"user\",\"content\":");
        if (!llm_escape_json(request->user_prompt, out, cap, &used)) return 0;
        APPEND("}");
        first = false;
    }
    APPEND("]");

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

size_t llm_slice_member(const char *json, char *out, size_t cap)
{
    if (json == NULL || out == NULL || cap == 0) return 0;
    const char *open = strchr(json, '[');
    const char *colon = strchr(json, ':');
    if (open == NULL || colon == NULL || open < colon) return 0;
    /* The matching close is the last `]` before the wrapper's closing brace. Taking
     * everything after the colon instead would carry that brace, which is exactly the
     * bug: the caller then splices a closed object into its own. */
    const char *close = strrchr(json, ']');
    if (close == NULL || close < open) return 0;
    size_t len = (size_t)(close - open + 1);
    if (len + 1 > cap) return 0;
    memcpy(out, open, len);
    out[len] = '\0';
    return len;
}

int llm_next_provider(int current, int count)
{
    int next = current + 1;
    /* The chain is ordered and tried once per attempt: wrapping would let a flapping
     * provider spend the budget in a loop (`02-firmware.md` §llm). */
    return next < count ? next : -1;
}

bool llm_provider_loop_ok(int index, int count)
{
    /* The guard the chain walk uses. `-1` is `llm_next_provider`'s "exhausted" and
     * must end the walk: a bare `index < count` is true for -1, reads
     * `providers[-1]` and then keeps walking, which is an out-of-bounds read plus a
     * loop that never returns. */
    return index >= 0 && index < count;
}

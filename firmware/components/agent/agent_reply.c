/* agent_reply — parse one chat-completion body into the loop's terms.
 *
 * Every provider in `03-config.md` §Provider compatibility returns
 * `choices[0].message` with `content` and optional `tool_calls[]`. Pure: a shaped
 * reply is a rule, and a rule the loop branches on is tested on the host. */

#include "agent.h"

#include <string.h>

#include "esp_log.h"

static const char *TAG = "agent";

static bool copy_bounded(char *dst, size_t cap, const char *src)
{
    if (src == NULL) return false;
    size_t n = strlen(src);
    if (n >= cap) return false;
    memcpy(dst, src, n + 1);
    return true;
}

/* `arguments` is the JSON object the model produced, carried as a *string* by every
 * provider in `03-config.md` §Provider compatibility. It must parse as an object,
 * because `tools` will validate it and a reply whose arguments are not an object is
 * a provider fault the next provider may not repeat (`02-firmware.md` §4.2).
 *
 * An already-decoded object is refused rather than re-emitted: `json_emit` writes a
 * whole document, so serialising one node would mean wrapping and unwrapping it. No
 * supported provider sends that shape, and silently half-handling it would make the
 * refusal message a lie. */
static bool copy_arguments(char *dst, size_t cap, const json_node_t *node)
{
    const char *text = json_string(node);
    if (text == NULL || !copy_bounded(dst, cap, text)) return false;

    json_doc_t *doc = NULL;
    if (json_parse(text, strlen(text), &doc) != ESP_OK) return false;
    bool ok = json_is_object(json_root(doc));
    json_free(doc);
    return ok;
}

/* Copy one string into the reply's `content` field, truncating at the frame cap.
 * `04` §3.14 makes truncation a reported fact, not a silent one; the cut length is
 * recorded and the real length kept in `content_full`. An over-long body is a
 * provider reply the frame cannot carry either way, so it is copied truncated and
 * the length recorded, letting one code path decide. */
static void copy_content(agent_reply_t *out, const char *text)
{
    size_t n = strlen(text);
    if (n >= sizeof out->content) {
        out->content_full = n;
        n = sizeof out->content - 1;
    }
    memcpy(out->content, text, n);
    out->content[n] = '\0';
    out->content_len = n;
}

/* Copy the model's reasoning trace into `out->thinking`, truncating at the frame
 * bound. The three member names are the ones the providers in `03-config.md`
 * §Provider compatibility use: Ollama's `message.thinking`, and the
 * OpenAI-compatible `reasoning_content` and `reasoning`. First present wins, because
 * a provider that sends two is naming the same trace twice. */
static void copy_thinking(agent_reply_t *out, const json_node_t *message)
{
    static const char *const NAMES[] = {"thinking", "reasoning_content", "reasoning"};
    for (size_t i = 0; i < sizeof NAMES / sizeof NAMES[0]; i++) {
        const char *text = json_string(json_get(message, NAMES[i]));
        if (text == NULL || text[0] == '\0') continue;
        size_t n = strlen(text);
        if (n >= sizeof out->thinking) {
            out->thinking_full = n;
            /* Cut on a character boundary: `04` §3.21 promises UTF-8, and half a
             * sequence is bytes the bridge would render as a replacement character. */
            n = store_utf8_prefix((const uint8_t *)text, sizeof out->thinking - 1);
        }
        memcpy(out->thinking, text, n);
        out->thinking[n] = '\0';
        out->thinking_len = n;
        return;
    }
}

esp_err_t agent_parse_reply(const char *body, size_t len, agent_reply_t *out)
{
    if (body == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof *out);

    json_doc_t *doc = NULL;
    if (json_parse(body, len, &doc) != ESP_OK) return ESP_ERR_INVALID_ARG;

    const json_node_t *root = json_root(doc);
    const json_node_t *choices = json_get(root, "choices");
    const json_node_t *first = json_array_at(choices, 0);
    const json_node_t *message = json_get(first, "message");

    /* Some providers report a refusal with an `error` member and no choices. */
    if (message == NULL) {
        const json_node_t *error = json_get(root, "error");
        const char *detail = json_string(json_get(error, "message"));
        if (detail != NULL) ESP_LOGW(TAG, "provider error: %s", detail);
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    const char *content = json_string(json_get(message, "content"));
    if (content != NULL) copy_content(out, content);

    /* `finish_reason` tells the loop whether `content` is a prefix and whether a cut
     * `arguments` string is the output cap rather than a provider fault. A reason
     * longer than the field is left empty: nothing branches on it. */
    copy_bounded(out->finish_reason, sizeof out->finish_reason,
                 json_string(json_get(first, "finish_reason")));

    const json_node_t *calls = json_get(message, "tool_calls");
    for (size_t i = 0; i < json_array_len(calls); i++) {
        if (out->call_count >= AGENT_MAX_TOOL_CALLS) {
            /* More calls than the round can carry: refused rather than dropped, since
             * a dropped call is a silently skipped action. */
            json_free(doc);
            return ESP_ERR_INVALID_ARG;
        }
        const json_node_t *call = json_array_at(calls, i);
        const json_node_t *fn = json_get(call, "function");
        const char *id = json_string(json_get(call, "id"));
        const char *name = json_string(json_get(fn, "name"));
        if (name == NULL || !copy_bounded(out->calls[out->call_count].name,
                                          sizeof out->calls[0].name, name)) {
            json_free(doc);
            return ESP_ERR_INVALID_ARG;
        }
        agent_tool_call_t *slot = &out->calls[out->call_count];
        if (id != NULL && !copy_bounded(slot->id, sizeof slot->id, id)) {
            json_free(doc);
            return ESP_ERR_INVALID_ARG;
        }
        const json_node_t *args = json_get(fn, "arguments");
        if (args != NULL) {
            if (!copy_arguments(slot->args, sizeof slot->args, args)) {
                /* The output limit cut the arguments: keep the call, empty, so the
                 * loop answers its `tool_call_id` with the named error. Dropping it
                 * would leave the assistant turn referencing a call that never gets
                 * a result, which providers reject for the rest of the run. */
                if (strcmp(out->finish_reason, AGENT_FINISH_LENGTH) == 0) {
                    slot->args[0] = '\0';
                    slot->args_truncated = true;
                } else {
                    json_free(doc);
                    return ESP_ERR_INVALID_ARG;
                }
            }
        } else {
            memcpy(slot->args, "{}", 3);
        }
        out->call_count++;
    }

    /* The trace is carried beside the answer, never as it: `agent.c` emits it as its
     * own `agent.thinking` frame (`04-protocol.md` §3.21). Read unconditionally —
     * gating it on an empty `content` was the defect that hid every trace a model
     * produced alongside a real answer. */
    copy_thinking(out, message);

    /* Some models reason and then stop, so `content` is empty while a trace exists;
     * read as-is that reached `agent.final` as `text:""`, a blank turn the reader
     * could not tell from a silent failure. The trace is promoted to the answer only
     * when there is no visible content *and* no tool call: an empty `content` beside
     * `tool_calls` is the normal shape of a tool round, and promoting the trace there
     * would replay it as the assistant turn. */
    if (out->content_len == 0 && out->call_count == 0 && out->thinking_len > 0) {
        copy_content(out, out->thinking);
        /* The trace *is* the answer now, so it must not also be framed as the
         * reader's preview of it: the bridge would show the same text twice. */
        out->thinking_len = 0;
        out->thinking_full = 0;
        out->thinking[0] = '\0';
    }

    int64_t value = 0;
    const json_node_t *usage = json_get(root, "usage");
    if (json_int(json_get(usage, "prompt_tokens"), &value)) {
        out->prompt_tokens = (int32_t)value;
    }
    if (json_int(json_get(usage, "completion_tokens"), &value)) {
        out->completion_tokens = (int32_t)value;
    }

    json_free(doc);
    return ESP_OK;
}

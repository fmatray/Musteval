/* tools_declared — compile one `/tools/<stem>.json` into a callable tool.
 *
 * `tools/web.md` §Declarative tools fixes the file's shape and `03-config.md` §tools
 * fixes every key's bound. The loader lives here rather than in board-only `tools.c`
 * for one reason: every rule in it is pure — parse, check, copy — and two of them
 * were wrong in ways no board run would surface. It read `url`, `method`, `query`,
 * `body`, `response_path` and `timeout_ms` from the root while both documents place
 * them under `http`, so a conforming file was dropped as "description and url are
 * required strings"; and `compile_headers` emitted the object into `out->headers`
 * and then wrote into it again after the emit buffer was freed. Host-tested here,
 * both fail a test instead of a boot. */

#include "tools.h"

#include <stdio.h>
#include <string.h>

#include "cfg.h"
#include "esp_log.h"

#define TAG "tools"

/* One `http.query` member as `key=value`, appended to a shared template buffer. A
 * string member is copied verbatim, so a `{{arg}}` placeholder survives to the bind
 * step; any other scalar is written as its literal text. */
static bool render_member(char *dst, size_t cap, size_t *used, const char *key,
                          const json_node_t *value, char *scratch, size_t scratch_cap)
{
    const char *text = json_string(value);
    if (text == NULL) {
        int64_t integer = 0;
        double real = 0;
        if (json_int(value, &integer)) {
            snprintf(scratch, scratch_cap, "%lld", (long long)integer);
        } else if (json_double(value, &real)) {
            snprintf(scratch, scratch_cap, "%.10g", real);
        } else {
            bool flag = false;
            if (!json_bool(value, &flag)) return false;
            snprintf(scratch, scratch_cap, "%s", flag ? "true" : "false");
        }
        text = scratch;
    }
    int wrote = snprintf(dst + *used, cap - *used, "%s%s=%s", *used > 0 ? "&" : "",
                         key != NULL ? key : "", text);
    if (wrote < 0 || (size_t)wrote >= cap - *used) return false;
    *used += (size_t)wrote;
    return true;
}

static void copy_field(char *dst, size_t cap, const json_node_t *src)
{
    const char *text = src != NULL ? json_string(src) : NULL;
    if (text == NULL) {
        dst[0] = '\0';
        return;
    }
    snprintf(dst, cap, "%s", text);
}

/* `http.headers` accepts a string (an `nvs:` ref) or an object of name → value.
 * Both are kept as raw text because `tools_call` re-parses them at call time, and
 * copying the tree twice per call would defeat the point of the cache. */
static esp_err_t compile_headers(const json_node_t *headers,
                                 tools_secret_probe_t probe, void *probe_ctx,
                                 tools_declared_t *out)
{
    if (headers == NULL || json_is_null(headers)) {
        out->headers[0] = '\0';
        return ESP_OK;
    }
    if (json_is_string(headers)) {
        const char *ref = json_string(headers);
        /* `03-config.md` check 10: a `nvs:` ref that does not resolve drops the
         * file, reported distinctly so the caller can log an E_CONFIG. */
        if (ref == NULL || strncmp(ref, "nvs:", 4) != 0) return ESP_ERR_INVALID_ARG;
        if (probe != NULL && !probe(ref + 4, probe_ctx)) return ESP_ERR_NOT_FOUND;
        snprintf(out->headers, sizeof out->headers, "%s", ref);
        return ESP_OK;
    }
    if (json_is_object(headers)) {
        /* Copy each member in, then emit the wrapper: `json_emit` serialises the
         * wrapper, so emitting an empty one would have stored `{}` regardless of the
         * file's headers. Iteration keeps the values borrowed until `json_add_node`
         * deep-copies them. */
        json_doc_t *doc = json_new_object();
        if (doc == NULL) return ESP_ERR_NO_MEM;
        json_node_t *dst = json_root(doc);
        size_t members = json_object_len(headers);
        for (size_t i = 0; dst != NULL && i < members; i++) {
            const char *key = json_object_key(headers, i);
            const json_node_t *value = json_object_value(headers, i);
            if (json_add_node(dst, key, value) == NULL) {
                json_free(doc);
                return ESP_ERR_NO_MEM;
            }
        }
        size_t written = 0;
        esp_err_t err = json_emit(doc, out->headers, sizeof out->headers, &written);
        json_free(doc);
        return err;
    }
    return ESP_ERR_INVALID_ARG;
}

esp_err_t tools_declared_compile(const char *stem, const char *body, size_t len,
                                 tools_secret_probe_t secret_resolves, void *probe_ctx,
                                 tools_declared_t *out, char *why, size_t why_cap)
{
    if (stem == NULL || body == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof *out);

    /* `tools/web.md`: a declarative file with a built-in's stem is dropped. */
    if (tools_is_builtin(stem)) {
        if (why) snprintf(why, why_cap, "%s is a built-in name", stem);
        return ESP_ERR_INVALID_ARG;
    }
    /* The stem is the tool name, so it obeys the same charset the rest do. */
    size_t stem_len = strlen(stem);
    if (stem_len == 0 || stem_len >= sizeof out->name) {
        if (why) snprintf(why, why_cap, "name length %u", (unsigned)stem_len);
        return ESP_ERR_INVALID_ARG;
    }
    snprintf(out->name, sizeof out->name, "%s", stem);

    json_doc_t *doc = NULL;
    if (json_parse(body, len, &doc) != ESP_OK) {
        if (why) snprintf(why, why_cap, "not valid JSON");
        return ESP_ERR_INVALID_ARG;
    }
    json_node_t *root = json_root(doc);
    if (!json_is_object(root)) {
        json_free(doc);
        if (why) snprintf(why, why_cap, "root is not an object");
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t err = ESP_OK;
    /* `03-config.md` §tools and `tools/web.md` put every HTTP member under `http`;
     * reading them from the root meant a conforming file had no `url` and was
     * dropped as "description and url are required strings". */
    const json_node_t *http = json_get(root, "http");
    if (!json_is_object(http)) {
        if (why) snprintf(why, why_cap, "http is not an object");
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }
    const json_node_t *desc = json_get(root, "description");
    const json_node_t *url = json_get(http, "url");
    if (!json_is_string(desc) || !json_is_string(url)) {
        err = ESP_ERR_INVALID_ARG;
        if (why) snprintf(why, why_cap, "description and url are required strings");
        goto done;
    }
    copy_field(out->description, sizeof out->description, desc);
    copy_field(out->url, sizeof out->url, url);

    /* `06-security.md` T11: an absolute `https` URL, verified by the gate too. A
     * plain-`http` URL is refused here so the drop happens at load, not per call. */
    if (strncmp(out->url, "https://", 8) != 0) {
        err = ESP_ERR_INVALID_ARG;
        if (why) snprintf(why, why_cap, "url is not absolute https");
        goto done;
    }

    const json_node_t *method = json_get(http, "method");
    copy_field(out->method, sizeof out->method,
               json_is_string(method) ? method : NULL);
    if (out->method[0] == '\0') snprintf(out->method, sizeof out->method, "GET");
    if (strcmp(out->method, "GET") != 0 && strcmp(out->method, "POST") != 0) {
        err = ESP_ERR_INVALID_ARG;
        if (why) snprintf(why, why_cap, "method is not GET or POST");
        goto done;
    }

    const json_node_t *path = json_get(http, "response_path");
    if (json_is_string(path)) {
        copy_field(out->response_path, sizeof out->response_path, path);
        /* `tools/web.md` §Declarative tools: a bare key, dotted for nesting. Rejecting
         * the shapes `json_path_get` cannot walk keeps the drop at load time. */
        for (const char *p = out->response_path; *p != '\0'; p++) {
            if (*p == '[' || *p == ' ') {
                err = ESP_ERR_INVALID_ARG;
                if (why) snprintf(why, why_cap, "response_path is not dotted keys");
                goto done;
            }
        }
    }

    /* `http.query` is an object in both shipped files (`weather_now`, `web_search`)
     * and a string in the schema's own example. An object is flattened here into the
     * one `k=v&k=v` template the binder expands, because a dropped object meant both
     * shipped tools sent no query at all. */
    const json_node_t *query = json_get(http, "query");
    if (json_is_string(query)) {
        copy_field(out->query, sizeof out->query, query);
    } else if (json_is_object(query)) {
        size_t used = 0;
        out->query[0] = '\0';
        for (size_t i = 0; i < json_object_len(query) && used < sizeof out->query; i++) {
            const char *key = json_object_key(query, i);
            char rendered[128];
            /* A member that does not fit drops the whole template: a query missing
             * one parameter reaches the provider as a 400 that names nothing. */
            if (!render_member(&out->query[0], sizeof out->query, &used, key,
                               json_object_value(query, i), rendered,
                               sizeof rendered)) {
                used = 0;
                break;
            }
        }
        if (used == 0 && json_object_len(query) > 0) out->query[0] = '\0';
    }
    const json_node_t *body_tpl = json_get(http, "body");
    if (json_is_string(body_tpl)) copy_field(out->body, sizeof out->body, body_tpl);

    const json_node_t *timeout = json_get(http, "timeout_ms");
    int64_t ms = 0;
    out->timeout_ms = (json_int(timeout, &ms) && ms > 0 && ms <= 60000)
                          ? (int32_t)ms
                          : TOOLS_HTTP_TIMEOUT_MS;

    const json_node_t *schema = json_get(root, "parameters");
    if (schema != NULL && json_is_object(schema)) {
        /* Kept as text for the same reason the built-ins' schemas are: `tools_json`
         * embeds it verbatim. */
        json_doc_t *tmp = json_new_object();
        if (tmp == NULL) {
            err = ESP_ERR_NO_MEM;
            goto done;
        }
        json_add_node(json_root(tmp), "parameters", schema);
        size_t written = 0;
        /* Emitted straight into `out->schema` rather than a local of the same size.
         * A 2 KiB local here is a 2144-byte frame, and `agent_start` reaches this
         * through `tools_context_from_config` on the main task's 3584-byte stack —
         * that overflowed and panic-looped the device. The capacity is the same
         * either way, so this is the same refusal with no second buffer. */
        err = json_emit(tmp, out->schema, sizeof out->schema, &written);
        json_free(tmp);
        if (err != ESP_OK) {
            if (why) snprintf(why, why_cap, "parameters does not fit");
            goto done;
        }
        /* Re-slice just the value: `json_emit` wrapped it under "parameters", so the
         * text is `{"parameters":{...}}` and both the wrapper's opening text and its
         * closing brace have to go. Keeping that brace made every declarative
         * schema malformed, and `tools_json` answered by emitting the empty fallback
         * — a tool the model was offered with no parameters at all. */
        const char *colon = strchr(out->schema, ':');
        if (colon != NULL) {
            const char *value = colon + 1;
            size_t value_len = strlen(value);
            if (value_len > 0 && value[value_len - 1] == '}') value_len--;
            memmove(out->schema, value, value_len);
            out->schema[value_len] = '\0';
        } else {
            err = ESP_ERR_INVALID_ARG;
            if (why) snprintf(why, why_cap, "parameters did not emit");
            goto done;
        }
    } else {
        snprintf(out->schema, sizeof out->schema,
                 "{\"type\":\"object\",\"properties\":{},\"additionalProperties\":false}");
    }

    err = compile_headers(json_get(http, "headers"), secret_resolves, probe_ctx, out);
    if (err != ESP_OK && why) {
        snprintf(why, why_cap, err == ESP_ERR_NOT_FOUND
                                   ? "an nvs: header ref does not resolve"
                                   : "http.headers is not an object");
    }

done:
    json_free(doc);
    return err;
}


/* --- Binding -------------------------------------------------------------- */

static bool arg_is_declared(const tools_declared_t *tool, const char *key)
{
    /* The schema is the authority on what may be bound (`tools/web.md`
     * §Parameter binding). Walking the emitted text is enough: it is the exact
     * object the model was shown. */
    char needle[96];
    snprintf(needle, sizeof needle, "\"%s\"", key);
    return strstr(tool->schema, needle) != NULL;
}

/* Expand `{{name}}` from `args`. A placeholder whose name the schema does not declare
 * is `E_TOOL`, so a file's typo cannot become an argument injection. */
esp_err_t tools_bind_text(const tools_declared_t *tool, const char *tpl,
                          const json_node_t *args, char *out, size_t cap,
                          char *why, size_t why_cap)
{
    size_t used = 0;
    if (out == NULL || cap == 0) return ESP_ERR_INVALID_ARG;
    out[0] = '\0';
    if (tpl == NULL) return ESP_OK;

    for (const char *p = tpl; *p != '\0';) {
        if (p[0] != '{' || p[1] != '{') {
            if (used + 1 >= cap) return ESP_ERR_INVALID_SIZE;
            out[used++] = *p++;
            out[used] = '\0';
            continue;
        }
        const char *close = strstr(p + 2, "}}");
        if (close == NULL) return ESP_ERR_INVALID_ARG;
        char key[48];
        size_t key_len = (size_t)(close - (p + 2));
        if (key_len == 0 || key_len >= sizeof key) return ESP_ERR_INVALID_ARG;
        memcpy(key, p + 2, key_len);
        key[key_len] = '\0';

        if (!arg_is_declared(tool, key)) {
            if (why != NULL) snprintf(why, why_cap, "%s: %s is not a declared argument",
                                      tool->name, key);
            return ESP_ERR_INVALID_ARG;
        }
        /* A placeholder is always a string position in the request, so a numeric
         * argument is rendered rather than refused: `weather_now` takes
         * `latitude` and `longitude` as numbers and puts them in the query. */
        const json_node_t *value = json_get(args, key);
        const char *text = json_string(value);
        char rendered[64];
        if (text == NULL && value != NULL) {
            int64_t integer = 0;
            double real = 0;
            if (json_int(value, &integer)) {
                snprintf(rendered, sizeof rendered, "%lld", (long long)integer);
                text = rendered;
            } else if (json_double(value, &real)) {
                snprintf(rendered, sizeof rendered, "%.10g", real);
                text = rendered;
            }
        }
        if (text == NULL) {
            if (why != NULL) snprintf(why, why_cap, "%s: argument %s is required",
                                      tool->name, key);
            return ESP_ERR_INVALID_ARG;
        }
        size_t text_len = strlen(text);
        if (used + text_len >= cap) return ESP_ERR_INVALID_SIZE;
        memcpy(out + used, text, text_len);
        used += text_len;
        out[used] = '\0';
        p = close + 2;
    }
    return ESP_OK;
}

/* The compiled host, extracted through the same helper the built-in `http.get` gate
 * uses, so both apply one definition of "host". */
const char *tools_declared_host(const tools_declared_t *tool, char *buf, size_t cap)
{
    if (tool == NULL) return NULL;
    return tools_url_host(tool->url, buf, cap);
}

const tools_declared_t *tools_declared_find(const tools_declared_set_t *set,
                                            const char *name)
{
    if (set == NULL || name == NULL) return NULL;
    for (size_t i = 0; i < set->count; i++) {
        if (strcmp(set->items[i].name, name) == 0) return &set->items[i];
    }
    return NULL;
}

/* The result's `content`, cut at the model-facing cap. The cap is
 * `TOOLS_HTTP_BODY_MAX` rather than `TOOLS_RESULT_MAX` because the text is escaped
 * into a result object that must itself fit `TOOLS_RESULT_MAX` (`02-firmware.md`
 * §7.1), which is the same reason `http.get` uses it. The cut backs up to a character
 * boundary so the result is never half a UTF-8 sequence, and reports itself. */
void tools_result_add_content(json_node_t *root, const char *text)
{
    size_t full = strlen(text);
    size_t kept = full > TOOLS_HTTP_BODY_MAX ? TOOLS_HTTP_BODY_MAX : full;
    while (kept > 0 && kept < full && (text[kept] & 0xC0) == 0x80) kept--;
    char *copy = malloc(kept + 1);
    if (copy == NULL) return;
    memcpy(copy, text, kept);
    copy[kept] = '\0';
    json_add_string(root, "content", copy);
    free(copy);
    if (kept < full) json_add_bool(root, "truncated", true);
}

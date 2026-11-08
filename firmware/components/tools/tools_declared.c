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

    const json_node_t *query = json_get(http, "query");
    if (json_is_string(query)) copy_field(out->query, sizeof out->query, query);
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
        char text[sizeof out->schema];
        err = json_emit(tmp, text, sizeof text, &written);
        json_free(tmp);
        if (err != ESP_OK) {
            if (why) snprintf(why, why_cap, "parameters does not fit");
            goto done;
        }
        /* Re-slice just the value: `json_emit` wrapped it under "parameters". */
        const char *colon = strchr(text, ':');
        if (colon != NULL) {
            snprintf(out->schema, sizeof out->schema, "%s", colon + 1);
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

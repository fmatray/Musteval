/* tools_declared_io — the loader and the caller for `/tools/<stem>.json`.
 *
 * Split from `tools_declared.c` for the same reason `mcp_parse.c` and `mcp.c` are
 * split: `store_read_file`, `cfg_resolve_secret` and `net_request` are board-side, so
 * a host test of them tests a stub. Every rule that decides whether a file is kept,
 * what its template binds to and how a reply is projected stays host-tested next
 * door; this file only moves bytes. `tools.c`'s CMakeLists keeps it off the `linux`
 * target. */

#include "tools.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "esp_log.h"

#define TAG "tools"

/* The dotted keys `json_path_get` cannot walk are refused at compile time, so this
 * only has to handle a reply that is not JSON or a path that no longer resolves. */
static esp_err_t project_reply(const tools_declared_t *tool, const char *body,
                               json_doc_t **out, char *why, size_t why_cap)
{
    json_doc_t *parsed = NULL;
    json_node_t *value = NULL;

    if (tool->response_path[0] != '\0') {
        if (json_parse(body, strlen(body), &parsed) != ESP_OK) {
            if (why != NULL) snprintf(why, why_cap, "%s: the reply is not JSON", tool->name);
            return ESP_ERR_INVALID_RESPONSE;
        }
        value = json_path_get(parsed, tool->response_path);
        if (value == NULL) {
            json_free(parsed);
            if (why != NULL) {
                snprintf(why, why_cap, "%s: response_path %s did not resolve", tool->name,
                         tool->response_path);
            }
            return ESP_ERR_NOT_FOUND;
        }
    }

    *out = json_new_object();
    if (*out == NULL) {
        json_free(parsed);
        return ESP_ERR_NO_MEM;
    }
    json_node_t *root = json_root(*out);

    if (value != NULL) {
        json_doc_t *tmp = json_new_object();
        char *text = tmp != NULL ? malloc(TOOLS_RESULT_MAX) : NULL;
        if (text == NULL) {
            json_free(tmp);
            json_free(parsed);
            json_free(*out);
            *out = NULL;
            return ESP_ERR_NO_MEM;
        }
        json_add_node(json_root(tmp), "v", value);
        size_t written = 0;
        esp_err_t err = json_emit(tmp, text, TOOLS_RESULT_MAX, &written);
        json_free(tmp);
        json_free(parsed);
        if (err != ESP_OK) {
            free(text);
            json_free(*out);
            *out = NULL;
            if (why != NULL) snprintf(why, why_cap, "%s: the projection does not fit",
                                      tool->name);
            return ESP_ERR_INVALID_SIZE;
        }
        /* `json_emit` wrapped the value under a key, so the text is `{"v":...}` and
         * both the prefix and the closing brace belong to the wrapper, not the value. */
        const char *colon = strchr(text, ':');
        const char *start = colon != NULL ? colon + 1 : text;
        size_t len = strlen(start);
        if (len > 0 && start[len - 1] == '}') len--;
        char *joined = malloc(len + 1);
        if (joined == NULL) {
            free(text);
            json_free(*out);
            *out = NULL;
            return ESP_ERR_NO_MEM;
        }
        memcpy(joined, start, len);
        joined[len] = '\0';
        /* The same cap and the same note as the unprojected body: `tools/web.md` says
         * a result is cut at the model-facing cap with `truncated: true`, and a
         * projection that silently arrived whole at 3 KiB while a raw body was cut at
         * 2 KiB would be two rules for one limit. */
        tools_result_add_content(root, joined);
        free(joined);
        free(text);
        return ESP_OK;
    }

    /* No projection: the body itself, cut at the model-facing cap with the cut
     * reported, the same rule `http.get` applies to a long reply (`docs/tools/web.md`).
     * The cut backs up to a character boundary so the result is never half a UTF-8
     * sequence. */
    char *copy = malloc(strlen(body) + 1);
    if (copy == NULL) {
        json_free(parsed);
        json_free(*out);
        *out = NULL;
        return ESP_ERR_NO_MEM;
    }
    strcpy(copy, body);
    tools_result_add_content(root, copy);
    free(copy);
    json_free(parsed);
    return ESP_OK;
}

/* --- Loading -------------------------------------------------------------- */

#define DECLARED_EXT ".json"
#define DECLARED_BODY_MAX 4096

/* `03-config.md` check 10: an `nvs:` header ref that does not resolve drops the tool
 * at load, so it can never be offered and then fail per call. */
static bool ref_resolves(const char *key, void *ctx)
{
    (void)ctx;
    char value[CFG_SECRET_MAX];
    size_t len = 0;
    esp_err_t err = cfg_resolve_secret(key, value, sizeof value, &len);
    if (err == ESP_OK) memset(value, 0, sizeof value);
    return err == ESP_OK;
}

esp_err_t tools_declared_load(tools_declared_set_t *set)
{
    if (set == NULL) return ESP_ERR_INVALID_ARG;
    memset(set, 0, sizeof *set);

    /* `/tools` is part of the image, not the sandbox, so the image rule admits it.
     * The listing is heap, not a local: this runs on whatever stack first asks for
     * the set, and `hello` asks on the link task. 1.3 KiB of entries beside the path
     * and the parse is what overflowed that 4 KiB stack. */
    store_entry_t *entries = malloc(TOOLS_MAX_DECLARED * sizeof *entries);
    if (entries == NULL) return ESP_ERR_NO_MEM;
    size_t count = 0;
    esp_err_t err = store_list_image_dir("/tools", entries, TOOLS_MAX_DECLARED, &count);
    if (err != ESP_OK) {
        free(entries);
        /* A board whose image carries no `/tools/` has built-ins only. */
        return err == ESP_ERR_NOT_FOUND ? ESP_OK : err;
    }

    char *body = malloc(DECLARED_BODY_MAX);
    if (body == NULL) {
        free(entries);
        return ESP_ERR_NO_MEM;
    }

    for (size_t i = 0; i < count && set->count < TOOLS_MAX_DECLARED; i++) {
        const char *name = entries[i].name;
        size_t name_len = strlen(name);
        size_t ext_len = strlen(DECLARED_EXT);
        if (name_len <= ext_len || strcmp(name + name_len - ext_len, DECLARED_EXT) != 0) {
            continue;
        }
        char stem[sizeof set->items[0].name];
        size_t stem_len = name_len - ext_len;
        if (stem_len >= sizeof stem) continue;
        memcpy(stem, name, stem_len);
        stem[stem_len] = '\0';

        char path[STORE_ENTRY_NAME_MAX + 8];
        snprintf(path, sizeof path, "/tools/%s", name);
        size_t len = 0;
        if (store_read_file(path, body, DECLARED_BODY_MAX, &len) != ESP_OK) {
            ESP_LOGW(TAG, "%s: unreadable, dropped", path);
            continue;
        }
        char why[TOOLS_ERR_MAX] = "";
        err = tools_declared_compile(stem, body, len, ref_resolves, NULL,
                                     &set->items[set->count], why, sizeof why);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "%s: %s: dropped", path,
                     why[0] != '\0' ? why : esp_err_to_name(err));
            continue;
        }
        set->count++;
    }
    free(body);
    free(entries);
    return ESP_OK;
}

/* --- Calling -------------------------------------------------------------- */


#define DECLARED_HEADERS 8

/* Expand the compiled header values from the call's arguments, resolve an `nvs:` ref
 * in each, and perform the request. The value buffers are heap because `net` reads
 * them only for the exchange. */
static esp_err_t request(const tools_declared_t *tool, const json_node_t *args,
                         const char *url, const char *payload, json_doc_t **out,
                         char *why, size_t why_cap)
{
    /* Heap, not statics: 8 × 512 bytes of permanent DIRAM for values that live for
     * one exchange is the same trade `sched` and `agent` refuse. */
    char (*names)[48] = malloc(DECLARED_HEADERS * sizeof *names);
    char (*values)[CFG_SECRET_MAX] = malloc(DECLARED_HEADERS * sizeof *values);
    const char *name_ptrs[DECLARED_HEADERS];
    const char *value_ptrs[DECLARED_HEADERS];
    size_t count = 0;
    esp_err_t result = ESP_ERR_NO_MEM;

    if (names == NULL || values == NULL) goto release;

    if (tool->headers[0] != '\0') {
        json_doc_t *hdr = NULL;
        if (json_parse(tool->headers, strlen(tool->headers), &hdr) != ESP_OK) {
            if (why != NULL) snprintf(why, why_cap, "%s: http.headers is not an object",
                                      tool->name);
            result = ESP_ERR_INVALID_ARG;
            goto release;
        }
        const json_node_t *root = json_root(hdr);
        for (size_t i = 0; i < json_object_len(root) && count < DECLARED_HEADERS; i++) {
            const char *key = json_object_key(root, i);
            const char *value = json_string(json_object_value(root, i));
            if (key == NULL || value == NULL) continue;

            /* `tools/web.md` allows a `{{arg}}` in a header value, so the call's
             * arguments reach here; passing NULL silently sent the placeholder. */
            char expanded[CFG_SECRET_MAX];
            esp_err_t err = tools_bind_text(tool, value, args, expanded,
                                            sizeof expanded, why, why_cap);
            if (err != ESP_OK) {
                json_free(hdr);
                result = err;
                goto release;
            }
            if (strncmp(expanded, "nvs:", 4) == 0) {
                /* `06-security.md` §7: the ref is replaced out of the model's sight,
                 * so only the header carries the value and no frame ever shows it. */
                size_t len = 0;
                if (cfg_resolve_secret(expanded + 4, values[count], sizeof values[count],
                                       &len) != ESP_OK) {
                    json_free(hdr);
                    if (why != NULL) {
                        snprintf(why, why_cap, "%s: %s does not resolve", tool->name,
                                 expanded);
                    }
                    result = ESP_ERR_NOT_FOUND;
                    goto release;
                }
            } else {
                snprintf(values[count], sizeof values[count], "%s", expanded);
            }
            memset(expanded, 0, sizeof expanded);
            snprintf(names[count], sizeof names[count], "%s", key);
            name_ptrs[count] = names[count];
            value_ptrs[count] = values[count];
            count++;
        }
        json_free(hdr);
    }

    net_response_t resp;
    esp_err_t err = net_request(tool->method, url, name_ptrs, value_ptrs, count,
                                payload[0] != '\0' ? payload : NULL, strlen(payload),
                                tool->timeout_ms, &resp);
    /* Zero the resolved header values as soon as the client is done with them. */
    for (size_t i = 0; i < count; i++) memset(values[i], 0, sizeof values[i]);

    if (err != ESP_OK) {
        if (why != NULL) snprintf(why, why_cap, "%s: the request failed", tool->name);
        result = err;
        goto release;
    }
    size_t body_len = resp.body != NULL ? resp.body_len : 0;
    if (resp.status >= 400) {
        /* The exchange worked and the server refused, which is the tool failing: the
         * status is named so the model can tell a 404 from a transport fault. */
        free(resp.body);
        if (why != NULL) {
            snprintf(why, why_cap, "%s: the server answered %d", tool->name,
                     (int)resp.status);
        }
        result = ESP_ERR_INVALID_RESPONSE;
        goto release;
    }

    char *body = malloc(body_len + 1);
    if (body == NULL) {
        free(resp.body);
        goto release;
    }
    if (body_len > 0) memcpy(body, resp.body, body_len);
    body[body_len] = '\0';
    free(resp.body);

    result = project_reply(tool, body, out, why, why_cap);
    free(body);

release:
    free(names);
    free(values);
    return result;
}

esp_err_t tools_declared_call(const tools_declared_t *tool, const json_node_t *args,
                              const tools_policy_t *policy, json_doc_t **out,
                              char *why, size_t why_cap)
{
    if (tool == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    if (why != NULL && why_cap > 0) why[0] = '\0';

    /* The host is compiled in, not supplied by the model, so this is the only place a
     * declarative tool can be pointed. Checked before binding, so an unallowlisted
     * file never expands an argument either. */
    char host[128];
    const char *requested = tools_declared_host(tool, host, sizeof host);
    if (requested == NULL || !tools_host_allowed(policy, requested)) {
        if (why != NULL) {
            snprintf(why, why_cap, "%s refused: %s is not in tools.http_allow_hosts",
                     tool->name, requested != NULL ? requested : tool->url);
        }
        return ESP_ERR_NOT_ALLOWED;
    }

    char *url = malloc(TOOLS_RESULT_MAX);
    char *query = malloc(TOOLS_RESULT_MAX);
    char *payload = malloc(TOOLS_RESULT_MAX);
    esp_err_t err = ESP_ERR_NO_MEM;
    if (url == NULL || query == NULL || payload == NULL) goto cleanup;

    err = tools_bind_text(tool, tool->url, args, url, TOOLS_RESULT_MAX, why, why_cap);
    if (err != ESP_OK) goto cleanup;
    err = tools_bind_text(tool, tool->query, args, query, TOOLS_RESULT_MAX, why, why_cap);
    if (err != ESP_OK) goto cleanup;
    err = tools_bind_text(tool, tool->body, args, payload, TOOLS_RESULT_MAX, why, why_cap);
    if (err != ESP_OK) goto cleanup;

    if (query[0] != '\0') {
        /* The query is appended to the compiled URL, so it never replaces its path. */
        char *joined = malloc(TOOLS_RESULT_MAX);
        if (joined == NULL) { err = ESP_ERR_NO_MEM; goto cleanup; }
        snprintf(joined, TOOLS_RESULT_MAX, "%s%s%s", url,
                 strchr(url, '?') != NULL ? "&" : "?", query);
        free(url);
        url = joined;
    }

    err = request(tool, args, url, payload, out, why, why_cap);

cleanup:
    free(url);
    free(query);
    free(payload);
    return err;
}

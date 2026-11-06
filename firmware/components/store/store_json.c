/* store_json.c — the only JSON entry points in the firmware (`08-code-architecture.md` §3).
 * cJSON is an implementation detail of this file: no other module may call it. */

#include "store.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"

struct json_doc {
    cJSON *root;
};

/* A caller-visible node is a cJSON node; the wrapper types exist only to keep
 * cJSON out of the public header (`08-code-architecture.md` §2 rule 5). */
static const cJSON *node_of(const json_node_t *n) { return (const cJSON *)(const void *)n; }
static json_node_t *node_out(cJSON *n) { return (json_node_t *)n; }

typedef struct {
    char *buf;
    size_t cap;
    size_t len;
} sink_t;

static esp_err_t put(sink_t *s, const char *p, size_t n)
{
    if (s->buf != NULL) {
        if (s->len + n > s->cap) {
            return ESP_ERR_INVALID_SIZE;
        }
        memcpy(s->buf + s->len, p, n);
    }
    s->len += n;
    return ESP_OK;
}

static esp_err_t put_string(sink_t *s, const char *str)
{
    if (str == NULL) return ESP_ERR_INVALID_ARG;
    esp_err_t err = put(s, "\"", 1);
    for (const unsigned char *p = (const unsigned char *)str; err == ESP_OK && *p; p++) {
        const char *lit = NULL;
        switch (*p) {
        case '"': lit = "\\\""; break;
        case '\\': lit = "\\\\"; break;
        case '\b': lit = "\\b"; break;
        case '\f': lit = "\\f"; break;
        case '\n': lit = "\\n"; break;
        case '\r': lit = "\\r"; break;
        case '\t': lit = "\\t"; break;
        default:
            if (*p < 0x20) {
                char esc[7];
                snprintf(esc, sizeof esc, "\\u%04x", *p);
                err = put(s, esc, 6);
            } else {
                err = put(s, (const char *)p, 1);
            }
            continue;
        }
        err = put(s, lit, strlen(lit));
    }
    return err == ESP_OK ? put(s, "\"", 1) : err;
}

static esp_err_t put_number(sink_t *s, const cJSON *n)
{
    char tmp[32];
    int len;
    double d = n->valuedouble;
    if (d == (double)n->valueint) {
        len = snprintf(tmp, sizeof tmp, "%lld", (long long)n->valueint);
    } else {
        len = snprintf(tmp, sizeof tmp, "%.17g", d);
    }
    if (len < 0 || (size_t)len >= sizeof tmp) {
        return ESP_ERR_INVALID_ARG;
    }
    return put(s, tmp, (size_t)len);
}

static esp_err_t put_value(sink_t *s, const cJSON *n);

static esp_err_t put_object(sink_t *s, const cJSON *obj)
{
    size_t n = 0;
    for (const cJSON *c = obj->child; c; c = c->next) n++;
    const cJSON **keys = NULL;
    if (n > 0) {
        keys = malloc(n * sizeof *keys);
        if (keys == NULL) return ESP_ERR_NO_MEM;
    }
    size_t i = 0;
    for (const cJSON *c = obj->child; c; c = c->next) keys[i++] = c;
    
    /* Bytewise sort (`AGENTS.md` rule 4). `n > 1` guards the `n - 1` underflow
     * that would otherwise spin SIZE_MAX times on an empty object. */
    for (size_t a = 0; n > 1 && a < n - 1; a++) {
        for (size_t b = a + 1; b < n; b++) {
            if (strcmp(keys[b]->string, keys[a]->string) < 0) {
                const cJSON *tmp = keys[a]; keys[a] = keys[b]; keys[b] = tmp;
            }
        }
    }
    esp_err_t err = put(s, "{", 1);
    for (i = 0; err == ESP_OK && i < n; i++) {
        if (i > 0) err = put(s, ",", 1);
        if (err == ESP_OK) err = put_string(s, keys[i]->string);
        if (err == ESP_OK) err = put(s, ":", 1);
        if (err == ESP_OK) err = put_value(s, keys[i]);
    }
    free(keys);
    return err == ESP_OK ? put(s, "}", 1) : err;
}

static esp_err_t put_array(sink_t *s, const cJSON *arr)
{
    esp_err_t err = put(s, "[", 1);
    bool first = true;
    for (const cJSON *c = arr->child; err == ESP_OK && c; c = c->next) {
        if (!first) err = put(s, ",", 1);
        first = false;
        if (err == ESP_OK) err = put_value(s, c);
    }
    return err == ESP_OK ? put(s, "]", 1) : err;
}

static esp_err_t put_value(sink_t *s, const cJSON *n)
{
    if (n == NULL) return put(s, "null", 4);
    if (cJSON_IsObject(n)) return put_object(s, n);
    if (cJSON_IsArray(n)) return put_array(s, n);
    if (cJSON_IsString(n)) return put_string(s, n->valuestring);
    if (cJSON_IsNumber(n)) return put_number(s, n);
    if (cJSON_IsBool(n)) return put(s, cJSON_IsTrue(n) ? "true" : "false", cJSON_IsTrue(n) ? 4 : 5);
    if (cJSON_IsNull(n)) return put(s, "null", 4);
    return ESP_ERR_INVALID_ARG;
}

esp_err_t json_parse(const char *buf, size_t len, json_doc_t **out)
{
    if (buf == NULL || len == 0 || out == NULL) return ESP_ERR_INVALID_ARG;
    *out = NULL;
    const char *end = NULL;
    cJSON *root = cJSON_ParseWithLengthOpts(buf, len, &end, false);
    if (root == NULL) return ESP_ERR_INVALID_ARG;
    /* Exactly one value: remainder must be whitespace only. */
    while (end < buf + len) {
        char c = *end;
        if (c != ' ' && c != '\t' && c != '\n' && c != '\r') {
            cJSON_Delete(root);
            return ESP_ERR_INVALID_ARG;
        }
        end++;
    }
    json_doc_t *doc = malloc(sizeof *doc);
    if (doc == NULL) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }
    doc->root = root;
    *out = doc;
    return ESP_OK;
}

esp_err_t json_emit(json_doc_t *doc, char *buf, size_t cap, size_t *written)
{
    if (doc == NULL || buf == NULL || written == NULL) return ESP_ERR_INVALID_ARG;
    *written = 0;
    if (cap == 0) return ESP_ERR_INVALID_SIZE;
    buf[0] = '\0';
    sink_t size = { NULL, 0, 0 };
    esp_err_t err = put_value(&size, doc->root);
    if (err != ESP_OK) return err;
    /* the NUL terminator after *written does not count toward cap (`store.h`) */
    if (size.len > cap) {
        return ESP_ERR_INVALID_SIZE;
    }
    sink_t out = { buf, cap, 0 };
    err = put_value(&out, doc->root);
    if (err != ESP_OK) {
        buf[0] = '\0';
        return err;
    }
    buf[out.len] = '\0';
    *written = out.len;
    return ESP_OK;
}

void json_free(json_doc_t *doc)
{
    if (doc) {
        cJSON_Delete(doc->root);
        free(doc);
    }
}

/* --- Builders ------------------------------------------------------------- */

json_doc_t *json_new_object(void)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) return NULL;
    json_doc_t *doc = malloc(sizeof *doc);
    if (doc == NULL) {
        cJSON_Delete(root);
        return NULL;
    }
    doc->root = root;
    return doc;
}

/* `cJSON_AddItemToObject` takes ownership of `item`. Adding is only legal to an
 * object, and a duplicate key would corrupt the document, so it is refused. */
static bool add_item(json_node_t *obj, const char *key, cJSON *item)
{
    if (item == NULL) return false;
    cJSON *parent = (cJSON *)(void *)obj;
    if (!cJSON_IsObject(parent) || key == NULL ||
        cJSON_GetObjectItemCaseSensitive(parent, key) != NULL) {
        cJSON_Delete(item);
        return false;
    }
    cJSON_AddItemToObject(parent, key, item);
    return true;
}

bool json_add_string(json_node_t *obj, const char *key, const char *value)
{
    if (value == NULL) return false;
    return add_item(obj, key, cJSON_CreateString(value));
}

bool json_add_int(json_node_t *obj, const char *key, int64_t value)
{
    return add_item(obj, key, cJSON_CreateNumber((double)value));
}

bool json_add_bool(json_node_t *obj, const char *key, bool value)
{
    return add_item(obj, key, cJSON_CreateBool(value));
}

bool json_add_null(json_node_t *obj, const char *key)
{
    return add_item(obj, key, cJSON_CreateNull());
}

json_node_t *json_add_object(json_node_t *obj, const char *key)
{
    cJSON *child = cJSON_CreateObject();
    if (!add_item(obj, key, child)) return NULL;
    return node_out(child);
}

json_node_t *json_new_array(json_node_t *parent, const char *key)
{
    cJSON *child = cJSON_CreateArray();
    if (!add_item(parent, key, child)) return NULL;
    return node_out(child);
}

json_node_t *json_array_push_string(json_node_t *array, const char *value)
{
    if (value == NULL || !cJSON_IsArray((cJSON *)(void *)array)) return NULL;
    cJSON *item = cJSON_CreateString(value);
    if (item == NULL) return NULL;
    cJSON_AddItemToArray((cJSON *)(void *)array, item);
    return node_out(item);
}

json_node_t *json_root(json_doc_t *doc)
{
    return doc ? node_out(doc->root) : NULL;
}

json_node_t *json_get(const json_node_t *obj, const char *key)
{
    if (obj == NULL || key == NULL) return NULL;
    return node_out(cJSON_GetObjectItemCaseSensitive(node_of(obj), key));
}

bool json_is_object(const json_node_t *node) { return cJSON_IsObject(node_of(node)); }
bool json_is_array(const json_node_t *node) { return cJSON_IsArray(node_of(node)); }
bool json_is_string(const json_node_t *node) { return cJSON_IsString(node_of(node)); }
bool json_is_bool(const json_node_t *node) { return cJSON_IsBool(node_of(node)); }
bool json_is_null(const json_node_t *node) { return cJSON_IsNull(node_of(node)); }

bool json_is_int(const json_node_t *node)
{
    int64_t v;
    return json_int(node, &v);
}

bool json_int(const json_node_t *node, int64_t *out)
{
    const cJSON *n = node_of(node);
    if (out == NULL || !cJSON_IsNumber(n)) return false;
    double d = n->valuedouble;
    /* Integral and inside int64 range: NaN fails bounds (`03-config.md` §config.json types). */
    if (!(d >= -9223372036854775808.0 && d < 9223372036854775808.0) || d != (double)(int64_t)d) {
        return false;
    }
    *out = (int64_t)d;
    return true;
}

const char *json_string(const json_node_t *node)
{
    const cJSON *n = node_of(node);
    return (cJSON_IsString(n) && n->valuestring) ? n->valuestring : NULL;
}

bool json_bool(const json_node_t *node, bool *out)
{
    const cJSON *n = node_of(node);
    if (out == NULL || !cJSON_IsBool(n)) return false;
    *out = cJSON_IsTrue(n);
    return true;
}

size_t json_array_len(const json_node_t *node)
{
    const cJSON *n = node_of(node);
    if (!cJSON_IsArray(n)) return 0;
    int len = cJSON_GetArraySize(n);
    return (len < 0) ? 0 : (size_t)len;
}

json_node_t *json_array_at(const json_node_t *node, size_t index)
{
    const cJSON *n = node_of(node);
    if (!cJSON_IsArray(n)) return NULL;
    return node_out(cJSON_GetArrayItem(n, (int)index));
}

json_node_t *json_array_push_object(json_node_t *array)
{
    if (array == NULL || !cJSON_IsArray((cJSON *)(void *)array)) return NULL;
    cJSON *obj = cJSON_CreateObject();
    if (obj == NULL) return NULL;
    cJSON_AddItemToArray((cJSON *)(void *)array, obj);
    return node_out(obj);
}

/* --- Dotted paths (`04-protocol.md` §4.8) ---------------------------------- */

/* Walk `path` to the node *before* the last segment. A numeric segment indexes an
 * array, anything else is an object key. Returns NULL on the first miss, so a
 * caller can tell "no such path" from "wrong type" only by the same NULL: both are
 * E_CONFIG on the wire. `*last` receives the final segment. */
static json_node_t *walk_parent(json_doc_t *doc, const char *path,
                                char *last, size_t last_cap)
{
    if (doc == NULL || path == NULL || *path == '\0' || last == NULL || last_cap == 0) {
        return NULL;
    }
    json_node_t *cur = json_root(doc);
    const char *p = path;
    for (;;) {
        const char *dot = strchr(p, '.');
        size_t seg = (dot == NULL) ? strlen(p) : (size_t)(dot - p);
        if (seg == 0 || seg >= last_cap) return NULL;
        if (dot == NULL) {
            memcpy(last, p, seg);
            last[seg] = '\0';
            return json_is_object(cur) || json_is_array(cur) ? cur : NULL;
        }
        char key[64];
        if (seg >= sizeof key) return NULL;
        memcpy(key, p, seg);
        key[seg] = '\0';
        bool numeric = true;
        for (size_t i = 0; i < seg; i++) {
            if (key[i] < '0' || key[i] > '9') { numeric = false; break; }
        }
        if (numeric) {
            cur = json_array_at(cur, (size_t)strtoul(key, NULL, 10));
        } else {
            cur = json_get(cur, key);
        }
        if (cur == NULL) return NULL;
        p = dot + 1;
    }
}

json_node_t *json_path_get(json_doc_t *doc, const char *path)
{
    char last[64];
    json_node_t *parent = walk_parent(doc, path, last, sizeof last);
    if (parent == NULL) return NULL;
    bool numeric = true;
    for (const char *c = last; *c != '\0'; c++) {
        if (*c < '0' || *c > '9') { numeric = false; break; }
    }
    if (numeric) return json_array_at(parent, (size_t)strtoul(last, NULL, 10));
    return json_get(parent, last);
}

bool json_path_set(json_doc_t *doc, const char *path, const json_node_t *value)
{
    if (value == NULL) return false;
    char last[64];
    json_node_t *parent = walk_parent(doc, path, last, sizeof last);
    if (parent == NULL) return false;

    bool numeric = true;
    for (const char *c = last; *c != '\0'; c++) {
        if (*c < '0' || *c > '9') { numeric = false; break; }
    }
    /* cJSON owns what it is given, so a copy is made first and destroyed on a
     * failed insert; the source node stays owned by its own document. */
    cJSON *copy = cJSON_Duplicate(node_of(value), true);
    if (copy == NULL) return false;
    if (numeric) {
        size_t idx = (size_t)strtoul(last, NULL, 10);
        if (!cJSON_IsArray((cJSON *)(void *)parent) ||
            cJSON_GetArraySize((cJSON *)(void *)parent) <= (int)idx) {
            cJSON_Delete(copy);
            return false;
        }
        cJSON_ReplaceItemInArray((cJSON *)(void *)parent, (int)idx, copy);
        return true;
    }
    cJSON *obj = (cJSON *)(void *)parent;
    if (!cJSON_IsObject(obj) || cJSON_GetObjectItemCaseSensitive(obj, last) == NULL) {
        cJSON_Delete(copy);
        return false;
    }
    cJSON_ReplaceItemInObjectCaseSensitive(obj, last, copy);
    return true;
}

size_t json_object_len(const json_node_t *obj)
{
    const cJSON *n = node_of(obj);
    if (!cJSON_IsObject(n)) return 0;
    int len = cJSON_GetArraySize(n);
    return (len < 0) ? 0 : (size_t)len;
}

const char *json_object_key(const json_node_t *obj, size_t index)
{
    const cJSON *n = node_of(obj);
    if (!cJSON_IsObject(n)) return NULL;
    const cJSON *item = cJSON_GetArrayItem(n, (int)index);
    return (item != NULL && item->string != NULL) ? item->string : "";
}

json_node_t *json_object_value(const json_node_t *obj, size_t index)
{
    const cJSON *n = node_of(obj);
    if (!cJSON_IsObject(n)) return NULL;
    return node_out(cJSON_GetArrayItem(n, (int)index));
}

json_node_t *json_add_node(json_node_t *obj, const char *key, const json_node_t *node)
{
    if (obj == NULL || key == NULL || node == NULL || !cJSON_IsObject((cJSON *)(void *)obj)) {
        return NULL;
    }
    /* Deep copy: cJSON owns what it holds, and the source belongs to another
     * document that may be freed first. */
    cJSON *copy = cJSON_Duplicate(node_of(node), true);
    if (copy == NULL) return NULL;
    cJSON_AddItemToObject((cJSON *)(void *)obj, key, copy);
    return node_out(copy);
}

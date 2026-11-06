/* cfg.c — the loaded `config.json` and the `secrets` namespace (`02-firmware.md`
 * §`cfg`). L0: depends on `store` only. The document lives in RAM for the life of a
 * boot and is replaced whole by `reload`; a failed load keeps the previous one, so a
 * typo in `config.set` cannot leave the device with no config (`04` §4.9). */

#include "cfg.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"

static const char *TAG = "cfg";

/* The parse buffer for a load. `s_doc` and the document state live in `cfg_doc.c`,
 * which owns every accessor (`cfg_get`, `cfg_clone`, `cfg_set`). */
static char s_text_scratch[CFG_JSON_MAX + 1];

esp_err_t cfg_load_degraded(char *degraded, size_t degraded_cap)
{
    if (degraded == NULL) degraded_cap = 0;
    if (degraded_cap > 0) degraded[0] = '\0';
    size_t len = 0;
    esp_err_t err = store_read_file("/config.json", s_text_scratch, sizeof s_text_scratch, &len);
    if (err == ESP_ERR_NOT_FOUND) {
        /* A blank device is not a failure: the caller answers `E_CONFIG` to a
         * command that needs config, and boot continues to the link (`04` §5). */
        cfg_unload();
        return ESP_ERR_NOT_FOUND;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "read /config.json: %s", esp_err_to_name(err));
        if (degraded_cap > 0) {
            snprintf(degraded, degraded_cap, "cannot read /config.json: %s",
                     esp_err_to_name(err));
        }
        return err;
    }

    json_doc_t *doc = NULL;
    err = json_parse(s_text_scratch, len, &doc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "parse /config.json: not one JSON value");
        if (degraded_cap > 0) snprintf(degraded, degraded_cap, "/config.json is not valid JSON");
        return ESP_ERR_INVALID_ARG;
    }
    char why[96];
    err = cfg_validate(doc, why, sizeof why);
    if (err != ESP_OK) {
        /* The reason names the key, never a value: a config can hold a URL with a
         * credential in it (`06-security.md` §secret handling). */
        ESP_LOGE(TAG, "config rejected: %s", why);
        /* Out even on a refusal: `reload` relays this text, and a rejection without
         * its reason cannot be repaired from the bridge. */
        if (degraded_cap > 0) snprintf(degraded, degraded_cap, "%s", why);
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }
    err = cfg_validate_refs(doc, why, sizeof why);
    if (err != ESP_OK) {
        /* `03-config.md` step 9. Degraded, not fatal: `07-roadmap.md` M2 boots with
         * an unresolved ref, keeps the link up, and lets `secret.set` fix it. The
         * reason travels out so `reload` can put it in `warnings` (§4.2). */
        ESP_LOGE(TAG, "config accepted with an unresolved reference: %s", why);
        if (degraded_cap > 0) {
            snprintf(degraded, degraded_cap, "%s", why);
        }
    }

    cfg_adopt(doc);
    return ESP_OK;
}

esp_err_t cfg_load(void)
{
    return cfg_load_degraded(NULL, 0);
}

/* --- Secrets (`02-firmware.md` §`cfg`, `04` §4.10) ------------------------ */

/* The one namespace a secret may live in, and the only place it is opened
 * (`08-code-architecture.md` §3). */
#define CFG_SECRETS_NAMESPACE "secrets"

esp_err_t cfg_store_secret(const char *key, const char *value, size_t *stored)
{
    /* `04` §4.10 bounds the key by the ref grammar and the value by NVS. An empty
     * value is ESP_ERR_INVALID_ARG: a secret is never cleared by writing "" , which
     * would silently turn a working provider into an unauthenticated one. */
    /* The key is checked, not a `nvs:` prefix built around it: a 20-byte buffer
     * truncates a 16-character key to 15, which passes the grammar and then fails
     * inside NVS as E_STORAGE instead of the E_CONFIG `04` §4.10 names. */
    if (key == NULL || *key == '\0') return ESP_ERR_INVALID_ARG;
    for (const char *c = key; *c != '\0'; c++) {
        bool ok = (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') ||
                  (*c >= '0' && *c <= '9') || *c == '_';
        if (!ok) return ESP_ERR_INVALID_ARG;
    }
    if (strlen(key) > 15) return ESP_ERR_INVALID_ARG;
    if (value == NULL || *value == '\0' || strlen(value) > CFG_SECRET_MAX) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = store_nvs_set_str(CFG_SECRETS_NAMESPACE, key, value, stored);
    if (err == ESP_OK) {
        /* A key, a length, never the value (`06-security.md` §secret handling). */
        ESP_LOGI(TAG, "secret %s stored (%u bytes)", key, (unsigned)*stored);
    }
    return err;
}

esp_err_t cfg_resolve_secret(const char *ref, char *buf, size_t cap, size_t *len)
{
    if (!cfg_ref_is_valid(ref)) return ESP_ERR_INVALID_ARG;
    const char *key = ref + 4;
    esp_err_t err = store_nvs_get_str(CFG_SECRETS_NAMESPACE, key, buf, cap, len);
    if (err == ESP_ERR_NOT_FOUND) {
        /* Rule 3: an unresolved ref is an error, never an empty string. */
        ESP_LOGE(TAG, "secret %s is not set", key);
        return ESP_ERR_NOT_FOUND;
    }
    return err;
}

/* --- Validation step 9: secret resolution ---------------------------------
 * Split from `cfg_validate` because it reads NVS and the schema walk must stay
 * provable from a host test. Every `*_ref` leaf is found by walking the document,
 * not by a second key table: the table already froze which leaves are refs, and a
 * copy of it here would drift. */

static esp_err_t check_ref_leaf(const json_node_t *node, const char *where,
                                char *why, size_t cap)
{
    const char *ref = json_string(node);
    if (!cfg_ref_is_valid(ref)) {
        snprintf(why, cap, "%s: not an nvs: reference", where);
        return ESP_ERR_INVALID_ARG;
    }
    char value[CFG_SECRET_MAX];
    size_t len = 0;
    esp_err_t err = cfg_resolve_secret(ref, value, sizeof value, &len);
    if (err != ESP_OK) {
        /* The key is named, never the value: a log line is copied into bug
         * reports (`06-security.md` §secret handling). */
        snprintf(why, cap, "%s: %s is not set", where, ref);
    }
    return err;
}

static esp_err_t walk_refs(const json_node_t *node, char *why, size_t cap)
{
    if (node == NULL) return ESP_OK;
    /* Members of an array are walked too: `llm.providers` is an array of objects,
     * and a walker that stops at object members never reaches `api_key_ref`, which
     * is the one ref a provider cannot do without. */
    if (json_is_array(node)) {
        size_t items = json_array_len(node);
        for (size_t i = 0; i < items; i++) {
            esp_err_t err = walk_refs(json_array_at(node, i), why, cap);
            if (err != ESP_OK) return err;
        }
        return ESP_OK;
    }
    size_t members = json_object_len(node);
    for (size_t i = 0; i < members; i++) {
        const char *key = json_object_key(node, i);
        const json_node_t *value = json_object_value(node, i);
        if (value == NULL) continue;
        size_t n = strlen(key);
        if (n > 4 && strcmp(key + n - 4, "_ref") == 0) {
            esp_err_t err = check_ref_leaf(value, key, why, cap);
            if (err != ESP_OK) return err;
            continue;
        }
        if (!json_is_object(value) && !json_is_array(value)) continue;
        esp_err_t err = walk_refs(value, why, cap);
        if (err != ESP_OK) return err;
    }
    return ESP_OK;
}

esp_err_t cfg_validate_refs(json_doc_t *doc, char *why, size_t why_cap)
{
    if (doc == NULL) return ESP_ERR_INVALID_ARG;
    if (why == NULL) why_cap = 0;
    if (why_cap > 0) why[0] = '\0';
    return walk_refs(json_root(doc), why, why_cap);
}

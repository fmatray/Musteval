/* cfg_validate.c — the frozen key tree and its bounds (`03-config.md` §Full schema,
 * notes 1-5, validation steps 3-9). Pure: no filesystem, no NVS, no logging, so
 * every rule here is provable from a host test. */

#include "cfg.h"

#include <stdio.h>
#include <string.h>

/* --- Schema as data ------------------------------------------------------
 * One row per frozen key. `path` is dotted and a numeric segment indexes an array.
 * `min`/`max` are the table's Bounds column; for a string they are the length.
 * `required` means "the parent object must carry this key"; a row is checked only
 * when present otherwise, and an absent optional key takes its documented default.
 * A key absent from every table is an unknown key, which `03` §Validation step 4
 * rejects because the tree is frozen. */

typedef enum {
    CFG_INT,
    CFG_BOOL,
    CFG_STRING,
    CFG_ENUM,
    CFG_REF,      /* `^nvs:[A-Za-z0-9_]{1,15}$` */
    CFG_PATH,     /* starts with `/` */
    CFG_URL,      /* `http://` or `https://` */
    CFG_OBJECT,
    CFG_ARRAY,
    /* `wifi.networks` and `llm.providers` element rules: the row names a relative
     * suffix, checked once per member. */
    CFG_ELEMENT,
} cfg_kind_t;

typedef struct {
    const char *path;
    cfg_kind_t  kind;
    int64_t     min;
    int64_t     max;
    bool        required;
    const char *enums;   /* space-separated, for CFG_ENUM */
    const char *suffix;  /* for CFG_ELEMENT: the dotted path inside one member */
} cfg_rule_t;

#define NONE 0, 0

static const cfg_rule_t k_rules[] = {
    { "version", CFG_INT, 1, 1, true, NULL, NULL },

    { "device",                 CFG_OBJECT, NONE, false, NULL, NULL },
    { "device.name",            CFG_STRING, 1, 32, false, NULL, NULL },
    { "device.log_level",       CFG_ENUM,   NONE, false, "none error warn info debug", NULL },

    { "wifi",                   CFG_OBJECT, NONE, false, NULL, NULL },
    { "wifi.connect_timeout_ms", CFG_INT, 1000, 120000, false, NULL, NULL },
    { "wifi.retry_backoff_ms",  CFG_ARRAY, 1, 6, false, NULL, NULL },
    { "wifi.networks",          CFG_ARRAY, 1, 8, true, NULL, NULL },
    { "wifi.networks.*",        CFG_ELEMENT, NONE, true, NULL, "*" },

    { "ntp",                    CFG_OBJECT, NONE, false, NULL, NULL },
    { "ntp.servers",            CFG_ARRAY, 1, 4, false, NULL, NULL },
    { "ntp.timezone",           CFG_STRING, 1, 64, false, NULL, NULL },
    { "ntp.sync_interval_s",    CFG_INT, 60, 86400, false, NULL, NULL },
    { "ntp.sync_timeout_ms",    CFG_INT, 100, 60000, false, NULL, NULL },

    { "llm",                    CFG_OBJECT, NONE, false, NULL, NULL },
    { "llm.providers",          CFG_ARRAY, 1, 4, true, NULL, NULL },
    { "llm.providers.*",        CFG_ELEMENT, NONE, true, NULL, "providers" },
    { "llm.max_tool_rounds",    CFG_INT, 1, 16, false, NULL, NULL },
    { "llm.max_history_messages", CFG_INT, 0, 32, false, NULL, NULL },
    { "llm.max_calls_per_hour", CFG_INT, 0, 100000, false, NULL, NULL },
    { "llm.max_calls_per_day",  CFG_INT, 0, 1000000, false, NULL, NULL },

    { "mcp",                    CFG_OBJECT, NONE, false, NULL, NULL },
    { "mcp.servers",            CFG_ARRAY, 0, 4, false, NULL, NULL },
    { "mcp.servers.*",          CFG_ELEMENT, NONE, false, NULL, "servers" },

    { "agent",                  CFG_OBJECT, NONE, false, NULL, NULL },
    { "agent.system_prompt",    CFG_PATH, NONE, false, NULL, NULL },
    { "agent.max_input_bytes",  CFG_INT, 256, 4096, false, NULL, NULL },

    { "tools",                  CFG_OBJECT, NONE, false, NULL, NULL },
    { "tools.allow_dangerous",  CFG_BOOL, NONE, false, NULL, NULL },
    { "tools.http_allow_hosts", CFG_ARRAY, 0, 8, false, NULL, NULL },
    { "tools.memory_max_bytes", CFG_INT, 256, 4096, false, NULL, NULL },

    { "sys",                    CFG_OBJECT, NONE, false, NULL, NULL },
    { "sys.safe_mode_after",    CFG_INT, 2, 10, false, NULL, NULL },

    { "serial",                 CFG_OBJECT, NONE, false, NULL, NULL },
    { "serial.max_line_bytes",  CFG_INT, 512, 4096, false, NULL, NULL },
};

/* Element rules, one per member of the array named by the prefix. */
static const cfg_rule_t k_element_rules[] = {
    { "wifi.networks.ssid",         CFG_STRING, 1, 32, true, NULL, NULL },
    { "wifi.networks.password_ref", CFG_REF,    NONE, true, NULL, NULL },
    { "wifi.networks.priority",     CFG_INT,    0, 100, false, NULL, NULL },

    { "llm.providers.id",           CFG_STRING, 1, 16, true, NULL, NULL },
    { "llm.providers.base_url",     CFG_URL,    NONE, true, NULL, NULL },
    { "llm.providers.model",        CFG_STRING, 1, 64, true, NULL, NULL },
    { "llm.providers.api_key_ref",  CFG_REF,    NONE, true, NULL, NULL },
    { "llm.providers.timeout_ms",   CFG_INT,    1000, 120000, false, NULL, NULL },
    { "llm.providers.max_output_tokens", CFG_INT, 16, 8192, false, NULL, NULL },
    { "llm.providers.token_param",  CFG_ENUM,   NONE, false,
      "max_tokens max_completion_tokens", NULL },
    { "llm.providers.extra_body",   CFG_OBJECT, NONE, false, NULL, NULL },

    { "mcp.servers.id",             CFG_STRING, 1, 16, true, NULL, NULL },
    { "mcp.servers.url",            CFG_URL,    NONE, true, NULL, NULL },
    { "mcp.servers.enabled",        CFG_BOOL,   NONE, false, NULL, NULL },
    { "mcp.servers.auth_header_ref", CFG_REF,   NONE, false, NULL, NULL },
    { "mcp.servers.timeout_ms",     CFG_INT,    1000, 60000, false, NULL, NULL },
};

#define RULE_COUNT(a) (sizeof(a) / sizeof((a)[0]))

static esp_err_t fail(char *why, size_t cap, const char *fmt, const char *arg)
{
    if (why != NULL && cap > 0) {
        if (arg != NULL) snprintf(why, cap, fmt, arg);
        else snprintf(why, cap, "%s", fmt);
    }
    return ESP_ERR_INVALID_ARG;
}

bool cfg_ref_is_valid(const char *ref)
{
    if (ref == NULL || strncmp(ref, "nvs:", 4) != 0) return false;
    size_t n = strlen(ref + 4);
    if (n == 0 || n > 15) return false;
    for (const char *c = ref + 4; *c != '\0'; c++) {
        bool ok = (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') ||
                  (*c >= '0' && *c <= '9') || *c == '_';
        if (!ok) return false;
    }
    return true;
}

static bool starts_with(const char *s, const char *prefix)
{
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

/* --- Per-value checks ---------------------------------------------------- */

static esp_err_t check_rule(const json_node_t *node, const cfg_rule_t *rule,
                            char *why, size_t cap)
{
    switch (rule->kind) {
    case CFG_INT: {
        int64_t v = 0;
        if (!json_int(node, &v)) return fail(why, cap, "%s: not an integer", rule->path);
        if (v < rule->min || v > rule->max) {
            return fail(why, cap, "%s: out of range", rule->path);
        }
        return ESP_OK;
    }
    case CFG_BOOL:
        return json_is_bool(node) ? ESP_OK : fail(why, cap, "%s: not a boolean", rule->path);
    case CFG_STRING: {
        const char *s = json_string(node);
        if (s == NULL) return fail(why, cap, "%s: not a string", rule->path);
        size_t n = strlen(s);
        if ((int64_t)n < rule->min || (int64_t)n > rule->max) {
            return fail(why, cap, "%s: bad length", rule->path);
        }
        return ESP_OK;
    }
    case CFG_ENUM: {
        const char *s = json_string(node);
        if (s == NULL) return fail(why, cap, "%s: not a string", rule->path);
        /* Space-separated members, matched whole, so `info` never matches `inf`.
         * Bounded by the list, not by the input. */
        size_t want = strlen(s);
        for (const char *p = rule->enums; *p != '\0';) {
            const char *sp = strchr(p, ' ');
            size_t n = sp ? (size_t)(sp - p) : strlen(p);
            if (n == want && memcmp(p, s, n) == 0) return ESP_OK;
            if (!sp) break;
            p = sp + 1;
        }
        return fail(why, cap, "%s: not an allowed value", rule->path);
    }
    case CFG_REF:
        return cfg_ref_is_valid(json_string(node))
                   ? ESP_OK
                   : fail(why, cap, "%s: not an nvs: reference", rule->path);
    case CFG_PATH: {
        const char *s = json_string(node);
        return (s != NULL && s[0] == '/') ? ESP_OK
                                          : fail(why, cap, "%s: not a path", rule->path);
    }
    case CFG_URL: {
        const char *s = json_string(node);
        bool ok = s != NULL && (starts_with(s, "https://") || starts_with(s, "http://"));
        return ok ? ESP_OK : fail(why, cap, "%s: not an http(s) url", rule->path);
    }
    case CFG_OBJECT:
        return json_is_object(node) ? ESP_OK : fail(why, cap, "%s: not an object", rule->path);
    case CFG_ARRAY:
        if (!json_is_array(node)) return fail(why, cap, "%s: not an array", rule->path);
        if (json_array_len(node) < (size_t)rule->min ||
            json_array_len(node) > (size_t)rule->max) {
            return fail(why, cap, "%s: wrong element count", rule->path);
        }
        return ESP_OK;
    case CFG_ELEMENT:
        break; /* never dispatched through here */
    }
    return ESP_OK;
}

/* One member of `wifi.networks` / `llm.providers` / `mcp.servers`. */
static esp_err_t check_member(const json_node_t *member, const char *group,
                              char *why, size_t cap)
{
    if (!json_is_object(member)) return fail(why, cap, "%s: member is not an object", group);
    for (size_t i = 0; i < RULE_COUNT(k_element_rules); i++) {
        const cfg_rule_t *rule = &k_element_rules[i];
        if (!starts_with(rule->path, group) || rule->path[strlen(group)] != '.') continue;
        const char *key = rule->path + strlen(group) + 1;
        /* A dotted key in an element rule (`extra_body`) has no member of its own. */
        if (strchr(key, '.') != NULL) continue;
        const json_node_t *v = json_get(member, key);
        if (v == NULL) {
            if (rule->required) return fail(why, cap, "%s: required key missing", rule->path);
            continue;
        }
        esp_err_t err = check_rule(v, rule, why, cap);
        if (err != ESP_OK) return err;
    }
    return ESP_OK;
}

/* Cross-field rules and the array members, which a flat table cannot express. */
static esp_err_t check_cross(json_doc_t *doc, char *why, size_t cap)
{
    const char *groups[] = { "wifi.networks", "llm.providers", "mcp.servers" };
    for (size_t g = 0; g < RULE_COUNT(groups); g++) {
        json_node_t *arr = json_path_get(doc, groups[g]);
        if (arr == NULL) continue;
        size_t n = json_array_len(arr);
        for (size_t i = 0; i < n; i++) {
            esp_err_t err = check_member(json_array_at(arr, i), groups[g], why, cap);
            if (err != ESP_OK) return err;
        }
    }

    /* `03` §Full schema note 1: the input budget cannot exceed the RX line. */
    json_node_t *in_max = json_path_get(doc, "agent.max_input_bytes");
    json_node_t *line_max = json_path_get(doc, "serial.max_line_bytes");
    if (in_max != NULL && line_max != NULL) {
        int64_t a = 0, b = 0;
        if (json_int(in_max, &a) && json_int(line_max, &b) && a > b) {
            return fail(why, cap, "agent.max_input_bytes exceeds serial.max_line_bytes", NULL);
        }
    }

    /* Ids are unique and dot-free (note 4): the id becomes a namespace on the wire. */
    const char *id_groups[] = { "llm.providers", "mcp.servers" };
    for (size_t g = 0; g < RULE_COUNT(id_groups); g++) {
        json_node_t *arr = json_path_get(doc, id_groups[g]);
        if (arr == NULL) continue;
        size_t n = json_array_len(arr);
        for (size_t i = 0; i < n; i++) {
            const char *id = json_string(json_get(json_array_at(arr, i), "id"));
            if (id == NULL) continue;
            if (strchr(id, '.') != NULL) {
                return fail(why, cap, "%s: id must not contain a dot", id_groups[g]);
            }
            for (size_t j = i + 1; j < n; j++) {
                const char *other = json_string(json_get(json_array_at(arr, j), "id"));
                if (other != NULL && strcmp(id, other) == 0) {
                    return fail(why, cap, "%s: duplicate id", id_groups[g]);
                }
            }
        }
    }
    return ESP_OK;
}

/* --- Unknown keys --------------------------------------------------------
 * Step 4 rejects a key the tree does not name, so a typo is a refusal rather than
 * a silently ignored setting. The walk is over the document, not the table, so a
 * key nobody thought of is caught too. */
static const cfg_rule_t *find_rule(const char *path, bool *is_element)
{
    *is_element = false;
    for (size_t i = 0; i < RULE_COUNT(k_rules); i++) {
        if (k_rules[i].kind == CFG_ELEMENT) continue;
        if (strcmp(k_rules[i].path, path) == 0) return &k_rules[i];
    }
    /* `X.*` marks an element group; its members are `k_element_rules`. */
    for (size_t i = 0; i < RULE_COUNT(k_rules); i++) {
        if (k_rules[i].kind != CFG_ELEMENT) continue;
        size_t n = strlen(k_rules[i].path);
        if (n > 2 && strcmp(k_rules[i].path + n - 2, ".*") == 0 &&
            strncmp(path, k_rules[i].path, n - 2) == 0 && path[n - 2] == '.') {
            *is_element = true;
            return &k_rules[i];
        }
    }
    return NULL;
}

static esp_err_t walk(const json_node_t *obj, const char *prefix,
                      char *why, size_t cap)
{
    char path[160];
    size_t count = json_object_len(obj);
    for (size_t idx = 0; idx < count; idx++) {
        const char *key = json_object_key(obj, idx);
        const json_node_t *c = json_object_value(obj, idx);
        if (key == NULL || c == NULL) return fail(why, cap, "object member unreadable", NULL);
        int n = snprintf(path, sizeof path, "%s%s%s", prefix, prefix[0] ? "." : "", key);
        if (n <= 0 || (size_t)n >= sizeof path) return fail(why, cap, "key too long", key);

        bool element = false;
        const cfg_rule_t *rule = find_rule(path, &element);
        if (rule == NULL) return fail(why, cap, "unknown key %s", path);
        if (element) {
            /* The `*` row only says "array of objects"; members are checked above. */
            if (!json_is_object(c)) return fail(why, cap, "%s: not an object", path);
            continue;
        }
        if (rule->kind == CFG_OBJECT) {
            esp_err_t err = walk(c, path, why, cap);
            if (err != ESP_OK) return err;
            continue;
        }
        if (rule->kind == CFG_ARRAY) {
            /* Array members are values, not keys: `ntp.servers` holds strings. */
            esp_err_t err = check_rule(c, rule, why, cap);
            if (err != ESP_OK) return err;
            /* `03` §Full schema types each element, and a table row cannot say
             * "array of int" without a second column nobody would read. */
            bool ints = strcmp(path, "wifi.retry_backoff_ms") == 0;
            bool strs = strcmp(path, "ntp.servers") == 0 ||
                        strcmp(path, "tools.http_allow_hosts") == 0;
            size_t n = json_array_len(c);
            for (size_t k = 0; k < n; k++) {
                const json_node_t *e = json_array_at(c, k);
                if (ints) {
                    int64_t v = 0;
                    if (!json_int(e, &v) || v < 100 || v > 60000) {
                        return fail(why, cap, "%s: element out of range", path);
                    }
                } else if (strs && !json_is_string(e)) {
                    return fail(why, cap, "%s: element is not a string", path);
                }
            }
        }
    }
    return ESP_OK;
}

esp_err_t cfg_validate(json_doc_t *doc, char *why, size_t cap)
{
    if (doc == NULL) return fail(why, cap, "no document", NULL);
    json_node_t *root = json_root(doc);
    if (!json_is_object(root)) return fail(why, cap, "root is not an object", NULL);

    /* Step 3 first: a wrong version makes every other message misleading. */
    const json_node_t *version = json_get(root, "version");
    if (version == NULL) return fail(why, cap, "version: required key missing", NULL);
    esp_err_t err = check_rule(version, &k_rules[0], why, cap);
    if (err != ESP_OK) return err;

    /* Steps 4-8: every present key is known and in bounds, every required key is
     * there. The version row is checked above, so it is skipped by the walk. */
    for (size_t i = 1; i < RULE_COUNT(k_rules); i++) {
        const cfg_rule_t *rule = &k_rules[i];
        if (rule->kind == CFG_ELEMENT || !rule->required) continue;
        if (json_path_get(doc, rule->path) == NULL) {
            return fail(why, cap, "%s: required key missing", rule->path);
        }
    }
    err = walk(root, "", why, cap);
    if (err != ESP_OK) return err;

    /* Step 5 for every present leaf, then the cross-field rules and step 9. */
    for (size_t i = 1; i < RULE_COUNT(k_rules); i++) {
        const cfg_rule_t *rule = &k_rules[i];
        if (rule->kind == CFG_ELEMENT || rule->kind == CFG_OBJECT ||
            rule->kind == CFG_ARRAY) continue;
        json_node_t *v = json_path_get(doc, rule->path);
        if (v == NULL) continue;
        err = check_rule(v, rule, why, cap);
        if (err != ESP_OK) return err;
    }
    return check_cross(doc, why, cap);
}

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
    CFG_HTTPS,    /* `https://` only: geolocation leaves the device */
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

    { "geo",                    CFG_OBJECT, NONE, false, NULL, NULL },
    { "geo.enabled",            CFG_BOOL,   NONE, false, NULL, NULL },
    { "geo.cache_ttl_s",        CFG_INT,    60, 86400, false, NULL, NULL },
    { "geo.timeout_ms",         CFG_INT,    1000, 15000, false, NULL, NULL },
    { "geo.max_bssids",         CFG_INT,    4, 32, false, NULL, NULL },
    { "geo.providers",          CFG_ARRAY,  1, 4, false, NULL, NULL },
    { "geo.providers.*",        CFG_ELEMENT, NONE, false, NULL, "providers" },

    { "mqtt",                   CFG_OBJECT, NONE, false, NULL, NULL },
    { "mqtt.max_servers",       CFG_INT,    1, 8, false, NULL, NULL },
    { "mqtt.servers",           CFG_ARRAY,  0, 8, false, NULL, NULL },
    { "mqtt.servers.*",         CFG_ELEMENT, NONE, false, NULL, "servers" },

    { "agent",                  CFG_OBJECT, NONE, false, NULL, NULL },
    { "agent.system_prompt",    CFG_PATH, NONE, false, NULL, NULL },
    { "agent.max_input_bytes",  CFG_INT, 256, 4096, false, NULL, NULL },

    { "tools",                  CFG_OBJECT, NONE, false, NULL, NULL },
    { "tools.allow_dangerous",  CFG_BOOL, NONE, false, NULL, NULL },
    { "tools.http_allow_hosts", CFG_ARRAY, 0, 8, false, NULL, NULL },
    /* Names `tools_offered` refuses: the model is neither shown nor allowed to call
     * them. 8 is the `03-config.md` ceiling, the same length as the host array: a
     * reader turns off a handful, not the registry. */
    { "tools.disabled",         CFG_ARRAY, 0, 8, false, NULL, NULL },
    { "tools.memory_max_bytes", CFG_INT, 256, 4096, false, NULL, NULL },
    /* The pin allowlist is not a range — GPIO3 sits between two usable pins and is
     * excluded — so only the outer bound is here and `check_cross` holds the set. */
    { "tools.i2c_sda",          CFG_INT, 1, 9, false, NULL, NULL },
    { "tools.i2c_scl",          CFG_INT, 1, 9, false, NULL, NULL },
    { "tools.task_max",         CFG_INT, 1, 16, false, NULL, NULL },

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

    { "geo.providers.id",           CFG_STRING, 1, 16, true, NULL, NULL },
    { "geo.providers.kind",         CFG_ENUM,   NONE, true, "wifi_scan ip", NULL },
    { "geo.providers.url",          CFG_HTTPS,  NONE, true, NULL, NULL },
    { "geo.providers.api_key_ref",  CFG_REF,    NONE, false, NULL, NULL },

    { "mqtt.servers.id",            CFG_STRING, 1, 16, true, NULL, NULL },
    { "mqtt.servers.host",          CFG_STRING, 1, 128, true, NULL, NULL },
    { "mqtt.servers.port",          CFG_INT,    1, 65535, false, NULL, NULL },
    { "mqtt.servers.tls",           CFG_BOOL,   NONE, false, NULL, NULL },
    { "mqtt.servers.client_id",     CFG_STRING, 1, 64, false, NULL, NULL },
    { "mqtt.servers.username",      CFG_STRING, 1, 64, false, NULL, NULL },
    { "mqtt.servers.password_ref",  CFG_REF,    NONE, false, NULL, NULL },
    { "mqtt.servers.keepalive_s",   CFG_INT,    10, 300, false, NULL, NULL },
    { "mqtt.servers.topics",        CFG_ARRAY,  0, 8, false, NULL, NULL },
    { "mqtt.servers.qos",           CFG_INT,    0, 1, false, NULL, NULL },

    { "mqtt.servers.topics.filter", CFG_STRING, 1, 128, true, NULL, NULL },
    { "mqtt.servers.topics.qos",    CFG_INT,    0, 1, false, NULL, NULL },

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
    /* `03-config.md` §Default / reject matrix: an optional key may be `null`, which
     * reads as absent. A required key may not, and falls through to its own refusal. */
    if (json_is_null(node) && !rule->required) return ESP_OK;
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
    case CFG_HTTPS: {
        const char *s = json_string(node);
        return (s != NULL && starts_with(s, "https://"))
                   ? ESP_OK
                   : fail(why, cap, "%s: not an https url", rule->path);
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

/* `03-config.md` §Full schema step 8 and the `extra_body` row: at most four members,
 * flat scalars only, and none of the four keys that would rewrite the request's
 * structure. The table's `CFG_OBJECT` row only proves `json_is_object`, which left
 * every one of those rules unenforced — a body with a nested member is a 400 from the
 * provider and an unnameable failure for the reader. */
static esp_err_t check_extra_body(const json_node_t *extra, char *why, size_t cap)
{
    if (extra == NULL) return ESP_OK;
    if (!json_is_object(extra)) return fail(why, cap, "extra_body: not an object", NULL);
    if (json_object_len(extra) > 4) {
        return fail(why, cap, "extra_body: more than 4 members", NULL);
    }
    static const char *reserved[] = { "model", "messages", "tools", "stream" };
    size_t count = json_object_len(extra);
    for (size_t i = 0; i < count; i++) {
        /* `json_object_key` answers `NULL` only for a non-object, which the guard
         * above already refused, and `json_object_value` is non-`NULL` for every
         * index below the member count. There is no unreadable member here. */
        const char *key = json_object_key(extra, i);
        const json_node_t *value = json_object_value(extra, i);
        for (size_t r = 0; r < RULE_COUNT(reserved); r++) {
            if (strcmp(key, reserved[r]) == 0) {
                return fail(why, cap, "extra_body: reserved key %s", key);
            }
        }
        /* Flat scalar only: a nested object or array cannot be represented as a
         * request member by the flat merge, and a provider that wants one wants it
         * under a name the firmware does not know. Any other JSON scalar — string,
         * number, bool — is a member a provider can read, and `temperature: 0.2` is
         * the ordinary case. */
        if (!(json_is_string(value) || json_is_number(value) || json_is_bool(value))) {
            return fail(why, cap, "extra_body: %s is not a flat scalar", key);
        }
    }
    return ESP_OK;
}

/* True when `group` names an element array: some element rule starts with `group.`.
 * Lets a nested array (`mqtt.servers.topics`) be recognised as a second group without
 * a second table. */
static bool has_element_rules(const char *group)
{
    size_t glen = strlen(group);
    for (size_t i = 0; i < RULE_COUNT(k_element_rules); i++) {
        const char *path = k_element_rules[i].path;
        if (strncmp(path, group, glen) == 0 && path[glen] == '.') {
            const char *rest = path + glen + 1;
            if (strchr(rest, '.') == NULL) return true;
        }
    }
    return false;
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
        /* The object's *contents* are a rule the table cannot express; `CFG_OBJECT`
         * only proves it is an object. */
        if (rule->kind == CFG_OBJECT && strcmp(key, "extra_body") == 0) {
            err = check_extra_body(v, why, cap);
            if (err != ESP_OK) return err;
        }
        /* An array of objects inside an element (`mqtt.servers[].topics[]`) is a
         * second element group; recurse rather than teach the table two levels. */
        if (rule->kind == CFG_ARRAY && has_element_rules(rule->path)) {
            size_t members = json_array_len(v);
            for (size_t m = 0; m < members; m++) {
                err = check_member(json_array_at(v, m), rule->path, why, cap);
                if (err != ESP_OK) return err;
            }
        }
    }
    return ESP_OK;
}

/* `03-config.md` §mqtt filter grammar: plain levels, optionally a `*` as the entire
 * final level. `+`/`#` are refused wherever they appear, and a `*` inside a level or
 * not at the end is refused too, so a config entry can never widen the allowlist past
 * what the user wrote. Pure. */
bool cfg_mqtt_filter_is_valid(const char *filter)
{
    size_t n = strlen(filter);
    if (n == 0) return false;
    for (size_t i = 0; i < n; i++) {
        if (filter[i] == '+' || filter[i] == '#') return false;
    }
    const char *star = strchr(filter, '*');
    if (star == NULL) return true;
    /* Exactly one `*`, and it is the whole last level. */
    if (strchr(star + 1, '*') != NULL) return false;
    if (star[1] != '\0') return false;
    return star != filter && star[-1] == '/';
}

/* True when `filter` covers `topic`: exact, or one level deeper when the filter ends in
 * a star. The filter has already been validated, so a star is only ever the final
 * level. */
bool cfg_mqtt_filter_covers(const char *filter, const char *topic)
{
    if (filter == NULL || topic == NULL) return false;
    size_t n = strlen(filter);
    if (n > 0 && filter[n - 1] == '*') {
        /* Drop the star, then require the topic to be the remaining prefix plus one
         * non-empty level. `sensors/` plus a star covers `sensors/temp` but not
         * `sensors/temp/raw`
         * (`03-config.md` §mqtt: "matches exactly one level"). */
        size_t prefix = n - 1;
        if (strncmp(filter, topic, prefix) != 0) return false;
        const char *rest = topic + prefix;
        if (rest[0] == '\0') return false;
        return strchr(rest, '/') == NULL;
    }
    return strcmp(filter, topic) == 0;
}

/* Cross-field rules and the array members, which a flat table cannot express. */
static esp_err_t check_cross(json_doc_t *doc, char *why, size_t cap)
{
    const char *groups[] = { "wifi.networks", "llm.providers", "mcp.servers",
                             "geo.providers", "mqtt.servers" };
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
    const char *id_groups[] = { "llm.providers", "mcp.servers", "geo.providers",
                                "mqtt.servers" };
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

    /* `03-config.md` §tools: the I2C pins are a set, not a range — GPIO3 sits between
     * two usable pins and is excluded as a strapping pin (`01-hardware.md` §6.1) —
     * and the two must differ, or the bus has one wire. */
    const int i2c_allowed[] = {1, 2, 4, 5, 6, 7, 8, 9};
    int64_t sda = -1, scl = -1;
    json_node_t *sda_node = json_path_get(doc, "tools.i2c_sda");
    json_node_t *scl_node = json_path_get(doc, "tools.i2c_scl");
    bool has_sda = sda_node != NULL && json_int(sda_node, &sda);
    bool has_scl = scl_node != NULL && json_int(scl_node, &scl);
    for (int which = 0; which < 2; which++) {
        bool present = which == 0 ? has_sda : has_scl;
        if (!present) continue;
        int64_t pin = which == 0 ? sda : scl;
        bool ok = false;
        for (size_t i = 0; i < RULE_COUNT(i2c_allowed); i++) {
            if (pin == i2c_allowed[i]) ok = true;
        }
        if (!ok) {
            return fail(why, cap, "tools.i2c_%s: not a usable header pin",
                        which == 0 ? "sda" : "scl");
        }
    }
    if (has_sda != has_scl) {
        return fail(why, cap, "tools.i2c_sda and tools.i2c_scl must be set together", NULL);
    }
    if (has_sda && sda == scl) {
        return fail(why, cap, "tools.i2c_sda and tools.i2c_scl must differ", NULL);
    }

    /* `03-config.md` §geo: the credential rule is keyed on `kind`, so it is a cross
     * rule and not a row. A `wifi_scan` provider without its key would fail at the
     * first lookup with a 401 the reader cannot act on; an `ip` provider with one
     * would ship a secret to a service that never asked for it. */
    {
        json_node_t *arr = json_path_get(doc, "geo.providers");
        size_t n = arr == NULL ? 0 : json_array_len(arr);
        for (size_t i = 0; i < n; i++) {
            json_node_t *p = json_array_at(arr, i);
            const char *kind = json_string(json_get(p, "kind"));
            if (kind == NULL) continue; /* the element rule already refused it */
            const json_node_t *ref = json_get(p, "api_key_ref");
            bool has_ref = ref != NULL && !json_is_null(ref);
            if (strcmp(kind, "wifi_scan") == 0 && !has_ref) {
                return fail(why, cap, "geo.providers: wifi_scan requires api_key_ref", NULL);
            }
            if (strcmp(kind, "ip") == 0 && has_ref) {
                return fail(why, cap, "geo.providers: ip must not carry api_key_ref", NULL);
            }
        }
    }

    /* `03-config.md` §mqtt: `topics[]` is the allowlist, so the filter grammar is
     * deliberately smaller than MQTT's. `+` and `#` would widen it past what the user
     * wrote; `*` is legal only as the whole final level. */
    {
        json_node_t *arr = json_path_get(doc, "mqtt.servers");
        size_t n = arr == NULL ? 0 : json_array_len(arr);
        for (size_t i = 0; i < n; i++) {
            json_node_t *topics = json_get(json_array_at(arr, i), "topics");
            size_t t = topics == NULL ? 0 : json_array_len(topics);
            for (size_t k = 0; k < t; k++) {
                const char *filter = json_string(json_get(json_array_at(topics, k), "filter"));
                if (filter != NULL && !cfg_mqtt_filter_is_valid(filter)) {
                    return fail(why, cap,
                                "mqtt.servers.topics.filter: only a trailing * is allowed", NULL);
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
/* Every `X.*` group names an array, and `walk` does not descend into one, so no
 * walked path can equal a group. There is no element arm here: a group's members
 * are checked by `check_member`, which reads the array itself. */
static const cfg_rule_t *find_rule(const char *path)
{
    for (size_t i = 0; i < RULE_COUNT(k_rules); i++) {
        if (k_rules[i].kind == CFG_ELEMENT) continue;
        if (strcmp(k_rules[i].path, path) == 0) return &k_rules[i];
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

        const cfg_rule_t *rule = find_rule(path);
        if (rule == NULL) return fail(why, cap, "unknown key %s", path);
        /* `null` reads as absent (§Default / reject matrix), so there is nothing to
         * descend into and nothing to type-check. */
        if (json_is_null(c) && !rule->required) continue;
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
                        strcmp(path, "tools.http_allow_hosts") == 0 ||
                        strcmp(path, "tools.disabled") == 0;
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

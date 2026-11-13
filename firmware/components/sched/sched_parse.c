/* sched_parse — the pure half of the scheduler: the task schema, the trigger
 * matchers, the due-time arithmetic and the `tasks.*` result shapes.
 *
 * Every rule here is a sentence of `03-config.md` §tasks or `02-firmware.md` §5, and
 * every one is host-tested. Nothing in this file touches flash, NVS, the clock, the
 * radio or the agent: it takes text and numbers and returns verdicts, which is what
 * lets the frozen schema be enforced by a test instead of by review.
 */

#include <stdio.h>
#include <string.h>

#include "sched.h"

/* --- Small helpers -------------------------------------------------------- */

static bool key_in(const char *key, const char *const *allowed, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        if (allowed[i] != NULL && strcmp(key, allowed[i]) == 0) return true;
    }
    return false;
}

/* `03-config.md` §Task schema: "The task schema is closed ... A task object carrying
 * any key outside this table is rejected whole". One helper so every object level
 * applies the same rule rather than each remembering to. */
static esp_err_t closed(const json_node_t *obj, const char *const *allowed, size_t n,
                        const char *where, char *why, size_t cap)
{
    if (why != NULL && cap > 0) why[0] = '\0';
    if (!json_is_object(obj)) {
        if (why != NULL && cap > 0) snprintf(why, cap, "%s: not an object", where);
        return ESP_ERR_INVALID_ARG;
    }
    size_t count = json_object_len(obj);
    for (size_t i = 0; i < count; i++) {
        /* `json_object_key` answers "" for a member it cannot name, never NULL, so
         * there is no unreadable-member case to guard. */
        const char *key = json_object_key(obj, i);
        if (!key_in(key, allowed, n)) {
            if (why != NULL && cap > 0) snprintf(why, cap, "%s: unknown key %s", where, key);
            return ESP_ERR_INVALID_ARG;
        }
    }
    return ESP_OK;
}

static esp_err_t need_string(const json_node_t *obj, const char *key, size_t min_len,
                             size_t max_len, char *out, size_t cap,
                             const char *where, char *why, size_t why_cap)
{
    const json_node_t *node = json_get(obj, key);
    if (node == NULL || json_is_null(node)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "%s: %s is required", where, key);
        return ESP_ERR_INVALID_ARG;
    }
    const char *text = json_string(node);
    if (text == NULL) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "%s.%s: not a string", where, key);
        return ESP_ERR_INVALID_ARG;
    }
    size_t len = strlen(text);
    if (len < min_len || len > max_len) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "%s.%s: must be %u..%u bytes", where, key,
                     (unsigned)min_len, (unsigned)max_len);
        }
        return ESP_ERR_INVALID_ARG;
    }
    /* `cap` always exceeds `max_len` at every call site, so the range check above is
     * the only overflow this copy can be handed. */
    memcpy(out, text, len + 1);
    return ESP_OK;
}

static esp_err_t opt_string(const json_node_t *obj, const char *key, size_t max_len,
                            char *out, size_t cap, const char *where, char *why,
                            size_t why_cap)
{
    out[0] = '\0';
    const json_node_t *node = json_get(obj, key);
    if (node == NULL || json_is_null(node)) return ESP_OK;
    return need_string(obj, key, 0, max_len, out, cap, where, why, why_cap);
}

static esp_err_t need_int(const json_node_t *obj, const char *key, int64_t min, int64_t max,
                          int64_t *out, const char *where, char *why, size_t why_cap)
{
    const json_node_t *node = json_get(obj, key);
    if (node == NULL || json_is_null(node)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "%s: %s is required", where, key);
        return ESP_ERR_INVALID_ARG;
    }
    int64_t value = 0;
    if (!json_int(node, &value)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "%s.%s: not an integer", where, key);
        return ESP_ERR_INVALID_ARG;
    }
    if (value < min || value > max) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "%s.%s: must be %lld..%lld", where, key,
                     (long long)min, (long long)max);
        }
        return ESP_ERR_INVALID_ARG;
    }
    *out = value;
    return ESP_OK;
}

/* `[a-z0-9_-]{1,15}` for an id, `[a-z0-9._-]{1,32}` for an event name. Two charsets
 * because the documents name two: `03-config.md` §Task schema and §Trigger schema. */
static bool charset_ok(const char *text, size_t len, const char *extra)
{
    for (size_t i = 0; i < len; i++) {
        char c = text[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                  (c == '_') || (c == '-') || (extra != NULL && strchr(extra, c) != NULL);
        if (!ok) return false;
    }
    return len > 0;
}

/* --- Frozen names --------------------------------------------------------- */

const char *sched_trigger_name(sched_trigger_t type)
{
    switch (type) {
    case SCHED_TRIGGER_BOOT:             return "boot";
    case SCHED_TRIGGER_WIFI_CONNECTED:   return "wifi_connected";
    case SCHED_TRIGGER_NTP_SYNCED:       return "ntp_synced";
    case SCHED_TRIGGER_SERIAL_CONNECTED: return "serial_connected";
    case SCHED_TRIGGER_GPIO:             return "gpio";
    case SCHED_TRIGGER_MQTT_MESSAGE:     return "mqtt_message";
    case SCHED_TRIGGER_EVENT:            return "event";
    case SCHED_TRIGGER_SCHEDULE:         return "schedule";
    case SCHED_TRIGGER_MANUAL:           return "manual";
    default:                             return NULL;
    }
}

int sched_trigger_from_name(const char *name)
{
    if (name == NULL) return -1;
    for (int t = 0; t < (int)SCHED_TRIGGER_COUNT; t++) {
        const char *frozen = sched_trigger_name((sched_trigger_t)t);
        if (frozen != NULL && strcmp(frozen, name) == 0) return t;
    }
    return -1;
}

/* --- Time arithmetic ------------------------------------------------------ */

/* Days since 1970-01-01 for a proleptic Gregorian date, by the days-from-civil
 * algorithm. Pure integer arithmetic rather than `timegm`, so the conversion is a
 * host test and a device with a broken libc still schedules correctly. */
static int64_t days_from_civil(int64_t y, int64_t m, int64_t d)
{
    y -= m <= 2;
    int64_t era = (y >= 0 ? y : y - 399) / 400;
    int64_t yoe = y - era * 400;                                  /* [0, 399] */
    int64_t doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1; /* [0, 365] */
    int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;          /* [0, 146096] */
    return era * 146097 + doe - 719468;
}

static int64_t unix_of(int64_t y, int64_t mo, int64_t d, int64_t h, int64_t mi, int64_t s)
{
    return days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s;
}

static bool digits(const char *p, size_t n, int *out)
{
    int value = 0;
    for (size_t i = 0; i < n; i++) {
        if (p[i] < '0' || p[i] > '9') return false;
        value = value * 10 + (p[i] - '0');
    }
    *out = value;
    return true;
}

int32_t sched_parse_hhmmss(const char *text)
{
    if (text == NULL) return -1;
    size_t len = strlen(text);
    if (len != 8 || text[2] != ':' || text[5] != ':') return -1;
    int h = 0, m = 0, s = 0;
    if (!digits(text, 2, &h) || !digits(text + 3, 2, &m) || !digits(text + 6, 2, &s)) {
        return -1;
    }
    if (h > 23 || m > 59 || s > 59) return -1;
    return (int32_t)(h * 3600 + m * 60 + s);
}

/* `YYYY-MM-DDTHH:MM:SS` with an optional trailing `Z`, read as UTC. The validity is
 * a separate answer from the instant, because `1970-01-01T00:00:00Z` is a real
 * instant whose number is zero: conflating the two would refuse it. */
static bool parse_iso(const char *text, int64_t *out)
{
    if (text == NULL) return false;
    size_t len = strlen(text);
    if (len != 19 && !(len == 20 && text[19] == 'Z')) return false;
    if (text[4] != '-' || text[7] != '-' || (text[10] != 'T' && text[10] != ' ') ||
        text[13] != ':' || text[16] != ':') {
        return false;
    }
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
    if (!digits(text, 4, &y) || !digits(text + 5, 2, &mo) || !digits(text + 8, 2, &d) ||
        !digits(text + 11, 2, &h) || !digits(text + 14, 2, &mi) ||
        !digits(text + 17, 2, &s)) {
        return false;
    }
    if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return false;
    if (out != NULL) *out = unix_of(y, mo, d, h, mi, s);
    return true;
}

bool sched_is_iso(const char *text)
{
    return parse_iso(text, NULL);
}

/* --- Schema --------------------------------------------------------------- */

esp_err_t sched_task_validate(const json_node_t *obj, sched_task_t *out,
                              char *why, size_t why_cap)
{
    static const char *const k_task_keys[] = {
        "id", "enabled", "description", "trigger", "action", "repeat", "missed",
    };
    static const char *const k_repeat_keys[] = {"mode", "count"};
    static const char *const k_action_keys[] = {"kind", "prompt", "template", "vars"};

    if (out == NULL) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof *out);
    out->enabled = true;
    out->cooldown_s = SCHED_COOLDOWN_DEF_S;
    out->repeat_mode = SCHED_REPEAT_FOREVER;
    out->missed = SCHED_MISSED_SKIP;

    esp_err_t err = closed(obj, k_task_keys, sizeof k_task_keys / sizeof *k_task_keys,
                           "task", why, why_cap);
    if (err != ESP_OK) return err;

    char *id = out->id;
    err = need_string(obj, "id", 1, SCHED_ID_MAX, id, sizeof out->id, "task", why, why_cap);
    if (err != ESP_OK) return err;
    if (!charset_ok(id, strlen(id), NULL)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "task.id: must be [a-z0-9_-]");
        return ESP_ERR_INVALID_ARG;
    }

    const json_node_t *enabled = json_get(obj, "enabled");
    if (enabled != NULL && !json_is_null(enabled)) {
        bool value = true;
        if (!json_bool(enabled, &value)) {
            if (why != NULL && why_cap > 0) snprintf(why, why_cap, "task.enabled: not a boolean");
            return ESP_ERR_INVALID_ARG;
        }
        out->enabled = value;
    }

    err = opt_string(obj, "description", sizeof out->description - 1, out->description,
                     sizeof out->description, "task", why, why_cap);
    if (err != ESP_OK) return err;

    /* --- trigger, whose closed key set depends on its own `type` --- */
    const json_node_t *trigger = json_get(obj, "trigger");
    if (trigger == NULL || !json_is_object(trigger)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "task.trigger: required object");
        return ESP_ERR_INVALID_ARG;
    }
    char type_name[24] = "";
    err = need_string(trigger, "type", 1, sizeof type_name - 1, type_name,
                      sizeof type_name, "trigger", why, why_cap);
    if (err != ESP_OK) return err;
    int type = sched_trigger_from_name(type_name);
    if (type < 0) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "trigger.type: %s is not one of the nine", type_name);
        }
        return ESP_ERR_INVALID_ARG;
    }
    out->type = (sched_trigger_t)type;

    static const char *const k_bare[] = {"type"};
    static const char *const k_gpio[] = {"type", "pin", "level"};
    static const char *const k_mqtt[] = {"type", "server_id", "topic", "payload_contains",
                                         "cooldown_s"};
    static const char *const k_event[] = {"type", "name"};
    static const char *const k_sched[] = {"type", "kind", "at", "interval_s", "first_at"};

    const char *const *allowed = k_bare;
    size_t allowed_n = sizeof k_bare / sizeof *k_bare;
    switch (out->type) {
    case SCHED_TRIGGER_GPIO:
        allowed = k_gpio; allowed_n = sizeof k_gpio / sizeof *k_gpio; break;
    case SCHED_TRIGGER_MQTT_MESSAGE:
        allowed = k_mqtt; allowed_n = sizeof k_mqtt / sizeof *k_mqtt; break;
    case SCHED_TRIGGER_EVENT:
        allowed = k_event; allowed_n = sizeof k_event / sizeof *k_event; break;
    case SCHED_TRIGGER_SCHEDULE:
        allowed = k_sched; allowed_n = sizeof k_sched / sizeof *k_sched; break;
    default:
        break;
    }
    err = closed(trigger, allowed, allowed_n, "trigger", why, why_cap);
    if (err != ESP_OK) return err;

    switch (out->type) {
    case SCHED_TRIGGER_GPIO: {
        int64_t pin = 0, level = 0;
        /* `01-hardware.md` §6.1: the exposed header, 0..48 in number. The readable
         * allowlist is `sys`'s, checked at arm time; the schema only bounds the int. */
        err = need_int(trigger, "pin", 0, 48, &pin, "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        err = need_int(trigger, "level", 0, 1, &level, "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        out->pin = (int)pin;
        out->level = (int)level;
        break;
    }
    case SCHED_TRIGGER_MQTT_MESSAGE: {
        err = need_string(trigger, "server_id", 1, sizeof out->server_id - 1,
                          out->server_id, sizeof out->server_id, "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        err = need_string(trigger, "topic", 1, sizeof out->topic - 1, out->topic,
                          sizeof out->topic, "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        /* `02-firmware.md` §5.4: `+` and `#` are not accepted here; the subscription
         * list is the allowlist and this filter is a narrowing of it. */
        if (strchr(out->topic, '+') != NULL || strchr(out->topic, '#') != NULL) {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "trigger.topic: + and # are not accepted; use a trailing *");
            }
            return ESP_ERR_INVALID_ARG;
        }
        err = opt_string(trigger, "payload_contains", SCHED_PAYLOAD_MAX,
                         out->payload_contains, sizeof out->payload_contains,
                         "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        const json_node_t *cool = json_get(trigger, "cooldown_s");
        if (cool != NULL && !json_is_null(cool)) {
            int64_t value = 0;
            err = need_int(trigger, "cooldown_s", 0, SCHED_COOLDOWN_MAX_S, &value,
                           "trigger", why, why_cap);
            if (err != ESP_OK) return err;
            out->cooldown_s = (int32_t)value;
        }
        break;
    }
    case SCHED_TRIGGER_EVENT: {
        err = need_string(trigger, "name", 1, sizeof out->name - 1, out->name,
                          sizeof out->name, "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        if (!sched_event_name_ok(out->name)) {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "trigger.name: must be [a-z0-9._-]{1,32}");
            }
            return ESP_ERR_INVALID_ARG;
        }
        break;
    }
    case SCHED_TRIGGER_SCHEDULE: {
        char kind[8] = "";
        err = need_string(trigger, "kind", 1, sizeof kind - 1, kind, sizeof kind,
                          "trigger", why, why_cap);
        if (err != ESP_OK) return err;
        if (strcmp(kind, "at") == 0) {
            out->kind = SCHED_AT;
            err = need_string(trigger, "at", 1, sizeof out->at - 1, out->at,
                              sizeof out->at, "trigger", why, why_cap);
            if (err != ESP_OK) return err;
            /* One of the two shapes `03-config.md` §Schedule sub-schema allows. An
             * instant that parses as neither is refused here, not left armed. */
            if (sched_parse_hhmmss(out->at) < 0 && !sched_is_iso(out->at)) {
                if (why != NULL && why_cap > 0) {
                    snprintf(why, why_cap,
                             "trigger.at: must be HH:MM:SS or an ISO-8601 instant");
                }
                return ESP_ERR_INVALID_ARG;
            }
        } else if (strcmp(kind, "every") == 0) {
            out->kind = SCHED_EVERY;
            int64_t interval = 0;
            err = need_int(trigger, "interval_s", SCHED_INTERVAL_MIN_S, SCHED_INTERVAL_MAX_S,
                           &interval, "trigger", why, why_cap);
            if (err != ESP_OK) return err;
            out->interval_s = (int32_t)interval;
            const json_node_t *first = json_get(trigger, "first_at");
            if (first != NULL && !json_is_null(first)) {
                int64_t value = 0;
                err = need_int(trigger, "first_at", 0, INT64_MAX, &value, "trigger",
                               why, why_cap);
                if (err != ESP_OK) return err;
                out->first_at = value;
                out->has_first_at = true;
            }
        } else {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "trigger.kind: must be at or every");
            }
            return ESP_ERR_INVALID_ARG;
        }
        break;
    }
    default:
        break;
    }

    /* --- action --- */
    const json_node_t *action = json_get(obj, "action");
    if (action == NULL || !json_is_object(action)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "task.action: required object");
        return ESP_ERR_INVALID_ARG;
    }
    err = closed(action, k_action_keys, sizeof k_action_keys / sizeof *k_action_keys,
                 "action", why, why_cap);
    if (err != ESP_OK) return err;
    char kind[10] = "";
    err = need_string(action, "kind", 1, sizeof kind - 1, kind, sizeof kind, "action",
                      why, why_cap);
    if (err != ESP_OK) return err;
    if (strcmp(kind, "prompt") == 0) {
        out->action = SCHED_ACTION_PROMPT;
        err = need_string(action, "prompt", 1, sizeof out->prompt - 1, out->prompt,
                          sizeof out->prompt, "action", why, why_cap);
        if (err != ESP_OK) return err;
    } else if (strcmp(kind, "template") == 0) {
        out->action = SCHED_ACTION_TEMPLATE;
        err = need_string(action, "template", 1, sizeof out->template_name - 1,
                          out->template_name, sizeof out->template_name, "action",
                          why, why_cap);
        if (err != ESP_OK) return err;
        /* `03-config.md` §Template schema: the name is the file stem, `[a-z0-9_-]{1,32}`,
         * and `action.template` names it without the `.md`. */
        if (!charset_ok(out->template_name, strlen(out->template_name), NULL)) {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "action.template: must be [a-z0-9_-]{1,32}");
            }
            return ESP_ERR_INVALID_ARG;
        }
        /* `action.vars` is accepted and validated as an object of strings. It is
         * carried by the renderer, which reads the file's own `vars` list. */
        const json_node_t *vars = json_get(action, "vars");
        if (vars != NULL && !json_is_null(vars)) {
            if (!json_is_object(vars)) {
                if (why != NULL && why_cap > 0) snprintf(why, why_cap, "action.vars: not an object");
                return ESP_ERR_INVALID_ARG;
            }
            size_t n = json_object_len(vars);
            if (n > SCHED_MAX_VARS) {
                if (why != NULL && why_cap > 0) {
                    snprintf(why, why_cap, "action.vars: at most %d declarations", SCHED_MAX_VARS);
                }
                return ESP_ERR_INVALID_ARG;
            }
            for (size_t i = 0; i < n; i++) {
                const json_node_t *value = json_object_value(vars, i);
                if (!json_is_string(value)) {
                    if (why != NULL && why_cap > 0) {
                        snprintf(why, why_cap, "action.vars: every value must be a string");
                    }
                    return ESP_ERR_INVALID_ARG;
                }
                const char *key = json_object_key(vars, i);
                if (key == NULL || strlen(key) > SCHED_VAR_NAME_MAX ||
                    strlen(json_string(value)) > SCHED_VAR_VALUE_MAX) {
                    if (why != NULL && why_cap > 0) {
                        snprintf(why, why_cap, "action.vars: name or value too long");
                    }
                    return ESP_ERR_INVALID_ARG;
                }
                snprintf(out->vars[out->vars_count].name, SCHED_VAR_NAME_MAX + 1, "%s", key);
                snprintf(out->vars[out->vars_count].value, SCHED_VAR_VALUE_MAX + 1, "%s",
                         json_string(value));
                out->vars_count++;
            }
        }
    } else {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "action.kind: must be prompt or template");
        }
        return ESP_ERR_INVALID_ARG;
    }

    /* --- repeat --- */
    const json_node_t *repeat = json_get(obj, "repeat");
    if (repeat != NULL && !json_is_null(repeat)) {
        err = closed(repeat, k_repeat_keys, sizeof k_repeat_keys / sizeof *k_repeat_keys,
                     "repeat", why, why_cap);
        if (err != ESP_OK) return err;
        char mode[10] = "";
        err = need_string(repeat, "mode", 1, sizeof mode - 1, mode, sizeof mode,
                          "repeat", why, why_cap);
        if (err != ESP_OK) return err;
        if (strcmp(mode, "once") == 0) {
            out->repeat_mode = SCHED_REPEAT_ONCE;
        } else if (strcmp(mode, "forever") == 0) {
            out->repeat_mode = SCHED_REPEAT_FOREVER;
        } else if (strcmp(mode, "count") == 0) {
            out->repeat_mode = SCHED_REPEAT_COUNT_MODE;
            int64_t count = 0;
            err = need_int(repeat, "count", SCHED_REPEAT_COUNT_MIN, SCHED_REPEAT_COUNT_MAX,
                           &count, "repeat", why, why_cap);
            if (err != ESP_OK) return err;
            out->repeat_count = (int32_t)count;
        } else {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "repeat.mode: must be once, count or forever");
            }
            return ESP_ERR_INVALID_ARG;
        }
    }

    /* --- missed --- */
    const json_node_t *missed = json_get(obj, "missed");
    if (missed != NULL && !json_is_null(missed)) {
        const char *text = json_string(missed);
        if (text == NULL) {
            if (why != NULL && why_cap > 0) snprintf(why, why_cap, "task.missed: not a string");
            return ESP_ERR_INVALID_ARG;
        }
        if (strcmp(text, "skip") == 0) {
            out->missed = SCHED_MISSED_SKIP;
        } else if (strcmp(text, "run_once") == 0) {
            out->missed = SCHED_MISSED_RUN_ONCE;
        } else {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "task.missed: must be skip or run_once");
            }
            return ESP_ERR_INVALID_ARG;
        }
    }

    return ESP_OK;
}

esp_err_t sched_parse(const char *json, size_t len, sched_tasks_t *out,
                      char *why, size_t why_cap, size_t *dropped)
{
    if (why != NULL && why_cap > 0) why[0] = '\0';
    if (dropped != NULL) *dropped = 0;
    if (out == NULL) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof *out);
    if (json == NULL || len == 0) return ESP_ERR_INVALID_ARG;
    /* `03-config.md` §Storage limits: checked before parsing, so an oversized file
     * cannot exhaust RAM on the way to being refused. */
    if (len > SCHED_JSON_MAX) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "/tasks.json is too large");
        return ESP_ERR_INVALID_SIZE;
    }

    json_doc_t *doc = NULL;
    esp_err_t err = json_parse(json, len, &doc);
    if (err != ESP_OK || doc == NULL) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "/tasks.json does not parse");
        /* A JSON error carries no `esp_err_t` of its own here; `json_parse` already
         * returned one, and `main` maps any non-OK to `E_CONFIG`. */
        return err != ESP_OK ? err : ESP_ERR_INVALID_ARG;
    }

    json_node_t *root = json_root(doc);
    if (!json_is_object(root)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "root is not an object");
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }
    /* Step 3: the version makes every other message meaningful. */
    int64_t version = 0;
    const json_node_t *node = json_get(root, "version");
    if (node == NULL || !json_int(node, &version) || version != 1) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "version: must be 1");
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }
    const json_node_t *list = json_get(root, "tasks");
    if (list == NULL || !json_is_array(list)) {
        if (why != NULL && why_cap > 0) snprintf(why, why_cap, "tasks: required array");
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }
    size_t n = json_array_len(list);
    if (n > SCHED_MAX_TASKS) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "tasks: %u entries, at most %d", (unsigned)n,
                     SCHED_MAX_TASKS);
        }
        json_free(doc);
        return ESP_ERR_INVALID_ARG;
    }

    for (size_t i = 0; i < n; i++) {
        sched_task_t task;
        char item_why[96] = "";
        err = sched_task_validate(json_array_at(list, i), &task, item_why, sizeof item_why);
        if (err != ESP_OK) {
            /* One bad task does not cost the file: `03-config.md` §Degradation policy
             * drops the item and leaves the rest usable. */
            if (dropped != NULL) (*dropped)++;
            if (why != NULL && why_cap > 0 && why[0] == '\0') {
                snprintf(why, why_cap, "%s", item_why);
            }
            continue;
        }
        /* Step 6: a duplicate id rejects the whole file, because the id is the NVS
         * runtime key and two tasks would share a counter. */
        bool duplicate = false;
        for (size_t k = 0; k < out->count; k++) {
            if (strcmp(out->tasks[k].id, task.id) == 0) duplicate = true;
        }
        if (duplicate) {
            if (why != NULL && why_cap > 0) {
                snprintf(why, why_cap, "tasks[].id: %s appears twice", task.id);
            }
            /* A refused file leaves no half-loaded schedule behind: `out` is empty,
             * which is the same state the other whole-file refusals produce. */
            memset(out, 0, sizeof *out);
            json_free(doc);
            return ESP_ERR_INVALID_ARG;
        }
        out->tasks[out->count++] = task;
    }

    json_free(doc);
    return ESP_OK;
}

/* --- Result shapes -------------------------------------------------------- */

void sched_trigger_json(json_node_t *parent, const sched_task_t *task)
{
    json_node_t *trigger = json_add_object(parent, "trigger");
    if (trigger == NULL) return;
    json_add_string(trigger, "type", sched_trigger_name(task->type));
    switch (task->type) {
    case SCHED_TRIGGER_GPIO:
        json_add_int(trigger, "pin", task->pin);
        json_add_int(trigger, "level", task->level);
        break;
    case SCHED_TRIGGER_MQTT_MESSAGE:
        json_add_string(trigger, "server_id", task->server_id);
        json_add_string(trigger, "topic", task->topic);
        if (task->payload_contains[0] != '\0') {
            json_add_string(trigger, "payload_contains", task->payload_contains);
        }
        json_add_int(trigger, "cooldown_s", task->cooldown_s);
        break;
    case SCHED_TRIGGER_EVENT:
        json_add_string(trigger, "name", task->name);
        break;
    case SCHED_TRIGGER_SCHEDULE:
        json_add_string(trigger, "kind", task->kind == SCHED_AT ? "at" : "every");
        if (task->kind == SCHED_AT) {
            json_add_string(trigger, "at", task->at);
        } else {
            json_add_int(trigger, "interval_s", task->interval_s);
            if (task->has_first_at) json_add_int(trigger, "first_at", task->first_at);
        }
        break;
    default:
        break;
    }
}

esp_err_t sched_list_json(const sched_tasks_t *tasks, int64_t now, int64_t boot_at,
                          int32_t tz_offset_s, json_doc_t **out)
{
    if (tasks == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_node_t *list = json_new_array(root, "tasks");
    if (list == NULL) {
        json_free(*out);
        *out = NULL;
        return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < tasks->count; i++) {
        const sched_task_t *task = &tasks->tasks[i];
        json_node_t *entry = json_array_push_object(list);
        if (entry == NULL) {
            json_free(*out);
            *out = NULL;
            return ESP_ERR_NO_MEM;
        }
        json_add_string(entry, "id", task->id);
        json_add_string(entry, "description", task->description);
        json_add_bool(entry, "enabled", task->enabled);
        sched_trigger_json(entry, task);
        json_node_t *repeat = json_add_object(entry, "repeat");
        if (repeat != NULL) {
            const char *mode = task->repeat_mode == SCHED_REPEAT_ONCE  ? "once"
                               : task->repeat_mode == SCHED_REPEAT_FOREVER ? "forever"
                                                                           : "count";
            json_add_string(repeat, "mode", mode);
            if (task->repeat_mode == SCHED_REPEAT_COUNT_MODE) {
                json_add_int(repeat, "count", task->repeat_count);
            }
        }
        json_add_int(entry, "fired_count", task->fired_count);
        /* `04` §4.4 marks `next_fire_at` optional: only a schedule task has one, and
         * only once it can be computed. Reporting 0 would read as "now". */
        if (task->type == SCHED_TRIGGER_SCHEDULE) {
            int64_t next = sched_next_fire(task, now, boot_at, tz_offset_s);
            /* `04` §4.4: only for an enabled trigger with a known clock. */
            if (task->enabled && next > 0) json_add_int(entry, "next_fire_at", next);
        }
    }
    return ESP_OK;
}

esp_err_t sched_tasks_upsert(sched_tasks_t *tasks, const sched_task_t *task,
                             bool *created)
{
    if (tasks == NULL || task == NULL) return ESP_ERR_INVALID_ARG;
    if (created != NULL) *created = false;
    for (size_t i = 0; i < tasks->count; i++) {
        if (strcmp(tasks->tasks[i].id, task->id) == 0) {
            /* An update keeps the runtime state: `03-config.md` §tasks says an
             * existing task's `fired_count` survives a `tasks.set`, and only a
             * delete-then-set starts over. */
            int32_t count = tasks->tasks[i].fired_count;
            int64_t last = tasks->tasks[i].last_fired;
            tasks->tasks[i] = *task;
            tasks->tasks[i].fired_count = count;
            tasks->tasks[i].last_fired = last;
            return ESP_OK;
        }
    }
    if (tasks->count >= SCHED_MAX_TASKS) return ESP_ERR_INVALID_SIZE;
    tasks->tasks[tasks->count++] = *task;
    if (created != NULL) *created = true;
    return ESP_OK;
}

esp_err_t sched_tasks_remove(sched_tasks_t *tasks, const char *id, bool *deleted)
{
    if (tasks == NULL || id == NULL) return ESP_ERR_INVALID_ARG;
    if (deleted != NULL) *deleted = false;
    for (size_t i = 0; i < tasks->count; i++) {
        if (strcmp(tasks->tasks[i].id, id) != 0) continue;
        for (size_t k = i; k + 1 < tasks->count; k++) tasks->tasks[k] = tasks->tasks[k + 1];
        tasks->count--;
        if (deleted != NULL) *deleted = true;
        return ESP_OK;
    }
    return ESP_OK;
}

esp_err_t sched_emit_json(const sched_tasks_t *tasks, json_doc_t **out)
{
    if (tasks == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_int(root, "version", 1);
    json_node_t *list = json_new_array(root, "tasks");
    if (list == NULL) {
        json_free(*out);
        *out = NULL;
        return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < tasks->count; i++) {
        const sched_task_t *task = &tasks->tasks[i];
        json_node_t *entry = json_array_push_object(list);
        if (entry == NULL) {
            json_free(*out);
            *out = NULL;
            return ESP_ERR_NO_MEM;
        }
        json_add_string(entry, "id", task->id);
        json_add_bool(entry, "enabled", task->enabled);
        if (task->description[0] != '\0') {
            json_add_string(entry, "description", task->description);
        }
        sched_trigger_json(entry, task);
        json_node_t *action = json_add_object(entry, "action");
        if (action != NULL) {
            if (task->action == SCHED_ACTION_PROMPT) {
                json_add_string(action, "kind", "prompt");
                json_add_string(action, "prompt", task->prompt);
            } else {
                json_add_string(action, "kind", "template");
                json_add_string(action, "template", task->template_name);
            }
            /* `03-config.md` §Action schema: `vars` substitutes into the template, so
             * a rewrite that dropped it would silently change what the task says. */
            if (task->vars_count > 0) {
                json_node_t *vars = json_add_object(action, "vars");
                if (vars != NULL) {
                    for (size_t v = 0; v < task->vars_count; v++) {
                        json_add_string(vars, task->vars[v].name, task->vars[v].value);
                    }
                }
            }
        }
        json_node_t *repeat = json_add_object(entry, "repeat");
        if (repeat != NULL) {
            const char *mode = task->repeat_mode == SCHED_REPEAT_ONCE  ? "once"
                               : task->repeat_mode == SCHED_REPEAT_FOREVER ? "forever"
                                                                           : "count";
            json_add_string(repeat, "mode", mode);
            if (task->repeat_mode == SCHED_REPEAT_COUNT_MODE) {
                json_add_int(repeat, "count", task->repeat_count);
            }
        }
        json_add_string(entry, "missed", task->missed == SCHED_MISSED_RUN_ONCE ? "run_once"
                                                                              : "skip");
    }
    return ESP_OK;
}

/* --- Matching ------------------------------------------------------------- */

bool sched_topic_matches(const char *pattern, const char *topic)
{
    if (pattern == NULL || topic == NULL) return false;
    size_t plen = strlen(pattern);
    /* A lone `*` matches everything: it is the zero-level prefix plus one level. */
    if (plen == 1 && pattern[0] == '*') return strchr(topic, '/') == NULL;
    if (plen > 0 && pattern[plen - 1] == '*') {
        /* Exactly one final level: the prefix, then no `/` left over. */
        if (plen < 2 || pattern[plen - 2] != '/') return false;
        size_t prefix = plen - 1;
        if (strncmp(pattern, topic, prefix) != 0) return false;
        const char *rest = topic + prefix;
        return *rest != '\0' && strchr(rest, '/') == NULL;
    }
    return strcmp(pattern, topic) == 0;
}

bool sched_event_name_ok(const char *name)
{
    if (name == NULL) return false;
    size_t len = strlen(name);
    if (len < 1 || len > SCHED_EVENT_NAME_MAX) return false;
    return charset_ok(name, len, ".");
}

size_t sched_match_bare(const sched_tasks_t *tasks, sched_trigger_t type,
                        sched_hit_t *hits, size_t cap)
{
    if (tasks == NULL) return 0;
    size_t found = 0;
    for (size_t i = 0; i < tasks->count && found < cap; i++) {
        const sched_task_t *task = &tasks->tasks[i];
        if (!task->enabled || task->type != type) continue;
        hits[found].index = i;
        hits[found].level = -1;
        found++;
    }
    return found;
}

size_t sched_match_event(const sched_tasks_t *tasks, const char *name,
                         sched_hit_t *hits, size_t cap)
{
    if (tasks == NULL || name == NULL) return 0;
    size_t found = 0;
    for (size_t i = 0; i < tasks->count && found < cap; i++) {
        const sched_task_t *task = &tasks->tasks[i];
        if (!task->enabled || task->type != SCHED_TRIGGER_EVENT) continue;
        /* Exact and case-sensitive (`02-firmware.md` §5.5), not a prefix. */
        if (strcmp(task->name, name) != 0) continue;
        hits[found].index = i;
        hits[found].level = -1;
        found++;
    }
    return found;
}

size_t sched_match_gpio(const sched_tasks_t *tasks, int pin, int level,
                        sched_hit_t *hits, size_t cap)
{
    if (tasks == NULL) return 0;
    size_t found = 0;
    for (size_t i = 0; i < tasks->count && found < cap; i++) {
        const sched_task_t *task = &tasks->tasks[i];
        if (!task->enabled || task->type != SCHED_TRIGGER_GPIO) continue;
        if (task->pin != pin || task->level != level) continue;
        hits[found].index = i;
        hits[found].level = level;
        found++;
    }
    return found;
}

size_t sched_match_mqtt(const sched_tasks_t *tasks, const char *server_id,
                        const char *topic, const char *payload, int64_t now,
                        sched_hit_t *hits, size_t cap)
{
    if (tasks == NULL || server_id == NULL || topic == NULL) return 0;
    size_t found = 0;
    /* Two passes so an exact topic is offered before a wildcard, which is the order
     * `02-firmware.md` §5.4 states. Both passes apply the same filter otherwise. */
    for (int pass = 0; pass < 2 && found < cap; pass++) {
        for (size_t i = 0; i < tasks->count && found < cap; i++) {
            const sched_task_t *task = &tasks->tasks[i];
            if (!task->enabled || task->type != SCHED_TRIGGER_MQTT_MESSAGE) continue;
            size_t tlen = strlen(task->topic);
            bool wildcard = tlen > 0 && task->topic[tlen - 1] == '*';
            if ((pass == 1) != wildcard) continue;
            if (strcmp(task->server_id, server_id) != 0) continue;
            if (!sched_topic_matches(task->topic, topic)) continue;
            if (task->payload_contains[0] != '\0') {
                if (payload == NULL || strstr(payload, task->payload_contains) == NULL) {
                    continue;
                }
            }
            /* `cooldown_s` is a wall-clock window measured from the last fire, so a
             * burst of matching messages cannot start a burst of runs. */
            if (task->cooldown_s > 0 && task->last_fired > 0 &&
                now - task->last_fired < task->cooldown_s) {
                continue;
            }
            hits[found].index = i;
            hits[found].level = -1;
            found++;
        }
    }
    return found;
}

/* --- Schedule arithmetic -------------------------------------------------- */

bool sched_repeat_allows(const sched_task_t *task)
{
    if (task == NULL) return false;
    switch (task->repeat_mode) {
    case SCHED_REPEAT_ONCE:
        return task->fired_count == 0;
    case SCHED_REPEAT_COUNT_MODE:
        return task->fired_count < task->repeat_count;
    case SCHED_REPEAT_FOREVER:
        return true;
    default:
        return false;
    }
}

int64_t sched_next_fire(const sched_task_t *task, int64_t now, int64_t boot_at,
                        int32_t tz_offset_s)
{
    if (task == NULL || task->type != SCHED_TRIGGER_SCHEDULE) return 0;

    if (task->kind == SCHED_AT) {
        int32_t sod = sched_parse_hhmmss(task->at);
        if (sod >= 0) {
            /* A daily wall-clock time. `03-config.md` §Schedule sub-schema: it fires
             * at the next occurrence, in local time, so the offset is applied to both
             * sides and cancels out of the answer. */
            int64_t local = now + tz_offset_s;
            int64_t midnight = local - ((local % 86400) + 86400) % 86400;
            int64_t target = midnight + sod;
            if (target <= local) target += 86400;
            return target - tz_offset_s;
        }
        /* A fixed instant is absolute: no timezone, and no second occurrence. */
        int64_t fixed = 0;
        return parse_iso(task->at, &fixed) ? fixed : 0;
    }

    /* `every`: a fixed interval from `first_at`, or from boot when it was omitted
     * (`02-firmware.md` §5.2). */
    if (task->interval_s <= 0) return 0;
    int64_t anchor = task->has_first_at ? task->first_at : boot_at;
    if (anchor <= 0) return 0;
    if (now < anchor) return anchor;
    int64_t elapsed = now - anchor;
    int64_t steps = elapsed / task->interval_s;
    int64_t next = anchor + (steps + 1) * task->interval_s;
    /* At exactly the boundary the next fire is now, not one interval later. */
    if (elapsed == steps * task->interval_s) next = now;
    return next;
}

int64_t sched_last_due(const sched_task_t *task, int64_t now, int64_t boot_at,
                       int32_t tz_offset_s)
{
    if (task == NULL || task->type != SCHED_TRIGGER_SCHEDULE) return 0;

    if (task->kind == SCHED_AT) {
        int32_t sod = sched_parse_hhmmss(task->at);
        if (sod >= 0) {
            /* The occurrence at or before now, in local time. */
            int64_t local = now + tz_offset_s;
            int64_t midnight = local - ((local % 86400) + 86400) % 86400;
            int64_t target = midnight + sod;
            if (target > local) target -= 86400;
            return target - tz_offset_s;
        }
        /* A fixed instant is due only once it has arrived. Returning it early would
         * report a task as due before its moment, which the tick then fires. */
        int64_t fixed = 0;
        if (!parse_iso(task->at, &fixed) || fixed > now) return 0;
        return fixed;
    }

    if (task->interval_s <= 0) return 0;
    int64_t anchor = task->has_first_at ? task->first_at : boot_at;
    if (anchor <= 0 || now < anchor) return 0;
    int64_t steps = (now - anchor) / task->interval_s;
    return anchor + steps * task->interval_s;
}

bool sched_missed_should_fire(const sched_task_t *task, int64_t due_at, int64_t now,
                              int64_t grace_s)
{
    if (task == NULL || due_at <= 0) return false;
    if (now < due_at) return false;
    /* `skip` is the default: a fire the device slept through is dropped once it is
     * more than `grace_s` late. `run_once` runs it however late it is. */
    if (task->missed == SCHED_MISSED_RUN_ONCE) return true;
    return now - due_at <= grace_s;
}

/* --- Runtime state -------------------------------------------------------- */

esp_err_t sched_state_encode(int32_t fired_count, int64_t last_fired,
                             char *out, size_t cap)
{
    if (out == NULL || cap == 0) return ESP_ERR_INVALID_ARG;
    int n = snprintf(out, cap, "%ld:%lld", (long)fired_count, (long long)last_fired);
    if (n <= 0 || (size_t)n >= cap) return ESP_ERR_INVALID_SIZE;
    return ESP_OK;
}

bool sched_state_decode(const char *text, int32_t *fired_count, int64_t *last_fired)
{
    if (text == NULL || *text == '\0') return false;
    const char *colon = strchr(text, ':');
    if (colon == NULL || colon == text || colon[1] == '\0') return false;
    int32_t count = 0;
    for (const char *p = text; p < colon; p++) {
        if (*p < '0' || *p > '9') return false;
        count = count * 10 + (*p - '0');
    }
    int64_t last = 0;
    for (const char *p = colon + 1; *p != '\0'; p++) {
        if (*p < '0' || *p > '9') return false;
        last = last * 10 + (*p - '0');
    }
    if (fired_count != NULL) *fired_count = count;
    if (last_fired != NULL) *last_fired = last;
    return true;
}

/* --- Template rendering (`03-config.md` §Template schema) ------------------ */

/* Frontmatter is a leading `---` line, the block, and a closing `---` line. The
 * `vars` list inside it is the only field rendering needs: an undeclared `{{x}}`
 * must stay literal, so the declaration is what makes a placeholder real. */
static bool frontmatter_vars(const char *text, size_t len, char names[][SCHED_VAR_NAME_MAX + 1],
                             size_t *count, size_t cap, size_t *body)
{
    *count = 0;
    *body = 0;
    if (len < 4 || strncmp(text, "---", 3) != 0) return false;
    size_t line_end = 3;
    while (line_end < len && text[line_end] != '\n') line_end++;
    if (line_end >= len) return false;
    size_t cursor = line_end + 1;
    while (cursor < len) {
        size_t end = cursor;
        while (end < len && text[end] != '\n') end++;
        if (end - cursor >= 3 && strncmp(text + cursor, "---", 3) == 0) {
            *body = end < len ? end + 1 : end;
            return true;
        }
        /* `vars: [a, b]`, one line, which is the documented shape. A multi-line list
         * is not: §Template schema's example is inline and the parser refuses to grow
         * a YAML implementation for a field it can read here. */
        if (end - cursor > 5 && strncmp(text + cursor, "vars:", 5) == 0) {
            size_t i = cursor + 5;
            while (i < end && (text[i] == ' ' || text[i] == '[')) i++;
            while (i < end && *count < cap) {
                /* The space after a comma separates, it is not part of the name. */
                while (i < end && text[i] == ' ') i++;
                size_t start = i;
                while (i < end && text[i] != ',' && text[i] != ']') i++;
                size_t stop = i;
                while (stop > start && (text[stop - 1] == ' ' || text[stop - 1] == '\r')) {
                    stop--;
                }
                if (stop > start && stop - start <= SCHED_VAR_NAME_MAX &&
                    charset_ok(text + start, stop - start, ".")) {
                    memcpy(names[*count], text + start, stop - start);
                    names[*count][stop - start] = '\0';
                    (*count)++;
                }
                while (i < end && text[i] != ',') i++;
                if (i < end && text[i] == ',') i++;
            }
        }
        cursor = end < len ? end + 1 : end;
    }
    return false;
}

static const char *declared_value(const sched_task_t *task, const char *name, size_t len)
{
    for (size_t i = 0; i < task->vars_count; i++) {
        if (strlen(task->vars[i].name) == len &&
            strncmp(task->vars[i].name, name, len) == 0) {
            return task->vars[i].value;
        }
    }
    return NULL;
}

esp_err_t sched_render_template(const char *text, size_t len, const sched_task_t *task,
                                char *out, size_t cap, size_t *out_len)
{
    if (text == NULL || task == NULL || out == NULL || cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    char names[SCHED_MAX_VARS][SCHED_VAR_NAME_MAX + 1];
    size_t declared = 0;
    size_t body = 0;
    if (!frontmatter_vars(text, len, names, &declared, SCHED_MAX_VARS, &body)) {
        /* No frontmatter: the whole file is the prompt and nothing is declared, so
         * every `{{x}}` survives. That is rule 2, not a failure. */
        body = 0;
        declared = 0;
    }

    size_t written = 0;
    for (size_t i = body; i < len;) {
        if (i + 1 < len && text[i] == '{' && text[i + 1] == '{') {
            size_t close = i + 2;
            while (close + 1 < len && !(text[close] == '}' && text[close + 1] == '}')) close++;
            if (close + 1 < len) {
                size_t name_len = close - (i + 2);
                bool is_declared = false;
                for (size_t k = 0; k < declared; k++) {
                    if (strlen(names[k]) == name_len &&
                        strncmp(names[k], text + i + 2, name_len) == 0) {
                        is_declared = true;
                        break;
                    }
                }
                if (is_declared) {
                    const char *value = declared_value(task, text + i + 2, name_len);
                    size_t vlen = value != NULL ? strlen(value) : 0;
                    if (written + vlen >= cap) return ESP_ERR_INVALID_SIZE;
                    if (vlen > 0) memcpy(out + written, value, vlen);
                    written += vlen;
                    i = close + 2;
                    continue;
                }
                /* Undeclared: copy the placeholder verbatim, rule 2. */
                size_t span = close + 2 - i;
                if (written + span >= cap) return ESP_ERR_INVALID_SIZE;
                memcpy(out + written, text + i, span);
                written += span;
                i = close + 2;
                continue;
            }
        }
        if (written + 1 >= cap) return ESP_ERR_INVALID_SIZE;
        out[written++] = text[i++];
    }
    out[written] = '\0';
    /* Rule 3: the rendered size is bounded by `agent.max_input_bytes`, which the
     * caller checks; this only reports what it built. */
    if (out_len != NULL) *out_len = written;
    return ESP_OK;
}

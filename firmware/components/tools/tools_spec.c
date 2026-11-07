/* tools_spec — the built-in registry and the validator every call passes first.
 *
 * The schema is the single source of truth for what a call may contain: validation
 * walks the same text the model receives, so an edit to one cannot silently disagree
 * with the other (`02-firmware.md` §7.1). Pure. */

#include "tools.h"

#include <stdio.h>
#include <string.h>

/* Canonical member order, so a schema reads the same here as in its domain file. */
#define S_PIN            "{\"type\":\"object\",\"properties\":{\"pin\":{\"type\":\"integer\"}},\"required\":[\"pin\"]}"
#define S_PIN_LEVEL      "{\"type\":\"object\",\"properties\":{\"pin\":{\"type\":\"integer\"},\"level\":{\"type\":\"integer\",\"enum\":[0,1]}},\"required\":[\"pin\",\"level\"]}"
#define S_NONE           "{\"type\":\"object\",\"properties\":{}}"
#define S_I2C_READ       "{\"type\":\"object\",\"properties\":{\"addr\":{\"type\":\"integer\"},\"len\":{\"type\":\"integer\"}},\"required\":[\"addr\",\"len\"]}"
#define S_I2C_WRITE      "{\"type\":\"object\",\"properties\":{\"addr\":{\"type\":\"integer\"},\"hex\":{\"type\":\"string\"}},\"required\":[\"addr\",\"hex\"]}"
#define S_TIME_NOW       "{\"type\":\"object\",\"properties\":{}}"
#define S_FS_LIST        "{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"}},\"required\":[\"path\"]}"
#define S_FS_READ        "{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"}},\"required\":[\"path\"]}"
#define S_FS_WRITE       "{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"},\"content\":{\"type\":\"string\"}},\"required\":[\"path\",\"content\"]}"
#define S_FS_DELETE      "{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"},\"recursive\":{\"type\":\"boolean\"}},\"required\":[\"path\"]}"
#define S_HTTP_GET       "{\"type\":\"object\",\"properties\":{\"url\":{\"type\":\"string\"},\"headers\":{\"type\":\"object\"}},\"required\":[\"url\"]}"
#define S_SKILL_READ     "{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"}},\"required\":[\"name\"]}"
#define S_TASK_LIST      "{\"type\":\"object\",\"properties\":{}}"
#define S_EVENT_EMIT     "{\"type\":\"object\",\"properties\":{\"type\":{\"type\":\"string\"},\"data\":{\"type\":\"string\"}},\"required\":[\"type\"]}"
#define S_TODO           "{\"type\":\"object\",\"properties\":{\"op\":{\"type\":\"string\",\"enum\":[\"init\",\"append\",\"view\",\"start\",\"done\",\"drop\",\"rm\",\"block\",\"unblock\"]},\"list\":{\"type\":\"array\"},\"items\":{\"type\":\"array\"},\"phase\":{\"type\":\"string\"},\"task\":{\"type\":\"string\"},\"reason\":{\"type\":\"string\"}},\"required\":[\"op\"]}"
#define S_GEO            "{\"type\":\"object\",\"properties\":{}}"
#define S_MQTT_LIST      "{\"type\":\"object\",\"properties\":{}}"
#define S_MQTT_PUBLISH   "{\"type\":\"object\",\"properties\":{\"server\":{\"type\":\"string\"},\"topic\":{\"type\":\"string\"},\"payload\":{\"type\":\"string\"}},\"required\":[\"server\",\"topic\",\"payload\"]}"
#define S_MEM_GET        "{\"type\":\"object\",\"properties\":{\"key\":{\"type\":\"string\"}},\"required\":[\"key\"]}"
#define S_MEM_SET        "{\"type\":\"object\",\"properties\":{\"key\":{\"type\":\"string\"},\"value\":{\"type\":\"string\"}},\"required\":[\"key\",\"value\"]}"
#define S_MEM_LIST       "{\"type\":\"object\",\"properties\":{}}"
#define S_MEM_DELETE     "{\"type\":\"object\",\"properties\":{\"key\":{\"type\":\"string\"}},\"required\":[\"key\"]}"
#define S_SYS_DIAG       "{\"type\":\"object\",\"properties\":{\"scope\":{\"type\":\"string\",\"enum\":[\"quick\",\"runtime\",\"memory\",\"net\",\"all\"]}}}"

/* `task.set`'s `task` object mirrors `/tasks.json` verbatim (`02-firmware.md` §7.2).
 * Only the members the schema needs to type are named; `required` is the file's own
 * minimum. */
#define S_TASK_SET \
    "{\"type\":\"object\",\"properties\":{\"task\":{\"type\":\"object\",\"properties\":{" \
    "\"id\":{\"type\":\"string\"},\"name\":{\"type\":\"string\"},\"enabled\":{\"type\":\"boolean\"}," \
    "\"trigger\":{\"type\":\"object\"},\"prompt\":{\"type\":\"string\"}}," \
    "\"required\":[\"name\",\"trigger\",\"prompt\"]}},\"required\":[\"task\"]}"

/* `00-overview.md` §7 order, and the gate per tool from `06-security.md` §6. */
static const tools_builtin_t BUILTINS[TOOLS_MAX_BUILTINS] = {
    {"gpio.read",  "Read the digital level (0 or 1) of a header GPIO pin.", TOOLS_GATE_NONE, S_PIN},
    {"gpio.write", "Drive a header GPIO pin high (1) or low (0) once.", TOOLS_GATE_DANGEROUS, S_PIN_LEVEL},
    {"adc.read",   "Sample a 12-bit analog voltage on GPIO1, GPIO2 or GPIO4.", TOOLS_GATE_NONE, S_PIN},
    {"i2c.scan",   "Probe every 7-bit I2C address and list those that answered.", TOOLS_GATE_NONE, S_NONE},
    {"i2c.read",   "Read up to 64 bytes from a 7-bit I2C device, returned as hex.", TOOLS_GATE_NONE, S_I2C_READ},
    {"i2c.write",  "Write hex bytes to a 7-bit I2C device.", TOOLS_GATE_DANGEROUS, S_I2C_WRITE},
    {"time.now",   "Return the device clock as a unix timestamp and an ISO-8601 string.", TOOLS_GATE_NONE, S_TIME_NOW},
    {"fs.list",    "List one directory under /workspace/.", TOOLS_GATE_NONE, S_FS_LIST},
    {"fs.read",    "Read a UTF-8 text file under /workspace/.", TOOLS_GATE_NONE, S_FS_READ},
    {"fs.write",   "Write a UTF-8 text file under /workspace/, atomically.", TOOLS_GATE_DANGEROUS, S_FS_WRITE},
    {"fs.delete",  "Delete a file or empty directory under /workspace/.", TOOLS_GATE_DANGEROUS, S_FS_DELETE},
    {"http.get",   "Fetch one allowlisted https URL and return status, headers and body.", TOOLS_GATE_ALLOWLIST, S_HTTP_GET},
    {"skill.read", "Read the Markdown body of a user-authored skill from /skills/.", TOOLS_GATE_NONE, S_SKILL_READ},
    {"task.list",  "List the scheduled tasks in /tasks.json.", TOOLS_GATE_NONE, S_TASK_LIST},
    {"task.set",   "Add or update one scheduled task. Never deletes.", TOOLS_GATE_DANGEROUS, S_TASK_SET},
    {"event.emit", "Raise an event of the given type, optionally carrying a data string.", TOOLS_GATE_NONE, S_EVENT_EMIT},
    {"todo",       "Create and update a phased task checklist for this run.", TOOLS_GATE_NONE, S_TODO},
    {"geo.locate", "Return the device position from the configured geolocation provider.", TOOLS_GATE_NONE, S_GEO},
    {"mqtt.list",  "List the configured MQTT broker connections and their state.", TOOLS_GATE_NONE, S_MQTT_LIST},
    {"mqtt.publish", "Publish a payload to a topic on a configured MQTT broker.", TOOLS_GATE_DANGEROUS, S_MQTT_PUBLISH},
    {"memory.get", "Read one durable key/value memory entry.", TOOLS_GATE_NONE, S_MEM_GET},
    {"memory.set", "Write one durable key/value memory entry.", TOOLS_GATE_NONE, S_MEM_SET},
    {"memory.list", "List the durable memory keys.", TOOLS_GATE_NONE, S_MEM_LIST},
    {"memory.delete", "Delete one durable memory entry.", TOOLS_GATE_NONE, S_MEM_DELETE},
    {"sys.diagnostics", "Read a projected subset of device health counters.", TOOLS_GATE_NONE, S_SYS_DIAG},
};

_Static_assert(sizeof BUILTINS / sizeof BUILTINS[0] == TOOLS_MAX_BUILTINS,
               "the frozen surface is twenty-five built-ins (`00-overview.md` §7)");

const tools_builtin_t *tools_builtins(size_t *count)
{
    if (count != NULL) *count = TOOLS_MAX_BUILTINS;
    return BUILTINS;
}

const tools_builtin_t *tools_find(const char *name)
{
    if (name == NULL) return NULL;
    for (size_t i = 0; i < TOOLS_MAX_BUILTINS; i++) {
        if (strcmp(BUILTINS[i].name, name) == 0) return &BUILTINS[i];
    }
    return NULL;
}

bool tools_is_builtin(const char *name)
{
    return tools_find(name) != NULL;
}

/* --- Validation ----------------------------------------------------------- */

static void say(char *why, size_t cap, const char *fmt, const char *a, const char *b)
{
    if (why == NULL || cap == 0) return;
    snprintf(why, cap, fmt, a != NULL ? a : "", b != NULL ? b : "");
}

/* Integer bounds a schema states as `minimum`/`maximum`, and the two that only the
 * tool's prose states and so are enforced here (`tools/i2c.md`, `tools/event.md`). */
static bool int_in_range(const char *tool, const char *key, int64_t value,
                         char *why, size_t cap)
{
    int64_t min = INT64_MIN, max = INT64_MAX;
    if (strcmp(tool, "adc.read") == 0 && strcmp(key, "pin") == 0) {
        /* The allowlist, not a range: GPIO1, GPIO2, GPIO4 (`tools/adc.md`). */
        if (value != 1 && value != 2 && value != 4) {
            say(why, cap, "%s: pin is not ADC1-capable (1, 2 or 4)", tool, NULL);
            return false;
        }
        return true;
    }
    if (strncmp(tool, "i2c.", 4) == 0 && strcmp(key, "addr") == 0) {
        min = 8; max = 119;  /* 0x08-0x77, `tools/i2c.md` */
    }
    if (strcmp(tool, "i2c.read") == 0 && strcmp(key, "len") == 0) {
        min = 1; max = 64;
    }
    if (value < min || value > max) {
        say(why, cap, "%s: %s is out of range", tool, key);
        return false;
    }
    return true;
}

/* One property of the schema subset: type, `enum`, and recursion into `object` and
 * `array`. Anything outside the subset is a schema bug, not a call failure, so an
 * unknown keyword is ignored rather than refused. */
static bool check_value(const char *tool, const char *key, const json_node_t *prop,
                        const json_node_t *value, char *why, size_t cap)
{
    const char *type = json_string(json_get(prop, "type"));

    if (type != NULL) {
        bool ok = true;
        if (strcmp(type, "string") == 0) {
            ok = json_is_string(value);
        } else if (strcmp(type, "integer") == 0) {
            ok = json_is_int(value);
        } else if (strcmp(type, "number") == 0) {
            /* `weather_now` takes decimal degrees; an integer is a legal number. */
            ok = json_is_int(value);
        } else if (strcmp(type, "boolean") == 0) {
            ok = json_is_bool(value);
        } else if (strcmp(type, "object") == 0) {
            ok = json_is_object(value);
        } else if (strcmp(type, "array") == 0) {
            ok = json_is_array(value);
        }
        if (!ok) {
            say(why, cap, "%s: %s has the wrong type", tool, key);
            return false;
        }
    }

    const json_node_t *allowed = json_get(prop, "enum");
    if (json_array_len(allowed) > 0) {
        bool found = false;
        for (size_t i = 0; i < json_array_len(allowed); i++) {
            const json_node_t *candidate = json_array_at(allowed, i);
            if (json_is_int(candidate) && json_is_int(value)) {
                int64_t a = 0, b = 0;
                json_int(candidate, &a);
                json_int(value, &b);
                if (a == b) found = true;
            } else {
                const char *a = json_string(candidate);
                const char *b = json_string(value);
                if (a != NULL && b != NULL && strcmp(a, b) == 0) found = true;
            }
            if (found) break;
        }
        if (!found) {
            say(why, cap, "%s: %s is not one of the allowed values", tool, key);
            return false;
        }
    }

    if (json_is_int(value)) {
        int64_t n = 0;
        json_int(value, &n);
        if (!int_in_range(tool, key, n, why, cap)) return false;
    }
    return true;
}

esp_err_t tools_validate(const char *tool, const json_node_t *args, char *why,
                         size_t why_cap)
{
    if (why != NULL && why_cap > 0) why[0] = '\0';
    const tools_builtin_t *spec = tools_find(tool);
    if (spec == NULL) {
        say(why, why_cap, "%s: unknown tool", tool, NULL);
        return ESP_ERR_INVALID_ARG;
    }

    json_doc_t *doc = NULL;
    if (json_parse(spec->schema, strlen(spec->schema), &doc) != ESP_OK) {
        /* The registry is compiled in; a schema that does not parse is a build-time
         * bug the host suite catches, never a call-time refusal. */
        say(why, why_cap, "%s: schema is malformed", tool, NULL);
        return ESP_ERR_INVALID_ARG;
    }
    const json_node_t *root = json_root(doc);
    if (args == NULL) {
        json_free(doc);
        say(why, why_cap, "%s: arguments must be an object", tool, NULL);
        return ESP_ERR_INVALID_ARG;
    }
    if (!json_is_object(args)) {
        json_free(doc);
        say(why, why_cap, "%s: arguments must be an object", tool, NULL);
        return ESP_ERR_INVALID_ARG;
    }

    const json_node_t *required = json_get(root, "required");
    for (size_t i = 0; i < json_array_len(required); i++) {
        const char *key = json_string(json_array_at(required, i));
        if (key == NULL) continue;
        if (json_get(args, key) == NULL) {
            json_free(doc);
            say(why, why_cap, "%s: %s is required", tool, key);
            return ESP_ERR_INVALID_ARG;
        }
    }

    const json_node_t *props = json_get(root, "properties");
    for (size_t i = 0; i < json_object_len(props); i++) {
        const char *key = json_object_key(props, i);
        const json_node_t *value = json_get(args, key);
        if (value == NULL) continue;
        const json_node_t *prop = json_object_value(props, i);
        if (!check_value(tool, key, prop, value, why, why_cap)) {
            json_free(doc);
            return ESP_ERR_INVALID_ARG;
        }
    }

    /* `tools/web.md` §Declarative tools: an argument not declared in
     * `parameters.properties` is `E_TOOL`. The same rule keeps a model from stuffing
     * extra members into `todo`'s nested objects. */
    for (size_t i = 0; i < json_object_len(args); i++) {
        const char *key = json_object_key(args, i);
        if (json_get(props, key) == NULL) {
            json_free(doc);
            say(why, why_cap, "%s: %s is not a declared argument", tool, key);
            return ESP_ERR_INVALID_ARG;
        }
    }

    json_free(doc);
    return ESP_OK;
}

/* tools.c — execute one validated call.
 *
 * Board-only: it touches GPIO, the ADC, the I2C bus, the filesystem, NVS and the
 * network. The names, schemas, validation and gating it sits behind are pure and
 * host-tested in `tools_spec.c` and `tools_gate.c`, so a refused call is a test case
 * and only an executed one needs a board. */

#include "tools.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <time.h>

#include "esp_log.h"
#include "nvs.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "cfg.h"
#include "clock.h"
#include "net.h"
#include "skills.h"
#include "store.h"
#include "sys.h"

/* `03-config.md` §tools: `/tools/` `.json` bodies and `tools_json` share this bound. */
#define TOOLS_TEXT_MAX 2048
#define TOOLS_HEADER_COUNT 4
/* `03-config.md` §tools `http_timeout_ms` default, used when the key is absent. */
#define TOOLS_HTTP_TIMEOUT_MS 10000

/* `tools/sys.md`: the diagnostic snapshot is a projected subset of `/status`, not a
 * second status document. */
static int32_t s_call_count;
static int32_t s_failure_count;

/* One text scratch for results that are returned as a string, and the caller-header
 * copies `http.get` needs alive across the exchange. Statics because a tool runs on
 * the agent task's 4096-byte stack. */
static char s_read_buf[TOOLS_RESULT_MAX + 1];
static char s_header_name[TOOLS_HEADER_COUNT][48];
static char s_header_value[TOOLS_HEADER_COUNT][256];

static void set_why(char *why, size_t cap, const char *text)
{
    if (why != NULL && cap > 0) snprintf(why, cap, "%s", text);
}

static void add_int(json_node_t *obj, const char *key, int64_t value)
{
    json_add_int(obj, key, value);
}

/* --- Hardware ------------------------------------------------------------- */

static esp_err_t call_gpio_read(const json_node_t *args, json_doc_t **out,
                                char *why, size_t why_cap)
{
    int64_t pin = 0;
    json_int(json_get(args, "pin"), &pin);
    int level = 0;
    esp_err_t err = sys_gpio_read((int)pin, &level);
    if (err != ESP_OK) {
        set_why(why, why_cap, "gpio.read: the pin cannot be read");
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    add_int(json_root(*out), "pin", pin);
    add_int(json_root(*out), "level", level);
    return ESP_OK;
}

static esp_err_t call_gpio_write(const json_node_t *args, json_doc_t **out,
                                 char *why, size_t why_cap)
{
    int64_t pin = 0, level = 0;
    json_int(json_get(args, "pin"), &pin);
    json_int(json_get(args, "level"), &level);
    esp_err_t err = sys_gpio_write((int)pin, (int)level);
    if (err != ESP_OK) {
        set_why(why, why_cap, "gpio.write: the pin cannot be driven");
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    add_int(json_root(*out), "pin", pin);
    add_int(json_root(*out), "level", level);
    return ESP_OK;
}

static esp_err_t call_adc_read(const json_node_t *args, json_doc_t **out,
                               char *why, size_t why_cap)
{
    int64_t pin = 0;
    json_int(json_get(args, "pin"), &pin);
    int raw = 0;
    esp_err_t err = sys_adc_read((int)pin, &raw);
    if (err != ESP_OK) {
        set_why(why, why_cap, "adc.read: the channel cannot be sampled");
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    add_int(root, "pin", pin);
    add_int(root, "raw", raw);
    /* Millivolts, rounded: 12-bit over 3.3 V (`tools/adc.md`). */
    add_int(root, "mv", (int64_t)((raw * 3300LL) / 4095));
    return ESP_OK;
}

static esp_err_t call_i2c(const char *tool, const tools_context_t *ctx,
                          const json_node_t *args, json_doc_t **out,
                          char *why, size_t why_cap)
{
    if (ctx->i2c_sda < 0 || ctx->i2c_scl < 0) {
        /* `tools/i2c.md`: the family is absent from `tools_json` when the bus is not
         * configured, so reaching here means a stale registry — refused, not guessed. */
        set_why(why, why_cap, "the I2C bus is not configured");
        return ESP_ERR_INVALID_STATE;
    }

    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);

    if (strcmp(tool, "i2c.scan") == 0) {
        json_node_t *found = json_new_array(root, "addrs");
        if (found == NULL) return ESP_ERR_NO_MEM;
        for (int addr = 0x08; addr <= 0x77; addr++) {
            if (sys_i2c_probe(ctx->i2c_sda, ctx->i2c_scl, addr) != ESP_OK) continue;
            if (json_array_push_int(found, addr) == NULL) return ESP_ERR_NO_MEM;
        }
        return ESP_OK;
    }

    int64_t addr = 0;
    json_int(json_get(args, "addr"), &addr);

    if (strcmp(tool, "i2c.read") == 0) {
        int64_t len = 0;
        json_int(json_get(args, "len"), &len);
        uint8_t buf[64];
        esp_err_t err = sys_i2c_read(ctx->i2c_sda, ctx->i2c_scl, (int)addr, buf,
                                     (size_t)len);
        if (err != ESP_OK) {
            set_why(why, why_cap, "i2c.read: the device did not answer");
            return err;
        }
        add_int(root, "addr", addr);
        add_int(root, "len", len);
        char hex[129];
        size_t at = 0;
        for (size_t i = 0; i < (size_t)len && at + 3 < sizeof hex; i++) {
            at += (size_t)snprintf(hex + at, sizeof hex - at, "%02x", buf[i]);
        }
        hex[at] = '\0';
        json_add_string(root, "hex", hex);
        return ESP_OK;
    }

    /* i2c.write: `hex` is an even-length string of hex digits (`tools/i2c.md`). */
    const char *hex = json_string(json_get(args, "hex"));
    size_t hex_len = hex != NULL ? strlen(hex) : 0;
    if (hex_len == 0 || hex_len % 2 != 0 || hex_len > 64) {
        set_why(why, why_cap, "i2c.write: hex must be an even-length string");
        json_free(*out);
        *out = NULL;
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t buf[32];
    for (size_t i = 0; i < hex_len / 2; i++) {
        unsigned value = 0;
        if (sscanf(hex + i * 2, "%2x", &value) != 1) {
            set_why(why, why_cap, "i2c.write: hex must be an even-length string");
            json_free(*out);
            *out = NULL;
            return ESP_ERR_INVALID_ARG;
        }
        buf[i] = (uint8_t)value;
    }
    esp_err_t err = sys_i2c_write(ctx->i2c_sda, ctx->i2c_scl, (int)addr, buf,
                                  hex_len / 2);
    if (err != ESP_OK) {
        set_why(why, why_cap, "i2c.write: the device did not acknowledge");
        return err;
    }
    add_int(root, "addr", addr);
    add_int(root, "written", (int64_t)(hex_len / 2));
    return ESP_OK;
}

/* --- Clock ---------------------------------------------------------------- */

static esp_err_t call_time_now(json_doc_t **out, char *why, size_t why_cap)
{
    if (!clock_synced()) {
        /* `tools/time.md`: a synced clock is required, because an unsynced one is a
         * number with no meaning. The refusal names the fix. */
        set_why(why, why_cap,
                "time.now: the clock is not synced; there is no real time to report");
        return ESP_ERR_INVALID_STATE;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    add_int(root, "unix", clock_now());
    /* ISO-8601 local time, which is what the doc's example shows; `strftime` reads the
     * TZ that `clock_init` installed. */
    time_t now = (time_t)clock_now();
    struct tm local;
    char iso[32] = "";
    if (localtime_r(&now, &local) != NULL) {
        strftime(iso, sizeof iso, "%Y-%m-%dT%H:%M:%S%z", &local);
    }
    json_add_string(root, "iso", iso);
    json_add_string(root, "tz", clock_tz());
    return ESP_OK;
}

/* --- Filesystem ----------------------------------------------------------- */

static esp_err_t call_fs(const char *tool, const json_node_t *args, json_doc_t **out,
                         char *why, size_t why_cap)
{
    const char *path = json_string(json_get(args, "path"));
    if (path == NULL) {
        set_why(why, why_cap, "fs: path is required");
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(tool, "fs.list") == 0) {
        /* Not a local: 64 entries is ~4.6 KiB, more than half the agent task's 8 KiB
         * stack, and listing `/workspace` overflowed it and panicked the board.
         * Allocated once into PSRAM rather than made static, because a static array
         * would move that cost into the 34 KiB DIRAM margin. Dispatch is
         * single-threaded (`02-firmware.md` §3.3). */
        static store_entry_t *entries;
        if (entries == NULL) {
            entries = heap_caps_malloc(TOOLS_LIST_MAX * sizeof *entries,
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (entries == NULL) entries = heap_caps_malloc(
                TOOLS_LIST_MAX * sizeof *entries, MALLOC_CAP_8BIT);
            if (entries == NULL) return ESP_ERR_NO_MEM;
        }
        size_t count = 0;
        esp_err_t err = store_list_dir(path, entries, TOOLS_LIST_MAX, &count);
        if (err != ESP_OK) {
            /* The listing reaches the read-only image mounts too (`/skills/`,
             * `/prompts/`, `/tools/`) as well as `/workspace/`, so the message names
             * the path rather than claiming a sandbox it is not confined to. */
            set_why(why, why_cap, "fs.list: no such directory");
            return err;
        }
        *out = json_new_object();
        if (*out == NULL) return ESP_ERR_NO_MEM;
        json_node_t *root = json_root(*out);
        json_add_string(root, "path", path);
        json_node_t *list = json_new_array(root, "entries");
        if (list == NULL) return ESP_ERR_NO_MEM;
        for (size_t i = 0; i < count; i++) {
            json_node_t *entry = json_array_push_object(list);
            if (entry == NULL) return ESP_ERR_NO_MEM;
            json_add_string(entry, "name", entries[i].name);
            json_add_bool(entry, "dir", entries[i].dir);
            add_int(entry, "size", (int64_t)entries[i].bytes);
        }
        return ESP_OK;
    }

    if (strcmp(tool, "fs.read") == 0) {
        /* OQ-6 (`06` §6.1): the model may not read the host config, which holds
         * provider base URLs and the shape of every secret reference. Both spellings
         * are named because `/fs/config.json` is the same file through the other
         * mount. */
        if (strcmp(path, "/config.json") == 0 || strcmp(path, "/fs/config.json") == 0 ||
            strcmp(path, "/fs/config.json/") == 0) {
            set_why(why, why_cap, "fs.read: config.json is not readable by a tool");
            return ESP_ERR_INVALID_STATE;
        }
        /* Over the cap is a reported truncation (`tools/filesystem.md`), which is why
         * `store_read_text` exists rather than `store_read_file`. */
        size_t len = 0, total = 0;
        bool truncated = false;
        esp_err_t err = store_read_text(path, s_read_buf, TOOLS_TEXT_MAX, &len, &total,
                                        &truncated);
        if (err != ESP_OK) {
            set_why(why, why_cap, "fs.read: no such readable text file");
            return err;
        }
        *out = json_new_object();
        if (*out == NULL) return ESP_ERR_NO_MEM;
        json_node_t *root = json_root(*out);
        json_add_string(root, "path", path);
        json_add_string(root, "content", s_read_buf);
        add_int(root, "bytes", (int64_t)total);
        json_add_bool(root, "truncated", truncated);
        return ESP_OK;
    }

    if (strcmp(tool, "fs.write") == 0) {
        const char *content = json_string(json_get(args, "content"));
        if (content == NULL) {
            set_why(why, why_cap, "fs.write: content is required");
            return ESP_ERR_INVALID_ARG;
        }
        /* One chunk, `final`: the same atomic path a bridge upload takes, so a
         * half-written file is never visible (`04` §4.16). */
        size_t written = 0;
        bool created = false;
        esp_err_t err = store_write_chunk(path, (const uint8_t *)content,
                                          strlen(content), 0, true, &written, &created);
        if (err != ESP_OK) {
            set_why(why, why_cap, "fs.write: refused by the workspace quota or sandbox");
            return err;
        }
        *out = json_new_object();
        if (*out == NULL) return ESP_ERR_NO_MEM;
        json_node_t *root = json_root(*out);
        json_add_string(root, "path", path);
        add_int(root, "bytes", (int64_t)written);
        json_add_bool(root, "created", created);
        return ESP_OK;
    }

    /* fs.delete */
    bool recursive = false;
    json_bool(json_get(args, "recursive"), &recursive);
    size_t entries = 0;
    esp_err_t err = store_delete_file(path, recursive, &entries);
    if (err != ESP_OK) {
        set_why(why, why_cap,
                "fs.delete: no such entry, or a populated directory without recursive");
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_string(root, "path", path);
    add_int(root, "entries", (int64_t)entries);
    return ESP_OK;
}

/* --- Network -------------------------------------------------------------- */

static esp_err_t call_http_get(const json_node_t *args, json_doc_t **out,
                               char *why, size_t why_cap)
{
    const char *url = json_string(json_get(args, "url"));
    const char *names[TOOLS_HEADER_COUNT];
    const char *values[TOOLS_HEADER_COUNT];
    size_t count = 0;

    /* Caller headers are copied, because the arguments document is freed with the
     * call and the client reads them during the exchange. */
    const json_node_t *headers = json_get(args, "headers");
    for (size_t i = 0; i < json_object_len(headers) && count < TOOLS_HEADER_COUNT; i++) {
        const char *name = json_object_key(headers, i);
        const char *value = json_string(json_object_value(headers, i));
        if (name == NULL || value == NULL) continue;
        snprintf(s_header_name[count], sizeof s_header_name[count], "%s", name);
        snprintf(s_header_value[count], sizeof s_header_value[count], "%s", value);
        names[count] = s_header_name[count];
        values[count] = s_header_value[count];
        count++;
    }

    net_response_t resp;
    esp_err_t err = net_get(url, names, values, count, TOOLS_HTTP_TIMEOUT_MS, &resp);
    if (err != ESP_OK) {
        set_why(why, why_cap, "http.get: the request failed");
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) {
        free(resp.body);
        return ESP_ERR_NO_MEM;
    }
    json_node_t *root = json_root(*out);
    add_int(root, "status", resp.status);
    json_add_string(root, "body", resp.body != NULL ? resp.body : "");
    add_int(root, "elapsed_ms", resp.elapsed_ms);
    free(resp.body);
    return ESP_OK;
}

/* --- Skills, tasks, events ------------------------------------------------ */

static esp_err_t call_skill_read(const json_node_t *args, json_doc_t **out,
                                 char *why, size_t why_cap)
{
    const char *name = json_string(json_get(args, "name"));
    size_t len = 0;
    esp_err_t err = skills_read_body(name, s_read_buf, TOOLS_RESULT_MAX, &len);
    if (err == ESP_ERR_INVALID_SIZE) {
        set_why(why, why_cap, "skill.read: the body exceeds the skill's max_body_bytes");
        return err;
    }
    if (err != ESP_OK) {
        set_why(why, why_cap, "skill.read: no such skill");
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_string(root, "name", name);
    json_add_string(root, "body", s_read_buf);
    add_int(root, "bytes", (int64_t)len);
    return ESP_OK;
}

/* --- Memory --------------------------------------------------------------- */

#define MEMORY_NAMESPACE "memory"

/* `tools/memory.md` §Limits: at most 32 keys in the namespace. */
#define MEMORY_MAX_KEYS 32

static esp_err_t call_memory(const char *tool, const tools_context_t *ctx,
                             const json_node_t *args, json_doc_t **out,
                             char *why, size_t why_cap)
{
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);

    if (strcmp(tool, "memory.list") == 0) {
        json_node_t *keys = json_new_array(root, "keys");
        if (keys == NULL) return ESP_ERR_NO_MEM;
        char name[65];
        for (size_t i = 0;; i++) {
            esp_err_t err = store_nvs_key_at(MEMORY_NAMESPACE, i, name, sizeof name);
            if (err == ESP_ERR_NOT_FOUND) break;
            if (err != ESP_OK) return err;
            if (json_array_push_string(keys, name) == NULL) return ESP_ERR_NO_MEM;
        }
        return ESP_OK;
    }

    const char *key = json_string(json_get(args, "key"));
    /* `memory.list` takes no key, so it is answered above. Deletes are checked too:
     * a reserved key is not this tool's to create, so it is not its to destroy. */
    if (!tools_memory_key_ok(key)) {
        set_why(why, why_cap, "memory: key must be 1-15 bytes of [A-Za-z0-9_]");
        json_free(*out);
        *out = NULL;
        return ESP_ERR_INVALID_ARG;
    }
    const char *reserved = tools_memory_reserved_prefix(key);
    if (reserved != NULL) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap,
                     "memory: the %s prefix is reserved for firmware state", reserved);
        }
        json_free(*out);
        *out = NULL;
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(tool, "memory.get") == 0) {
        size_t len = 0;
        esp_err_t err = store_nvs_get_str(MEMORY_NAMESPACE, key, s_read_buf,
                                          ctx->memory_max_bytes, &len);
        /* `nvs_get_str` reports an absent key as `ESP_ERR_NVS_NOT_FOUND`, which is
         * the storage layer's, not the wire's; `tools_call` maps it to `E_TOOL`. */
        if (err == ESP_ERR_NVS_NOT_FOUND || err == ESP_ERR_NOT_FOUND) {
            set_why(why, why_cap, "memory.get: no such key");
            json_free(*out);
            *out = NULL;
            return err;
        }
        if (err != ESP_OK) {
            set_why(why, why_cap, "memory.get: the value does not fit tools.memory_max_bytes");
            json_free(*out);
            *out = NULL;
            return err;
        }
        json_add_string(root, "key", key);
        json_add_string(root, "value", s_read_buf);
        return ESP_OK;
    }

    if (strcmp(tool, "memory.set") == 0) {
        const char *value = json_string(json_get(args, "value"));
        size_t len = strlen(value);
        if (len > ctx->memory_max_bytes) {
            set_why(why, why_cap, "memory.set: the value exceeds tools.memory_max_bytes");
            json_free(*out);
            *out = NULL;
            return ESP_ERR_INVALID_SIZE;
        }
        /* `tools/memory.md` §Limits: at most 32 keys. Counted only for a key that is
         * not already there, because overwriting never adds one. */
        char probe[16];
        bool exists = store_nvs_get_str(MEMORY_NAMESPACE, key, probe, sizeof probe,
                                        NULL) != ESP_ERR_NVS_NOT_FOUND;
        if (!exists) {
            size_t count = 0;
            char name[16];
            while (store_nvs_key_at(MEMORY_NAMESPACE, count, name, sizeof name) ==
                   ESP_OK) {
                count++;
            }
            if (count >= MEMORY_MAX_KEYS) {
                set_why(why, why_cap,
                        "memory.set: the namespace already holds 32 keys; delete one");
                json_free(*out);
                *out = NULL;
                return ESP_ERR_INVALID_SIZE;
            }
        }
        esp_err_t err = store_nvs_set_str(MEMORY_NAMESPACE, key, value, &len);
        if (err != ESP_OK) {
            set_why(why, why_cap, "memory.set: the write failed");
            json_free(*out);
            *out = NULL;
            return err;
        }
        json_add_string(root, "key", key);
        add_int(root, "bytes", (int64_t)len);
        /* `tools/memory.md` §`memory.set`: `stored` is always true on success, so the
         * model can confirm the write without inspecting `ok`. */
        json_add_bool(root, "stored", true);
        return ESP_OK;
    }

    /* memory.delete */
    esp_err_t err = store_nvs_erase(MEMORY_NAMESPACE, key);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        set_why(why, why_cap, "memory.delete: no such key");
        json_free(*out);
        *out = NULL;
        return err;
    }
    if (err != ESP_OK) {
        set_why(why, why_cap, "memory.delete: the erase failed");
        json_free(*out);
        *out = NULL;
        return err;
    }
    json_add_string(root, "key", key);
    return ESP_OK;
}

/* --- Diagnostics ---------------------------------------------------------- */

static esp_err_t call_sys_diagnostics(const json_node_t *args, json_doc_t **out,
                                      char *why, size_t why_cap)
{
    (void)why;
    (void)why_cap;
    const char *scope = json_string(json_get(args, "scope"));
    if (scope == NULL) scope = "quick";

    json_doc_t *doc = json_new_object();
    if (doc == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(doc);
    json_add_string(root, "scope", scope);

    bool all = strcmp(scope, "all") == 0;
    if (all || strcmp(scope, "quick") == 0 || strcmp(scope, "runtime") == 0) {
        json_node_t *runtime = json_add_object(root, "runtime");
        if (runtime == NULL) return ESP_ERR_NO_MEM;
        add_int(runtime, "uptime_s", (int64_t)(esp_timer_get_time() / 1000000));
        add_int(runtime, "tool_calls", (int64_t)s_call_count);
    }
    if (all || strcmp(scope, "quick") == 0 || strcmp(scope, "memory") == 0) {
        json_node_t *memory = json_add_object(root, "memory");
        if (memory == NULL) return ESP_ERR_NO_MEM;
        add_int(memory, "free_heap", esp_get_free_heap_size());
        add_int(memory, "min_free_heap", esp_get_minimum_free_heap_size());
    }
    if (all || strcmp(scope, "net") == 0) {
        net_status_t status;
        net_status(&status);
        json_node_t *json = json_add_object(root, "net");
        if (json == NULL) return ESP_ERR_NO_MEM;
        json_add_string(json, "state", net_state_name(status.state));
        json_add_string(json, "ssid", status.ssid != NULL ? status.ssid : "");
        add_int(json, "rssi", status.rssi);
    }
    *out = doc;
    return ESP_OK;
}

/* --- Dispatcher ----------------------------------------------------------- */

esp_err_t tools_call(const tools_context_t *ctx, const char *name,
                     const json_node_t *args, json_doc_t **out,
                     const char **wire_code, char *why, size_t why_cap)
{
    if (out == NULL || name == NULL) return ESP_ERR_INVALID_ARG;
    *out = NULL;
    if (wire_code != NULL) *wire_code = "E_TOOL";
    if (why != NULL && why_cap > 0) why[0] = '\0';
    if (ctx == NULL) return ESP_ERR_INVALID_ARG;

    esp_err_t err;
    if (strcmp(name, "gpio.read") == 0) {
        err = call_gpio_read(args, out, why, why_cap);
    } else if (strcmp(name, "gpio.write") == 0) {
        err = call_gpio_write(args, out, why, why_cap);
    } else if (strcmp(name, "adc.read") == 0) {
        err = call_adc_read(args, out, why, why_cap);
    } else if (strncmp(name, "i2c.", 4) == 0) {
        err = call_i2c(name, ctx, args, out, why, why_cap);
    } else if (strcmp(name, "time.now") == 0) {
        err = call_time_now(out, why, why_cap);
    } else if (strncmp(name, "fs.", 3) == 0) {
        err = call_fs(name, args, out, why, why_cap);
    } else if (strcmp(name, "http.get") == 0) {
        err = call_http_get(args, out, why, why_cap);
    } else if (strcmp(name, "skill.read") == 0) {
        err = call_skill_read(args, out, why, why_cap);
    } else if (strncmp(name, "memory.", 7) == 0) {
        err = call_memory(name, ctx, args, out, why, why_cap);
    } else if (strcmp(name, "sys.diagnostics") == 0) {
        err = call_sys_diagnostics(args, out, why, why_cap);
    } else if (strcmp(name, "todo") == 0) {
        /* Through the port, because the tree belongs to `agent` (L3) and calling up
         * would break `08-code-architecture.md` §1.1. */
        if (ctx->todo.apply == NULL) {
            set_why(why, why_cap, "todo is unavailable in this build");
            return ESP_ERR_NOT_SUPPORTED;
        }
        err = ctx->todo.apply(args, out, why, why_cap, ctx->todo.ctx);
    } else {
        /* The tools registered for later milestones are refused rather than
         * answered wrongly, and each says which milestone owes it: `task.*` and
         * `event.emit` wait on the scheduler (M5), `geo.locate` on the `geo` module
         * and `mqtt.*` on the `mqtt` module (`07-roadmap.md` §M6). One shared
         * message named the scheduler for all of them, which was true for two of
         * the three families and misdirected a reader for the rest. */
        if (strcmp(name, "geo.locate") == 0) {
            set_why(why, why_cap, "geo.locate is not implemented before M6");
        } else if (strncmp(name, "mqtt.", 5) == 0) {
            set_why(why, why_cap, "mqtt is not implemented before M6");
        } else {
            set_why(why, why_cap, "the scheduler is not implemented before M5");
        }
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_NOT_SUPPORTED;
    }

    if (err == ESP_OK) {
        s_call_count++;
        return ESP_OK;
    }
    s_failure_count++;
    if (out != NULL && *out != NULL) {
        json_free(*out);
        *out = NULL;
    }
    if (wire_code != NULL) {
        /* The table lives in `tools_spec.c` so it is host-tested; see
         * `tools_wire_code`. */
        const char *code = tools_wire_code(name, err);
        if (code != NULL) *wire_code = code;
    }
    return err;
}

/* --- Configuration -------------------------------------------------------- */

static int32_t cfg_int_or(const char *path, int32_t fallback)
{
    int64_t value = 0;
    json_node_t *node = cfg_get(path);
    return (node != NULL && json_int(node, &value)) ? (int32_t)value : fallback;
}

void tools_context_from_config(tools_context_t *ctx)
{
    if (ctx == NULL) return;
    memset(ctx, 0, sizeof *ctx);

    json_node_t *allow = cfg_get("tools.allow_dangerous");
    bool dangerous = false;
    if (allow != NULL) json_bool(allow, &dangerous);
    ctx->policy.allow_dangerous = dangerous;
    ctx->policy.http_allow_hosts = cfg_get("tools.http_allow_hosts");

    ctx->memory_max_bytes = (size_t)cfg_int_or("tools.memory_max_bytes", 2048);
    ctx->i2c_sda = cfg_int_or("tools.i2c_sda", -1);
    ctx->i2c_scl = cfg_int_or("tools.i2c_scl", -1);
    /* `todo` is `agent`'s tree, installed by whoever owns it (`tools_todo_port_t`). */
    ctx->todo.apply = NULL;
    ctx->todo.ctx = NULL;
}

void tools_counters(tool_counters_t *out)
{
    if (out == NULL) return;
    out->calls = s_call_count;
    out->failures = s_failure_count;
}

/* --- `tools_json` and the offered set ------------------------------------- */

bool tools_offered(const char *name, const tools_context_t *ctx)
{
    if (name == NULL) return false;
    /* `tools/i2c.md`: the three appear together or not at all, and only when the bus
     * pins are configured. Everything else is always offered. */
    if (strncmp(name, "i2c.", 4) == 0) {
        return ctx != NULL && ctx->i2c_sda >= 0 && ctx->i2c_scl >= 0;
    }
    return true;
}

/* One `{"type":"function","function":{"name","description","parameters"}}`. The
 * `parameters` text is embedded verbatim rather than rebuilt, because the document
 * order of a schema is what the provider sees and a rebuild would only re-copy it. */
static void push_function(json_node_t *out, const char *name, const char *desc,
                          const char *schema_text)
{
    json_node_t *entry = json_array_push_object(out);
    if (entry == NULL) return;
    json_add_string(entry, "type", "function");
    json_node_t *fn = json_add_object(entry, "function");
    if (fn == NULL) return;
    json_add_string(fn, "name", name);
    json_add_string(fn, "description", desc != NULL ? desc : "");
    /* A schema is valid JSON by construction (`tools_spec.c` holds literals), so it
     * parses; a failure is a firmware bug and leaves the tool without parameters
     * rather than emitting malformed JSON. */
    json_doc_t *doc = NULL;
    if (schema_text != NULL && json_parse(schema_text, strlen(schema_text), &doc) == ESP_OK) {
        json_add_node(fn, "parameters", json_root(doc));
        json_free(doc);
    } else {
        json_add_object(fn, "parameters");
    }
}

esp_err_t tools_json_build(const tools_context_t *ctx,
                           const tools_declared_set_t *declared,
                           json_node_t *out)
{
    if (out == NULL || !json_is_array(out)) return ESP_ERR_INVALID_ARG;

    size_t count = 0;
    const tools_builtin_t *builtins = tools_builtins(&count);
    for (size_t i = 0; i < count; i++) {
        if (!tools_offered(builtins[i].name, ctx)) continue;
        push_function(out, builtins[i].name, builtins[i].description, builtins[i].schema);
    }

    if (declared != NULL) {
        for (size_t i = 0; i < declared->count; i++) {
            if (!tools_offered(declared->items[i].name, ctx)) continue;
            push_function(out, declared->items[i].name, declared->items[i].description,
                          declared->items[i].schema);
        }
    }
    return ESP_OK;
}

esp_err_t tools_capability_names(const tools_context_t *ctx,
                                 const tools_declared_set_t *declared,
                                 json_node_t *out)
{
    if (out == NULL || !json_is_array(out)) return ESP_ERR_INVALID_ARG;

    size_t count = 0;
    const tools_builtin_t *builtins = tools_builtins(&count);
    for (size_t i = 0; i < count; i++) {
        if (!tools_offered(builtins[i].name, ctx)) continue;
        json_array_push_string(out, builtins[i].name);
    }
    if (declared != NULL) {
        for (size_t i = 0; i < declared->count; i++) {
            if (!tools_offered(declared->items[i].name, ctx)) continue;
            json_array_push_string(out, declared->items[i].name);
        }
    }
    return ESP_OK;
}

/* --- Declarative `/tools/` `.json` ------------------------------------------ */

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
        json_doc_t *doc = json_new_object();
        if (doc == NULL) return ESP_ERR_NO_MEM;
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
    const json_node_t *desc = json_get(root, "description");
    const json_node_t *url = json_get(root, "url");
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

    const json_node_t *method = json_get(root, "method");
    copy_field(out->method, sizeof out->method,
               json_is_string(method) ? method : NULL);
    if (out->method[0] == '\0') snprintf(out->method, sizeof out->method, "GET");
    if (strcmp(out->method, "GET") != 0 && strcmp(out->method, "POST") != 0) {
        err = ESP_ERR_INVALID_ARG;
        if (why) snprintf(why, why_cap, "method is not GET or POST");
        goto done;
    }

    const json_node_t *path = json_get(root, "response_path");
    if (json_is_string(path)) {
        copy_field(out->response_path, sizeof out->response_path, path);
        /* `tools/web.md` §Declarative tools: a bare key, dotted for nesting. Rejecting
         * the shapes `json_path_get` cannot walk keeps the drop at load time. */
        for (const char *p = out->response_path; *p != '\0'; p++) {
            if (*p == '[' || *p == ' ' || *p == '\0') {
                err = ESP_ERR_INVALID_ARG;
                if (why) snprintf(why, why_cap, "response_path is not dotted keys");
                goto done;
            }
        }
    }

    const json_node_t *query = json_get(root, "query");
    if (json_is_string(query)) copy_field(out->query, sizeof out->query, query);
    const json_node_t *body_tpl = json_get(root, "body");
    if (json_is_string(body_tpl)) copy_field(out->body, sizeof out->body, body_tpl);

    const json_node_t *timeout = json_get(root, "timeout_ms");
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

    err = compile_headers(json_get(root, "http.headers"), secret_resolves,
                          probe_ctx, out);
    if (err != ESP_OK && why) {
        snprintf(why, why_cap, err == ESP_ERR_NOT_FOUND
                                   ? "an nvs: header ref does not resolve"
                                   : "http.headers is not an object");
    }

done:
    json_free(doc);
    return err;
}

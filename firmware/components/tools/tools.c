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
#include "llm.h"
#include "sys.h"

/* `03-config.md` §tools: `/tools/` `.json` bodies and `tools_json` share this bound. */
#define TOOLS_TEXT_MAX 2048
#define TOOLS_HEADER_COUNT 4
/* `03-config.md` §tools `http_timeout_ms` default, used when the key is absent. */

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

/* --- Reachability (`04-protocol.md` §4.25) -------------------------------- */

esp_err_t tools_ping(const char *target, char *host, size_t host_cap,
                     net_ping_stats_t *stats, char *why, size_t why_cap)
{
    if (host == NULL || stats == NULL) return ESP_ERR_INVALID_ARG;
    if (target == NULL) {
        set_why(why, why_cap, "net.ping: target is required");
        return ESP_ERR_INVALID_ARG;
    }
    if (host_cap == 0) return ESP_ERR_INVALID_SIZE;

    bool router = strcmp(target, "router") == 0;
    bool provider = strcmp(target, "llm") == 0;
    bool ntp = strcmp(target, "ntp") == 0;
    if (!router && !provider && !ntp) {
        set_why(why, why_cap, "net.ping: unknown target");
        return ESP_ERR_INVALID_ARG;
    }

    if (!net_connected()) {
        set_why(why, why_cap, "net.ping: link is down");
        return ESP_ERR_INVALID_STATE;
    }

    if (router) {
        esp_err_t err = net_gateway(host, host_cap);
        if (err == ESP_ERR_INVALID_SIZE) return err;
        if (err != ESP_OK) {
            set_why(why, why_cap, "net.ping: no gateway");
            return ESP_ERR_NOT_FOUND;
        }
    } else if (provider) {
        if (!llm_active_host(host, host_cap)) {
            set_why(why, why_cap, "net.ping: no llm provider");
            return ESP_ERR_NOT_FOUND;
        }
    } else {
        const char *server = clock_server_name();
        if (server == NULL) {
            set_why(why, why_cap, "net.ping: no ntp server");
            return ESP_ERR_NOT_FOUND;
        }
        int n = snprintf(host, host_cap, "%s", server);
        if (n <= 0 || (size_t)n >= host_cap) {
            set_why(why, why_cap, "net.ping: the ntp server name does not fit");
            return ESP_ERR_INVALID_SIZE;
        }
    }

    int32_t rtt[NET_PING_COUNT];
    for (size_t i = 0; i < NET_PING_COUNT; i++) rtt[i] = -1;
    esp_err_t err = net_ping_icmp(host, rtt, NET_PING_COUNT);
    if (err != ESP_OK) {
        set_why(why, why_cap, "net.ping: the probe failed");
        return err;
    }
    net_ping_summarise(rtt, NET_PING_COUNT, stats);
    return ESP_OK;
}

static esp_err_t call_net_ping(const json_node_t *args, json_doc_t **out,
                               char *why, size_t why_cap)
{
    const char *target = json_string(json_get(args, "target"));
    char host[128] = "";
    net_ping_stats_t stats;
    esp_err_t err = tools_ping(target, host, sizeof host, &stats, why, why_cap);
    if (err != ESP_OK) return err;

    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_string(root, "target", target);
    json_add_string(root, "host", host);
    add_int(root, "sent", stats.sent);
    add_int(root, "received", stats.received);
    add_int(root, "loss_pct", stats.loss_pct);
    add_int(root, "rtt_min_ms", stats.rtt_min_ms);
    add_int(root, "rtt_avg_ms", stats.rtt_avg_ms);
    add_int(root, "rtt_max_ms", stats.rtt_max_ms);
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
    } else if (strcmp(name, "net.ping") == 0) {
        err = call_net_ping(args, out, why, why_cap);
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
         * answered wrongly. The wording lives in `tools_spec.c` so each family's
         * message is host-tested (`07-roadmap.md`: M5 scheduler, M6 geo/mqtt). */
        const char *reason = tools_unbuilt_reason(name);
        set_why(why, why_cap, reason != NULL ? reason : "unknown tool");
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

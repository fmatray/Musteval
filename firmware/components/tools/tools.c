/* tools.c — execute one validated call.
 *
 * Board-only: it touches GPIO, the ADC, the I2C bus, the filesystem, NVS and the
 * network. The names, schemas, validation and gating it sits behind are pure and
 * host-tested in `tools_spec.c` and `tools_gate.c`, so a refused call is a test case
 * and only an executed one needs a board. */

#include "tools.h"
#include "geo.h"
#include "mqtt.h"
#include "sched.h"

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
        static const char DIGITS[] = "0123456789abcdef";
        size_t at = 0;
        for (size_t i = 0; i < (size_t)len; i++) {
            hex[at++] = DIGITS[buf[i] >> 4];
            hex[at++] = DIGITS[buf[i] & 0x0f];
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
        int64_t offset_val = 0;
        const json_node_t *off_node = json_get(args, "offset");
        if (off_node && !json_int(off_node, &offset_val)) {
            set_why(why, why_cap, "fs.read: invalid offset");
            return ESP_ERR_INVALID_ARG;
        }
        size_t offset = (offset_val < 0) ? 0 : (size_t)offset_val;

        /* `limit` narrows the window inside the fixed 2048-byte page; it can never
         * widen it, so the result stays inside its render slot (`filesystem.md`). */
        size_t window = TOOLS_TEXT_MAX;
        int64_t limit_val = 0;
        const json_node_t *limit_node = json_get(args, "limit");
        if (limit_node) {
            if (!json_int(limit_node, &limit_val) || limit_val < 1) {
                set_why(why, why_cap, "fs.read: invalid limit");
                return ESP_ERR_INVALID_ARG;
            }
            if ((size_t)limit_val < window) window = (size_t)limit_val;
        }

        esp_err_t err = store_read_text(path, offset, s_read_buf, window, &len, &total,
                                        &truncated);
        if (err == ESP_ERR_INVALID_SIZE && offset > total) {
            /* `*total` is filled before the offset is validated, so this is exact
             * rather than a guess: the model passed an offset past the end, which it
             * can fix from `bytes`. Returning `INVALID_ARG` keeps it an `E_TOOL`
             * instead of the quota `E_STORAGE` this code otherwise means for `fs.*`. */
            set_why(why, why_cap, "fs.read: offset is past the end of the file");
            return ESP_ERR_INVALID_ARG;
        }
        if (err != ESP_OK) {
            set_why(why, why_cap, "fs.read: no such readable text file");
            return err;
        }
        /* `store_read_text` reports a length, it does not terminate; without this the
         * `content` string runs on into whatever the previous tool left in this
         * shared buffer. `window` is at most `TOOLS_TEXT_MAX`, so there is always room. */
        s_read_buf[len] = '\0';
        *out = json_new_object();
        if (*out == NULL) return ESP_ERR_NO_MEM;
        json_node_t *root = json_root(*out);
        json_add_string(root, "path", path);
        json_add_string(root, "content", s_read_buf);
        add_int(root, "offset", (int64_t)offset);
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
    /* `docs/tools/web.md`: the body is cut at `TOOLS_HTTP_BODY_MAX` before it can
     * crowd the 4096-byte render slot, and the cut is reported with the source's
     * real length so the model can tell a short body from a clipped one. */
    size_t body_len = resp.body != NULL ? strlen(resp.body) : 0;
    size_t kept = body_len > TOOLS_HTTP_BODY_MAX ? TOOLS_HTTP_BODY_MAX : body_len;
    if (kept < body_len) {
        /* Never cut a UTF-8 sequence in half: the result is re-encoded as JSON and
         * an invalid byte would poison the whole envelope. */
        while (kept > 0 && ((unsigned char)resp.body[kept] & 0xC0) == 0x80) kept--;
    }
    if (resp.body != NULL) {
        memcpy(s_read_buf, resp.body, kept);
        s_read_buf[kept] = '\0';
        json_add_string(root, "body", s_read_buf);
    } else {
        json_add_string(root, "body", "");
    }
    add_int(root, "bytes", (int64_t)body_len);
    json_add_bool(root, "truncated", kept < body_len);
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
        set_why(why, why_cap, "skill.read: the body is larger than a tool result can carry");
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
            /* `tools/memory.md` §`memory.list`: an object per key, never a bare
             * string, so the model can size a read before making it. */
            size_t len = 0;
            /* The buffer bound, not `memory_max_bytes`: a value written under a larger
             * config must still be listed and sized rather than dropped silently. */
            if (store_nvs_get_str(MEMORY_NAMESPACE, name, s_read_buf,
                                  TOOLS_RESULT_MAX, &len) != ESP_OK) {
                continue;   /* removed between the iteration and this read */
            }
            json_node_t *entry = json_array_push_object(keys);
            if (entry == NULL) return ESP_ERR_NO_MEM;
            json_add_string(entry, "key", name);
            add_int(entry, "bytes", (int64_t)len);
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
        /* `tools/memory.md` §`memory.delete`: an absent key is a success with
         * `deleted:false`. The caller's intent is satisfied, and a model that lost
         * track of the key set can clear one without a preceding `memory.list`. */
        json_add_string(root, "key", key);
        json_add_bool(root, "deleted", false);
        return ESP_OK;
    }
    if (err != ESP_OK) {
        set_why(why, why_cap, "memory.delete: the erase failed");
        json_free(*out);
        *out = NULL;
        return err;
    }
    json_add_string(root, "key", key);
    json_add_bool(root, "deleted", true);
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

/* --- Events (`tools/events.md`) ------------------------------------------ */

/* The two destinations are both reached here and both are outside this module: the
 * scheduler owns the match and the frames (`08-code-architecture.md` §1.1 makes
 * `tools` → `sched` one of the seven permitted edges), and `sched` answers with the
 * hit count the tool reports. A call with no hits is a success, not an error. */
/* `tools/tasks.md` §`task.list`: the stored task objects, read through `sched` so the
 * tool and `/tasks.json` cannot disagree. `sched_emit_json` writes the same document
 * the whole-file rewrite does, and the array is copied out under its lock. */
static esp_err_t call_task_list(json_doc_t **out, const char **wire_code, char *why,
                                size_t why_cap)
{
    json_doc_t *doc = NULL;
    esp_err_t err = sched_stored_json(&doc);
    if (err != ESP_OK) {
        set_why(why, why_cap, "task.list: the schedule is unavailable");
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return err;
    }
    const json_node_t *tasks = json_get(json_root(doc), "tasks");
    *out = json_new_object();
    if (*out == NULL) {
        json_free(doc);
        return ESP_ERR_NO_MEM;
    }
    json_node_t *root = json_root(*out);
    if (tasks != NULL) {
        json_add_node(root, "tasks", tasks);
    } else {
        json_new_array(root, "tasks");
    }
    json_free(doc);
    return ESP_OK;
}

static esp_err_t call_event_emit(const json_node_t *args, json_doc_t **out,
                                 const char **wire_code, char *why, size_t why_cap)
{
    const char *type = json_string(json_get(args, "type"));
    const char *data = json_string(json_get(args, "data"));
    if (!tools_event_args_ok(type, data, why, why_cap)) {
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_INVALID_ARG;
    }
    size_t hits = sched_trigger_event(type, data);
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_bool(root, "ok", true);
    add_int(root, "task_hits", (int64_t)hits);
    return ESP_OK;
}

/* `geo.locate` (`04-protocol.md` §4.13, `docs/tools/geo.md`). The cached fix is returned
 * while fresh; a lookup blocks for up to `geo.timeout_ms` per provider, which is what the
 * tool contract tells the model. */
/* A tool whose name is in the MCP namespace is the server's, not a built-in's: the
 * caller's arguments are forwarded as the model produced them, because the only
 * validator for them is the schema the server published, which is what the model was
 * shown. Re-validating here would be a second grammar that could disagree.
 *
 * `06-security.md` T9: a remote server's text reaches the model as a tool result, so it
 * is bounded by `MCP_BODY_MAX` upstream and by the result cap here. */
static esp_err_t call_mcp(const tools_context_t *ctx, const char *name,
                          const json_node_t *args,
                          json_doc_t **out, const char **wire_code, char *why,
                          size_t why_cap)
{
    char server[MCP_ID_MAX + 1];
    char tool[MCP_TOOL_NAME_MAX + 1];
    if (!mcp_tool_split(name, server, sizeof server, tool, sizeof tool)) {
        set_why(why, why_cap, "mcp: not a namespaced tool name");
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_INVALID_ARG;
    }
    /* Offered but not advertised: the model invented a name, or the server dropped the
     * tool since it was listed. Refused as the caller's mistake, not the socket's. */
    bool offered = false;
    if (ctx != NULL && ctx->mcp_tools != NULL) {
        for (size_t i = 0; i < ctx->mcp_count; i++) {
            if (strcmp(ctx->mcp_tools[i].name, name) == 0) {
                offered = true;
                break;
            }
        }
    }
    if (!offered) {
        set_why(why, why_cap, "mcp: that server did not advertise this tool");
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_NOT_FOUND;
    }
    /* A remote server's tool is third-party code driven by whatever the model was
     * told (`06-security.md` T9), so it takes the same gate as `gpio.write`. The
     * registry cannot carry this row because the name is only known at run time. */
    if (ctx == NULL || !ctx->policy.allow_dangerous) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "%s refused: tools.allow_dangerous is false", name);
        }
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_NOT_ALLOWED;
    }

    /* Heap, not a local: this frame sits on the agent's 8 KiB task stack, and the
     * request underneath it is a TLS handshake. `mcp_call` allocates its own bodies
     * for the same reason. */
    char *text = heap_caps_malloc(TOOLS_RESULT_MAX, MALLOC_CAP_8BIT);
    if (text == NULL) {
        set_why(why, why_cap, "mcp: out of memory");
        if (wire_code != NULL) *wire_code = MCP_WIRE_CODE;
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = mcp_call(server, tool, args, text, TOOLS_RESULT_MAX, why, why_cap);
    if (err != ESP_OK) {
        free(text);
        if (wire_code != NULL) *wire_code = MCP_WIRE_CODE;
        return err;
    }
    /* Text results become a string member rather than an object: a remote tool's reply
     * is prose, and inventing a structure for it would be a grammar the server never
     * agreed to. */
    *out = json_new_object();
    if (*out != NULL) json_add_string(json_root(*out), "content", text);
    free(text);
    return *out != NULL ? ESP_OK : ESP_ERR_NO_MEM;
}

static esp_err_t call_geo_locate(json_doc_t **out, const char **wire_code, char *why,
                                 size_t why_cap)
{
    if (!geo_enabled()) {
        /* `04` §4.13: a disabled geolocator is `E_CONFIG`, which tells the reader to
         * turn it on rather than that the chain failed. */
        set_why(why, why_cap, "geo.locate: geo.enabled is false");
        if (wire_code != NULL) *wire_code = "E_CONFIG";
        return ESP_ERR_INVALID_STATE;
    }
    geo_fix_t fix = {0};
    esp_err_t err = geo_locate(&fix);
    if (err != ESP_OK) {
        /* The chain's own reason, so the message names the provider and what it did
         * rather than repeating the tool name. `geo_last_error` is empty only when no
         * provider was tried at all, which `geo_init` has already refused. */
        const char *cause = geo_last_error();
        if (cause != NULL && cause[0] != '\0') {
            snprintf(why, why_cap, "geo.locate: %s", cause);
        } else {
            set_why(why, why_cap,
                    err == ESP_ERR_TIMEOUT ? "geo.locate: no provider answered"
                                           : "geo.locate: every provider failed");
        }
        /* `tools/geo.md` §Errors names both: the chain exhausted is `E_GEO`, the chain
         * that ran out of time is `E_TIMEOUT`. They are different readings — one says
         * every provider refused or failed, the other says none answered in time — and
         * the reader's next move differs (reconfigure versus retry later). */
        if (wire_code != NULL) *wire_code = err == ESP_ERR_TIMEOUT ? "E_TIMEOUT" : "E_GEO";
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_double(root, "lat", fix.lat);
    json_add_double(root, "lon", fix.lon);
    json_add_int(root, "accuracy_m", fix.accuracy_m);
    json_add_string(root, "provider", fix.provider);
    /* `docs/tools/geo.md` §Result: the tool reports when the fix was taken, which is
     * what the cache age is measured against. The command surface reports an age
     * instead (`04-protocol.md` §4.13), and the two shapes are deliberately apart. */
    json_add_int(root, "source_ts", fix.source_ts);
    /* The tool reports the same place and locale members as the command surface
     * (`04` §4.13), from the same single list. */
    geo_add_place(root, &fix);
    return ESP_OK;
}

/* `mqtt.list` (`docs/tools/mqtt.md`): every configured broker, its live state and the
 * filters it subscribed to. No secret material is in the result: ids and topic strings
 * are the only server-side values it carries (`06-security.md` §11, S11). */
static esp_err_t call_mqtt_list(json_doc_t **out, const char **wire_code, char *why,
                                size_t why_cap)
{
    mqtt_state_t states[MQTT_MAX_SERVERS];
    size_t n = mqtt_states(states, MQTT_MAX_SERVERS);
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_node_t *arr = json_new_array(root, "servers");
    if (arr == NULL) {
        json_free(*out);
        *out = NULL;
        return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < n; i++) {
        json_node_t *entry = json_array_push_object(arr);
        if (entry == NULL) {
            json_free(*out);
            *out = NULL;
            return ESP_ERR_NO_MEM;
        }
        json_add_string(entry, "id", states[i].id);
        json_add_bool(entry, "connected", states[i].connected);
        json_node_t *subs = json_new_array(entry, "subscriptions");
        for (size_t j = 0; subs != NULL && j < states[i].topic_count; j++) {
            json_array_push_string(subs, states[i].topics[j].filter);
        }
    }
    (void)wire_code;
    (void)why;
    (void)why_cap;
    return ESP_OK;
}

/* `task.set` (`docs/tools/tasks.md`): add or replace one task, never delete. The schema
 * and the write are `sched`'s — one owner for `/tasks.json` — so this only adds the one
 * rule the scheduler cannot know: the model's cap is `tools.task_max`, not the schema's
 * 16. An update does not consume a slot, so the cap is checked against a *new* id only. */
static esp_err_t call_task_set(const tools_context_t *ctx, const json_node_t *args,
                               json_doc_t **out, const char **wire_code, char *why,
                               size_t why_cap)
{
    const json_node_t *task_obj = args != NULL ? json_get(args, "task") : NULL;
    if (task_obj == NULL) task_obj = args;
    const char *id = json_string(json_get(task_obj, "id"));
    const sched_tasks_t *state = sched_state();
    if (state == NULL) {
        set_why(why, why_cap, "task.set: the schedule is unavailable");
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_INVALID_STATE;
    }
    bool known = false;
    for (size_t i = 0; id != NULL && i < state->count; i++) {
        if (strcmp(state->tasks[i].id, id) == 0) { known = true; break; }
    }
    if (!tools_task_set_fits(state->count, known, ctx->task_max)) {
        /* The reader has to edit `tools.task_max` or drop a task, so the message names
         * the cap rather than saying "too many". */
        set_why(why, why_cap, "task.set: tools.task_max is reached; update a task or raise it");
        if (wire_code != NULL) *wire_code = "E_TOOL";
        return ESP_ERR_INVALID_SIZE;
    }

    json_doc_t *written = NULL;
    esp_err_t err = sched_command("tasks.set", args, &written, why, why_cap);
    if (err != ESP_OK) {
        /* The schema and the 16-entry cap are the caller's fault, so `E_TOOL`; a
         * refused write is the storage layer's, so `E_STORAGE` (`tasks.md` §Errors). */
        bool refused = err == ESP_ERR_INVALID_ARG || err == ESP_ERR_INVALID_SIZE;
        if (wire_code != NULL) *wire_code = refused ? "E_TOOL" : "E_STORAGE";
        return err;
    }
    /* The command's reply carries the count and the created flag for the bridge; the
     * tool contract returns the id alone. */
    *out = json_new_object();
    if (*out == NULL) {
        json_free(written);
        return ESP_ERR_NO_MEM;
    }
    const char *written_id = json_string(json_get(json_root(written), "id"));
    json_add_string(json_root(*out), "id", written_id != NULL ? written_id : "");
    json_free(written);
    return ESP_OK;
}

/* `mqtt.publish` (`docs/tools/mqtt.md`). Every argument is validated here rather than in
 * `mqtt`, so the model gets a message naming the field it got wrong; the allowlist and
 * the connection state are `mqtt`'s to answer. */
static esp_err_t call_mqtt_publish(const json_node_t *args, json_doc_t **out,
                                   const char **wire_code, char *why, size_t why_cap)
{
    const char *server_id = json_string(json_get(args, "server_id"));
    const char *topic = json_string(json_get(args, "topic"));
    const char *payload = json_string(json_get(args, "payload"));
    if (server_id == NULL || topic == NULL || payload == NULL) {
        set_why(why, why_cap, "mqtt.publish: server_id, topic and payload are required strings");
        if (wire_code != NULL) *wire_code = "E_MQTT";
        return ESP_ERR_INVALID_ARG;
    }
    uint8_t qos = 0;
    json_node_t *q = json_get(args, "qos");
    if (q != NULL) {
        int64_t v = 0;
        if (!json_int(q, &v) || !mqtt_qos_ok(v)) {
            set_why(why, why_cap, "mqtt.publish: qos: must be 0 or 1");
            if (wire_code != NULL) *wire_code = "E_MQTT";
            return ESP_ERR_INVALID_ARG;
        }
        qos = (uint8_t)v;
    }
    size_t len = strlen(payload);
    esp_err_t err = mqtt_publish(server_id, topic, payload, len, qos);
    if (err != ESP_OK) {
        if (err == ESP_ERR_NOT_FOUND) {
            set_why(why, why_cap, "mqtt.publish: unknown server_id");
        } else if (err == ESP_ERR_INVALID_STATE) {
            set_why(why, why_cap, "mqtt.publish: that server is not connected");
        } else {
            /* The allowlist refusal names the reason, because the fix is a config edit:
             * the topic is not in `mqtt.servers[].topics[]`. */
            set_why(why, why_cap, "mqtt.publish: topic is not in the allowlist");
        }
        /* `docs/tools/mqtt.md`: every failure of this tool is `E_MQTT`, including a
         * malformed call, because its authorisation input is the configuration. */
        if (wire_code != NULL) *wire_code = "E_MQTT";
        return err;
    }
    *out = json_new_object();
    if (*out == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(*out);
    json_add_bool(root, "ok", true);
    json_add_int(root, "bytes", (int64_t)len);
    return ESP_OK;
}

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
    } else if (strcmp(name, "task.list") == 0) {
        err = call_task_list(out, wire_code, why, why_cap);
    } else if (strcmp(name, "event.emit") == 0) {
        err = call_event_emit(args, out, wire_code, why, why_cap);
    } else if (strcmp(name, "geo.locate") == 0) {
        err = call_geo_locate(out, wire_code, why, why_cap);
    } else if (strcmp(name, "mqtt.list") == 0) {
        err = call_mqtt_list(out, wire_code, why, why_cap);
    } else if (strcmp(name, "mqtt.publish") == 0) {
        err = call_mqtt_publish(args, out, wire_code, why, why_cap);
    } else if (strncmp(name, "mcp.", 4) == 0) {
        err = call_mcp(ctx, name, args, out, wire_code, why, why_cap);
    } else if (strcmp(name, "task.set") == 0) {
        /* The gate already refused an unauthorised caller with `E_TOOL`, because the
         * registry marks this tool `TOOLS_GATE_DANGEROUS`. */
        err = call_task_set(ctx, args, out, wire_code, why, why_cap);
    } else if (ctx->declared != NULL && tools_declared_find(ctx->declared, name) != NULL) {
        /* A declarative tool is a name the model was offered from a file, so it is
         * dispatched only if the set still carries it: a file deleted between the
         * offer and the call is `E_TOOL`, not a different tool's result. */
        err = tools_declared_call(tools_declared_find(ctx->declared, name), args,
                                  &ctx->policy, out, why, why_cap);
        if (err != ESP_OK && wire_code != NULL) *wire_code = "E_TOOL";
    } else if (strcmp(name, "todo") == 0) {
        /* Through the port, because the tree belongs to `agent` (L3) and calling up
         * would break `08-code-architecture.md` §1.1. */
        if (ctx->todo.apply == NULL) {
            set_why(why, why_cap, "todo is unavailable in this build");
            return ESP_ERR_NOT_SUPPORTED;
        }
        err = ctx->todo.apply(args, out, why, why_cap, ctx->todo.ctx);
    } else if (strcmp(name, "ask") == 0) {
        /* Blocking, so it goes through the port for the same layering reason `todo`
         * does and for one more: only `agent` knows the run id, the round and how to
         * end a wait that the bridge or a cancel interrupted (`tools/ask.md`).
         * The answer is one buffer, so the result object is built here. */
        if (ctx->ask.ask == NULL) {
            set_why(why, why_cap, "ask is unavailable in this build");
            return ESP_ERR_NOT_SUPPORTED;
        }
        const char *question = json_string(json_get(args, "question"));
        char answer[TOOLS_ASK_ANSWER_MAX + 1] = "";
        const char *code = "E_TOOL";
        err = ctx->ask.ask(question, json_get(args, "options"), answer, sizeof answer,
                           &code, why, why_cap, ctx->ask.ctx);
        if (err != ESP_OK) {
            if (wire_code != NULL) *wire_code = code;
            return err;
        }
        *out = json_new_object();
        if (*out == NULL) return ESP_ERR_NO_MEM;
        json_add_string(json_root(*out), "question", question != NULL ? question : "");
        json_add_string(json_root(*out), "answer", answer);
    } else {
        set_why(why, why_cap, "unknown tool");
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
        /* The rule lives in `tools_spec.c` so it is host-tested; see
         * `tools_wire_code_refine`. A handler that classified its own failure keeps
         * that code — the generic table only answers for the ones that left `E_TOOL`
         * standing. */
        const char *code = tools_wire_code_refine(name, err, *wire_code);
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

/* The compiled `/tools/` set, loaded once. 16 rows of fixed strings is about
 * 104 KiB, so it is heap — PSRAM first, internal as fallback — and shared by every
 * context rather than copied per round. */
static tools_declared_set_t *s_declared;
static bool s_declared_loaded;

const tools_declared_set_t *tools_declared_loaded(void)
{
    if (s_declared_loaded) return s_declared;
    s_declared_loaded = true;

    s_declared = heap_caps_malloc(sizeof *s_declared, MALLOC_CAP_SPIRAM |
                                                       MALLOC_CAP_8BIT);
    if (s_declared == NULL) s_declared = heap_caps_malloc(sizeof *s_declared,
                                                          MALLOC_CAP_8BIT);
    if (s_declared == NULL) return NULL;
    if (tools_declared_load(s_declared) != ESP_OK) {
        free(s_declared);
        s_declared = NULL;
    }
    return s_declared;
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
    ctx->policy.disabled = cfg_get("tools.disabled");

    ctx->memory_max_bytes = (size_t)cfg_int_or("tools.memory_max_bytes", 2048);
    ctx->i2c_sda = cfg_int_or("tools.i2c_sda", -1);
    ctx->i2c_scl = cfg_int_or("tools.i2c_scl", -1);
    ctx->task_max = (size_t)cfg_int_or("tools.task_max", SCHED_MAX_TASKS);
    /* Read here so `agent`'s round and `hello_ack`'s capability list cannot disagree
     * about which remote tools exist. */
    ctx->mcp_tools = mcp_discovered(&ctx->mcp_count);
    ctx->declared = tools_declared_loaded();
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

/* Boot to the point where the link is reachable (`02-firmware.md` §3.1). Steps 8-10
 * are not implemented yet; the boot guard they exist to protect already is, so a
 * device that stops here counts as a failed boot exactly as it will later. */

#include "sdkconfig.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_flash.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_psram.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_system.h"

#include "link.h"
#include "store.h"
#include "sys.h"

static const char *TAG = "musteval";

/* `01-hardware.md` §5: the R8 module reports 8 MiB of flash and 8 MiB of octal
 * PSRAM. A mismatch is a wrong-variant or wrong-sdkconfig build, not a warning to
 * ignore, because the partition table is sized for the larger part. */
#define BOARD_FLASH_BYTES (8u * 1024u * 1024u)
#define BOARD_PSRAM_BYTES (8u * 1024u * 1024u)

/* `04-protocol.md` §3.2: `boot_id` distinguishes one boot from the next. It is
 * generated once here and never changes until the next reset. */
static char s_boot_id[9];

/* Boot guard state, published for `status` (§3.9) because `answer_command` runs on
 * `link_task` and cannot see `app_main`'s locals. */
static bool s_safe_mode;
static int32_t s_boot_failures = 0;

static int64_t uptime_s(void)
{
    return esp_timer_get_time() / 1000000;
}

#define SANDBOX_READ_CAP 8192
#define SANDBOX_LIST_MAX STORE_WS_FILE_COUNT

static esp_err_t emit_failure_detail(const char *name, const char *reply_to,
                                     const char *code, const char *msg,
                                     const char *path, int64_t entries)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return ESP_ERR_NO_MEM;
    json_node_t *root = json_root(body);
    json_node_t *err = json_add_object(root, "error");
    json_add_string(root, "name", name ? name : "");
    json_add_bool(root, "ok", false);
    if (err) {
        json_add_string(err, "code", code);
        json_add_string(err, "message", msg);
        if (path != NULL) {
            json_node_t *detail = json_add_object(err, "detail");
            if (detail) {
                json_add_string(detail, "path", path);
                if (entries >= 0) json_add_int(detail, "entries", entries);
            }
        }
    }
    esp_err_t e = link_emit_body("command.result", link_next_id(), 0, reply_to, body);
    json_free(body);
    return e;
}

static esp_err_t emit_failure(const char *name, const char *reply_to, const char *code, const char *msg)
{
    return emit_failure_detail(name, reply_to, code, msg, NULL, -1);
}

static const char *wire_code(esp_err_t err)
{
    switch (err) {
    case ESP_ERR_INVALID_ARG:
    case ESP_ERR_INVALID_STATE: return "E_CONFIG";
    case ESP_ERR_INVALID_SIZE:
    case ESP_ERR_NO_MEM:
    case ESP_ERR_NOT_FOUND:     return "E_STORAGE";
    default:                    return "E_STORAGE";
    }
}

static esp_err_t handle_sandbox(const char *name, const json_node_t *args, const char *reply_to, json_doc_t *body)
{
    json_node_t *root = json_root(body);
    if (strcmp(name, "sandbox.list") == 0) {
        const char *path = json_string(json_get(args, "path"));
        if (path == NULL) path = STORE_ROOT_WORKSPACE;
        static store_entry_t s_entries[SANDBOX_LIST_MAX];
        size_t n = 0;
        esp_err_t err = store_list_dir(path, s_entries, SANDBOX_LIST_MAX, &n);
        if (err != ESP_OK) {
            emit_failure("sandbox.list", reply_to, wire_code(err), "Listing failed");
            return err;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sandbox.list");
        json_add_bool(root, "ok", true);
        if (result) {
            static char s_shown[STORE_PATH_MAX];
            snprintf(s_shown, sizeof s_shown, "%s%s", path, (path[0] && path[strlen(path)-1] == '/') ? "" : "/");
            json_add_string(result, "path", s_shown);
            json_node_t *arr = json_new_array(result, "entries");
            for (size_t i = 0; i < n && arr; i++) {
                json_node_t *o = json_array_push_object(arr);
                if (!o) break;
                json_add_string(o, "name", s_entries[i].name);
                json_add_bool(o, "dir", s_entries[i].dir);
                json_add_int(o, "bytes", (int64_t)s_entries[i].bytes);
            }
        }
        return ESP_OK;
    } else if (strcmp(name, "sandbox.read") == 0) {
        const char *path = json_string(json_get(args, "path"));
        if (path == NULL) { emit_failure("sandbox.read", reply_to, "E_PROTOCOL", "Missing path"); return ESP_ERR_INVALID_ARG; }
        /* Read not implemented until M2 */
        emit_failure("sandbox.read", reply_to, "E_INTERNAL", "Read not implemented");
        return ESP_ERR_NOT_SUPPORTED;
    } else if (strcmp(name, "sandbox.write") == 0) {
        const char *path = json_string(json_get(args, "path"));
        const char *data = json_string(json_get(args, "data"));
        if (path == NULL || data == NULL) { emit_failure("sandbox.write", reply_to, "E_PROTOCOL", "Missing arguments"); return ESP_ERR_INVALID_ARG; }
        const char *enc = json_string(json_get(args, "encoding"));
        if (enc == NULL) enc = "utf8";
        int64_t offset = 0;
        const json_node_t *off_node = json_get(args, "offset");
        if (off_node) {
            if (!json_int(off_node, &offset)) { emit_failure("sandbox.write", reply_to, "E_CONFIG", "Invalid offset"); return ESP_ERR_INVALID_ARG; }
        }
        if (offset < 0) { emit_failure("sandbox.write", reply_to, "E_CONFIG", "Negative offset"); return ESP_ERR_INVALID_ARG; }
        bool final = false;
        const json_node_t *fin_node = json_get(args, "final");
        if (fin_node) {
            if (!json_bool(fin_node, &final)) { emit_failure("sandbox.write", reply_to, "E_CONFIG", "Invalid final"); return ESP_ERR_INVALID_ARG; }
        }
        static uint8_t s_chunk[STORE_CHUNK_BYTES];
        size_t raw = 0;
        esp_err_t derr = store_decode_chunk(data, enc, s_chunk, sizeof s_chunk, &raw);
        if (derr != ESP_OK) {
            const char *code = (enc != NULL && strcmp(enc, "utf8") != 0 && strcmp(enc, "base64") != 0) ? "E_CONFIG" : "E_PROTOCOL";
            emit_failure("sandbox.write", reply_to, code, "Chunk decode failed");
            return derr;
        }
        size_t out_bytes = 0; bool out_created = false;
        esp_err_t err = store_write_chunk(path, s_chunk, raw, (size_t)offset, final, &out_bytes, &out_created);
        if (err != ESP_OK) {
            const char *code = (err == ESP_ERR_INVALID_STATE) ? "E_PROTOCOL" : wire_code(err);
            emit_failure("sandbox.write", reply_to, code, "Write failed");
            return err;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sandbox.write");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_string(result, "path", path);
            json_add_int(result, "bytes", (int64_t)out_bytes);
            json_add_bool(result, "created", out_created);
        }
        return ESP_OK;
    } else if (strcmp(name, "sandbox.mkdir") == 0) {
        const char *path = json_string(json_get(args, "path"));
        if (path == NULL) { emit_failure("sandbox.mkdir", reply_to, "E_PROTOCOL", "Missing path"); return ESP_ERR_INVALID_ARG; }
        bool created = false;
        esp_err_t err = store_mkdir(path, &created);
        if (err != ESP_OK) {
            if (err == ESP_ERR_INVALID_STATE) {
                emit_failure_detail("sandbox.mkdir", reply_to, "E_CONFIG", "Parent directory missing or path is a file", path, -1);
            } else {
                emit_failure("sandbox.mkdir", reply_to, wire_code(err), "Mkdir failed");
            }
            return err;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sandbox.mkdir");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_string(result, "path", path);
            json_add_bool(result, "created", created);
        }
        return ESP_OK;
    } else if (strcmp(name, "sandbox.move") == 0) {
        const char *from = json_string(json_get(args, "from"));
        const char *to = json_string(json_get(args, "to"));
        if (from == NULL || to == NULL) { emit_failure("sandbox.move", reply_to, "E_PROTOCOL", "Missing arguments"); return ESP_ERR_INVALID_ARG; }
        esp_err_t err = store_move(from, to);
        if (err != ESP_OK) {
            if (err == ESP_ERR_INVALID_STATE) {
                emit_failure_detail("sandbox.move", reply_to, "E_CONFIG", "Destination exists or move is invalid", from, -1);
            } else {
                emit_failure("sandbox.move", reply_to, wire_code(err), "Move failed");
            }
            return err;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sandbox.move");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_string(result, "from", from);
            json_add_string(result, "to", to);
            json_add_bool(result, "moved", true);
        }
        return ESP_OK;
    } else if (strcmp(name, "sandbox.delete") == 0) {
        const char *path = json_string(json_get(args, "path"));
        if (path == NULL) { emit_failure("sandbox.delete", reply_to, "E_PROTOCOL", "Missing path"); return ESP_ERR_INVALID_ARG; }
        bool recursive = false;
        const json_node_t *rec = json_get(args, "recursive");
        if (rec && !json_bool(rec, &recursive)) { emit_failure("sandbox.delete", reply_to, "E_CONFIG", "Invalid recursive"); return ESP_ERR_INVALID_ARG; }
        size_t entries = 0;
        esp_err_t err = store_delete_file(path, recursive, &entries);
        if (err != ESP_OK) {
            if (err == ESP_ERR_INVALID_STATE) {
                emit_failure_detail("sandbox.delete", reply_to, "E_CONFIG", "Directory not empty; pass recursive:true", path, (int64_t)entries);
            } else {
                emit_failure("sandbox.delete", reply_to, wire_code(err), "Delete failed");
            }
            return err;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sandbox.delete");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_string(result, "path", path);
            json_add_bool(result, "deleted", true);
            json_add_int(result, "entries", (int64_t)entries);
        }
        return ESP_OK;
    }
    return ESP_ERR_INVALID_ARG;
}

static void answer_command(const json_node_t *frame)
{
    const json_node_t *args = json_get(frame, "args");
    const char *reply_to = json_string(json_get(frame, "id"));
    if (args == NULL || reply_to == NULL) return;

    const char *name = json_string(json_get(frame, "name"));
    if (name == NULL) return;

    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);

    esp_err_t built = ESP_FAIL;
    if (name && strcmp(name, "status") == 0) {
        size_t ufs_used = 0, ufs_total = 0, wfs_used = 0, wfs_total = 0;
        if (store_fs_usage(STORE_FS_USERFS, &ufs_used, &ufs_total) != ESP_OK ||
            store_fs_usage(STORE_FS_WSFS, &wfs_used, &wfs_total) != ESP_OK) {
            emit_failure("status", reply_to, "E_STORAGE", "Usage failed");
            json_free(body); return;
        }
        link_status_t st = {
            .safe_mode = s_safe_mode,
            .boot_failures = s_boot_failures,
            .uptime_s = uptime_s(),
            .boot_id = s_boot_id,
            .userfs_used = (int64_t)ufs_used,
            .userfs_total = (int64_t)ufs_total,
            .wsfs_used = (int64_t)wfs_used,
            .wsfs_total = (int64_t)wfs_total,
            .heap_free = (int64_t)esp_get_free_heap_size(),
            .psram_free = 0,
        };
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "status");
        json_add_bool(root, "ok", true);
        if (result == NULL || link_build_status(result, &st) != ESP_OK) {
            emit_failure("status", reply_to, "E_STORAGE", "Status build failed");
            json_free(body); return;
        }
        built = ESP_OK;
    } else if (name && strcmp(name, "sys.reset_counter") == 0) {
        int32_t prev = 0;
        if (store_nvs_get_int("boot_failures", &prev) != ESP_OK) prev = 0;
        esp_err_t nerr = store_nvs_set_int("boot_failures", 0);
        if (nerr != ESP_OK) {
            emit_failure("sys.reset_counter", reply_to, "E_INTERNAL", "Counter not persisted");
            json_free(body); return;
        }
        s_boot_failures = 0;
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sys.reset_counter");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_string(result, "reset", "boot_failures");
            json_add_int(result, "previous", (int64_t)prev);
            json_add_bool(result, "safe_mode", s_safe_mode);
        }
        built = ESP_OK;
    } else if (name && strncmp(name, "sandbox.", 8) == 0) {
        built = handle_sandbox(name, args, reply_to, body);
        if (built != ESP_OK) {
            json_free(body); return;
        }
    } else {
        emit_failure(name, reply_to, "E_PROTOCOL", "unknown or unwired command");
        json_free(body); return;
    }

    if (built == ESP_OK) {
        esp_err_t e = link_emit_body("command.result", link_next_id(), 0, reply_to, body);
        if (e == ESP_ERR_INVALID_SIZE) {
            emit_failure(name, reply_to, "E_STORAGE", "Frame too large");
        }
    }
    json_free(body);
}

static void on_link_frame(const json_node_t *frame)
{
    const char *type = json_string(json_get(frame, "type"));
    if (type == NULL) return;

    if (strcmp(type, "hello") == 0) {
        const char *reply_to = json_string(json_get(frame, "id"));
        link_hello_t hello = {
            .device = "musteval",
            .fw_version = "1.0.0",
            .boot_id = s_boot_id,
            .time_synced = false,
        };
        json_doc_t *body = json_new_object();
        if (body == NULL) return;
        if (link_build_hello_ack(json_root(body), &hello) == ESP_OK) {
            link_emit_body("hello_ack", link_next_id(), 0, reply_to, body);
        } else {
            ESP_LOGE(TAG, "hello: body build failed");
        }
        json_free(body);
        return;
    }

    if (strcmp(type, "command") == 0) {
        answer_command(frame);
        return;
    }

    if (strcmp(type, "prompt") == 0) {
        const char *reply_to = json_string(json_get(frame, "id"));
        json_doc_t *body = json_new_object();
        if (body == NULL) return;
        json_node_t *root = json_root(body);
        json_add_string(root, "code", "E_INTERNAL");
        json_add_string(root, "message", "agent loop not implemented before M4");
        json_add_bool(root, "retryable", true);
        link_emit_body("agent.error", link_next_id(), 0, reply_to, body);
        json_free(body);
        return;
    }

    ESP_LOGW(TAG, "inbound frame of type %s has no handler", type);
}

static link_sink_t s_link_sink = { .on_frame = on_link_frame };

void app_main(void)
{
    ESP_LOGI(TAG, "esp-idf %s, target %s", esp_get_idf_version(), CONFIG_IDF_TARGET);

    snprintf(s_boot_id, sizeof(s_boot_id), "%08" PRIx32,
             (uint32_t)esp_random());

    size_t psram_size = esp_psram_get_size();
    if (psram_size != BOARD_PSRAM_BYTES) {
        ESP_LOGE(TAG, "psram: %u bytes, expected %u: check CONFIG_SPIRAM_MODE_OCT "
                      "and that this is the R8 variant",
                 (unsigned)psram_size, (unsigned)BOARD_PSRAM_BYTES);
    } else {
        ESP_LOGI(TAG, "psram: %u bytes", (unsigned)psram_size);
    }

    uint32_t flash_size = 0;
    esp_err_t err = esp_flash_get_size(NULL, &flash_size);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "flash: size query failed: %s", esp_err_to_name(err));
    } else if (flash_size != BOARD_FLASH_BYTES) {
        ESP_LOGE(TAG, "flash: %" PRIu32 " bytes, expected %u: the partition table "
                      "assumes 8 MiB",
                 flash_size, (unsigned)BOARD_FLASH_BYTES);
    } else {
        ESP_LOGI(TAG, "flash: %" PRIu32 " bytes", flash_size);
    }

    err = sys_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "sys: init failed: %s", esp_err_to_name(err));
    }

    err = store_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "store: nvs init failed: %s", esp_err_to_name(err));
    }
    int32_t boot_failures = 0;
    if (store_nvs_get_int("boot_failures", &boot_failures) != ESP_OK) {
        boot_failures = 0;
    }
    int32_t count = boot_guard_next_count(boot_failures);
    err = store_nvs_set_int("boot_failures", count);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "boot guard: counter not persisted: %s", esp_err_to_name(err));
    }

    bool safe_mode = boot_guard_should_enter_safe_mode(
        count, SYS_SAFE_MODE_AFTER_DEFAULT);
    s_safe_mode = safe_mode;
    s_boot_failures = count;
    ESP_LOGI(TAG, "boot: %s, failures %" PRId32 "",
             safe_mode ? "safe mode" : "normal", count);

    err = store_mount();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "store: mount failed: %s: halting (E_STORAGE)",
                 esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "store: /fs and /workspace mounted");

    link_set_sink(&s_link_sink);
    err = link_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "link: start failed: %s: the device is unreachable",
                 esp_err_to_name(err));
        return;
    }
    link_log_install("info");

    ESP_LOGW(TAG, "boot stops before the scheduler: counter stays at %" PRId32,
             count);
}

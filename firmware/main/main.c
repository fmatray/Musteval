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

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdlib.h>

#include "cfg.h"
#include "clock.h"
#include "llm.h"
#include "net.h"
#include "agent.h"
#include "link.h"
#include "store.h"
#include "sys.h"
#include "tools.h"

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
/* `02-firmware.md` §3.1 steps 4-8, shared by boot and `reload`. Each step logs and
 * continues: a missing radio or an unreachable provider must not cost the operator
 * the link, because the link is how they fix the config. Safe mode skips the radio
 * entirely (§3.2), which is why the clock is left unsynced. `reload` runs this with
 * `false` after clearing the flag, which is what makes it the way out (§3.2). */
static void start_services(bool safe_mode)
{
    if (safe_mode) {
        ESP_LOGW(TAG, "boot: safe mode: skipping network, clock and model");
        return;
    }

    esp_err_t err = net_init();
    if (err == ESP_OK) {
        err = net_connect();
    }
    if (err != ESP_OK) {
        /* `02-firmware.md` §4.2 names the failure `E_WIFI`, and the log is the only
         * place an operator sees it: `ESP_ERR_INVALID_STATE` would read as a
         * programming error rather than "no network". */
        ESP_LOGW(TAG, "boot: wifi: %s (E_WIFI)",
                 err == ESP_ERR_INVALID_STATE ? "no configured network joined"
                                              : esp_err_to_name(err));
    }

    err = clock_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_ARG) {
        ESP_LOGW(TAG, "boot: ntp: %s", esp_err_to_name(err));
    }
    if (net_connected() && clock_sync() != ESP_OK) {
        ESP_LOGW(TAG, "boot: ntp: %s", esp_err_to_name(ESP_ERR_INVALID_STATE));
    }

    err = llm_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "boot: llm: %s: provider commands answer E_LLM_UNAVAILABLE",
                 esp_err_to_name(err));
    }

    /* After `link_start`, so a run's first frame has a writer (`02` §3.1). */
    err = agent_start();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "boot: agent: %s", esp_err_to_name(err));
    }
}

static bool s_safe_mode;
static int32_t s_boot_failures = 0;

static int64_t uptime_s(void)
{
    return esp_timer_get_time() / 1000000;
}

#define SANDBOX_READ_CAP 8192
#define SANDBOX_LIST_MAX STORE_WS_FILE_COUNT

/* `04` §2: an id is allocated per frame from the device's one counter, so a frame
 * emitted here cannot collide with one the link or agent task emits. Dispatch is
 * single-threaded (§4.16) and `link_next_id` copies under the emit lock, so a
 * per-call buffer is enough. */
static const char *frame_id(void)
{
    static char s_id[16];
    link_next_id(s_id, sizeof s_id);
    return s_id;
}

/* `04` §3.20 requires `duration_ms` on every `command.result`. It is stamped from
 * here, one place, because a handler is not the thing that knows when the frame
 * arrived. Dispatch is single-threaded (§4.16), so one file-scope start is exact. */
static int64_t s_cmd_start_us;
static int64_t cmd_duration_ms(void)
{
    if (s_cmd_start_us == 0) return 0;
    return (esp_timer_get_time() - s_cmd_start_us) / 1000;
}

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
    json_add_int(root, "duration_ms", cmd_duration_ms());
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
    esp_err_t e = link_emit_body("command.result", frame_id(), 0, reply_to, body);
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
    /* `04` §4.17-§4.20 confine every `sandbox.*` path to `/workspace/`; `store` no
     * longer does, because `fs.list`/`fs.read` may reach `/fs` (`06` §6.1). */
    static char s_sandbox_path[STORE_PATH_MAX];
    const char *given = json_string(json_get(args, "path"));
    bool listing = strcmp(name, "sandbox.list") == 0;
    if (given == NULL && listing) given = STORE_ROOT_WORKSPACE;
    if (given != NULL) {
        /* A listing admits `/workspace` itself, which is its documented default
         * (`04` §4.17); every other command names a file target, which the root is
         * not. */
        esp_err_t perr = listing
            ? store_resolve_sandbox_path(given, s_sandbox_path, sizeof s_sandbox_path)
            : store_resolve_write_path(given, s_sandbox_path, sizeof s_sandbox_path);
        if (perr != ESP_OK) {
            emit_failure(name, reply_to, "E_CONFIG",
                         "path does not resolve under /workspace/");
            return perr;
        }
    }
    if (strcmp(name, "sandbox.list") == 0) {
        const char *path = s_sandbox_path;
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
            /* One byte of slack for the separator the path may need. */
            static char s_shown[STORE_PATH_MAX + 1];
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
        /* `04` §4.18: the buffer is static because `link_task` has 4 KiB of stack
         * and 8193 bytes of local would not fit. Dispatch is single-threaded, so
         * one buffer is correct; a mutex would be dead weight. The cap matches the
         * document exactly: a larger read would not fit the 16384-byte TX limit. */
        static char s_text[SANDBOX_READ_CAP + 1];
        size_t len = 0, total = 0;
        bool truncated = false;
        esp_err_t rerr = store_read_text(s_sandbox_path, 0, s_text, SANDBOX_READ_CAP,
                                         &len, &total, &truncated);
        if (rerr != ESP_OK) {
            emit_failure("sandbox.read", reply_to, wire_code(rerr), "Read failed");
            return rerr;
        }
        s_text[len] = '\0';
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "sandbox.read");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_string(result, "path", path);
            json_add_string(result, "text", s_text);
            json_add_int(result, "bytes", (int64_t)total);
            json_add_bool(result, "truncated", truncated);
        }
        return ESP_OK;
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
        /* §4.19's table lists `E_BUSY`: a recursive delete while a run is live would
         * pull the workspace out from under the tool calls reading it, so the
         * destructive form waits for idle. A single-file delete is allowed. */
        if (recursive) {
            agent_status_t agent;
            agent_status(&agent);
            if (agent.busy) {
                emit_failure("sandbox.delete", reply_to, "E_BUSY",
                             "recursive delete refused while a run is in flight");
                return ESP_ERR_INVALID_STATE;
            }
        }
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
    /* `04` §4: an unknown `sandbox.*` name is `E_PROTOCOL`, the same as any other
     * unknown command. Returning an error without emitting leaves the bridge
     * waiting for a frame that never comes, which it reports as a dead link. */
    emit_failure(name, reply_to, "E_PROTOCOL", "unknown or unwired command");
    return ESP_ERR_INVALID_ARG;
}

/* The canonical text of the loaded document, and the document it was parsed from.
 * Both static: `link_task` has 4096 bytes of stack and a whole config does not fit
 * on it (`02-firmware.md` §3.1). */
static char s_cfg_out[CFG_JSON_MAX + 1];


/* --- Templates (`04` §4.11, §4.12) ---------------------------------------- */

static esp_err_t handle_templates_list(const json_node_t *args, json_node_t *root,
                                       json_node_t *result)
{
    (void)args;
    static store_entry_t s_entries[SANDBOX_LIST_MAX];
    size_t n = 0;
    esp_err_t err = store_list_image_dir("/prompts", s_entries, SANDBOX_LIST_MAX, &n);
    if (err == ESP_ERR_NOT_FOUND) {
        /* No `/prompts` at all is an empty list, not a failure: a fresh image need
         * not ship one (`04` §4.11). */
        json_add_string(root, "name", "templates.list");
        json_add_bool(root, "ok", true);
        json_new_array(result, "templates");
        return ESP_OK;
    }
    if (err != ESP_OK) return err;

    json_add_string(root, "name", "templates.list");
    json_add_bool(root, "ok", true);
    json_node_t *arr = json_new_array(result, "templates");
    for (size_t i = 0; i < n && arr; i++) {
        if (s_entries[i].dir) continue;
        /* `system.md` is the agent's own prompt, not a template (§4.11). */
        if (strcmp(s_entries[i].name, "system.md") == 0) continue;
        if (!store_template_name_ok(s_entries[i].name)) continue;
        json_array_push_string(arr, s_entries[i].name);
    }
    return ESP_OK;
}

/* `04` §4.26: `wifi.connect` walks every configured AP with a
 * `wifi.connect_timeout_ms` wait and a backoff between, so it can hold the link task
 * for tens of seconds — `/wifi connect` after a `/wifi disconnect` is exactly the
 * case where it will. It gets the `net.ping` treatment: its own task, its own busy
 * flag, and a reply written from there. */
typedef struct {
    char reply_to[LINK_ID_MAX + 1];
    /* The name that was asked, not a constant: `reconnect` runs the same task as
     * `connect`, and a reply that renamed itself left the bridge matching a command
     * it never sent — the probe found `wifi.reconnect` answering as `wifi.connect`. */
    char name[16];
    int64_t start_us;
} wifi_args_t;

static volatile bool s_wifi_busy;

/* The §3.9 object, which every `wifi.*` result and `status` share, so the three
 * cannot disagree about what the radio is doing. */
static void wifi_add_state(json_node_t *result)
{
    net_status_t st;
    net_status(&st);
    json_add_string(result, "state", net_state_name(st.state));
    if (st.ssid != NULL) json_add_string(result, "ssid", st.ssid);
    else json_add_null(result, "ssid");
    if (st.ssid != NULL) json_add_int(result, "rssi", st.rssi);
    else json_add_null(result, "rssi");
    if (st.ip != NULL) json_add_string(result, "ip", st.ip);
    else json_add_null(result, "ip");
    json_add_int(result, "failovers", st.failovers);
}

static void wifi_emit(const wifi_args_t *wa, esp_err_t err)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    char id[16];
    link_next_id(id, sizeof id);
    json_add_string(root, "name", wa->name);
    json_add_bool(root, "ok", err == ESP_OK);
    json_add_int(root, "duration_ms", (esp_timer_get_time() - wa->start_us) / 1000);
    json_node_t *result = json_add_object(root, "result");
    if (result != NULL) wifi_add_state(result);
    if (err != ESP_OK) {
        /* §4.26: a join that failed is `E_WIFI`, which is the same code the boot
         * sequence reports for the same event (`02-firmware.md` §4.2). */
        json_node_t *error = json_add_object(root, "error");
        if (error != NULL) {
            json_add_string(error, "code", "E_WIFI");
            json_add_string(error, "message", "no configured network joined");
        }
    }
    link_emit_body("command.result", id, 0, wa->reply_to, body);
    json_free(body);
}

static bool agent_busy(void)
{
    agent_status_t agent;
    agent_status(&agent);
    return agent.busy;
}

static void wifi_connect_task(void *arg)
{
    wifi_args_t *wa = arg;
    esp_err_t err = net_connect();
    wifi_emit(wa, err);
    s_wifi_busy = false;
    free(wa);
    vTaskDelete(NULL);
}

/* `04` §4.25: four ICMP probes on `link_task` would hold `pong` for up to 2.8 s,
 * which §3.7 bounds at 1 s. The task owns every byte it needs: the caller's frame is
 * freed when `answer_command` returns. */
typedef struct {
    char reply_to[LINK_ID_MAX + 1];
    char target[8];
    char host[128];
    int64_t start_us;
} ping_args_t;

static volatile bool s_ping_busy;

static void ping_emit(const ping_args_t *pa, const net_ping_stats_t *stats,
                      const char *code, const char *msg)
{
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);
    /* `d-<counter>` is allocated per frame; `frame_id`'s buffer is shared with
     * `link_task`, which may be answering a `pong` right now. */
    char id[16];
    link_next_id(id, sizeof id);
    json_add_string(root, "name", "net.ping");
    json_add_bool(root, "ok", stats != NULL);
    json_add_int(root, "duration_ms", (esp_timer_get_time() - pa->start_us) / 1000);
    if (stats != NULL) {
        json_node_t *result = json_add_object(root, "result");
        if (result != NULL) {
            json_add_string(result, "target", pa->target);
            json_add_string(result, "host", pa->host);
            json_add_int(result, "sent", stats->sent);
            json_add_int(result, "received", stats->received);
            json_add_int(result, "loss_pct", stats->loss_pct);
            json_add_int(result, "rtt_min_ms", stats->rtt_min_ms);
            json_add_int(result, "rtt_avg_ms", stats->rtt_avg_ms);
            json_add_int(result, "rtt_max_ms", stats->rtt_max_ms);
        }
    } else {
        json_node_t *err = json_add_object(root, "error");
        if (err != NULL) {
            json_add_string(err, "code", code);
            json_add_string(err, "message", msg);
        }
    }
    link_emit_body("command.result", id, 0, pa->reply_to, body);
    json_free(body);
}

static void ping_task(void *arg)
{
    ping_args_t *pa = arg;
    /* Resolution lives in `tools_ping` so the command and the model's `net.ping`
     * tool (`tools/net.md`) cannot disagree about what a reachability answer means.
     * It runs here, not in the handler, because it ends in the 2.8 s probe. */
    char why[64] = "";
    net_ping_stats_t stats;
    esp_err_t err = tools_ping(pa->target, pa->host, sizeof pa->host, &stats,
                               why, sizeof why);
    if (err == ESP_OK) {
        ping_emit(pa, &stats, NULL, NULL);
    } else if (err == ESP_ERR_INVALID_ARG || err == ESP_ERR_NOT_FOUND ||
               err == ESP_ERR_INVALID_SIZE) {
        /* §4.25: a bad or missing reference is `E_CONFIG`, and `why` says which. */
        ping_emit(pa, NULL, "E_CONFIG", why);
    } else if (err == ESP_ERR_INVALID_STATE) {
        ping_emit(pa, NULL, "E_WIFI", "link is down");
    } else {
        ping_emit(pa, NULL, "E_INTERNAL", "ping session could not start");
    }
    s_ping_busy = false;
    free(pa);
    vTaskDelete(NULL);
}


static esp_err_t handle_template_get(const json_node_t *args, json_node_t *root,
                                     json_node_t *result)
{
    const char *name = json_string(json_get(args, "name"));
    if (!store_template_name_ok(name)) return ESP_ERR_INVALID_ARG;

    /* `/prompts/<name>`: the name is already proven a bare filename, so the
     * concatenation cannot escape `/prompts`. */
    char path[80];
    int n = snprintf(path, sizeof path, "/prompts/%s", name);
    if (n <= 0 || (size_t)n >= sizeof path) return ESP_ERR_INVALID_ARG;


    /* Static for the same reason as `sandbox.read`: 4097 bytes does not fit on the
     * 4 KiB link stack (`02-firmware.md` §3.1). */
    static char s_text[4096 + 1];
    size_t len = 0;
    esp_err_t err = store_read_file(path, s_text, sizeof s_text, &len);
    if (err != ESP_OK) return err;

    json_add_string(root, "name", "template.get");
    json_add_bool(root, "ok", true);
    json_add_string(result, "name", name);
    json_add_int(result, "bytes", (int64_t)len);
    json_add_string(result, "text", s_text);
    return ESP_OK;
}

/* --- fs.put (`04` §4.16) -------------------------------------------------- */

static esp_err_t handle_fs_put(const json_node_t *args, json_node_t *root,
                               json_node_t *result, const char **code,
                               const char **message)
{
    const char *path = json_string(json_get(args, "path"));
    const char *data = json_string(json_get(args, "data"));
    if (path == NULL || data == NULL) return ESP_ERR_INVALID_ARG;
    if (!store_import_path_ok(path)) {
        *code = "E_CONFIG";
        *message = "path is outside /config.json, /tasks.json, /skills/, /prompts/, /tools/";
        return ESP_ERR_INVALID_ARG;
    }
    /* `04` §4.16: an import may not replace a file the userfs image ships. The
     * registry is empty because this build ships no image, so a blank device is
     * provisionable through the same command a later image uses — the rule is
     * enforced by the table, not by refusing a directory wholesale. A prefix check
     * on `/prompts/`, `/skills/`, `/tools/` or `/config.json` would refuse every
     * import and leave a blank device with no way to accept one. `build_userfs.py`
     * (M5, `07-roadmap.md`) fills the table as it writes the image. */
    /* Empty today, so the loop over it is not written: `k_shipped` becomes a table
     * with entries when the image exists, and the check is added with them. */
    static const char *const k_shipped[] = {};
    (void)k_shipped;
    const char *encoding = json_string(json_get(args, "encoding"));
    bool base64 = encoding != NULL && strcmp(encoding, "base64") == 0;
    if (encoding != NULL && !base64 && strcmp(encoding, "utf8") != 0) {
        *code = "E_PROTOCOL";
        *message = "encoding is base64 or utf8";
        return ESP_ERR_INVALID_ARG;
    }

    /* `04` §4.16: one chunk is at most 2048 raw bytes, because base64 turns that
     * into 2732 characters and the line must stay under the 4 KiB RX cap. */
    static uint8_t s_chunk[2049];
    size_t len;
    if (base64) {
        esp_err_t derr = store_decode_chunk(data, "base64", s_chunk, sizeof s_chunk,
                                            &len);
        if (derr != ESP_OK) {
            *code = "E_PROTOCOL";
            *message = "data is not valid base64, or decodes past 2048 bytes";
            return ESP_ERR_INVALID_ARG;
        }
    } else {
        len = strlen(data);
    }
    if (len > 2048) {
        *code = "E_PROTOCOL";
        *message = "chunk is over 2048 bytes";
        return ESP_ERR_INVALID_SIZE;
    }
    /* Copied only once the length is known to fit `s_chunk`: the RX cap admits a
     * line four times this size, so a copy before the check would write past the
     * buffer. */
    if (!base64) {
        if (len > 0) memcpy(s_chunk, data, len);
    }

    int64_t offset = 0;
    bool final = false;
    json_int(json_get(args, "offset"), &offset);
    json_bool(json_get(args, "final"), &final);
    if (offset < 0) {
        *code = "E_PROTOCOL";
        *message = "offset is negative";
        return ESP_ERR_INVALID_ARG;
    }

    size_t bytes = 0;
    bool created = false;
    esp_err_t err = store_write_image_chunk(path, s_chunk, len, (size_t)offset,
                                            final, &bytes, &created);
    if (err == ESP_ERR_INVALID_STATE) {
        *code = "E_PROTOCOL";
        *message = "offset does not match the staged length";
    } else if (err == ESP_ERR_INVALID_SIZE) {
        *code = "E_STORAGE";
        *message = "the assembled file or the entry count exceeds the cap";
    } else if (err != ESP_OK) {
        *code = "E_STORAGE";
        *message = "write failed";
    } else {
        json_add_string(root, "name", "fs.put");
        json_add_bool(root, "ok", true);
        json_add_string(result, "path", path);
        json_add_int(result, "bytes", (int64_t)bytes);
        json_add_bool(result, "created", created);
        /* `04` §4.16: the running config is not the file until `reload`. */
        json_add_bool(result, "requires_reboot",
                      strcmp(path, "/config.json") == 0);
    }
    return err;
}

/* `config.get` (`04` §4.8). The reply carries the document or one value from it;
 * a `secret_ref` stays a reference string, because nothing here resolves NVS. */
static esp_err_t handle_config_get(const json_node_t *args, json_node_t *root,
                                   json_node_t *result)
{
    if (!cfg_loaded()) return ESP_ERR_INVALID_STATE;

    const char *path = json_string(json_get(args, "path"));
    if (path != NULL) {
        json_node_t *node = cfg_get(path);
        if (node == NULL) return ESP_ERR_INVALID_ARG;
        json_add_string(root, "name", "config.get");
        json_add_bool(root, "ok", true);
        json_add_string(result, "path", path);
        return json_add_node(result, "value", node) ? ESP_OK : ESP_ERR_NO_MEM;
    }

    json_doc_t *copy = cfg_clone();
    if (copy == NULL) return ESP_ERR_NO_MEM;
    size_t written = 0;
    esp_err_t err = cfg_emit(copy, s_cfg_out, sizeof s_cfg_out, &written);
    json_free(copy);
    if (err != ESP_OK) return err;

    json_doc_t *canonical = NULL;
    err = json_parse(s_cfg_out, written, &canonical);
    if (err != ESP_OK) return err;
    json_add_string(root, "name", "config.get");
    json_add_bool(root, "ok", true);
    bool ok = json_add_node(result, "config", json_root(canonical)) != NULL;
    json_free(canonical);
    return ok ? ESP_OK : ESP_ERR_NO_MEM;
}

/* The schema's refusal sentence for `config.set`. Static so the `*message` the
 * handler returns still points at it when the caller emits the frame. */
static char s_set_reason[128];

/* `config.set` (`04` §4.9): validate a copy, write it whole, keep the old document
 * when anything fails, so a rejected value never leaves the device configless. */
static esp_err_t handle_config_set(const json_node_t *args, json_node_t *root,
                                   json_node_t *result, const char **code,
                                   const char **message)
{
    const char *path = json_string(json_get(args, "path"));
    const json_node_t *value = json_get(args, "value");
    if (path == NULL || value == NULL) return ESP_ERR_INVALID_ARG;
    if (!cfg_loaded()) {
        /* No document means no tree to set into: a fresh device is onboarded by
         * writing whole arrays, which `reload` then validates (`04` §4.9). */
        *code = "E_CONFIG";
        *message = "no config loaded: write the file, then reload";
        return ESP_ERR_INVALID_STATE;
    }

    json_doc_t *doc = cfg_clone();
    if (doc == NULL) return ESP_ERR_NO_MEM;
    if (!cfg_set(doc, path, value)) {
        json_free(doc);
        *code = "E_CONFIG";
        *message = "no such path in config.json; a set creates a missing object but never an array or array index";
        return ESP_ERR_INVALID_ARG;
    }

    /* `why` is the schema's own sentence and it names the member it refused. The
     * caller is a human looking at the offending key, so it travels in the refusal
     * rather than only in a log they are not reading (`04` §4.9). The buffer is
     * static because `*message` outlives this frame: the caller emits it after the
     * handler returns. */
    if (cfg_validate(doc, s_set_reason, sizeof s_set_reason) != ESP_OK) {
        json_free(doc);
        ESP_LOGW(TAG, "config.set rejected: %s", s_set_reason);
        *message = s_set_reason;
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg_validate_refs(doc, s_set_reason, sizeof s_set_reason) != ESP_OK) {
        /* `03-config.md` step 9: a `nvs:` ref whose key is unset is `E_CONFIG` and
         * the old document is kept, so a typo cannot point at nothing. */
        json_free(doc);
        ESP_LOGW(TAG, "config.set rejected: %s", s_set_reason);
        *code = "E_CONFIG";
        *message = s_set_reason;
        return ESP_ERR_INVALID_ARG;
    }

    size_t written = 0;
    esp_err_t err = cfg_emit(doc, s_cfg_out, sizeof s_cfg_out, &written);
    json_free(doc);
    if (err != ESP_OK) return err;

    size_t bytes = 0;
    bool created = false;
    /* A config is larger than one chunk, so it is written in order with `final` on
     * the last one (`04` §4.16). */
    for (size_t at = 0; at < written; at += STORE_CHUNK_BYTES) {
        size_t n = written - at;
        if (n > STORE_CHUNK_BYTES) n = STORE_CHUNK_BYTES;
        err = store_write_image_chunk("/config.json", (const uint8_t *)s_cfg_out + at,
                                      n, at, at + n == written, &bytes, &created);
        if (err != ESP_OK) break;
    }
    if (err != ESP_OK) {
        *code = "E_STORAGE";
        *message = "could not write /config.json";
        return err;
    }

    /* The written file is now the truth: re-read it so an applied flag cannot
     * disagree with flash. */
    err = cfg_load();
    if (err != ESP_OK) {
        *code = "E_CONFIG";
        *message = "written file did not load back";
        return err;
    }

    /* `llm.providers` is re-read by `llm_init` and by nothing else, so a set to it
     * is written but not live. Reporting `applied` there is the lie `/thinking` would
     * inherit: the reply would say the level changed while the chain kept the old
     * `extra_body`. `llm_init` is idempotent and holds no state an in-flight request
     * reads (its providers are copied per attempt), so re-running it here is the
     * whole of "applied". */
    bool live = !cfg_is_live_path(path);
    if (!live) {
        esp_err_t reloaded = llm_init();
        live = reloaded == ESP_OK;
        if (!live) {
            ESP_LOGW(TAG, "config.set: llm re-read failed: %s",
                     esp_err_to_name(reloaded));
        }
    }

    json_add_string(root, "name", "config.set");
    json_add_bool(root, "ok", true);
    json_add_string(result, "path", path);
    json_add_bool(result, "applied", live);
    json_add_bool(result, "requires_reboot", cfg_needs_reboot(path) || !live);
    return ESP_OK;
}

/* `secret.set` (`04` §4.10), shared by the command form and the §3.6 shorthand. */
static esp_err_t handle_secret_set(const char *key, const char *value,
                                   json_node_t *root, json_node_t *result,
                                   const char **code, const char **message)
{
    if (key == NULL || value == NULL) return ESP_ERR_INVALID_ARG;
    if (*value == '\0') {
        *code = "E_CONFIG";
        *message = "empty value: a secret is never cleared by writing a blank";
        return ESP_ERR_INVALID_ARG;
    }
    size_t stored = 0;
    esp_err_t err = cfg_store_secret(key, value, &stored);
    if (err != ESP_OK) {
        *code = (err == ESP_ERR_INVALID_ARG) ? "E_CONFIG" : "E_STORAGE";
        *message = "secret not stored";
        return err;
    }
    json_add_string(root, "name", "secret.set");
    json_add_bool(root, "ok", true);
    json_add_string(result, "key", key);
    json_add_bool(result, "stored", true);
    json_add_int(result, "length", (int64_t)stored);
    return ESP_OK;
}

static void answer_command(const json_node_t *frame)
{
    const json_node_t *args = json_get(frame, "args");
    const char *reply_to = json_string(json_get(frame, "id"));
    if (args == NULL || reply_to == NULL) return;

    const char *name = json_string(json_get(frame, "name"));
    if (name == NULL) return;

    s_cmd_start_us = esp_timer_get_time();
    json_doc_t *body = json_new_object();
    if (body == NULL) return;
    json_node_t *root = json_root(body);

    const char *err_code = "E_CONFIG";
    const char *err_message = "rejected";
    esp_err_t built = ESP_FAIL;
    if (name && strcmp(name, "status") == 0) {
        size_t ufs_used = 0, ufs_total = 0, wfs_used = 0, wfs_total = 0;
        if (store_fs_usage(STORE_FS_USERFS, &ufs_used, &ufs_total) != ESP_OK ||
            store_fs_usage(STORE_FS_WSFS, &wfs_used, &wfs_total) != ESP_OK) {
            emit_failure("status", reply_to, "E_STORAGE", "Usage failed");
            json_free(body); return;
        }
        net_status_t net;
        net_status(&net);
        int32_t calls_hour = 0, calls_day = 0;
        llm_counters(&calls_hour, &calls_day);
        int32_t limit_hour = 0, limit_day = 0;
        llm_limits(&limit_hour, &limit_day);
        const char *llm_id = NULL, *llm_model = NULL;
        bool llm_available = llm_last_answer(&llm_id, &llm_model);
        agent_status_t agent;
        agent_status(&agent);
        tool_counters_t tool_counts;
        tools_counters(&tool_counts);
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
            .wifi_state = net_connected()      ? "connected"
                          : net.state == NET_STATE_FAILED    ? "failed"
                          : net.state == NET_STATE_CONNECTING ? "connecting"
                                                             : "off",
            .wifi_ssid = net.ssid,
            .wifi_rssi = net.rssi,
            .wifi_ip = net.ip,
            .wifi_failovers = net.failovers,
            .time_synced = clock_synced(),
            .time_unix = clock_now(),
            .tz = clock_tz(),
            .llm_provider = llm_id,
            .llm_model = llm_model,
            .llm_available = llm_available,
            .llm_last_error = llm_last_error()[0] != '\0' ? llm_last_error() : NULL,
            .llm_calls_hour = calls_hour,
            .llm_calls_day = calls_day,
            .llm_limit_hour = limit_hour,
            .llm_limit_day = limit_day,
            .agent_busy = agent.busy,
            .agent_run_id = agent.run_id,
            .agent_queue_depth = agent.queue_depth,
            .tool_calls = tool_counts.calls,
            .tool_failures = tool_counts.failures,
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
    } else if (name && strcmp(name, "net.ping") == 0) {
        const char *target = json_string(json_get(args, "target"));
        if (target == NULL ||
            (strcmp(target, "router") != 0 && strcmp(target, "llm") != 0 &&
             strcmp(target, "ntp") != 0)) {
            emit_failure(name, reply_to, "E_CONFIG", "unknown target");
            json_free(body); return;
        }
        if (s_ping_busy) {
            emit_failure(name, reply_to, "E_BUSY", "a ping is already in flight");
            json_free(body); return;
        }
        if (!net_connected()) {
            emit_failure(name, reply_to, "E_WIFI", "link is down");
            json_free(body); return;
        }

        ping_args_t *pa = calloc(1, sizeof *pa);
        if (pa == NULL) {
            emit_failure(name, reply_to, "E_INTERNAL", "ping task not started");
            json_free(body); return;
        }
        snprintf(pa->reply_to, sizeof pa->reply_to, "%s", reply_to);
        snprintf(pa->target, sizeof pa->target, "%s", target);
        /* The same instant `answer_command` stamped, captured into the task's own
         * argument: `s_cmd_start_us` is one global that the next inbound command
         * overwrites, and this frame is answered up to 3 s later. */
        pa->start_us = esp_timer_get_time();

        s_ping_busy = true;
        if (xTaskCreate(ping_task, "net_ping", 3072, pa, 4, NULL) != pdPASS) {
            s_ping_busy = false;
            free(pa);
            emit_failure(name, reply_to, "E_INTERNAL", "ping task not started");
            json_free(body); return;
        }
        json_free(body);
        return;
    } else if (name && strcmp(name, "wifi.scan") == 0) {
        /* §4.24: a scan and a connection attempt cannot share the radio, so a run in
         * flight means `E_BUSY` rather than a scan that steals the station. */
        agent_status_t agent;
        agent_status(&agent);
        if (agent.busy) {
            emit_failure("wifi.scan", reply_to, "E_BUSY", "agent run in flight");
            json_free(body); return;
        }
        static net_scan_entry_t s_scan[NET_SCAN_MAX];
        size_t scan_count = 0;
        bool scan_truncated = false;
        esp_err_t serr = net_scan(s_scan, NET_SCAN_MAX, &scan_count, &scan_truncated);
        if (serr != ESP_OK) {
            emit_failure("wifi.scan", reply_to, "E_WIFI", "radio unavailable");
            json_free(body); return;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "wifi.scan");
        json_add_bool(root, "ok", true);
        json_node_t *nets = json_new_array(result, "networks");
        for (size_t i = 0; i < scan_count; i++) {
            json_node_t *entry = json_array_push_object(nets);
            if (entry == NULL) break;
            json_add_string(entry, "ssid", s_scan[i].ssid);
            json_add_int(entry, "rssi", s_scan[i].rssi);
            json_add_string(entry, "auth", net_auth_name(s_scan[i].auth));
        }
        json_add_bool(result, "truncated", scan_truncated);
        built = ESP_OK;
    } else if (name && strcmp(name, "todo.apply") == 0) {
        /* `04` §4.27: the `todo` tool's own apply path, so `/todo done 3` and the
         * model's `{"op":"done","task":"3"}` cannot disagree about what the op means.
         * The result is `tools/todo.md` §Result verbatim, spliced in rather than
         * rebuilt here. Busy-safe: the port takes `agent`'s todo lock, so a view
         * during a run reads a consistent list instead of a half-committed one. */
        json_doc_t *todo = NULL;
        char why[64] = {0};
        esp_err_t terr = agent_todo_apply(args, &todo, why, sizeof why);
        if (terr != ESP_OK) {
            const char *code = terr == ESP_ERR_INVALID_ARG || terr == ESP_ERR_INVALID_SIZE
                                   ? "E_TOOL" : "E_INTERNAL";
            emit_failure(name, reply_to, code, why[0] != '\0' ? why : "todo refused");
            json_free(body); return;
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "todo.apply");
        json_add_bool(root, "ok", true);
        if (result != NULL && todo != NULL) {
            json_add_node(result, "phases", json_get(json_root(todo), "phases"));
        }
        json_free(todo);
        built = ESP_OK;
    } else if (name && (strcmp(name, "wifi.status") == 0 ||
                        strcmp(name, "wifi.connect") == 0 ||
                        strcmp(name, "wifi.disconnect") == 0 ||
                        strcmp(name, "wifi.reconnect") == 0)) {
        if (strcmp(name, "wifi.status") == 0) {
            /* `status` is busy-safe and reports this same object, so a read is not
             * refused during a run: it touches no radio state, and refusing it would
             * make the one question an operator asks during a run unanswerable. */
            json_node_t *result = json_add_object(root, "result");
            json_add_string(root, "name", "wifi.status");
            json_add_bool(root, "ok", true);
            if (result != NULL) wifi_add_state(result);
            built = ESP_OK;
        } else if (agent_busy()) {
            /* §4.26: the three that steer are refused while a run is in flight. A
             * run's provider call is on the link a join would re-point, and a scan's
             * rule (§4.24) already says the radio holds one caller at a time. */
            emit_failure(name, reply_to, "E_BUSY", "agent run in flight");
            json_free(body); return;
        } else if (strcmp(name, "wifi.disconnect") == 0) {
            if (s_wifi_busy) {
                emit_failure(name, reply_to, "E_BUSY", "a connect is already in flight");
                json_free(body); return;
            }
            esp_err_t derr = net_disconnect();
            if (derr != ESP_OK) {
                emit_failure(name, reply_to, "E_WIFI", "radio did not leave");
                json_free(body); return;
            }
            json_node_t *result = json_add_object(root, "result");
            json_add_string(root, "name", "wifi.disconnect");
            json_add_bool(root, "ok", true);
            if (result != NULL) wifi_add_state(result);
            built = ESP_OK;
        } else {
            /* `connect` and `reconnect` are the same act from this side; the second
             * name exists because "make it work again" and "join" are different
             * questions to a reader, and `reconnect` additionally leaves a live link
             * to rejoin the best AP rather than returning the current one. */
            if (s_wifi_busy) {
                emit_failure(name, reply_to, "E_BUSY", "a connect is already in flight");
                json_free(body); return;
            }
            wifi_args_t *wa = calloc(1, sizeof *wa);
            if (wa == NULL) {
                emit_failure(name, reply_to, "E_INTERNAL", "connect task not started");
                json_free(body); return;
            }
            snprintf(wa->reply_to, sizeof wa->reply_to, "%s", reply_to);
            snprintf(wa->name, sizeof wa->name, "%s", name);
            /* The timer starts before the leave, so `duration_ms` covers the whole
             * act rather than only the join. */
            wa->start_us = esp_timer_get_time();
            s_wifi_busy = true;
            /* `reconnect` from a live link: leaving first makes the attempt loop join
             * the best AP instead of short-circuiting on the one already held. */
            if (strcmp(name, "wifi.reconnect") == 0) net_disconnect();
            /* 4096: `net_connect` blocks on the event group and holds a `wifi_config_t`
             * (about 100 bytes) and a `wifi_ap_record_t` across it, which will not fit
             * on the 3072 `net.ping` uses. */
            if (xTaskCreate(wifi_connect_task, "wifi_connect", 4096, wa, 4, NULL) != pdPASS) {
                s_wifi_busy = false;
                free(wa);
                emit_failure(name, reply_to, "E_INTERNAL", "connect task not started");
                json_free(body); return;
            }
            json_free(body);
            return;
        }
    } else if (name && strcmp(name, "config.get") == 0) {
        built = handle_config_get(args, root, json_add_object(root, "result"));
        if (built != ESP_OK) {
            /* `04` §10: "bad path" is `E_CONFIG`, not `E_PROTOCOL` — the frame was
             * well formed, the key was not there. */
            emit_failure(name, reply_to, "E_CONFIG",
                         built == ESP_ERR_INVALID_STATE ? "no config loaded" : "no such path");
            json_free(body); return;
        }
    } else if (name && strcmp(name, "config.set") == 0) {
        json_node_t *result = json_add_object(root, "result");
        built = handle_config_set(args, root, result, &err_code, &err_message);
        if (built != ESP_OK) {
            emit_failure(name, reply_to, err_code, err_message);
            json_free(body); return;
        }
    } else if (name && strcmp(name, "secret.set") == 0) {
        json_node_t *result = json_add_object(root, "result");
        built = handle_secret_set(json_string(json_get(args, "key")),
                                  json_string(json_get(args, "value")),
                                  root, result, &err_code, &err_message);
        if (built != ESP_OK) {
            emit_failure(name, reply_to, err_code, err_message);
            json_free(body); return;
        }
    } else if (name && (strcmp(name, "tasks.list") == 0 ||
                        strcmp(name, "tasks.run") == 0 ||
                        strcmp(name, "tasks.set") == 0 ||
                        strcmp(name, "tasks.delete") == 0)) {
        /* `07-roadmap.md` M5 delivers `sched` and `tasks.json` execution; until it
         * lands there is no task store to read, and inventing an empty list would
         * tell the bridge "no tasks" about a device that has never read one. The
         * honest answer is the one the catalog gives a missing dependency: the
         * command is named, the reason is stated. */
        /* `02-firmware.md` §safe mode: `tasks.run` needs the scheduler and is
         * refused with `E_SAFE_MODE` there. Answered before the unwired message so
         * the reason names the real blocker; a run that cannot exist yet is still
         * a different answer than "this device is in safe mode". */
        if (s_safe_mode && strcmp(name, "tasks.run") == 0) {
            emit_failure(name, reply_to, "E_SAFE_MODE",
                         "safe mode: repair config, then reload");
            json_free(body); return;
        }
        emit_failure(name, reply_to, "E_INTERNAL",
                     "tasks are not wired yet: the scheduler lands in M5");
        json_free(body); return;
    } else if (name && (strcmp(name, "mqtt.publish") == 0 ||
                        strcmp(name, "mqtt.status") == 0)) {
        /* M6 (`07-roadmap.md`). Neither `E_MQTT` nor `E_GEO` fits: both are
         * documented as retryable broker/provider failures, and "the client does
         * not exist yet" is not retryable. `E_INTERNAL` is the catalog's code for a
         * device-side gap, so the bridge reports the truth instead of inviting a
         * retry that can never succeed. */
        emit_failure(name, reply_to, "E_INTERNAL",
                     "mqtt is not wired yet: the client lands in M6");
        json_free(body); return;
    } else if (name && strcmp(name, "geo.locate") == 0) {
        emit_failure(name, reply_to, "E_INTERNAL",
                     "geo is not wired yet: the providers land in M6");
        json_free(body); return;
    } else if (name && strcmp(name, "templates.list") == 0) {
        esp_err_t r = handle_templates_list(args, root, json_add_object(root, "result"));
        if (r != ESP_OK) {
            /* A handler that cannot answer must still answer: silence makes the
             * bridge time out, which reads as a dead link, not as a bad request. */
            emit_failure("templates.list", reply_to, "E_STORAGE", "Listing failed");
            json_free(body); return;
        }
        built = ESP_OK;
    } else if (name && strcmp(name, "template.get") == 0) {
        esp_err_t r = handle_template_get(args, root, json_add_object(root, "result"));
        if (r != ESP_OK) {
            emit_failure("template.get", reply_to,
                         r == ESP_ERR_INVALID_ARG ? "E_PROTOCOL" : "E_STORAGE",
                         r == ESP_ERR_INVALID_ARG ? "name is not [A-Za-z0-9._-]+.md"
                                                 : "template not found or unreadable");
            json_free(body); return;
        }
        built = ESP_OK;
    } else if (name && strcmp(name, "fs.put") == 0) {
        err_code = "E_CONFIG";
        err_message = "rejected";
        esp_err_t r = handle_fs_put(args, root, json_add_object(root, "result"),
                                    &err_code, &err_message);
        if (r != ESP_OK) {
            emit_failure("fs.put", reply_to, err_code, err_message);
            json_free(body); return;
        }
        built = ESP_OK;
    } else if (name && strcmp(name, "reload") == 0) {
        /* `04` §4.2: re-read the files. Only `/config.json` exists to re-read until
         * the scheduler lands, and the reply says so rather than claiming more. */
        char degraded[96];
        esp_err_t r = cfg_load_degraded(degraded, sizeof degraded);
        if (r == ESP_ERR_NOT_FOUND) {
            emit_failure(name, reply_to, "E_CONFIG", "/config.json is missing");
            json_free(body); return;
        }
        if (r != ESP_OK) {
            /* The reason, not a category: `reload` is the recovery path
             * (`02-firmware.md` §3.2), so a refusal that does not name the offending
             * key leaves the operator with nothing to fix. */
            char why[96];
            snprintf(why, sizeof why, "%s",
                     r == ESP_ERR_INVALID_SIZE ? "/config.json is too large"
                                               : (degraded[0] ? degraded
                                                              : "config rejected; see the log"));
            emit_failure(name, reply_to, "E_CONFIG", why);
            json_free(body); return;
        }
        /* `02-firmware.md` §3.2: `reload` is the way out of safe mode. A successful
         * re-read is the operator saying the cause is repaired, so the counter goes
         * back to zero and the flag clears without a reboot. This is the only
         * command that does it, which is why the guard cannot be cleared by simply
         * reconnecting the bridge. */
        if (store_nvs_set_int("boot_failures", 0) == ESP_OK) {
            s_boot_failures = 0;
            bool was_safe = s_safe_mode;
            s_safe_mode = false;
            /* Clearing the flag without starting the services left the device in a
             * state that reported `safe_mode: false` and still refused every prompt:
             * the queue, the radio and the model had never been created. Booting the
             * services here is what makes `reload` the exit §3.2 promises. */
            start_services(false);
            if (was_safe) ESP_LOGI(TAG, "reload: safe mode cleared, services started");
        } else {
            ESP_LOGW(TAG, "boot counter not cleared on reload");
        }
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "reload");
        json_add_bool(root, "ok", true);
        if (result) {
            json_node_t *list = json_new_array(result, "reloaded");
            if (list) json_array_push_string(list, "config");
            json_node_t *warnings = json_new_array(result, "warnings");
            if (warnings && degraded[0] != '\0') {
                /* `04` §4.2: the config loaded, and this is why it is not fully
                 * usable. An empty array means the document is clean. */
                json_node_t *w = json_array_push_object(warnings);
                if (w) {
                    json_add_string(w, "scope", "config");
                    json_add_string(w, "message", degraded);
                }
            }
        }
        built = ESP_OK;
    } else if (name && strcmp(name, "reboot") == 0) {
        /* `04` §4.3: reply first, then reset, so the frame leaves the CDC buffer. */
        int64_t delay_ms = 250;
        json_int(json_get(args, "delay_ms"), &delay_ms);
        if (delay_ms < 0 || delay_ms > 5000) delay_ms = 250;
        json_node_t *result = json_add_object(root, "result");
        json_add_string(root, "name", "reboot");
        json_add_bool(root, "ok", true);
        if (result) {
            json_add_bool(result, "rebooting", true);
            json_add_int(result, "grace_ms", delay_ms);
        }
        json_add_int(root, "duration_ms", cmd_duration_ms());
        esp_err_t e = link_emit_body("command.result", frame_id(), 0, reply_to, body);
        (void)e;
        json_free(body);
        vTaskDelay(pdMS_TO_TICKS((uint32_t)delay_ms));
        esp_restart();
        return;
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
        json_add_int(root, "duration_ms", cmd_duration_ms());
        esp_err_t e = link_emit_body("command.result", frame_id(), 0, reply_to, body);
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
        /* `§3.2`: the set callable *now*. Built from the same registry and the same
         * `tools_offered` predicate `tools_json` uses, so a tool the model cannot
         * call cannot be advertised either. */
        json_doc_t *caps = json_new_object();
        json_node_t *names = caps != NULL ? json_new_array(json_root(caps), "tools")
                                          : NULL;
        tools_context_t ctx;
        tools_context_from_config(&ctx);
        if (names != NULL) tools_capability_names(&ctx, NULL, names);

        link_hello_t hello = {
            .device = "musteval",
            .fw_version = "1.0.0",
            .boot_id = s_boot_id,
            .time_synced = clock_synced(),
            .tools = names,
            .dangerous_tools = ctx.policy.allow_dangerous,
        };
        json_doc_t *body = json_new_object();
        if (body == NULL) {
            json_free(caps);
            return;
        }
        if (link_build_hello_ack(json_root(body), &hello) == ESP_OK) {
            link_emit_body("hello_ack", frame_id(), 0, reply_to, body);
        } else {
            ESP_LOGE(TAG, "hello: body build failed");
        }
        json_free(body);
        json_free(caps);
        return;
    }

    if (strcmp(type, "ping") == 0) {
        /* §3.7: the pong carries the ping's own `ts`, so the bridge measures one
         * clock and one round trip. */
        int64_t ts = 0;
        json_int(json_get(frame, "ts"), &ts);
        link_emit_pong(json_string(json_get(frame, "id")), ts);
        return;
    }

    if (strcmp(type, "ack") == 0) {
        /* §3.8: `ack` is advisory and an unknown `ack_of` is ignored. */
        return;
    }

    if (strcmp(type, "cancel") == 0) {
        /* §3.5: a live run answers with `agent.error{E_CANCELLED}` — which the run
         * task emits, because it owns the terminal frame — and nothing in flight is
         * `event{cancel.noop}` (`§3.19`), since silence would read as a hang. */
        if (!agent_cancel()) {
            json_doc_t *body = json_new_object();
            if (body == NULL) return;
            json_add_string(json_root(body), "kind", "cancel.noop");
            json_node_t *data = json_add_object(json_root(body), "data");
            if (data != NULL) {
                json_add_string(data, "reason",
                                json_string(json_get(frame, "run_id")) != NULL
                                    ? "no run with that id in flight"
                                    : "nothing to cancel");
            }
            link_emit_body("event", frame_id(), 0, NULL, body);
            json_free(body);
        }
        return;
    }

    if (strcmp(type, "command") == 0) {
        answer_command(frame);
        return;
    }

    if (strcmp(type, "secret.set") == 0) {
        /* `04` §3.6: the shorthand and the command are behaviourally identical, so
         * both go through `handle_secret_set` rather than two write paths. */
        const char *reply_to = json_string(json_get(frame, "id"));
        const char *code = "E_CONFIG";
        const char *message = "rejected";
        json_doc_t *body = json_new_object();
        if (body == NULL) return;
        json_node_t *root = json_root(body);
        esp_err_t err = handle_secret_set(json_string(json_get(frame, "key")),
                                          json_string(json_get(frame, "value")),
                                          root, json_add_object(root, "result"),
                                          &code, &message);
        if (err != ESP_OK) {
            emit_failure("secret.set", reply_to, code, message);
        } else {
            link_emit_body("command.result", frame_id(), 0, reply_to, body);
        }
        json_free(body);
        return;
    }

    if (strcmp(type, "prompt") == 0) {
        const char *reply_to = json_string(json_get(frame, "id"));
        const char *text = json_string(json_get(frame, "text"));
        if (reply_to == NULL) return;

        if (s_safe_mode) {
            /* `04` §4.11: the agent loop needs the network, which safe mode does not
             * start. Answered before the submit so the reason names safe mode rather
             * than the missing queue (`02-firmware.md` §3.2). */
            json_doc_t *body = json_new_object();
            if (body == NULL) return;
            json_node_t *root = json_root(body);
            json_add_string(root, "code", "E_SAFE_MODE");
            json_add_string(root, "message",
                            "safe mode: repair config, then reload");
            json_add_bool(root, "retryable", false);
            json_add_string(root, "reply_to", reply_to);
            link_emit_body("agent.error", frame_id(), 0, reply_to, body);
            json_free(body);
            return;
        }

        esp_err_t err = agent_submit(text, text != NULL ? strlen(text) : 0,
                                     AGENT_SOURCE_PROMPT, reply_to, NULL, 0);
        if (err == ESP_OK) return;   /* the run answers with its own frames */

        /* The codes and wording are `agent_prompt_rejection`'s, so a rejected prompt
         * is host-tested rather than only observable over the link. */
        const agent_rejection_t *rejection = agent_prompt_rejection(err);
        json_doc_t *body = json_new_object();
        if (body == NULL) return;
        json_node_t *root = json_root(body);
        json_add_string(root, "code", rejection->code);
        json_add_string(root, "message", rejection->message);
        json_add_bool(root, "retryable", rejection->retryable);
        if (rejection->queue_detail) {
            /* `04` §3.15's E_BUSY example, including the detail object. */
            json_node_t *detail = json_add_object(root, "detail");
            if (detail != NULL) {
                json_add_int(detail, "queue_depth", AGENT_MAX_QUEUE);
                json_add_int(detail, "max_queue", AGENT_MAX_QUEUE);
            }
        }
        /* `reply_to` is the envelope's field (`04` §3.4), not the body's: adding it to
         * both emits the key twice at the same JSON level, and a parser that keeps the
         * last value silently decides which one wins. */
        link_emit_body("agent.error", frame_id(), 0, reply_to, body);
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

    err = sys_board_init();
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

    /* `03-config.md` §config.json: load after the mounts, before anything reads a
     * setting. A missing file is not fatal, so `config.get` answers `E_CONFIG`
     * rather than the device refusing to boot. */
    err = cfg_load();
    if (err == ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "config: /config.json is missing: every config command is E_CONFIG");
    } else if (err != ESP_OK) {
        ESP_LOGE(TAG, "config: rejected: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "config: loaded");
    }

    link_set_sink(&s_link_sink);
    err = link_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "link: start failed: %s: the device is unreachable",
                 esp_err_to_name(err));
        return;
    }
    link_log_install("info");

    start_services(safe_mode);

    ESP_LOGW(TAG, "boot stops before the scheduler: counter stays at %" PRId32,
             count);
}

/* store.c — NVS runtime namespace (`03-config.md` §runtime state). */

#include "store.h"

#include "esp_littlefs.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include <string.h>

static const char *TAG = "store";
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

/* Static buffers for directory scanning to avoid link task stack overflow.
 * Max recursion depth 8, each with a path buffer. */
#define STORE_SCAN_DEPTH 8
static char s_scan_bufs[STORE_SCAN_DEPTH][STORE_PATH_MAX];
/* `.new` is appended, so the staging slot is wider than one path. */
/* The open transfer's *target* path, and the `.new` file it is being staged in.
 * Two slots, not one: the continuation check compares the target, and overwriting
 * it with the staging name made every `offset > 0` chunk look like a new file. */
static char s_stage_target[STORE_PATH_MAX];
static char s_stage_path[STORE_PATH_MAX + 8];
static size_t s_stage_bytes;

static void quota_scan(const char *dir, store_quota_t *q, int depth, const char *skip)
{
    if (depth >= STORE_SCAN_DEPTH) return;
    DIR *d = opendir(dir);
    if (d == NULL) return;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (!store_entry_name_ok(e->d_name)) continue;
        char *child = s_scan_bufs[depth];
        int n = snprintf(child, STORE_PATH_MAX, "%s/%s", dir, e->d_name);
        if (n <= 0 || (size_t)n >= STORE_PATH_MAX) continue;
        if (skip != NULL && strcmp(child, skip) == 0) continue;
        struct stat st;
        if (stat(child, &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) quota_scan(child, q, depth + 1, skip);
        else { q->files++; q->bytes += (size_t)st.st_size; }
    }
    closedir(d);
}

static esp_err_t resolve_dir(const char *in, char *out, size_t out_cap)
{
    if (strcmp(in, "/workspace") == 0 || strcmp(in, "/workspace/") == 0) {
        return store_map_path(STORE_ROOT_WORKSPACE, out, out_cap);
    }
    return store_resolve_write_path(in, out, out_cap);
}

static int entry_qsort_cmp(const void *a, const void *b)
{
    return store_entry_cmp((const store_entry_t *)a, (const store_entry_t *)b);
}

esp_err_t store_list_dir(const char *agent_path, store_entry_t *out, size_t cap, size_t *count)
{
    if (agent_path == NULL || out == NULL || count == NULL) return ESP_ERR_INVALID_ARG;
    char resolved[STORE_PATH_MAX];
    esp_err_t err = resolve_dir(agent_path, resolved, sizeof resolved);
    if (err != ESP_OK) return err;
    /* Static, not a second stack array: `store_map_path` clears its output before
     * reading the input, so the two cannot alias, and the link task stack is 4 KiB.
     * Safe because command dispatch is single-threaded (§4.16). */
    static char s_vfs[STORE_PATH_MAX];
    err = store_map_path(resolved, s_vfs, sizeof s_vfs);
    if (err != ESP_OK) return err;
    const char *vfs = s_vfs;

    DIR *d = opendir(vfs);
    if (d == NULL) return ESP_ERR_NOT_FOUND;
    size_t n = 0;
    struct dirent *e;
    /* The link task stack is 4 KiB, so a 1 KiB child path per entry would overflow
     * it. One static slot is safe: dispatch is single-threaded (§4.16). */
    static char s_child[STORE_PATH_MAX * 2];
    while ((e = readdir(d)) != NULL) {
        if (!store_entry_name_ok(e->d_name)) continue;
        if (strlen(e->d_name) >= STORE_ENTRY_NAME_MAX) continue;
        if (n == cap) { closedir(d); return ESP_ERR_INVALID_SIZE; }
        store_entry_t *ent = &out[n];
        snprintf(ent->name, sizeof ent->name, "%.*s", STORE_ENTRY_NAME_MAX - 1, e->d_name);
        snprintf(s_child, sizeof s_child, "%s/%s", vfs, e->d_name);
        struct stat st;
        if (stat(s_child, &st) != 0) { closedir(d); return ESP_FAIL; }
        ent->dir = S_ISDIR(st.st_mode);
        ent->bytes = ent->dir ? 0 : (size_t)st.st_size;
        n++;
    }
    closedir(d);
    qsort(out, n, sizeof(store_entry_t), entry_qsort_cmp);
    *count = n;
    return ESP_OK;
}

esp_err_t store_read_text(const char *agent_path, char *buf, size_t cap, size_t *len,
                          size_t *total, bool *truncated)
{
    if (agent_path == NULL || buf == NULL || len == NULL || total == NULL || truncated == NULL) return ESP_ERR_INVALID_ARG;
    char vfs[STORE_PATH_MAX];
    esp_err_t err = store_resolve_write_path(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    
    struct stat st;
    if (stat(vfs, &st) != 0) return ESP_ERR_NOT_FOUND;
    if (S_ISDIR(st.st_mode)) return ESP_ERR_INVALID_ARG;

    FILE *f = fopen(vfs, "rb");
    if (f == NULL) return ESP_FAIL;
    size_t read_n = fread(buf, 1, cap, f);
    *len = read_n;
    *total = (size_t)st.st_size;
    *truncated = (read_n == cap && (size_t)st.st_size > cap);
    fclose(f);

    /* The text is cut at `cap`, so an incomplete trailing sequence is a truncation,
     * but bytes that are not UTF-8 at all are a refusal (§4.18). */
    size_t valid = store_utf8_prefix((const uint8_t *)buf, read_n);
    if (valid == 0 && read_n > 0) return ESP_ERR_INVALID_SIZE;
    *len = valid;
    if (valid < read_n) *truncated = true;
    return ESP_OK;
}

esp_err_t store_write_chunk(const char *agent_path, const uint8_t *data, size_t len,
                            size_t offset, bool final, size_t *out_bytes, bool *out_created)
{
    if (agent_path == NULL || (data == NULL && len > 0) || out_bytes == NULL || out_created == NULL) return ESP_ERR_INVALID_ARG;
    char vfs[STORE_PATH_MAX];
    esp_err_t err = store_resolve_write_path(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;

    if (offset == 0) {
        if (s_stage_target[0] != '\0' && strcmp(s_stage_target, vfs) != 0) remove(s_stage_path);
        s_stage_target[0] = '\0';
        s_stage_path[0] = '\0';
        s_stage_bytes = 0;
    } else if (s_stage_target[0] == '\0' || strcmp(s_stage_target, vfs) != 0) return ESP_ERR_INVALID_STATE;

    if (!store_chunk_fits(offset, len, s_stage_bytes)) return ESP_ERR_INVALID_STATE;

    snprintf(s_stage_target, sizeof s_stage_target, "%s", vfs);
    snprintf(s_stage_path, sizeof s_stage_path, "%s.new", vfs);
    FILE *f = fopen(s_stage_path, offset == 0 ? "wb" : "r+b");
    if (f == NULL) return ESP_FAIL;
    if (offset > 0) fseek(f, offset, SEEK_SET);
    size_t written = fwrite(data, 1, len, f);
    fclose(f);
    if (written != len) return ESP_FAIL;
    s_stage_bytes = offset + len;
    struct stat st;
    bool existed = (stat(vfs, &st) == 0);

    if (final) {
        if (existed && S_ISDIR(st.st_mode)) { remove(s_stage_path); s_stage_path[0]=0; s_stage_target[0]=0; return ESP_ERR_INVALID_STATE; }
        store_quota_t q = {0};
        quota_scan(STORE_VFS_WORKSPACE, &q, 0, s_stage_path);
        if (existed && !S_ISDIR(st.st_mode)) { q.replacing = true; q.replacing_bytes = (size_t)st.st_size; }
        if (store_quota_exceeded(&q, s_stage_bytes)) { remove(s_stage_path); s_stage_path[0]=0; s_stage_target[0]=0; return ESP_ERR_INVALID_SIZE; }
        if (rename(s_stage_path, vfs) != 0) return ESP_FAIL;
        s_stage_path[0] = '\0';
        s_stage_target[0] = '\0';
        *out_bytes = s_stage_bytes;
        *out_created = !existed;
        s_stage_bytes = 0;
        return ESP_OK;
    }
    *out_bytes = s_stage_bytes;
    *out_created = (stat(vfs, &st) != 0);
    return ESP_OK;
}

esp_err_t store_delete_file(const char *agent_path)
{
    char vfs[STORE_PATH_MAX];
    esp_err_t err = store_resolve_write_path(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    struct stat st;
    if (stat(vfs, &st) != 0) return ESP_ERR_NOT_FOUND;
    if (S_ISDIR(st.st_mode)) {
        if (rmdir(vfs) != 0) return ESP_ERR_INVALID_STATE;
        return ESP_OK;
    }
    return unlink(vfs) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t store_is_dir(const char *agent_path, bool *out)
{
    if (agent_path == NULL || out == NULL) return ESP_ERR_INVALID_ARG;
    if (strcmp(agent_path, "/workspace") == 0 || strcmp(agent_path, "/workspace/") == 0) { *out = true; return ESP_OK; }
    char vfs[STORE_PATH_MAX];
    esp_err_t err = store_resolve_write_path(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    struct stat st;
    if (stat(vfs, &st) != 0) return ESP_ERR_NOT_FOUND;
    *out = S_ISDIR(st.st_mode);
    return ESP_OK;
}

#define STORE_NVS_NAMESPACE "runtime"

/* `07-roadmap.md` M0 requires `secrets`, `runtime` and `memory` to exist after
 * boot. `nvs_open(NVS_READWRITE)` already writes the namespace index entry
 * (`Storage::createOrOpenNamespace` -> `writeItem(Page::NS_INDEX, ...)`), so an
 * unused namespace is created by merely opening it: no key, no commit. */
static const char *const k_namespaces[] = {
    "runtime",  /* `03-config.md` §runtime state: counters, budget, boot guard */
    "secrets",  /* `03-config.md` §secrets: `nvs:<key>` targets */
    "memory",   /* `03-config.md` §`tools.memory_max_bytes`: model memory */
};

esp_err_t store_init(void)
{
    /* Both of these mean the partition holds data this build cannot read (a first
     * boot after a repartition, or a layout change). Erasing and retrying once is
     * the documented recovery; failing instead leaves a device that cannot boot at
     * all, and the only state lost is counters that have no meaning yet. */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        err = nvs_flash_erase();
        if (err != ESP_OK) {
            return err;
        }
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return err;
    }

    for (size_t i = 0; i < sizeof(k_namespaces) / sizeof(k_namespaces[0]); i++) {
        nvs_handle_t h;
        err = nvs_open(k_namespaces[i], NVS_READWRITE, &h);
        if (err != ESP_OK) {
            return err;
        }
        nvs_close(h);
    }
    return ESP_OK;
}

esp_err_t store_nvs_set_int(const char *key, int32_t value)
{
    if (key == NULL || *key == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(STORE_NVS_NAMESPACE, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_i32(h, key, value);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

esp_err_t store_nvs_get_int(const char *key, int32_t *out)
{
    if (key == NULL || *key == '\0' || out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(STORE_NVS_NAMESPACE, NVS_READONLY, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_get_i32(h, key, out);
    nvs_close(h);
    return err;
}

/* --- Mounts (`02-firmware.md` §store) ------------------------------------- */

/* `01-hardware.md` §4 labels; the same strings appear in `partitions.csv`. */
#define STORE_USERFS_LABEL "userfs"
#define STORE_WSFS_LABEL   "wsfs"

static bool s_mounted = false;

esp_err_t store_mount(void)
{
    if (s_mounted) return ESP_OK;

    /* `userfs` holds config, prompts, skills and tools. Formatting it would destroy
     * the user's files, so a mount failure is fatal and never reformats (§store). */
    esp_vfs_littlefs_conf_t conf = {
        .base_path = STORE_VFS_USERFS,
        .partition_label = STORE_USERFS_LABEL,
        .format_if_mount_failed = false,
        .read_only = false,
    };
    esp_err_t err = esp_vfs_littlefs_register(&conf);
    if (err != ESP_OK) return err;

    /* `/workspace/` is agent-writable scratch, so a corrupt filesystem there is
     * recoverable by wiping it: exactly once, never in a loop (§store). */
    esp_vfs_littlefs_conf_t ws = {
        .base_path = STORE_VFS_WORKSPACE,
        .partition_label = STORE_WSFS_LABEL,
        .format_if_mount_failed = true,
        .read_only = false,
    };
    err = esp_vfs_littlefs_register(&ws);
    if (err != ESP_OK) {
        esp_vfs_littlefs_unregister(STORE_USERFS_LABEL);
        return err;
    }

    /* `07-roadmap.md` M0 greps the monitor for each mount and its size, so both are
     * logged here rather than left to a caller. `nvs` is not consulted: the numbers
     * come from the mounted filesystem. */
    size_t total = 0, used = 0;
    if (esp_littlefs_info(STORE_USERFS_LABEL, &total, &used) == ESP_OK) {
        ESP_LOGI(TAG, "userfs at %s (%u bytes)", STORE_VFS_USERFS, (unsigned)total);
    }
    if (esp_littlefs_info(STORE_WSFS_LABEL, &total, &used) == ESP_OK) {
        ESP_LOGI(TAG, "wsfs at %s (%u bytes)", STORE_VFS_WORKSPACE, (unsigned)total);
    }

    s_mounted = true;
    return ESP_OK;
}

esp_err_t store_fs_usage(store_fs_t fs, size_t *used, size_t *total)
{
    if (used == NULL || total == NULL) return ESP_ERR_INVALID_ARG;
    const char *label = fs == STORE_FS_USERFS ? STORE_USERFS_LABEL
                       : fs == STORE_FS_WSFS  ? STORE_WSFS_LABEL
                                              : NULL;
    if (label == NULL) return ESP_ERR_INVALID_ARG;
    /* The labels stay private to this file; a caller that needs the numbers goes
     * through here rather than naming a partition (`08-code-architecture.md` §3). */
    return esp_littlefs_info(label, total, used) == ESP_OK ? ESP_OK
                                                           : ESP_ERR_NOT_FOUND;
}


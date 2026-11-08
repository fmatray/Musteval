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
#include <errno.h>


/* One path per filesystem call that holds its own while another runs, so they are
 * not shared with `s_scan_bufs`. Single-threaded dispatch makes the statics safe. */
static char s_delete_path[STORE_PATH_MAX];
static char s_mkdir_path[STORE_PATH_MAX];
static char s_move_from[STORE_PATH_MAX];
static char s_move_to[STORE_PATH_MAX];

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

static esp_err_t image_vfs(const char *agent_path, char *out, size_t out_cap);

static esp_err_t resolve_dir(const char *in, char *out, size_t out_cap)
{
    if (strcmp(in, "/workspace") == 0 || strcmp(in, "/workspace/") == 0) {
        return store_map_path(STORE_ROOT_WORKSPACE, out, out_cap);
    }
    /* Both mounts are listable (`06` §6.1): `fs.list` may list `/fs`, and the skill
     * scan lists `/skills/`. Confinement belongs to the host-only `sandbox.*`
     * commands, which admit their own paths. */
    return image_vfs(in, out, out_cap);
}

static int entry_qsort_cmp(const void *a, const void *b)
{
    return store_entry_cmp((const store_entry_t *)a, (const store_entry_t *)b);
}

/* An image path is canonicalised for the `06` §6.1 rules and then mapped onto the
 * mount (`03-config.md` §1). Skipping the map writes to the VFS root, which no
 * partition is mounted at, so every image import failed with ESP_FAIL. */
static esp_err_t image_vfs(const char *agent_path, char *out, size_t out_cap)
{
    char canon[STORE_PATH_MAX];
    esp_err_t err = store_resolve_image_path(agent_path, canon, sizeof canon);
    if (err != ESP_OK) return err;
    /* `/` canonicalises to the empty path, which `store_map_path` refuses; it is the
     * `userfs` root and `fs.list`'s default (`tools/filesystem.md`). */
    if (canon[0] == '\0') {
        memcpy(canon, "/", 2);
    }
    return store_map_path(canon, out, out_cap);
}

/* Shared by the sandbox listing and the image listing; only the admission rule
 * differs, so the readdir loop is written once (`04` §4.11, §4.17). */
static esp_err_t list_canonical(const char *vfs, store_entry_t *out,
                                size_t cap, size_t *count)
{

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
        /* An in-flight staged write must not appear: §4.16 forbids exposing partial
         * state, and listing it invites a `sandbox.read` of half a file. */
        if (store_is_staging_entry(vfs, e->d_name, s_stage_path)) continue;
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

esp_err_t store_list_dir(const char *agent_path, store_entry_t *out, size_t cap,
                         size_t *count)
{
    if (agent_path == NULL || out == NULL || count == NULL) return ESP_ERR_INVALID_ARG;
    char resolved[STORE_PATH_MAX];
    esp_err_t err = resolve_dir(agent_path, resolved, sizeof resolved);
    if (err != ESP_OK) return err;
    char vfs[STORE_PATH_MAX];
    err = store_map_path(resolved, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    return list_canonical(vfs, out, cap, count);
}

esp_err_t store_list_image_dir(const char *agent_path, store_entry_t *out, size_t cap,
                               size_t *count)
{
    if (agent_path == NULL || out == NULL || count == NULL) return ESP_ERR_INVALID_ARG;
    char vfs[STORE_PATH_MAX];
    esp_err_t err = image_vfs(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    return list_canonical(vfs, out, cap, count);
}

esp_err_t store_read_text(const char *agent_path, char *buf, size_t cap, size_t *len,
                          size_t *total, bool *truncated)
{
    if (agent_path == NULL || buf == NULL || len == NULL || total == NULL || truncated == NULL) return ESP_ERR_INVALID_ARG;
    char vfs[STORE_PATH_MAX];
    /* Reading is not a write, so the workspace confinement does not apply: the agent
     * may read its own skills, prompts and the declarative tool files (`06` §6.1).
     * The model's `/config.json` carve-out is the `fs.read` tool's, not this
     * reader's. */
    esp_err_t err = image_vfs(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    
    struct stat st;
    if (stat(vfs, &st) != 0) return ESP_ERR_NOT_FOUND;
    if (S_ISDIR(st.st_mode)) return ESP_ERR_INVALID_ARG;

    FILE *f = fopen(vfs, "rb");
    if (f == NULL) return ESP_FAIL;
    size_t read_n = fread(buf, 1, cap, f);
    *len = read_n;
    *total = (size_t)st.st_size;
    *truncated = store_read_truncated(read_n, cap, (size_t)st.st_size);
    fclose(f);

    /* The text is cut at `cap`, so an incomplete trailing sequence is a truncation,
     * but bytes that are not UTF-8 at all are a refusal (§4.18). */
    size_t valid = store_utf8_prefix((const uint8_t *)buf, read_n);
    if (valid == 0 && read_n > 0) return ESP_ERR_INVALID_SIZE;
    *len = valid;
    if (valid < read_n) *truncated = true;
    return ESP_OK;
}

esp_err_t store_read_file(const char *agent_path, char *buf, size_t cap, size_t *len)
{
    /* The image reader: `/config.json`, `/prompts/<name>.md`. Not confined to the
     * sandbox, because these are what the firmware ships, and no model path
     * reaches it (`03-config.md` §1). No truncation: a file that does not fit the
     * caller's buffer is ESP_ERR_INVALID_SIZE, so a caller never parses half a
     * config. */
    if (agent_path == NULL || buf == NULL || len == NULL || cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    *len = 0;
    char vfs[STORE_PATH_MAX];
    esp_err_t err = image_vfs(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;

    struct stat st;
    if (stat(vfs, &st) != 0) return ESP_ERR_NOT_FOUND;
    if (S_ISDIR(st.st_mode)) return ESP_ERR_INVALID_ARG;
    if ((size_t)st.st_size >= cap) return ESP_ERR_INVALID_SIZE;

    FILE *f = fopen(vfs, "rb");
    if (f == NULL) return ESP_FAIL;
    size_t read_n = fread(buf, 1, cap, f);
    bool short_read = ferror(f) != 0;
    fclose(f);
    if (short_read) return ESP_FAIL;

    buf[read_n] = '\0';
    *len = read_n;
    return ESP_OK;
}

/* Shared by the sandbox and the image writer: the staging protocol, quota check
 * and rename are identical, only the admission rule differs (`04` §4.16, §4.20). */
static esp_err_t write_chunk_canonical(const char *agent_path, const char *vfs,
                                       const uint8_t *data, size_t len,
                                       size_t offset, bool final,
                                       size_t *out_bytes, bool *out_created)
{
    if (vfs == NULL || (data == NULL && len > 0) || out_bytes == NULL || out_created == NULL) return ESP_ERR_INVALID_ARG;

    if (offset == 0) {
        if (s_stage_target[0] != '\0' && strcmp(s_stage_target, vfs) != 0) remove(s_stage_path);
        s_stage_target[0] = '\0';
        s_stage_path[0] = '\0';
        s_stage_bytes = 0;
    } else if (s_stage_target[0] == '\0' || strcmp(s_stage_target, vfs) != 0) return ESP_ERR_INVALID_STATE;

    /* A refused chunk kills the transfer, so its staging file must go now rather
     * than wait for a later write that may never come: `sandbox.list` would
     * otherwise show `<name>.new`, which is partial state §4.16 forbids exposing. */
    if (!store_chunk_fits(offset, len, s_stage_bytes)) {
        if (s_stage_path[0] != '\0') remove(s_stage_path);
        s_stage_path[0] = '\0';
        s_stage_target[0] = '\0';
        s_stage_bytes = 0;
        return ESP_ERR_INVALID_STATE;
    }

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
        /* Two budgets, one check site: the workspace quota bounds `/workspace/`,
         * the per-directory cap bounds an image import (`03-config.md`
         * §Storage limits). Never both, because no path is in both trees. */
        size_t cap = store_import_cap(agent_path);
        if (cap != 0) {
            if (s_stage_bytes > cap) { remove(s_stage_path); s_stage_path[0]=0; s_stage_target[0]=0; return ESP_ERR_INVALID_SIZE; }
        } else {
            store_quota_t q = {0};
            quota_scan(STORE_VFS_WORKSPACE, &q, 0, s_stage_path);
            if (existed && !S_ISDIR(st.st_mode)) { q.replacing = true; q.replacing_bytes = (size_t)st.st_size; }
            if (store_quota_exceeded(&q, s_stage_bytes)) { remove(s_stage_path); s_stage_path[0]=0; s_stage_target[0]=0; return ESP_ERR_INVALID_SIZE; }
        }
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

esp_err_t store_write_chunk(const char *agent_path, const uint8_t *data, size_t len,
                            size_t offset, bool final, size_t *out_bytes,
                            bool *out_created)
{
    char vfs[STORE_PATH_MAX];
    esp_err_t err = store_resolve_write_path(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    return write_chunk_canonical(agent_path, vfs, data, len, offset, final,
                                 out_bytes, out_created);
}

esp_err_t store_write_image_chunk(const char *agent_path, const uint8_t *data,
                                  size_t len, size_t offset, bool final,
                                  size_t *out_bytes, bool *out_created)
{
    /* The host-only import path (`04` §4.16, `03-config.md` §Storage limits). Not
     * confined to `/workspace/` on purpose; the caller has already refused a path
     * that names a built-in file. */
    char vfs[STORE_PATH_MAX];
    esp_err_t err = image_vfs(agent_path, vfs, sizeof vfs);
    if (err != ESP_OK) return err;
    return write_chunk_canonical(agent_path, vfs, data, len, offset, final,
                                 out_bytes, out_created);
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

esp_err_t store_nvs_set_str(const char *namespace, const char *key,
                            const char *value, size_t *out_len)
{
    /* An empty value is never a deletion (`04` §3.6): the caller refuses it before
     * reaching here, so a blank arrives as ESP_ERR_INVALID_ARG rather than a write. */
    if (namespace == NULL || key == NULL || *key == '\0' ||
        value == NULL || *value == '\0' || out_len == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(namespace, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(h, key, value);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err == ESP_OK) {
        *out_len = strlen(value);
    }
    return err;
}

esp_err_t store_nvs_get_str(const char *namespace, const char *key,
                            char *out, size_t cap, size_t *out_len)
{
    if (namespace == NULL || key == NULL || out == NULL || cap == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t h;
    esp_err_t err = nvs_open(namespace, NVS_READONLY, &h);
    if (err != ESP_OK) {
        return err;
    }
    /* Two calls on purpose: the first sizes the value so a short caller buffer is
     * ESP_ERR_INVALID_SIZE, never a silently truncated secret. */
    size_t need = 0;
    err = nvs_get_str(h, key, NULL, &need);
    if (err == ESP_OK) {
        if (need > cap) {
            err = ESP_ERR_INVALID_SIZE;
        } else {
            err = nvs_get_str(h, key, out, &need);
            if (err == ESP_OK && out_len != NULL) *out_len = need - 1;
        }
    }
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

static size_t count_children(const char *dir)
{
    size_t n = 0;
    DIR *d = opendir(dir);
    if (d == NULL) return 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) n = store_count_entry(n, e->d_name);
    closedir(d);
    return n;
}

/* Post-order removal below `dir`. `depth` indexes the shared scan buffers. */
static esp_err_t remove_tree(const char *dir, int depth, size_t *removed)
{
    if (depth >= STORE_SCAN_DEPTH) return ESP_ERR_INVALID_SIZE;
    DIR *d = opendir(dir);
    if (d == NULL) return ESP_FAIL;
    struct dirent *e;
    char *child = s_scan_bufs[depth];
    esp_err_t err = ESP_OK;
    while ((e = readdir(d)) != NULL) {
        if (!store_entry_name_ok(e->d_name)) continue;
        int n = snprintf(child, STORE_PATH_MAX, "%s/%s", dir, e->d_name);
        if (n <= 0 || (size_t)n >= STORE_PATH_MAX) { err = ESP_ERR_INVALID_SIZE; break; }
        struct stat st;
        if (stat(child, &st) != 0) { err = ESP_FAIL; break; }
        if (S_ISDIR(st.st_mode)) {
            esp_err_t derr = remove_tree(child, depth + 1, removed);
            if (derr != ESP_OK) { err = derr; break; }
            if (rmdir(child) != 0) { err = ESP_FAIL; break; }
            (*removed)++;
        } else {
            if (unlink(child) != 0) { err = ESP_FAIL; break; }
            (*removed)++;
        }
    }
    closedir(d);
    return err;
}

esp_err_t store_delete_file(const char *agent_path, bool recursive, size_t *entries)
{
    if (agent_path == NULL || entries == NULL) return ESP_ERR_INVALID_ARG;
    *entries = 0;
    esp_err_t err = store_resolve_write_path(agent_path, s_delete_path, sizeof s_delete_path);
    if (err != ESP_OK) return err;
    struct stat st;
    if (stat(s_delete_path, &st) != 0) return ESP_ERR_NOT_FOUND;

    bool is_dir = S_ISDIR(st.st_mode);
    size_t children = is_dir ? count_children(s_delete_path) : 0;
    switch (store_delete_action(is_dir, children, recursive)) {
    case STORE_DELETE_NEEDS_RECURSIVE:
        *entries = children;
        return ESP_ERR_INVALID_STATE;
    case STORE_DELETE_FILE:
        return unlink(s_delete_path) == 0 ? ESP_OK : ESP_FAIL;
    case STORE_DELETE_EMPTY:
        return rmdir(s_delete_path) == 0 ? ESP_OK : ESP_FAIL;
    case STORE_DELETE_TREE: {
        size_t removed = 0;
        err = remove_tree(s_delete_path, 0, &removed);
        if (err != ESP_OK) return err;
        if (rmdir(s_delete_path) != 0) return ESP_FAIL;
        *entries = removed;
        return ESP_OK;
    }
    }
    return ESP_FAIL;
}

esp_err_t store_mkdir(const char *agent_path, bool *created)
{
    if (agent_path == NULL || created == NULL) return ESP_ERR_INVALID_ARG;
    esp_err_t err = store_resolve_write_path(agent_path, s_mkdir_path, sizeof s_mkdir_path);
    if (err != ESP_OK) return err;

    struct stat st;
    if (stat(s_mkdir_path, &st) == 0) {
        if (S_ISDIR(st.st_mode)) { *created = false; return ESP_OK; }
        *created = false;
        return ESP_ERR_INVALID_STATE;
    }

    store_quota_t q = {0};
    quota_scan(STORE_VFS_WORKSPACE, &q, 0, NULL);
    if (store_quota_exceeded(&q, 0)) return ESP_ERR_INVALID_SIZE;

    if (mkdir(s_mkdir_path, 0777) != 0) {
        *created = false;
        return (errno == ENOENT) ? ESP_ERR_INVALID_STATE : ESP_FAIL;
    }
    *created = true;
    return ESP_OK;
}

esp_err_t store_move(const char *from, const char *to)
{
    if (from == NULL || to == NULL) return ESP_ERR_INVALID_ARG;
    esp_err_t err = store_resolve_write_path(from, s_move_from, sizeof s_move_from);
    if (err != ESP_OK) return err;
    err = store_resolve_write_path(to, s_move_to, sizeof s_move_to);
    if (err != ESP_OK) return err;

    struct stat st;
    if (stat(s_move_from, &st) != 0) return ESP_ERR_NOT_FOUND;
    bool target_exists = (stat(s_move_to, &st) == 0);
    switch (store_move_verdict(true, target_exists, s_move_from, s_move_to)) {
    case STORE_MOVE_SOURCE_MISSING: return ESP_ERR_NOT_FOUND;
    case STORE_MOVE_TARGET_TAKEN:   return ESP_ERR_INVALID_STATE;
    case STORE_MOVE_INTO_ITSELF:    return ESP_ERR_INVALID_STATE;
    case STORE_MOVE_OK: break;
    }
    return rename(s_move_from, s_move_to) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t store_nvs_key_at(const char *namespace, size_t index, char *out, size_t cap)
{
    if (namespace == NULL || out == NULL || cap == 0) return ESP_ERR_INVALID_ARG;

    nvs_iterator_t it = NULL;
    esp_err_t next = nvs_entry_find(NVS_DEFAULT_PART_NAME, namespace, NVS_TYPE_ANY,
                                    &it);
    size_t seen = 0;
    esp_err_t result = ESP_ERR_NOT_FOUND;
    while (next == ESP_OK) {
        nvs_entry_info_t info;
        if (nvs_entry_info(it, &info) == ESP_OK) {
            if (seen == index) {
                size_t n = strlen(info.key);
                if (n >= cap) {
                    result = ESP_ERR_INVALID_SIZE;
                } else {
                    memcpy(out, info.key, n + 1);
                    result = ESP_OK;
                }
                break;
            }
            seen++;
        }
        next = nvs_entry_next(&it);
    }
    nvs_release_iterator(it);
    return result;
}

esp_err_t store_nvs_erase(const char *namespace, const char *key)
{
    if (namespace == NULL || key == NULL || *key == '\0') return ESP_ERR_INVALID_ARG;
    nvs_handle_t h;
    esp_err_t err = nvs_open(namespace, NVS_READWRITE, &h);
    if (err != ESP_OK) return err;
    err = nvs_erase_key(h, key);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err;
}

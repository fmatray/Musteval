#include "store.h"
#include <string.h>
#include <mbedtls/base64.h>

int store_entry_cmp(const store_entry_t *a, const store_entry_t *b)
{
    if (a->dir != b->dir) return a->dir ? -1 : 1;
    return strcmp(a->name, b->name);
}

bool store_entry_name_ok(const char *name)
{
    if (name == NULL || name[0] == '\0') return false;
    if (strcmp(name, ".") == 0 || strcmp(name, "..") == 0) return false;
    return strchr(name, '/') == NULL;
}

esp_err_t store_decode_chunk(const char *data, const char *encoding,
                             uint8_t *out, size_t cap, size_t *len)
{
    if (data == NULL || out == NULL || len == NULL) return ESP_ERR_INVALID_ARG;
    *len = 0;

    if (encoding == NULL || strcmp(encoding, "utf8") == 0) {
        size_t n = strlen(data);
        if (n > cap || n > STORE_CHUNK_BYTES) return ESP_ERR_INVALID_ARG;
        memcpy(out, data, n);
        *len = n;
        return ESP_OK;
    }

    if (strcmp(encoding, "base64") != 0) return ESP_ERR_INVALID_ARG;

    size_t n = 0;
    if (mbedtls_base64_decode(out, cap, &n, (const unsigned char *)data, strlen(data)) != 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (n > STORE_CHUNK_BYTES) return ESP_ERR_INVALID_ARG;
    *len = n;
    return ESP_OK;
}

size_t store_utf8_prefix(const uint8_t *buf, size_t n)
{
    if (buf == NULL) return 0;
    size_t valid = 0;
    while (valid < n) {
        unsigned char c = buf[valid];
        size_t step;
        if (c < 0x80) { valid++; continue; }
        if (c < 0xC2) return 0;              /* continuation byte, or overlong C0/C1 */
        else if (c < 0xE0) step = 2;
        else if (c < 0xF0) step = 3;
        else if (c < 0xF5) step = 4;         /* F5-FF exceed U+10FFFF */
        else return 0;

        if (valid + step > n) break;         /* an incomplete tail is a truncation */
        for (size_t i = 1; i < step; i++) {
            if ((buf[valid + i] & 0xC0) != 0x80) return 0;
        }
        /* Overlong encodings: E0 needs a >= A0, F0 needs a >= 90, F4 needs <= 8F. */
        if (step == 3 && ((c == 0xE0 && buf[valid + 1] < 0xA0) ||
                          (c == 0xED && buf[valid + 1] >= 0xA0))) return 0;
        if (step == 4 && ((c == 0xF0 && buf[valid + 1] < 0x90) ||
                          (c == 0xF4 && buf[valid + 1] > 0x8F))) return 0;
        valid += step;
    }
    return valid;
}

bool store_is_staging_entry(const char *vfs_dir, const char *entry_name,
                            const char *stage_path)
{
    if (vfs_dir == NULL || entry_name == NULL || stage_path == NULL) return false;
    if (stage_path[0] == '\0') return false;
    size_t dlen = strlen(vfs_dir);
    if (strncmp(stage_path, vfs_dir, dlen) != 0) return false;
    const char *rest = stage_path + dlen;
    if (rest[0] == '/') rest++;
    return strcmp(rest, entry_name) == 0;
}

/* --- §4.19 delete, §4.21 mkdir, §4.22 move ----------------------------------- */

store_delete_action_t store_delete_action(bool is_dir, size_t entries, bool recursive)
{
    if (!is_dir) return STORE_DELETE_FILE;
    if (entries == 0) return STORE_DELETE_EMPTY;
    return recursive ? STORE_DELETE_TREE : STORE_DELETE_NEEDS_RECURSIVE;
}

size_t store_count_entry(size_t count, const char *name)
{
    return store_entry_name_ok(name) ? count + 1 : count;
}

store_move_verdict_t store_move_verdict(bool source_exists, bool target_exists,
                                        const char *from, const char *to)
{
    if (!source_exists) return STORE_MOVE_SOURCE_MISSING;
    if (target_exists) return STORE_MOVE_TARGET_TAKEN;
    if (from == NULL || to == NULL) return STORE_MOVE_INTO_ITSELF;
    if (store_path_is_under(from, to)) return STORE_MOVE_INTO_ITSELF;
    return STORE_MOVE_OK;
}

/* store_path.c — path translation and workspace confinement, pure C, no filesystem
 * access. Sole owner (`08-code-architecture.md` §3): no other module builds a path from a
 * model string. Rules: `02-firmware.md` §`store`, `06-security.md` §6.1. */

#include "store.h"

#include <string.h>

/* prefix lengths for the fixed roots in `store.h` */
#define WS_PREFIX_LEN (sizeof(STORE_ROOT_WORKSPACE) - 1)

esp_err_t store_map_path(const char *agent_path, char *out, size_t out_cap)
{
    if (agent_path == NULL || out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (out_cap == 0) {
        return ESP_ERR_INVALID_SIZE;
    }
    out[0] = '\0';
    /* Empty and relative inputs have no mapping (`02-firmware.md` §`store`). */
    if (agent_path[0] != '/') {
        return ESP_ERR_INVALID_ARG;
    }

    /* Prefix substitution only (`03-config.md` §1 table): `/workspace/...` is the
     * `wsfs` mount already; everything else is rooted at the `userfs` mount. */
    const char *prefix = "";
    const char *rest = agent_path;
    if (strncmp(agent_path, STORE_ROOT_WORKSPACE, WS_PREFIX_LEN) != 0) {
        prefix = STORE_ROOT_USERFS;
        rest = agent_path + 1;
    }

    size_t plen = strlen(prefix);
    size_t rlen = strlen(rest);
    if (plen + rlen + 1 > out_cap) {
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(out, prefix, plen);
    memcpy(out + plen, rest, rlen + 1);
    return ESP_OK;
}

/* One canonicaliser, two admissions: `within` NULL accepts anywhere, otherwise the
 * result must be `within` or strictly below it with a further segment. Static
 * because both public resolvers are the only callers. */
static esp_err_t resolve_canonical(const char *in, char *out, size_t out_cap,
                                   const char *within)
{
    /* Every refusal below is ESP_ERR_INVALID_ARG; the caller maps it to the wire
     * code E_TOOL (`02-firmware.md` §`store`, `06-security.md` §6.1 step 3). */
    if (in == NULL || out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (out_cap == 0) {
        return ESP_ERR_INVALID_SIZE;
    }
    out[0] = '\0';
    /* Step 1 (`06-security.md` §6.1): empty and non-absolute paths are refused. */
    if (in[0] != '/') {
        return ESP_ERR_INVALID_ARG;
    }

    /* Steps 2 and 3: split on `/`, drop empty and `.` segments, pop one segment on
     * `..`. The canonical path is assembled in `out`, so popping is a truncation at
     * the previous `/` and no segment stack is needed. */
    size_t n = 0;
    for (const char *p = in; *p != '\0';) {
        while (*p == '/') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        const char *seg = p;
        while (*p != '\0' && *p != '/') {
            p++;
        }
        size_t seg_len = (size_t)(p - seg);

        if (seg_len == 1 && seg[0] == '.') {
            continue;
        }
        if (seg_len == 2 && seg[0] == '.' && seg[1] == '.') {
            if (n == 0) {
                return ESP_ERR_INVALID_ARG; /* above the root is a refusal, not a clamp */
            }
            while (n > 0 && out[n - 1] != '/') {
                n--;
            }
            if (n > 0) {
                n--;
            }
            continue;
        }

        /* Step 4: over-long is a refusal, including a result that will not fit. */
        if (n + 1 + seg_len + 1 > out_cap) {
            return ESP_ERR_INVALID_ARG;
        }
        out[n++] = '/';
        memcpy(out + n, seg, seg_len);
        n += seg_len;
    }

    out[n] = '\0';
    if (within != NULL) {
        /* Require the literal `/workspace/` prefix plus at least one further
         * segment: `/workspace` and `/workspace/` both canonicalise to `/workspace`
         * and fail. */
        size_t wlen = strlen(within);
        if (n <= wlen || memcmp(out, within, wlen) != 0) {
            out[0] = '\0';
            return ESP_ERR_INVALID_ARG;
        }
    }
    return ESP_OK;
}

esp_err_t store_resolve_write_path(const char *in, char *out, size_t out_cap)
{
    return resolve_canonical(in, out, out_cap, STORE_ROOT_WORKSPACE);
}

esp_err_t store_resolve_image_path(const char *in, char *out, size_t out_cap)
{
    /* No root to confine: the host commands read what the image ships, and no
     * model-supplied path reaches here (`03-config.md` §1). */
    return resolve_canonical(in, out, out_cap, NULL);
}
bool store_path_is_under(const char *dir, const char *path)
{
    if (dir == NULL || path == NULL) return false;
    size_t n = strlen(dir);
    if (strncmp(dir, path, n) != 0) return false;
    return path[n] == '\0' || path[n] == '/';
}

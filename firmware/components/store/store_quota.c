/* Quota and chunk sequencing: the arithmetic from `06-security.md` §6.2 and
 * `04-protocol.md` §4.16, with no filesystem access so it is provable on the host
 * (`AGENTS.md`, firmware host row). `store.c` owns the I/O that feeds these. */

#include <string.h>

#include "store.h"

bool store_quota_exceeded(const store_quota_t *q, size_t new_size)
{
    if (q == NULL) return true;

    /* `06` §6.2: checked before truncation, and an overwrite counts the new size
     * only, so the existing file's bytes must not be counted twice. */
    size_t bytes = q->bytes;
    size_t files = q->files;
    if (q->replacing) {
        if (q->replacing_bytes > bytes) {
            bytes = 0; /* inconsistent reading: treat as no reclaimable bytes */
        } else {
            bytes -= q->replacing_bytes;
        }
    } else {
        files += 1;
    }

    /* A single file over its own cap is refused even into an empty tree. */
    if (new_size > STORE_WS_FILE_BYTES) return true;
    if (files > STORE_WS_FILE_COUNT) return true;
    /* An already-over-quota tree refuses everything, and this makes the
     * subtraction below safe. */
    if (bytes > STORE_WS_TOTAL_BYTES) return true;
    return new_size > STORE_WS_TOTAL_BYTES - bytes;
}

/* Canonicalise before either rule looks at the path: `/skills/../workspace/x.md`
 * names the sandbox, and a raw prefix match would both admit it as an import and
 * grant it the larger skill cap. Returns NULL when the path does not canonicalise. */
static const char *canonical(const char *agent_path, char *out, size_t out_cap);

size_t store_import_cap(const char *agent_path)
{
    char canon[STORE_PATH_MAX];
    agent_path = canonical(agent_path, canon, sizeof canon);
    if (agent_path == NULL) return 0;

    /* `03-config.md` §Storage limits: the per-file cap of the directory, one
     * budget for baked-in and imported files alike. `/workspace/` has no
     * per-directory cap; the quota bounds it instead. */
    if (strncmp(agent_path, "/skills/", 8) == 0) return 8u * 1024u;
    if (strncmp(agent_path, "/prompts/", 9) == 0) return 4u * 1024u;
    if (strncmp(agent_path, "/tools/", 7) == 0) return 2u * 1024u;
    if (strcmp(agent_path, "/config.json") == 0) return 8u * 1024u;
    if (strcmp(agent_path, "/tasks.json") == 0) return 8u * 1024u;
    return 0;
}

bool store_chunk_fits(size_t offset, size_t chunk_raw, size_t staged)
{
    /* `04` §4.16: a chunk over 2048 raw bytes is E_PROTOCOL regardless of where
     * it lands, and an offset that is not the staged length would splice two
     * transfers together. Offset 0 always starts over. */
    if (chunk_raw > STORE_CHUNK_BYTES) return false;
    if (offset == 0) return true;
    return offset == staged;
}

bool store_import_path_ok(const char *path)
{
    char canon[STORE_PATH_MAX];
    path = canonical(path, canon, sizeof canon);
    if (path == NULL) return false;
    if (strcmp(path, "/config.json") == 0 || strcmp(path, "/tasks.json") == 0) return true;

    /* `04` §4.16's "disallowed extension": the directory fixes it, since each one
     * carries exactly one file type (`03-config.md` §Storage limits). A name
     * without the suffix would be listed as a template or tool and never parsed. */
    static const struct { const char *dir; const char *ext; } dirs[] = {
        { "/skills/",  ".md"   },
        { "/prompts/", ".md"   },
        { "/tools/",   ".json" },
    };
    for (size_t i = 0; i < sizeof dirs / sizeof dirs[0]; i++) {
        size_t n = strlen(dirs[i].dir);
        if (strncmp(path, dirs[i].dir, n) != 0 || path[n] == '\0') continue;
        size_t len = strlen(path), ext = strlen(dirs[i].ext);
        /* The stem must exist: `/prompts/.md` is a dotfile, not a name, and would
         * list as a template no one can ask for. */
        return len > n + ext && strcmp(path + len - ext, dirs[i].ext) == 0;
    }
    return false;
}

static const char *canonical(const char *agent_path, char *out, size_t out_cap)
{
    if (store_resolve_image_path(agent_path, out, out_cap) != ESP_OK) return NULL;
    return out;
}

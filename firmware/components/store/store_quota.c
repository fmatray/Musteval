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

size_t store_import_cap(const char *agent_path)
{
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

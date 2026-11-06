/* cfg_doc.c — the loaded document and redaction, none of it touching storage
 * (`08-code-architecture.md` §3). Split from `cfg.c` for the same reason `store_path.c`
 * is split from `store.c`: these functions are pure, so the host suite can prove
 * them, while `cfg.c` reads `/config.json` and opens NVS and cannot. */

#include <string.h>

#include "cfg.h"

/* The loaded document, owned here because every accessor is here. `cfg.c` parses a
 * file and hands the result over with `cfg_adopt`, so a rejected load keeps the
 * previous document (`04` §4.9). */
static json_doc_t *s_doc;

void cfg_adopt(json_doc_t *doc)
{
    cfg_unload();
    s_doc = doc;
}

void cfg_unload(void)
{
    json_free(s_doc);
    s_doc = NULL;
}

bool cfg_loaded(void) { return s_doc != NULL; }

json_node_t *cfg_get(const char *path)
{
    if (s_doc == NULL || path == NULL || *path == '\0') return NULL;
    return json_path_get(s_doc, path);
}

json_doc_t *cfg_clone(void)
{
    if (s_doc == NULL) return NULL;
    /* Cloned straight from the parsed tree: a round trip through `json_emit` would
     * need a second whole-config buffer (8 KiB of static RAM) to parse back, and
     * the canonical form is what `cfg_emit` is for. */
    return json_duplicate(s_doc);
}

bool cfg_set(json_doc_t *doc, const char *path, const json_node_t *value)
{
    return json_path_set(doc, path, value);
}

esp_err_t cfg_emit(json_doc_t *doc, char *buf, size_t cap, size_t *written)
{
    return json_emit(doc, buf, cap, written);
}

bool cfg_redact(char *line, size_t cap, const char *const *secrets, size_t n)
{
    if (line == NULL || cap == 0 || n == 0) return false;
    static const char kMask[] = "[redacted]";
    const size_t mask_len = sizeof kMask - 1;
    bool changed = false;

    for (size_t i = 0; i < n; i++) {
        const char *secret = (secrets != NULL) ? secrets[i] : NULL;
        /* An empty secret would match at offset 0 of every line, so it is skipped
         * rather than shredding the string. A short secret is still redacted: a
         * leaked key is leaked whatever its length. */
        if (secret == NULL || *secret == '\0') continue;
        const size_t slen = strlen(secret);

        /* Offsets, not a moving pointer: a pointer into the buffer plus its own
         * offset double-counts and writes past the match. */
        size_t from = 0;
        for (;;) {
            char *hit = strstr(line + from, secret);
            if (hit == NULL) break;
            size_t used = (size_t)(hit - line);
            size_t rest = strlen(line + used + slen);
            if (used + mask_len >= cap) {
                /* No room for even the mask: keep the buffer terminated. */
                line[cap - 1] = '\0';
                return true;
            }
            size_t keep = rest;
            if (used + mask_len + keep >= cap) {
                keep = cap - 1 - used - mask_len;
            }
            memmove(line + used + mask_len, line + used + slen, keep);
            memcpy(line + used, kMask, mask_len);
            line[used + mask_len + keep] = '\0';
            from = used + mask_len;
            changed = true;
            if (keep < rest) break;
        }
    }
    return changed;
}

